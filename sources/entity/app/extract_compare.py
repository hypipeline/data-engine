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


# ══════════════════════════════════════════════════════════════════════════════════════════════
# Ground truth for Stage 1 — what GOOD DATA should extraction pass to the next stage?
#
# This stage is NOT about picking the final entity. It's about extracting good, real data —
# entity names, key PEOPLE, and ADDRESSES — for Stage 2 to search on. So per case we derive a
# reference target set = the CURRENTLY-LIVE production model's extraction, filtered to items that
# are actually PRESENT VERBATIM in the website text (the alphanumeric-normalised page contains the
# alphanumeric-normalised item). That grounding drops anything the live model hallucinated or knew
# only from training (e.g. known_parent), leaving concrete, checkable facts. Each candidate model is
# then scored on RECALL of that reference set across the three dimensions.
# ══════════════════════════════════════════════════════════════════════════════════════════════
def _list(report, key) -> list:
    v = report.get(key) if isinstance(report, dict) else None
    return [str(x) for x in v if str(x).strip()] if isinstance(v, list) else []


def _extracted_names(report) -> list:
    """All name-like candidates a model produced, deduped. known_parent is included because
    production folds it into entity_names too (see extract_entities_with_llm)."""
    out = _list(report, "entity_names") + _list(report, "short_names")
    kp = report.get("known_parent") if isinstance(report, dict) else None
    if kp and str(kp).strip():
        out.append(str(kp))
    seen, uniq = set(), []
    for n in out:
        if n.lower() not in seen:
            seen.add(n.lower())
            uniq.append(n)
    return uniq


def _loads_loose(text):
    """Parse a model's JSON output tolerating ```json fences / surrounding prose (no config needed)."""
    import re
    s = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", s)
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


def _soft_in(needle, haystacks, minlen=4) -> bool:
    """Lenient bidirectional match: needle matches if its normalised form contains, or is contained
    by, any haystack's normalised form (min length guard to avoid trivial hits)."""
    a = _norm(needle)
    if len(a) < minlen:
        return False
    for h in haystacks:
        b = _norm(h)
        if len(b) >= minlen and (a in b or b in a):
            return True
    return False


def _ground(items, hay_norm, minlen=5) -> list:
    """Keep only items that appear (alphanumeric-normalised) in the website text — deduped."""
    out, seen = [], set()
    for it in items:
        n = _norm(it)
        if len(n) >= minlen and n in hay_norm and n not in seen:
            seen.add(n)
            out.append(it)
    return out


def reference_targets(content) -> dict:
    """The grounded good-data the live model found in the page, per dimension. {} keys always present."""
    eio = (content or {}).get("extraction_io") or {}
    hay = _norm(eio.get("user") or "")
    live = _loads_loose(eio.get("output") or "") or {}
    names = _ground(_list(live, "entity_names") + _list(live, "short_names"), hay, minlen=4)
    people = _ground(_list(live, "key_people"), hay, minlen=5)
    addrs = _ground(_list(live, "addresses"), hay, minlen=8)
    return {"entity_names": names, "key_people": people, "addresses": addrs}


def _recall(targets, got, minlen):
    if not targets:
        return None
    hit = [t for t in targets if _soft_in(t, got, minlen)]
    missed = [t for t in targets if not _soft_in(t, got, minlen)]
    return {"total": len(targets), "hit": len(hit), "hit_items": hit, "missed": missed}


def score(report, targets) -> dict:
    """Grade a model on RECALL of the grounded reference targets across names / people / addresses.
    ok = recovered EVERY grounded entity name (so Stage 2 has something to search) AND overall
    recall >= 0.6 of all good data. Per-dimension detail is returned for display."""
    names = _extracted_names(report)
    people = _list(report, "key_people")
    addrs = _list(report, "addresses")
    got = {"names": names, "people": people, "addresses": addrs}
    dims = {
        "entity_names": _recall(targets.get("entity_names") or [], names, 4),
        "key_people": _recall(targets.get("key_people") or [], people, 5),
        "addresses": _recall(targets.get("addresses") or [], addrs, 8),
    }
    present = {k: v for k, v in dims.items() if v}
    if not present:                               # nothing grounded to grade against
        return {"scored": False, "dims": dims, "got": got}
    tot = sum(v["total"] for v in present.values())
    hit = sum(v["hit"] for v in present.values())
    recall = round(hit / tot, 3) if tot else 0.0
    ent = dims["entity_names"]
    ent_ok = (ent is None) or (ent["hit"] == ent["total"])
    ok = ent_ok and recall >= 0.6
    return {"scored": True, "ok": ok, "recall": recall, "hit": hit, "total": tot,
            "dims": dims, "got": got,
            "verdict": (f"recovered {hit}/{tot} good-data items" if ok
                        else (f"missed searchable name(s)" if not ent_ok
                              else f"only {hit}/{tot} good-data items"))}


# ── run one model / one case (parsing the EXTRACTION json, scoring vs grounded targets) ────────
def run_one_model(config, inp, model, targets, cid=None) -> dict:
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
    sc = score(report, targets)
    row = {
        "model": model, "route": raw.get("route"), "provider": raw.get("provider"),
        "error": raw.get("error"),
        "names": sc["got"]["names"], "people": sc["got"]["people"], "addresses": sc["got"]["addresses"],
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
    content = coverage.build_content(config, case, refresh=refresh_input)
    if refresh_input and case.get("id"):
        _clear_results(case["id"])
    eio = content.get("extraction_io") or {}
    meta = dict(content.get("meta") or {})
    meta["stage1_na"] = not bool(eio.get("user"))
    meta["user_chars"] = len(eio.get("user") or "")
    inp = {"system": eio.get("system") or "", "user": eio.get("user") or "", "meta": meta}
    cid = case.get("id")
    targets = reference_targets(content)
    if meta.get("stage1_na"):                         # names-mode case: no extraction stage to test
        return {"case_id": cid, "input_meta": meta, "targets": targets, "stage1_na": True,
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
            results.extend(ex.map(lambda m: run_one_model(config, inp, m, targets, cid), to_run))
    total_cost = round(sum((r.get("cost_usd") or 0) for r in results), 4)
    scored = [r for r in results if r["score"].get("scored")]
    return {"case_id": cid, "input_meta": meta, "targets": targets,
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
            "targets": cached.get("targets"), "input_meta": cached.get("input_meta"),
            "stage1_na": not bool(c.get("url")),      # no website → no extraction stage
            "results": {r["model"]: r for r in (cached.get("results") or [])},
        })
    return {"cases": out, "models": DEFAULT_MODELS}


def get_cached(cid):
    content = coverage.content_cache_get(cid)
    meta = (content or {}).get("meta")
    targets = reference_targets(content) if content else {"entity_names": [], "key_people": [], "addresses": []}
    with closing(coverage._conn()) as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT result FROM entity.extract_compare_results WHERE case_id=%s ORDER BY model", (cid,))
            rows = [r["result"] for r in cur.fetchall()]
    return {"case_id": cid, "input_meta": meta, "targets": targets, "results": rows}


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
