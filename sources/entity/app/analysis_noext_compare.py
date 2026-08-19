"""
Stage-2 (analysis) model comparison — WITHOUT the extraction-layer input.

An A/B sibling of model_compare.py. It answers: does the analysis LLM still pick the right entity
if we DON'T re-send it the raw website pages (the exact material Stage-1 extraction already consumed)?
The stripped input keeps everything analysis needs that ISN'T the extraction input — the distilled
CANDIDATE ENTITY NAMES + jurisdiction (extraction's OUTPUT), plus all registry evidence — and drops
only the `=== WEBSITE: … ===` sections. Built by calling the production build_analysis_messages with
website_data={'pages': {}}, so it's byte-identical to what production would send minus those pages.

SELF-CONTAINED / EASY TO REMOVE: everything here is namespaced. To delete the whole feature, drop
this file, table entity.model_compare_results_noext, the /api/noext/* endpoints, the
/entity/tools/analysis-noext* routes, the tool_analysis_noext.html template, and the additive
`noextcase` branch + sidebar link in entity.html. Reuses model_compare's transport/parser/scoring
and the SAME expectation specs (model_compare_expect), so it can't drift from the real Stage-2 tool.
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

from psycopg2.extras import RealDictCursor

import coverage
import model_compare
from model_compare import DEFAULT_MODELS, call_model, agent_parse, _is_transient_err, _domain_of
from agent import EntityLookup

_AGENT = None


def _agent(config):
    global _AGENT
    if _AGENT is None:
        _AGENT = EntityLookup(config, progress_callback=None)
    return _AGENT


# ── build the STRIPPED analysis input (no website pages) ───────────────────────────────────────
def build_input(config: dict, case: dict, refresh: bool = False) -> dict:
    """The analysis input with the extraction material (website pages) removed. Reuses the SAME
    cached Phase-1 content model_compare uses (so registries/extraction are identical), then re-runs
    build_analysis_messages with empty pages — cheap, no extra API calls."""
    content = coverage.build_content(config, case, refresh=refresh)
    if refresh and case.get("id"):
        _clear_results(case["id"])
    info = content.get("info") or {}
    blob = content.get("blob") or {}                    # merged registry evidence (strings), incl. any google intel
    url = case.get("url") or ""
    domain = _domain_of(url)
    system, user, sections = _agent(config).build_analysis_messages(
        url, domain, {"pages": {}, "pageUrls": {}}, info, blob)
    # flag whether google intel (which was ALSO fed to extraction) is present in the kept evidence
    google_keys = [k for k in blob if str(k).split(":")[0] in
                   ("linkedin", "yahoo_finance", "google_results", "google_intel")]
    meta = {
        "mode": content.get("meta", {}).get("mode"),
        "registries": list(blob.keys()),
        "extracted_names": info.get("entity_names"),
        "jurisdiction": info.get("jurisdiction"),
        "user_chars": len(user),
        "full_user_chars": content.get("meta", {}).get("user_chars"),
        "google_intel_present": google_keys,        # non-empty → some extraction input still leaks via registries
        "coverage": content.get("meta", {}).get("coverage"),
    }
    return {"system": system, "user": user, "sections": sections, "meta": meta}


# ── run one model / one case (identical machinery to model_compare, stripped input + own table) ──
def run_one_model(config, inp, model, spec, cid=None) -> dict:
    raw = {}
    for attempt in range(3):
        try:
            raw = call_model(model, inp["system"], inp["user"], config)
        except Exception as e:  # noqa: BLE001
            raw = {"model": model, "error": f"{type(e).__name__}: {e}"}
        if (raw.get("text") or "").strip():
            break
        if raw.get("error") and not _is_transient_err(raw["error"]):
            break
        if attempt < 2:
            time.sleep(2)
    if not raw.get("error") and not (raw.get("text") or "").strip():
        raw["error"] = "empty response — model returned no content (reasoning only) after 3 tries"
    report = None
    if raw.get("text"):
        try:
            report = agent_parse(config, raw["text"])
        except Exception as e:  # noqa: BLE001
            raw["error"] = raw.get("error") or f"parse: {type(e).__name__}: {e}"
    rec = (report or {}).get("recommended_entity")
    conf = (report or {}).get("confidence")
    sc = model_compare.score(report, spec)
    row = {
        "model": model, "route": raw.get("route"), "provider": raw.get("provider"),
        "error": raw.get("error"),
        "recommended": rec, "confidence": conf,
        "cost_usd": raw.get("cost_usd"), "latency_ms": raw.get("latency_ms"),
        "input_tokens": raw.get("input_tokens"), "output_tokens": raw.get("output_tokens"),
        "json_ok": bool(rec is not None or (raw.get("text") and not raw.get("error"))),
        "truncated": raw.get("truncated"),
        "score": sc, "raw": (raw.get("text") or "")[:60000],
    }
    if cid:
        _result_put(cid, model, row)
    return row


def run_case(config: dict, case: dict, models: list, refresh_input: bool = False,
             refresh_models: bool = False) -> dict:
    inp = build_input(config, case, refresh=refresh_input)
    cid = case.get("id")
    spec = model_compare.spec_for(case)
    cov = (inp["meta"].get("coverage") or {})
    if cov.get("status") == "fail":                   # same gate as model_compare: don't test on broken evidence
        return {"case_id": cid, "input_meta": inp["meta"], "spec": spec, "blocked": cov,
                "summary": {"models": 0, "scored": 0, "pass": 0, "cost_usd": 0}, "results": []}
    results, to_run = [], []
    for m in models:
        if cid and not refresh_models:
            cached = _result_get(cid, m)
            if cached and not cached.get("error"):
                results.append(cached)
                continue
        to_run.append(m)
    if to_run:
        with ThreadPoolExecutor(max_workers=min(10, len(to_run))) as ex:
            results.extend(ex.map(lambda m: run_one_model(config, inp, m, spec, cid), to_run))
    total_cost = round(sum((r.get("cost_usd") or 0) for r in results), 4)
    scored = [r for r in results if r["score"].get("scored")]
    return {"case_id": cid, "input_meta": inp["meta"], "spec": spec,
            "summary": {"models": len(results), "scored": len(scored),
                        "pass": sum(1 for r in scored if r["score"].get("ok")),
                        "cost_usd": total_cost},
            "results": results}


# ── overview: stripped results, each paired with the FULL-input result for A/B comparison ───────
def _label(rec):
    if not rec:
        return None
    nm = rec.get("legal_entity_name") or "?"
    return nm + (f" (#{rec['registry_id']})" if rec.get("registry_id") else "")


def matrix():
    out = []
    for c in coverage.list_cases():
        cid = c["id"]
        stripped = {r["model"]: r for r in _rows(cid)}
        full = {}
        for m in DEFAULT_MODELS:
            fr = model_compare.result_for(cid, m)        # the normal Stage-2 result (with website)
            if fr:
                full[m] = {"entity": _label(fr.get("recommended")), "ok": (fr.get("score") or {}).get("ok"),
                           "cost_usd": fr.get("cost_usd")}
        out.append({
            "id": cid, "name": c["name"], "url": c.get("url"),
            "stage1_na": not bool(c.get("url")),
            "results": stripped, "full": full,
        })
    return {"cases": out, "models": DEFAULT_MODELS}


def input_for(config, case):
    return build_input(config, case, refresh=False)


def result_for(cid, model):
    return _result_get(cid, model)


# ── schema + storage (own table) ────────────────────────────────────────────────────────────────
def ensure_schema():
    if not coverage.enabled():
        return
    with closing(coverage._conn()) as c:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS entity.model_compare_results_noext (
                    case_id      bigint NOT NULL,
                    model        text   NOT NULL,
                    result       jsonb  NOT NULL,
                    created_at   timestamptz NOT NULL DEFAULT now(),
                    PRIMARY KEY (case_id, model)
                );
            """)
        c.commit()


def _clear_results(cid):
    with closing(coverage._conn()) as c:
        with c.cursor() as cur:
            cur.execute("DELETE FROM entity.model_compare_results_noext WHERE case_id=%s", (cid,))
        c.commit()


def _rows(cid):
    with closing(coverage._conn()) as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT result FROM entity.model_compare_results_noext WHERE case_id=%s ORDER BY model", (cid,))
            return [r["result"] for r in cur.fetchall()]


def _result_get(cid, model):
    with closing(coverage._conn()) as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT result FROM entity.model_compare_results_noext WHERE case_id=%s AND model=%s", (cid, model))
            r = cur.fetchone()
            return r["result"] if r else None


def _result_put(cid, model, row):
    with closing(coverage._conn()) as c:
        with c.cursor() as cur:
            cur.execute(
                "INSERT INTO entity.model_compare_results_noext (case_id, model, result) VALUES (%s,%s,%s) "
                "ON CONFLICT (case_id, model) DO UPDATE SET result=EXCLUDED.result, created_at=now()",
                (cid, model, json.dumps(row, default=str)))
        c.commit()
