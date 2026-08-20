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
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

from psycopg2.extras import RealDictCursor

import coverage
from model_compare import call_model, _is_transient_err

_PROMPT_PATHS = {
    "full": pathlib.Path(__file__).parent / "prompts" / "buyerqa.txt",     # full buyer profile
    "names": pathlib.Path(__file__).parent / "prompts" / "buyerqa_names.txt",  # contacts-only (find new names)
}
_PROMPT_PATH = _PROMPT_PATHS["full"]  # backwards compat

# candidate replacement models (editable from the UI). The task needs web knowledge; search-capable
# models are preferred. The retired current model is kept as a baseline (it 404s — proof it's dead).
DEFAULT_MODELS = [
    "perplexity/sonar",                 # native web search — the leader so far
    "google/gemini-2.5-flash:online",   # Google flash + OpenRouter web search
    "google/gemini-3.7-flash:online",   # latest Google flash + search
    "anthropic/claude-sonnet-4-6",      # Claude (no live search via OpenRouter — training-only)
    "anthropic/claude-haiku-4.5",       # Claude cheap
    "openai/gpt-4o:web",                # closest cousin to the retired gpt-4o-search-preview — gpt-4o
                                        # WITH web search via OpenAI's Responses API (:web = that path)
    "openai/gpt-4.1-mini:web",          # OpenAI mini WITH web search (Responses API)
    "openai/gpt-5-mini:web",            # OpenAI mini WITH web search (Responses API)
    "openai/gpt-5:web",                 # full GPT-5 WITH web search (Responses API)
    "openai/gpt-5-search-api",          # OpenAI's dedicated search model (chat/completions, search built in)
]


# ── OpenAI Responses API + web_search (the current replacement for the *-search-preview models) ──
def _call_openai_web(config, model, prompt):
    """gpt-4o (or other) WITH live web search via OpenAI's Responses API. This is the proper way to
    give a standard OpenAI model search now that the chat-completions *-search-preview models are
    retired (and OpenRouter :online / +web don't work for OpenAI). Model id convention: '...:web'."""
    key = config.get("openai_api_key")
    if not key:
        return {"error": "no OpenAI API key", "route": "openai-responses", "provider": "OpenAI"}
    base = model.split("/")[-1].replace(":web", "")
    body = {"model": base, "tools": [{"type": "web_search"}], "input": prompt}
    req = urllib.request.Request("https://api.openai.com/v1/responses", data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    t0 = time.time()
    try:
        d = json.load(urllib.request.urlopen(req, timeout=150))
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read().decode()[:150]}",
                "route": "openai-responses", "provider": "OpenAI", "latency_ms": int((time.time() - t0) * 1000)}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}", "route": "openai-responses", "provider": "OpenAI"}
    txt = d.get("output_text") or ""
    if not txt:
        for o in d.get("output", []):
            for cp in (o.get("content") or []):
                if cp.get("type") == "output_text":
                    txt += cp.get("text", "")
    u = d.get("usage") or {}
    it, ot = u.get("input_tokens", 0), u.get("output_tokens", 0)
    # gpt-4o token rates + a rough web_search tool fee (~$0.025/call) — search context inflates input
    cost = round(it * 2.5 / 1e6 + ot * 10.0 / 1e6 + 0.025, 4)
    return {"text": txt, "cost_usd": cost, "input_tokens": it, "output_tokens": ot,
            "route": "openai-responses", "provider": "OpenAI web_search", "latency_ms": int((time.time() - t0) * 1000)}


def _call_openai_search_api(config, model, prompt):
    """OpenAI's dedicated gpt-5-search-api model — search is BUILT IN (no tool to attach), called via
    chat/completions. Faster (~7s vs ~3min for gpt-5+web_search) and not reasoning-heavy. The injected
    web-search results are billed as prompt tokens; gpt-5 token rates, no separate tool fee."""
    key = config.get("openai_api_key")
    if not key:
        return {"error": "no OpenAI API key", "route": "openai-search-api", "provider": "OpenAI"}
    base = model.split("/")[-1]
    body = {"model": base, "messages": [{"role": "user", "content": prompt}]}
    req = urllib.request.Request("https://api.openai.com/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    t0 = time.time()
    try:
        d = json.load(urllib.request.urlopen(req, timeout=150))
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read().decode()[:150]}",
                "route": "openai-search-api", "provider": "OpenAI", "latency_ms": int((time.time() - t0) * 1000)}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}", "route": "openai-search-api", "provider": "OpenAI"}
    txt = ((d.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    u = d.get("usage") or {}
    it, ot = u.get("prompt_tokens", 0), u.get("completion_tokens", 0)
    cost = round(it * 1.25 / 1e6 + ot * 10.0 / 1e6, 4)  # gpt-5 rates; search results billed as input
    return {"text": txt, "cost_usd": cost, "input_tokens": it, "output_tokens": ot,
            "route": "openai-search-api", "provider": "OpenAI gpt-5-search-api", "latency_ms": int((time.time() - t0) * 1000)}


def _dispatch(config, prompt, model):
    if model.endswith("-search-api"):
        return _call_openai_search_api(config, model, prompt)
    if model.endswith(":web"):
        return _call_openai_web(config, model, prompt)
    return call_model(model, "", prompt, config)   # no system; whole prompt is the user msg

# the 12 real completed domains from bulk_add_reports (a mix of well-known + obscure/foreign)
SEED_DOMAINS = [
    "drivenbrands.com", "neighborlybrands.com", "schott.com", "carygroup.com", "pgwautoglass.com",
    "apcoholdings.com", "gb-corporation.com", "fenzigroup.com", "sinotruk.com", "sanoh.com",
    "almansour.com.eg", "mcv-eg.com",
]


def prompt_template(mode="full"):
    return _PROMPT_PATHS.get(mode, _PROMPT_PATHS["full"]).read_text(encoding="utf-8")


def build_prompt(domain, mode="full"):
    url = domain if re.match(r"^https?://", domain) else ("https://" + domain)
    dom = re.sub(r"^https?://(www\.)?", "", domain).rstrip("/")
    return prompt_template(mode).replace("{url}", url).replace("{domain}", dom)


# ── parse the model's JSON + extract comparison signals ─────────────────────────────────────────
def _json_candidates(s):
    """Every balanced {..}/[..] span in s, honoring string quoting so stray brackets in prose (and
    search-model citation markers like [1]) don't derail extraction."""
    out = []
    for oc, cc in (("{", "}"), ("[", "]")):
        i, n = 0, len(s)
        while i < n:
            if s[i] == oc:
                depth = 0
                in_str = False
                esc = False
                j = i
                while j < n:
                    ch = s[j]
                    if in_str:
                        if esc:
                            esc = False
                        elif ch == "\\":
                            esc = True
                        elif ch == '"':
                            in_str = False
                    else:
                        if ch == '"':
                            in_str = True
                        elif ch == oc:
                            depth += 1
                        elif ch == cc:
                            depth -= 1
                            if depth == 0:
                                out.append(s[i:j + 1])
                                break
                    j += 1
                i = j + 1
            else:
                i += 1
    return out


def _loads_loose(text):
    """Parse the model output as JSON — tolerating fences, prose preambles ("Here is the JSON..."),
    citation markers, and BOTH an object (full profile) and a bare array (contacts-only names)."""
    s = (text or "").strip()
    m = re.search(r"```(?:json)?\s*([\[{][\s\S]*[\]}])\s*```", s)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:  # noqa: BLE001
            pass
    try:
        return json.loads(s)
    except Exception:  # noqa: BLE001
        pass
    best, best_score = None, -1
    for c in _json_candidates(s):
        try:
            v = json.loads(c)
        except Exception:  # noqa: BLE001
            continue
        score = len(c)
        if isinstance(v, list) and v and not all(isinstance(x, dict) for x in v):
            score = 0  # e.g. a bare [1] citation array — not the names payload
        if score > best_score:
            best, best_score = v, score
    return best


def _selected(d):
    """The LABELS a model set to true — for showing preference chips (regions/industries/etc)."""
    if not isinstance(d, dict):
        return []
    return [k for k, v in d.items() if v is True or (isinstance(v, str) and v.strip().lower() == "true")]


def _blank_signals():
    return {"json_ok": False, "is_pe": None, "official_name": None, "leadership_page": None,
            "names": [], "names_count": 0,
            "industries_sel": [], "regions_sel": [], "ebitda_sel": [], "deal_sel": [],
            "industries_n": 0, "regions_n": 0, "ebitda_n": 0, "deal_n": 0}


def _names_from(items):
    return [{"first_name": n.get("first_name"), "last_name": n.get("last_name")}
            for n in items if isinstance(n, dict)]


def signals(report, mode="full") -> dict:
    """Objective comparison signals. mode='names' → the output is a bare JSON ARRAY of contacts (no
    profile fields). mode='full' → the profile object with the selected preference labels for chips."""
    if mode == "names":
        items = report if isinstance(report, list) else (report.get("names") if isinstance(report, dict) else None)
        if not isinstance(items, list):
            return _blank_signals()
        nms = _names_from(items)
        s = _blank_signals()
        s.update({"json_ok": True, "names": nms,
                  "names_count": len([n for n in nms if n["first_name"] or n["last_name"]])})
        return s
    if not isinstance(report, dict):
        return _blank_signals()
    names = report.get("names") if isinstance(report.get("names"), list) else []
    pe = report.get("is_a_private_equity_firm_or_family_office")
    if isinstance(pe, str):
        pe = pe.strip().lower() == "true"
    ind, reg, eb, dl = (_selected(report.get("industries")), _selected(report.get("regions")),
                        _selected(report.get("ebitda_ranges")), _selected(report.get("deal_types")))
    return {
        "json_ok": True,
        "is_pe": pe,
        "official_name": report.get("official_company_name"),
        "leadership_page": report.get("leadership_page_on_website"),
        "names": [{"first_name": n.get("first_name"), "last_name": n.get("last_name")}
                  for n in names if isinstance(n, dict)],
        "names_count": len([n for n in names if isinstance(n, dict) and (n.get("first_name") or n.get("last_name"))]),
        "industries_sel": ind, "regions_sel": reg, "ebitda_sel": eb, "deal_sel": dl,
        "industries_n": len(ind), "regions_n": len(reg), "ebitda_n": len(eb), "deal_n": len(dl),
    }


# ── run one model / one case ─────────────────────────────────────────────────────────────────────
def run_one_model(config, prompt, model, cid=None, mode="full") -> dict:
    raw = {}
    for attempt in range(3):
        try:
            raw = _dispatch(config, prompt, model)
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
    sig = signals(report, mode)
    row = {
        "mode": mode,
        "model": model, "route": raw.get("route"), "provider": raw.get("provider"),
        "error": raw.get("error"),
        "cost_usd": raw.get("cost_usd"), "latency_ms": raw.get("latency_ms"),
        "input_tokens": raw.get("input_tokens"), "output_tokens": raw.get("output_tokens"),
        "raw": (raw.get("text") or "")[:60000],
        **sig,
    }
    if cid:
        _result_put(cid, model, row, mode)
    return row


def run_case(config: dict, case: dict, models: list, refresh_models: bool = False, mode: str = "full") -> dict:
    cid = case.get("id")
    prompt = build_prompt(case["domain"], mode)
    results, to_run = [], []
    for m in models:
        if cid and not refresh_models:
            cached = _result_get(cid, m, mode)
            if cached and not cached.get("error"):
                results.append(cached)
                continue
        to_run.append(m)
    if to_run:
        with ThreadPoolExecutor(max_workers=min(8, len(to_run))) as ex:
            results.extend(ex.map(lambda m: run_one_model(config, prompt, m, cid, mode), to_run))
    total_cost = round(sum((r.get("cost_usd") or 0) for r in results), 4)
    ok = [r for r in results if r.get("json_ok") and not r.get("error")]
    return {"case_id": cid, "domain": case["domain"], "mode": mode,
            "summary": {"models": len(results), "json_ok": len(ok),
                        "avg_names": round(sum(r.get("names_count", 0) for r in ok) / len(ok), 1) if ok else 0,
                        "cost_usd": total_cost},
            "results": results}


# ── overview + reads ─────────────────────────────────────────────────────────────────────────────
def _buyer_type(cid):
    """Classify a domain as 'pe' (PE firm / family office) or 'trade' (operating company) by majority
    vote of the models' is_pe in FULL mode — stable regardless of which view mode is being rendered."""
    yes = no = 0
    for r in _rows(cid, "full"):
        if r.get("error") or not r.get("json_ok"):
            continue
        if r.get("is_pe") is True:
            yes += 1
        elif r.get("is_pe") is False:
            no += 1
    if yes == 0 and no == 0:
        return None
    return "pe" if yes > no else "trade"


def matrix(mode="full"):
    out = []
    for c in list_cases():
        rows = _rows(c["id"], mode)
        out.append({"id": c["id"], "domain": c["domain"], "note": c.get("note"),
                    "buyer_type": _buyer_type(c["id"]),
                    "results": {r["model"]: r for r in rows}})
    return {"cases": out, "models": DEFAULT_MODELS, "mode": mode, "prompt_chars": len(prompt_template(mode))}


def input_for(domain, mode="full"):
    return {"domain": domain, "prompt": build_prompt(domain, mode)}


def result_for(cid, model, mode="full"):
    return _result_get(cid, model, mode)


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
                ALTER TABLE entity.buyerqa_results ADD COLUMN IF NOT EXISTS mode text NOT NULL DEFAULT 'full';
            """)
            # widen the PK to include mode so 'full' and 'names' runs coexist per (case, model)
            cur.execute("""
                DO $$ BEGIN
                    IF EXISTS (SELECT 1 FROM information_schema.table_constraints
                               WHERE constraint_name='buyerqa_results_pkey' AND table_schema='entity') THEN
                        BEGIN
                            ALTER TABLE entity.buyerqa_results DROP CONSTRAINT buyerqa_results_pkey;
                            ALTER TABLE entity.buyerqa_results ADD PRIMARY KEY (case_id, model, mode);
                        EXCEPTION WHEN others THEN NULL; END;
                    END IF;
                END $$;
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


def _rows(cid, mode="full"):
    # Trust the JSON result->>'mode' (what the result actually IS) over the `mode` column — historical
    # rows were mislabeled (early names-arrays got stamped mode='full'). Dedupe to the latest per model.
    with closing(coverage._conn()) as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT DISTINCT ON (model) result FROM entity.buyerqa_results "
                "WHERE case_id=%s AND COALESCE(result->>'mode', 'full')=%s "
                "ORDER BY model, created_at DESC", (cid, mode))
            return [r["result"] for r in cur.fetchall()]


def _result_get(cid, model, mode="full"):
    with closing(coverage._conn()) as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT result FROM entity.buyerqa_results "
                "WHERE case_id=%s AND model=%s AND COALESCE(result->>'mode', 'full')=%s "
                "ORDER BY created_at DESC LIMIT 1", (cid, model, mode))
            r = cur.fetchone()
            return r["result"] if r else None


def _result_put(cid, model, row, mode="full"):
    with closing(coverage._conn()) as c:
        with c.cursor() as cur:
            cur.execute(
                "INSERT INTO entity.buyerqa_results (case_id, model, mode, result) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT (case_id, model, mode) DO UPDATE SET result=EXCLUDED.result, created_at=now()",
                (cid, model, mode, json.dumps(row, default=str)))
        c.commit()
