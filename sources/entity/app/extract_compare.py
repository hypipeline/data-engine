"""
Model-comparison tester for STAGE 1 — the NAME-EXTRACTION LLM pass.

The pipeline runs the LLM twice:
    Stage 1 (extraction) — phases_extract.extract_entities_with_llm: reads the website text and
                           pulls out candidate entity_names / short_names / people / addresses.
    Stage 2 (analysis)   — the final report with recommended_entity (that's model_compare.py).

THIS module is the Stage-1 sibling of model_compare.py. It reproduces the EXACT (system, user)
that production's extraction call receives — which coverage.build_content already captures as
`extraction_io` — then fans it out across candidate models and compares the names each surfaces,
plus cost and latency.

Scoring is deliberately LENIENT: extraction's job is to surface a candidate name good enough to
drive registry search, NOT the exact legal name. So a case expecting "Quest Global Services Pte.
Ltd." is satisfied by an extraction of "Quest Global" — we match an expected option against an
extracted name if either (normalised) contains the other.

Reuses coverage_cases as the example set, and the transport + parser from model_compare (so a
model called here behaves byte-identically to the analysis comparison).
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

from psycopg2.extras import RealDictCursor

import coverage
import model_compare
from model_compare import DEFAULT_MODELS, call_model, agent_parse, _is_transient_err, _norm


# ── build the Stage-1 input (reuse the SAME content build coverage/model_compare use) ──────────
def build_input(config: dict, case: dict, refresh: bool = False, progress=None) -> dict:
    """The extraction input for a case: production's own extraction (system, user), pulled from the
    cached content's `extraction_io`. Delegates to the ONE shared builder, coverage.build_content,
    so the website text the models receive is byte-identical to what production's Stage 1 sees.
    Rebuilding (refresh) invalidates this case's stored Stage-1 model results."""
    content = coverage.build_content(config, case, refresh=refresh, progress=progress)
    if refresh and case.get("id"):
        _clear_results(case["id"])
    eio = content.get("extraction_io") or {}
    meta = dict(content.get("meta") or {})
    meta["stage1_na"] = not bool(eio.get("user"))     # names-mode cases have no extraction stage
    meta["prod_extracted"] = (meta.get("extracted_names") or [])   # what prod's own model extracted
    meta["user_chars"] = len(eio.get("user") or "")
    return {"system": eio.get("system") or "", "user": eio.get("user") or "", "meta": meta}


# ── scoring: did the model surface a name that matches an expected entity? ──────────────────────
def _extracted_names(report) -> list:
    """All name-like candidates a model produced, deduped. known_parent is included because
    production folds it into entity_names too (see extract_entities_with_llm)."""
    if not isinstance(report, dict):
        return []
    out = []
    for k in ("entity_names", "short_names"):
        v = report.get(k)
        if isinstance(v, list):
            out.extend(str(x) for x in v if str(x).strip())
    kp = report.get("known_parent")
    if kp and str(kp).strip():
        out.append(str(kp))
    seen, uniq = set(), []
    for n in out:
        if n.lower() not in seen:
            seen.add(n.lower())
            uniq.append(n)
    return uniq


def _name_matches(expected: str, extracted_names: list) -> bool:
    """Lenient bidirectional match: an expected option is satisfied if its normalised form contains,
    or is contained by, any extracted name's normalised form (min 4 chars to avoid trivial hits)."""
    e = _norm(expected)
    if len(e) < 4:
        return False
    for n in extracted_names:
        x = _norm(n)
        if len(x) < 4:
            continue
        if e in x or x in e:
            return True
    return False


def score(report, spec) -> dict:
    """spec = {"mode":"entity","options":[{"name":..}, ...]} — expect at least one option surfaced.
    Extraction has no meaningful 'abstain' expectation, so mode 'none'/absent → not scored."""
    names = _extracted_names(report)
    got = ", ".join(names[:6]) if names else None
    if not spec or spec.get("mode") != "entity" or not spec.get("options"):
        return {"scored": False, "got": got, "names": names}
    hits = [o for o in spec["options"] if _name_matches(o.get("name") or o.get("registry_id") or "", names)]
    ok = bool(hits)
    return {"scored": True, "ok": ok, "got": got, "names": names,
            "verdict": ("surfaced expected name" if ok else "expected name not surfaced")}


def spec_for(case):
    """Extraction expectation reuses the case's editable `expect` names (managed in Search Coverage)
    — the names extraction must surface. Falls back to the stored analysis spec's options if present."""
    opts = [{"name": e} for e in (case.get("expect") or []) if str(e).strip()]
    if opts:
        return {"mode": "entity", "options": opts}
    # fall back to the analysis expectation's entity options, if the user set one there
    an = model_compare.spec_for(case)
    if an and an.get("mode") == "entity" and an.get("options"):
        return an
    return None


# ── run one model / one case (mirrors model_compare, parsing the EXTRACTION json) ──────────────
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
    sc = score(report, spec)
    row = {
        "model": model, "route": raw.get("route"), "provider": raw.get("provider"),
        "error": raw.get("error"),
        "names": sc.get("names") or [],
        "jurisdiction": (report or {}).get("jurisdiction") if isinstance(report, dict) else None,
        "cost_usd": raw.get("cost_usd"), "latency_ms": raw.get("latency_ms"),
        "input_tokens": raw.get("input_tokens"), "output_tokens": raw.get("output_tokens"),
        "json_ok": bool(isinstance(report, dict) and not raw.get("error")),
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
    spec = spec_for(case)
    if inp["meta"].get("stage1_na"):                  # names-mode case: no extraction stage to test
        return {"case_id": cid, "input_meta": inp["meta"], "spec": spec, "stage1_na": True,
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


# ── overview + cached reads ────────────────────────────────────────────────────────────────────
def matrix():
    out = []
    for c in coverage.list_cases():
        cid = c["id"]
        cached = get_cached(cid)
        out.append({
            "id": cid, "name": c["name"], "url": c.get("url"), "names": c.get("names"),
            "expect": c.get("expect") or [], "spec": cached.get("spec"),
            "input_meta": cached.get("input_meta"),
            "stage1_na": not bool(c.get("url")),      # no website → no extraction stage
            "results": {r["model"]: r for r in (cached.get("results") or [])},
        })
    return {"cases": out, "models": DEFAULT_MODELS}


def get_cached(cid):
    content = coverage.content_cache_get(cid)
    meta = (content or {}).get("meta")
    with closing(coverage._conn()) as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT result FROM entity.extract_compare_results WHERE case_id=%s ORDER BY model", (cid,))
            rows = [r["result"] for r in cur.fetchall()]
    return {"case_id": cid, "input_meta": meta, "spec": None, "results": rows}


def input_for(config, case):
    """The exact Stage-1 (system, user) a case's models receive — read-only display."""
    inp = build_input(config, case, refresh=False)
    return {"system": inp["system"], "user": inp["user"], "meta": inp["meta"]}


def result_for(cid, model):
    return _result_get(cid, model)


# ── schema + storage (own results table; shares coverage's connection + `entity` schema) ────────
def ensure_schema():
    if not coverage.enabled():
        return
    with closing(coverage._conn()) as c:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS entity.extract_compare_results (
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
            cur.execute("DELETE FROM entity.extract_compare_results WHERE case_id=%s", (cid,))
        c.commit()


def _result_get(cid, model):
    with closing(coverage._conn()) as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT result FROM entity.extract_compare_results WHERE case_id=%s AND model=%s", (cid, model))
            r = cur.fetchone()
            return r["result"] if r else None


def _result_put(cid, model, row):
    with closing(coverage._conn()) as c:
        with c.cursor() as cur:
            cur.execute(
                "INSERT INTO entity.extract_compare_results (case_id, model, result) VALUES (%s,%s,%s) "
                "ON CONFLICT (case_id, model) DO UPDATE SET result=EXCLUDED.result, created_at=now()",
                (cid, model, json.dumps(row, default=str)))
        c.commit()
