"""
Buyer Quick Add — model comparison.

The Origination Network "Buyer Quick Add" feature gives a company URL to an LLM and asks for a
structured JSON profile: is-it-a-PE-firm, official company name, leadership page, leadership NAMES
(the contacts it discovers), and the applicable industries / EBITDA ranges / deal types / regions.
It used `gpt-4o-search-preview` (a web-search model given only the URL, no page content). That model
is being RETIRED and now returns HTTP 404 — so Quick Add stopped working and needs a replacement.

This tool runs the EXACT production prompt (prompts/buyerqa.txt, from the ON DB) for a set of real
domains across candidate models, so you can compare which replacement performs best. The task needs
web knowledge (only a URL is given), so search-capable models matter — especially for obscure/foreign
domains. Reuses model_compare's OpenRouter transport. Own tables; namespaced `buyerqa`.
"""
from __future__ import annotations

import json
import pathlib
import re
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

from psycopg2.extras import RealDictCursor

import coverage
from model_compare import call_model, _is_transient_err

_PROMPT_PATH = pathlib.Path(__file__).parent / "prompts" / "buyerqa.txt"

# candidate replacement models (editable from the UI). The task needs web knowledge; search-capable
# models are preferred. The retired current model is kept as a baseline (it 404s — proof it's dead).
DEFAULT_MODELS = [
    "perplexity/sonar",                 # native web search — the leader so far
    "google/gemini-2.5-flash:online",   # Google flash + OpenRouter web search
    "google/gemini-3.7-flash:online",   # latest Google flash + search
    "anthropic/claude-sonnet-4-6",      # Claude (no live search via OpenRouter — training-only)
    "anthropic/claude-haiku-4.5",       # Claude cheap
    "openai/gpt-4.1-mini",              # active OpenAI (training-only; search-preview line is dead)
    "openai/gpt-5-mini",                # active OpenAI (training-only)
]

# the 12 real completed domains from bulk_add_reports (a mix of well-known + obscure/foreign)
SEED_DOMAINS = [
    "drivenbrands.com", "neighborlybrands.com", "schott.com", "carygroup.com", "pgwautoglass.com",
    "apcoholdings.com", "gb-corporation.com", "fenzigroup.com", "sinotruk.com", "sanoh.com",
    "almansour.com.eg", "mcv-eg.com",
]


def prompt_template():
    return _PROMPT_PATH.read_text(encoding="utf-8")


def build_prompt(domain):
    url = domain if re.match(r"^https?://", domain) else ("https://" + domain)
    dom = re.sub(r"^https?://(www\.)?", "", domain).rstrip("/")
    return prompt_template().replace("{url}", url).replace("{domain}", dom)


# ── parse the model's JSON + extract comparison signals ─────────────────────────────────────────
def _loads_loose(text):
    s = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(\{[\s\S]*\})\s*```", s)
    if m:
        s = m.group(1)
    try:
        return json.loads(s)
    except Exception:  # noqa: BLE001
        a, b = s.find("{"), s.rfind("}")
        if a != -1 and b > a:
            try:
                return json.loads(s[a:b + 1])
            except Exception:  # noqa: BLE001
                pass
    return None


def _truecount(d):
    if not isinstance(d, dict):
        return 0
    return sum(1 for v in d.values() if v is True or (isinstance(v, str) and v.strip().lower() == "true"))


def signals(report) -> dict:
    """Objective comparison signals — no single 'right' profile, so we surface what each model produced."""
    if not isinstance(report, dict):
        return {"json_ok": False, "is_pe": None, "official_name": None, "leadership_page": None,
                "names": [], "names_count": 0, "industries_n": 0, "regions_n": 0, "ebitda_n": 0, "deal_n": 0}
    names = report.get("names") if isinstance(report.get("names"), list) else []
    pe = report.get("is_a_private_equity_firm_or_family_office")
    if isinstance(pe, str):
        pe = pe.strip().lower() == "true"
    return {
        "json_ok": True,
        "is_pe": pe,
        "official_name": report.get("official_company_name"),
        "leadership_page": report.get("leadership_page_on_website"),
        "names": [{"first_name": n.get("first_name"), "last_name": n.get("last_name")}
                  for n in names if isinstance(n, dict)],
        "names_count": len([n for n in names if isinstance(n, dict) and (n.get("first_name") or n.get("last_name"))]),
        "industries_n": _truecount(report.get("industries")),
        "regions_n": _truecount(report.get("regions")),
        "ebitda_n": _truecount(report.get("ebitda_ranges")),
        "deal_n": _truecount(report.get("deal_types")),
    }


# ── run one model / one case ─────────────────────────────────────────────────────────────────────
def run_one_model(config, prompt, model, cid=None) -> dict:
    raw = {}
    for attempt in range(3):
        try:
            raw = call_model(model, "", prompt, config)   # no system; the whole prompt is the user msg
        except Exception as e:  # noqa: BLE001
            raw = {"model": model, "error": f"{type(e).__name__}: {e}"}
        if (raw.get("text") or "").strip():
            break
        if raw.get("error") and not _is_transient_err(raw["error"]):
            break
        if attempt < 2:
            time.sleep(2)
    if not raw.get("error") and not (raw.get("text") or "").strip():
        raw["error"] = "empty response after 3 tries"
    report = _loads_loose(raw.get("text")) if raw.get("text") else None
    sig = signals(report)
    row = {
        "model": model, "route": raw.get("route"), "provider": raw.get("provider"),
        "error": raw.get("error"),
        "cost_usd": raw.get("cost_usd"), "latency_ms": raw.get("latency_ms"),
        "input_tokens": raw.get("input_tokens"), "output_tokens": raw.get("output_tokens"),
        "raw": (raw.get("text") or "")[:60000],
        **sig,
    }
    if cid:
        _result_put(cid, model, row)
    return row


def run_case(config: dict, case: dict, models: list, refresh_models: bool = False) -> dict:
    cid = case.get("id")
    prompt = build_prompt(case["domain"])
    results, to_run = [], []
    for m in models:
        if cid and not refresh_models:
            cached = _result_get(cid, m)
            if cached and not cached.get("error"):
                results.append(cached)
                continue
        to_run.append(m)
    if to_run:
        with ThreadPoolExecutor(max_workers=min(8, len(to_run))) as ex:
            results.extend(ex.map(lambda m: run_one_model(config, prompt, m, cid), to_run))
    total_cost = round(sum((r.get("cost_usd") or 0) for r in results), 4)
    ok = [r for r in results if r.get("json_ok") and not r.get("error")]
    return {"case_id": cid, "domain": case["domain"],
            "summary": {"models": len(results), "json_ok": len(ok),
                        "avg_names": round(sum(r.get("names_count", 0) for r in ok) / len(ok), 1) if ok else 0,
                        "cost_usd": total_cost},
            "results": results}


# ── overview + reads ─────────────────────────────────────────────────────────────────────────────
def matrix():
    out = []
    for c in list_cases():
        rows = _rows(c["id"])
        out.append({"id": c["id"], "domain": c["domain"], "note": c.get("note"),
                    "results": {r["model"]: r for r in rows}})
    return {"cases": out, "models": DEFAULT_MODELS, "prompt_chars": len(prompt_template())}


def input_for(domain):
    return {"domain": domain, "prompt": build_prompt(domain)}


def result_for(cid, model):
    return _result_get(cid, model)


# ── schema + storage ──────────────────────────────────────────────────────────────────────────────
def ensure_schema():
    if not coverage.enabled():
        return
    with closing(coverage._conn()) as c:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS entity.buyerqa_cases (
                    id         bigserial PRIMARY KEY,
                    domain     text NOT NULL,
                    note       text,
                    created_at timestamptz NOT NULL DEFAULT now()
                );
                CREATE TABLE IF NOT EXISTS entity.buyerqa_results (
                    case_id    bigint NOT NULL,
                    model      text   NOT NULL,
                    result     jsonb  NOT NULL,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    PRIMARY KEY (case_id, model)
                );
            """)
        c.commit()
    _seed_if_empty()


def _seed_if_empty():
    with closing(coverage._conn()) as c:
        with c.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM entity.buyerqa_cases")
            if cur.fetchone()[0] == 0:
                for d in SEED_DOMAINS:
                    cur.execute("INSERT INTO entity.buyerqa_cases (domain) VALUES (%s)", (d,))
        c.commit()


def list_cases():
    with closing(coverage._conn()) as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id, domain, note FROM entity.buyerqa_cases ORDER BY id")
            return cur.fetchall()


def get_case(cid):
    with closing(coverage._conn()) as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id, domain, note FROM entity.buyerqa_cases WHERE id=%s", (cid,))
            return cur.fetchone()


def add_case(domain, note=None):
    with closing(coverage._conn()) as c:
        with c.cursor() as cur:
            cur.execute("INSERT INTO entity.buyerqa_cases (domain, note) VALUES (%s,%s) RETURNING id", (domain, note))
            cid = cur.fetchone()[0]
        c.commit()
    return cid


def update_case(cid, domain, note=None):
    with closing(coverage._conn()) as c:
        with c.cursor() as cur:
            cur.execute("UPDATE entity.buyerqa_cases SET domain=%s, note=%s WHERE id=%s", (domain, note, cid))
        c.commit()


def delete_case(cid):
    with closing(coverage._conn()) as c:
        with c.cursor() as cur:
            cur.execute("DELETE FROM entity.buyerqa_results WHERE case_id=%s", (cid,))
            cur.execute("DELETE FROM entity.buyerqa_cases WHERE id=%s", (cid,))
        c.commit()


def _rows(cid):
    with closing(coverage._conn()) as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT result FROM entity.buyerqa_results WHERE case_id=%s ORDER BY model", (cid,))
            return [r["result"] for r in cur.fetchall()]


def _result_get(cid, model):
    with closing(coverage._conn()) as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT result FROM entity.buyerqa_results WHERE case_id=%s AND model=%s", (cid, model))
            r = cur.fetchone()
            return r["result"] if r else None


def _result_put(cid, model, row):
    with closing(coverage._conn()) as c:
        with c.cursor() as cur:
            cur.execute(
                "INSERT INTO entity.buyerqa_results (case_id, model, result) VALUES (%s,%s,%s) "
                "ON CONFLICT (case_id, model) DO UPDATE SET result=EXCLUDED.result, created_at=now()",
                (cid, model, json.dumps(row, default=str)))
        c.commit()
