"""
Entity Lookup v3b (Python) — FastAPI server + streaming ("chatty") UI.

Faithful equivalent of php/index.php: streams each log entry live as the lookup runs
(SSE), rendering the same colorized phases + expandable sections, then the report card.
The lookup runs in a worker thread; its progress_callback pushes entries onto a queue.
"""
from __future__ import annotations

import asyncio
import json
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

import re
from urllib.parse import urlparse

from config import load_config
from agent import EntityLookup
from tools import LookupTools
import cache
import linkedin_cache
import coverage
import model_compare
import extract_compare
import analysis_noext_compare
import buyerqa_compare
import northdata_cases
import validation_cases

# Countries validated via NorthData (faithful to validate.php).
NORTHDATA_COUNTRIES = ['DE', 'NL', 'FR', 'AT', 'CH', 'BE', 'LU', 'IT', 'ES', 'DK',
                       'SE', 'NO', 'FI', 'PL', 'CZ', 'IE']

app = FastAPI(title="Entity Lookup v3b")
CONFIG = load_config()


@app.on_event("startup")
def _startup():
    try:
        cache.ensure_schema()
    except Exception as e:  # noqa: BLE001
        print(f"[cache] schema init skipped: {e}")
    try:
        linkedin_cache.ensure_schema()
    except Exception as e:  # noqa: BLE001
        print(f"[linkedin_cache] schema init skipped: {e}")
    try:
        coverage.ensure_schema()
    except Exception as e:  # noqa: BLE001
        print(f"[coverage] schema init skipped: {e}")
    try:
        model_compare.ensure_schema()
    except Exception as e:  # noqa: BLE001
        print(f"[model_compare] schema init skipped: {e}")
    try:
        extract_compare.ensure_schema()
    except Exception as e:  # noqa: BLE001
        print(f"[extract_compare] schema init skipped: {e}")
    try:
        analysis_noext_compare.ensure_schema()
    except Exception as e:  # noqa: BLE001
        print(f"[analysis_noext] schema init skipped: {e}")
    try:
        buyerqa_compare.ensure_schema()
    except Exception as e:  # noqa: BLE001
        print(f"[buyerqa] schema init skipped: {e}")
    try:
        northdata_cases.ensure_schema()
    except Exception as e:  # noqa: BLE001
        print(f"[northdata] schema init skipped: {e}")
    try:
        validation_cases.ensure_schema()
    except Exception as e:  # noqa: BLE001
        print(f"[validation_labels] schema init skipped: {e}")


def _domain(url: str) -> str:
    return re.sub(r'^www\.', '', (urlparse(url).hostname or ''))


def _run_lookup(url: str, q: "queue.Queue"):
    def progress(entry):
        q.put(("log", entry))
    try:
        agent = EntityLookup(CONFIG, progress_callback=progress)
        result = agent.run(url)
        try:
            cache.save(url, _domain(url), CONFIG.get('model'), result)
        except Exception as e:  # noqa: BLE001
            print(f"[cache] save failed: {e}")
        q.put(("result", result))
    except Exception as e:  # noqa: BLE001
        import traceback
        q.put(("error", f"{e}\n{traceback.format_exc()}"))
    finally:
        q.put(("__done__", None))


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/history")
def history(limit: int = 200):
    rows = []
    for r in cache.history(limit):
        rows.append({
            "url": r.get("url"),
            "domain": r.get("domain"),
            "entity_name": r.get("entity_name"),
            "jurisdiction": r.get("jurisdiction"),
            "confidence": r.get("confidence"),
            "cost_usd": float(r["cost_usd"]) if r.get("cost_usd") is not None else None,
            "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
        })
    return {"lookups": rows}


_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


@app.get("/lookup/stream")
async def lookup_stream(url: str, refresh: bool = False):
    # instant-on-hit: return the most recent cached result unless refresh=1
    if not refresh:
        try:
            cached = cache.get_latest(url, CONFIG.get('model'))
        except Exception as e:  # noqa: BLE001
            print(f"[cache] read failed: {e}"); cached = None
        if cached:
            async def hit():
                banner = {"time": 0.0, "phase": "phase",
                          "message": f"↑ Loaded from cache — original run at {cached.get('cached_at')}", "detail": None}
                yield f"event: log\ndata: {json.dumps(banner)}\n\n"
                # replay the full stored progress log so the whole original run is visible
                for entry in (cached.get('progress_log') or []):
                    yield "event: log\ndata: " + json.dumps(entry, default=str) + "\n\n"
                yield "event: result\ndata: " + json.dumps(cached, default=str) + "\n\n"
                yield "event: done\ndata: {}\n\n"
            return StreamingResponse(hit(), media_type="text/event-stream", headers=_SSE_HEADERS)

    q: "queue.Queue" = queue.Queue()
    threading.Thread(target=_run_lookup, args=(url, q), daemon=True).start()

    async def gen():
        loop = asyncio.get_event_loop()
        while True:
            kind, payload = await loop.run_in_executor(None, q.get)
            if kind == "log":
                yield f"event: log\ndata: {json.dumps(payload)}\n\n"
            elif kind == "result":
                yield "event: result\ndata: " + json.dumps(payload) + "\n\n"
            elif kind == "error":
                yield f"event: error\ndata: {json.dumps(payload)}\n\n"
            else:
                yield "event: done\ndata: {}\n\n"
                break

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/lookup")
async def lookup_api(request: Request):
    body = await request.json()
    url = body.get("url", "")
    refresh = bool(body.get("refresh"))
    if not url:
        return JSONResponse({"error": "url is required"}, status_code=400)
    if not refresh:
        try:
            cached = cache.get_latest(url, CONFIG.get('model'))
        except Exception:  # noqa: BLE001
            cached = None
        if cached:
            return JSONResponse(cached)
    loop = asyncio.get_event_loop()
    result_holder = {}

    def run():
        agent = EntityLookup(CONFIG, progress_callback=None)
        r = agent.run(url)
        try:
            cache.save(url, _domain(url), CONFIG.get('model'), r)
        except Exception as e:  # noqa: BLE001
            print(f"[cache] save failed: {e}")
        result_holder['r'] = r

    await loop.run_in_executor(None, run)
    return JSONResponse(result_holder.get('r', {}))


@app.get("/api/lookup")
async def lookup_api_get(url: str = ""):
    """Read-only companion to the POST handler — backs the report card's 'View API' link.
    A GET must be safe/idempotent, so this NEVER triggers a new lookup; it just returns the
    already-stored JSON for the url (the card is only shown once a result exists)."""
    if not url:
        return JSONResponse({"error": "url is required"}, status_code=400)
    try:
        cached = cache.get_latest(url, CONFIG.get('model'))
    except Exception:  # noqa: BLE001
        cached = None
    if cached:
        return JSONResponse(cached)
    return JSONResponse({"error": "No stored result for this URL yet — run the lookup first."},
                        status_code=404)


# ══════════════════════════════════════════════════════════════════════════
# Sidebar tools — thin JSON endpoints over existing LookupTools methods.
# Faithful ports of php/bizapedia.php, php/bizapedia_tm.php, php/validate.php.
# Reached same-origin at /entity-app/api/* via Caddy.
# ══════════════════════════════════════════════════════════════════════════
def _tools() -> LookupTools:
    return LookupTools(CONFIG)


@app.get("/api/company-search")
def api_company_search(q: str = "", state: str = ""):
    """Bizapedia US state-registry company search (port of bizapedia.php)."""
    q = (q or "").strip()
    if not q:
        return JSONResponse({"query": q, "state": state, "results": [], "error": None, "api_calls": {}})
    t = _tools()                                          # fresh instance → per-request API counts
    try:
        results = t.search_bizapedia(q)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"query": q, "state": state, "results": [], "error": str(e),
                             "api_calls": t.get_api_calls()})
    st = (state or "").strip().upper()
    if st:
        results = [r for r in results
                   if (r.get('FilingJurisdictionPostalAbbreviation') or '').upper() == st
                   or (r.get('DomesticJurisdictionPostalAbbreviation') or '').upper() == st]
    return JSONResponse({"query": q, "state": st, "results": results,
                         "error": None if results else f'No companies found for "{q}".',
                         "api_calls": t.get_api_calls()})


# ── Search-coverage test harness (grep the evidence, not the recommendation) ──
@app.get("/api/coverage/cases")
def api_coverage_cases():
    return JSONResponse({"cases": coverage.list_cases()})


@app.post("/api/coverage/cases")
async def api_coverage_add(request: Request):
    body = await request.json()
    if not (body.get("url") or body.get("names")):
        return JSONResponse({"error": "provide a url or names[]"}, status_code=400)
    cid = coverage.add_case(body)
    return JSONResponse({"id": cid})


@app.put("/api/coverage/cases/{cid}")
async def api_coverage_update(cid: int, request: Request):
    body = await request.json()
    if not (body.get("url") or body.get("names")):
        return JSONResponse({"error": "provide a url or names[]"}, status_code=400)
    coverage.update_case(cid, body)
    return JSONResponse({"ok": True, "id": cid})


@app.delete("/api/coverage/cases/{cid}")
def api_coverage_delete(cid: int):
    coverage.delete_case(cid)
    return JSONResponse({"ok": True})


# ── NorthData INTEGRATION tests (editable; LIVE search→resolve→pick). Parser/resolver unit
#    tests live in pytest (tests/test_northdata_cases.py), not here. ──────────────
@app.get("/api/northdata/resolution-cases")
def api_northdata_resolution_cases():
    return JSONResponse({"cases": northdata_cases.list_resolution_cases()})


@app.post("/api/northdata/resolution-cases")
async def api_northdata_resolution_add(request: Request):
    body = await request.json()
    if not (body.get("names") or []):
        return JSONResponse({"error": "provide names[] (the stage-1 name list)"}, status_code=400)
    return JSONResponse({"id": northdata_cases.add_resolution_case(body)})


@app.put("/api/northdata/resolution-cases/{cid}")
async def api_northdata_resolution_update(cid: int, request: Request):
    body = await request.json()
    if not (body.get("names") or []):
        return JSONResponse({"error": "provide names[] (the stage-1 name list)"}, status_code=400)
    northdata_cases.update_resolution_case(cid, body)
    return JSONResponse({"ok": True, "id": cid})


@app.delete("/api/northdata/resolution-cases/{cid}")
def api_northdata_resolution_delete(cid: int):
    northdata_cases.delete_resolution_case(cid)
    return JSONResponse({"ok": True})


@app.get("/api/northdata/resolve")
def api_northdata_resolve(id: int = 0):
    """Run the LIVE search→resolve→pick path for a stored name list and grade the chosen target."""
    if not id:
        return JSONResponse({"error": "id is required"}, status_code=400)
    try:
        return JSONResponse(northdata_cases.run_resolution_case(CONFIG, id))
    except Exception as e:  # noqa: BLE001
        import traceback
        return JSONResponse({"error": f"{type(e).__name__}: {e}",
                             "trace": traceback.format_exc()[-1200:]}, status_code=500)


# ── Registry-validation TEST cases (editable; LIVE — each Run instantiates the real EntityLookup
#    agent and calls the production validate_entity_in_registry against live registries, then grades
#    the derived actual status vs the expected one). Mirrors the NorthData integration tests. ──────
@app.get("/api/validation-labels/cases")
def api_validation_labels_cases():
    return JSONResponse({"cases": validation_cases.list_cases()})


@app.post("/api/validation-labels/cases")
async def api_validation_labels_add(request: Request):
    body = await request.json()
    if not (body.get("name") and body.get("expect_status")):
        return JSONResponse({"error": "provide name and expect_status"}, status_code=400)
    return JSONResponse({"id": validation_cases.add_case(body)})


@app.put("/api/validation-labels/cases/{cid}")
async def api_validation_labels_update(cid: int, request: Request):
    body = await request.json()
    if not (body.get("name") and body.get("expect_status")):
        return JSONResponse({"error": "provide name and expect_status"}, status_code=400)
    validation_cases.update_case(cid, body)
    return JSONResponse({"ok": True, "id": cid})


@app.delete("/api/validation-labels/cases/{cid}")
def api_validation_labels_delete(cid: int):
    validation_cases.delete_case(cid)
    return JSONResponse({"ok": True})


@app.get("/api/validation-labels/run")
def api_validation_labels_run(id: int = 0):
    """Run the LIVE production validator for a stored case and grade actual vs expected status."""
    if not id:
        return JSONResponse({"error": "id is required"}, status_code=400)
    try:
        return JSONResponse(validation_cases.run_validation_case(CONFIG, id))
    except Exception as e:  # noqa: BLE001
        import traceback
        return JSONResponse({"error": f"{type(e).__name__}: {e}",
                             "trace": traceback.format_exc()[-1200:]}, status_code=500)


@app.post("/api/coverage/run")
async def api_coverage_run(request: Request):
    """Run one case (body {id}) or all. Runs the pipeline up to search_registries only —
    no analysis LLM. Returns per-case, per-check pass/fail/inconclusive."""
    body = await request.json() if await request.body() else {}
    if body.get("id"):
        c = coverage.get_case(int(body["id"]))
        cases = [c] if c else []
    elif body.get("case"):                        # ad-hoc, unsaved case
        cases = [body["case"]]
    else:
        cases = coverage.list_cases()
    refresh = bool(body.get("refresh"))       # bypass the extraction cache (re-run fetch + LLM)
    results = []
    for c in cases:
        if not c:
            continue
        r = coverage.run_case(CONFIG, c, refresh=refresh)
        if c.get("id"):                        # persist so the page shows last result + when
            coverage.save_last_result(c["id"], r)
            if refresh:                        # rebuilt Phase-1 input → old model verdicts are now
                model_compare._clear_results(c["id"])   # stale; drop them so nothing sits on old input
        results.append(r)
    summary = {"total": len(results),
               "pass": sum(1 for r in results if r["status"] == "pass"),
               "fail": sum(1 for r in results if r["status"] == "fail"),
               "inconclusive": sum(1 for r in results if r["status"] == "inconclusive"),
               "error": sum(1 for r in results if r["status"] == "error"),
               "cost_usd": round(sum(r.get("cost_usd") or 0 for r in results), 4)}
    return JSONResponse({"summary": summary, "results": results})


# ── Model-comparison tester (the stage after coverage: same cases, feed the cached ────
# ── Phase-1 analysis input to different models and compare the recommended entity) ────
@app.get("/api/modelcompare/models")
def api_modelcompare_models():
    return JSONResponse({"models": model_compare.DEFAULT_MODELS})


@app.get("/api/modelcompare/results")
def api_modelcompare_results(case_id: int):
    """Cached results for a case (shown on page load, no LLM calls)."""
    return JSONResponse(model_compare.get_cached(int(case_id)))


@app.get("/api/modelcompare/matrix")
def api_modelcompare_matrix():
    """All cases × all cached model results — the overview grid (no LLM calls)."""
    return JSONResponse(model_compare.matrix())


@app.get("/api/modelcompare/input")
def api_modelcompare_input(case_id: int):
    """The FULL analysis input (system + user prompt) fed to every model for a case — the exact
    Phase-1 content the coverage/Stage-1 test validated. Read-only; requires content to exist."""
    c = coverage.content_cache_get(int(case_id))
    if not c:
        return JSONResponse({"error": "No content built yet — run Stage 1 (coverage) first."},
                            status_code=404)
    return JSONResponse({"system": c.get("system"), "user": c.get("user"),
                         "sections": c.get("sections"), "meta": c.get("meta")})


@app.get("/api/modelcompare/result")
def api_modelcompare_result(case_id: int, model: str = ""):
    """One model's stored result row (incl. its raw JSON output) for the full-result view."""
    r = model_compare.result_for(int(case_id), model)
    return JSONResponse(r or {"error": "no stored result for this case/model"})


@app.post("/api/modelcompare/run")
async def api_modelcompare_run(request: Request):
    """Run one coverage case across a set of models. Body: {id | case, models[], refresh_input,
    refresh_models}. Builds/reuses the cached Phase-1 analysis input, then fans out per model."""
    body = await request.json() if await request.body() else {}
    models = body.get("models") or model_compare.DEFAULT_MODELS
    if body.get("id"):
        c = coverage.get_case(int(body["id"]))
        if not c:
            return JSONResponse({"error": "case not found"}, status_code=404)
    elif body.get("case"):
        c = body["case"]
    else:
        return JSONResponse({"error": "provide an id or a case"}, status_code=400)
    try:
        out = model_compare.run_case(CONFIG, c, models,
                                     refresh_input=bool(body.get("refresh_input")),
                                     refresh_models=bool(body.get("refresh_models")))
    except Exception as e:  # noqa: BLE001
        import traceback
        return JSONResponse({"error": f"{type(e).__name__}: {e}",
                             "trace": traceback.format_exc()[-1200:]}, status_code=500)
    return JSONResponse(out)


# ── Buyer Quick Add — model comparison — /api/buyerqa/* ────────────────────────────────────────
@app.get("/api/buyerqa/matrix")
def api_buyerqa_matrix(mode: str = "full"):
    return JSONResponse(buyerqa_compare.matrix(mode))


@app.get("/api/buyerqa/input")
def api_buyerqa_input(id: int = 0, mode: str = "full"):
    c = buyerqa_compare.get_case(int(id))
    if not c:
        return JSONResponse({"error": "case not found"}, status_code=404)
    return JSONResponse(buyerqa_compare.input_for(c["domain"], mode))


@app.get("/api/buyerqa/result")
def api_buyerqa_result(case_id: int, model: str = "", mode: str = "full"):
    r = buyerqa_compare.result_for(int(case_id), model, mode)
    return JSONResponse(r or {"error": "no stored result for this case/model"})


@app.post("/api/buyerqa/run")
async def api_buyerqa_run(request: Request):
    body = await request.json() if await request.body() else {}
    models = body.get("models") or buyerqa_compare.DEFAULT_MODELS
    if not body.get("id"):
        return JSONResponse({"error": "id is required"}, status_code=400)
    c = buyerqa_compare.get_case(int(body["id"]))
    if not c:
        return JSONResponse({"error": "case not found"}, status_code=404)
    try:
        return JSONResponse(buyerqa_compare.run_case(CONFIG, c, models,
                            refresh_models=bool(body.get("refresh_models")), mode=body.get("mode", "full")))
    except Exception as e:  # noqa: BLE001
        import traceback
        return JSONResponse({"error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-1200:]}, status_code=500)


@app.get("/api/buyerqa/cases")
def api_buyerqa_cases():
    return JSONResponse({"cases": buyerqa_compare.list_cases()})


@app.post("/api/buyerqa/cases")
async def api_buyerqa_add(request: Request):
    body = await request.json()
    if not body.get("domain"):
        return JSONResponse({"error": "domain required"}, status_code=400)
    return JSONResponse({"id": buyerqa_compare.add_case(body["domain"], body.get("note"))})


@app.put("/api/buyerqa/cases/{cid}")
async def api_buyerqa_update(cid: int, request: Request):
    body = await request.json()
    if not body.get("domain"):
        return JSONResponse({"error": "domain required"}, status_code=400)
    buyerqa_compare.update_case(cid, body["domain"], body.get("note"))
    return JSONResponse({"ok": True})


@app.delete("/api/buyerqa/cases/{cid}")
def api_buyerqa_delete(cid: int):
    buyerqa_compare.delete_case(cid)
    return JSONResponse({"ok": True})


# ── STAGE 2 analysis WITHOUT the extraction input (A/B) — /api/noext/* ─────────────────────────
@app.get("/api/noext/matrix")
def api_noext_matrix():
    return JSONResponse(analysis_noext_compare.matrix())


@app.get("/api/noext/input")
def api_noext_input(case_id: int):
    c = coverage.get_case(int(case_id))
    if not c:
        return JSONResponse({"error": "case not found"}, status_code=404)
    try:
        return JSONResponse(analysis_noext_compare.input_for(CONFIG, c))
    except Exception as e:  # noqa: BLE001
        import traceback
        return JSONResponse({"error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-1200:]}, status_code=500)


@app.get("/api/noext/result")
def api_noext_result(case_id: int, model: str = ""):
    r = analysis_noext_compare.result_for(int(case_id), model)
    return JSONResponse(r or {"error": "no stored result for this case/model"})


@app.post("/api/noext/run")
async def api_noext_run(request: Request):
    body = await request.json() if await request.body() else {}
    models = body.get("models") or analysis_noext_compare.DEFAULT_MODELS
    if body.get("id"):
        c = coverage.get_case(int(body["id"]))
        if not c:
            return JSONResponse({"error": "case not found"}, status_code=404)
    elif body.get("case"):
        c = body["case"]
    else:
        return JSONResponse({"error": "provide an id or a case"}, status_code=400)
    try:
        out = analysis_noext_compare.run_case(CONFIG, c, models,
                                              refresh_input=bool(body.get("refresh_input")),
                                              refresh_models=bool(body.get("refresh_models")))
    except Exception as e:  # noqa: BLE001
        import traceback
        return JSONResponse({"error": f"{type(e).__name__}: {e}",
                             "trace": traceback.format_exc()[-1200:]}, status_code=500)
    return JSONResponse(out)


# ── STAGE 1 (name-extraction) model comparison — sibling of /api/modelcompare/* ────────────────
@app.get("/api/extractcompare/matrix")
def api_extractcompare_matrix():
    return JSONResponse(extract_compare.matrix())


@app.get("/api/extractcompare/results")
def api_extractcompare_results(case_id: int):
    return JSONResponse(extract_compare.get_cached(int(case_id)))


@app.get("/api/extractcompare/input")
def api_extractcompare_input(case_id: int):
    """The exact Stage-1 extraction input (system prompt + website text) fed to every model."""
    c = coverage.content_cache_get(int(case_id))
    if not c:
        return JSONResponse({"error": "No content built yet — run Search coverage first."},
                            status_code=404)
    eio = c.get("extraction_io") or {}
    if not eio.get("user"):
        return JSONResponse({"error": "This case has no extraction stage (names-mode case)."},
                            status_code=404)
    return JSONResponse({"system": eio.get("system"), "user": eio.get("user"), "meta": c.get("meta")})


@app.get("/api/extractcompare/result")
def api_extractcompare_result(case_id: int, model: str = ""):
    r = extract_compare.result_for(int(case_id), model)
    return JSONResponse(r or {"error": "no stored result for this case/model"})


@app.post("/api/extractcompare/run")
async def api_extractcompare_run(request: Request):
    """Run one coverage case's STAGE-1 extraction input across models. Body: {id | case, models[],
    refresh_input, refresh_models}. Reuses the cached Phase-1 content (extraction_io)."""
    body = await request.json() if await request.body() else {}
    models = body.get("models") or extract_compare.DEFAULT_MODELS
    if body.get("id"):
        c = coverage.get_case(int(body["id"]))
        if not c:
            return JSONResponse({"error": "case not found"}, status_code=404)
    elif body.get("case"):
        c = body["case"]
    else:
        return JSONResponse({"error": "provide an id or a case"}, status_code=400)
    try:
        out = extract_compare.run_case(CONFIG, c, models,
                                       refresh_input=bool(body.get("refresh_input")),
                                       refresh_models=bool(body.get("refresh_models")))
    except Exception as e:  # noqa: BLE001
        import traceback
        return JSONResponse({"error": f"{type(e).__name__}: {e}",
                             "trace": traceback.format_exc()[-1200:]}, status_code=500)
    return JSONResponse(out)


@app.get("/api/modelcompare/run-stream")
async def api_modelcompare_run_stream(case_id: int, models: str = "",
                                      refresh_input: bool = False, refresh_models: bool = False,
                                      scope: str = "failed"):
    """Streaming (SSE) model comparison — emits live progress so the UI isn't a black box during
    the slow Phase-1 build + per-model calls. Events: phase, input_ready, model_start, model_done,
    done, failed. Each model is isolated, so one failure can't abort the run.

    scope selects which case×model cells actually run (cached cells stream back instantly):
      'all'     — run every model (ignores stored results); implied by refresh_models=true.
      'failed'  — run missing + errored cells; keep any cell that produced a result.
      'missing' — run ONLY never-run cells; keep every cell that already has a stored result."""
    scope = (scope or "failed").lower()
    if refresh_models:
        scope = "all"
    model_list = [m.strip() for m in models.split(",") if m.strip()] or model_compare.DEFAULT_MODELS
    case = coverage.get_case(int(case_id))
    q: "queue.Queue" = queue.Queue()

    def worker():
        try:
            if not case:
                q.put(("failed", {"error": "case not found"})); return

            def progress(entry):
                msg = entry.get("message") if isinstance(entry, dict) else str(entry)
                if msg:
                    q.put(("phase", {"message": msg}))

            q.put(("phase", {"message": "Building Phase-1 analysis input (fetch → extract → registries)…"}))
            inp = model_compare.build_input(CONFIG, case, refresh=refresh_input, progress=progress)
            q.put(("input_ready", {"meta": inp["meta"]}))
            cov = inp["meta"].get("coverage") or {}
            if cov.get("status") == "fail":
                # first-stage gate: the Phase-1 evidence doesn't contain the expected entity, so
                # feeding it to the models is meaningless — block instead of a misleading run.
                q.put(("blocked", {"coverage": cov, "input_meta": inp["meta"]}))
                return
            spec = model_compare.spec_for(case)
            cid = case.get("id")
            n = len(model_list)
            results, to_run = [], []
            for m in model_list:
                if cid and scope != "all":
                    cached = model_compare._result_get(cid, m)
                    # 'missing' keeps ANY stored cell (only never-run cells re-run);
                    # 'failed' keeps only non-errored cells (missing + errors re-run).
                    keep = bool(cached) if scope == "missing" else bool(cached and not cached.get("error"))
                    if keep:
                        results.append(cached)
                        q.put(("model_done", {"result": cached, "done": len(results), "total": n, "cached": True}))
                        continue
                to_run.append(m)
            if to_run:
                # models are independent HTTP calls to different providers → run in PARALLEL and
                # stream each result the moment it lands (order = completion, fastest first).
                q.put(("phase", {"message": f"Running {len(to_run)} models in parallel…"}))
                from concurrent.futures import ThreadPoolExecutor, as_completed
                with ThreadPoolExecutor(max_workers=min(10, len(to_run))) as ex:
                    futs = {ex.submit(model_compare.run_one_model, CONFIG, inp, m, spec, cid): m for m in to_run}
                    for fut in as_completed(futs):
                        row = fut.result()
                        results.append(row)
                        q.put(("model_done", {"result": row, "done": len(results), "total": n}))
            total = round(sum((r.get("cost_usd") or 0) for r in results), 4)
            scored = [r for r in results if r["score"].get("scored")]
            q.put(("done", {"summary": {"models": len(results), "scored": len(scored),
                                        "pass": sum(1 for r in scored if r["score"].get("ok")),
                                        "cost_usd": total},
                            "input_meta": inp["meta"], "spec": spec}))
        except Exception as e:  # noqa: BLE001
            import traceback
            q.put(("failed", {"error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-1500:]}))
        finally:
            q.put(("__done__", None))

    threading.Thread(target=worker, daemon=True).start()

    async def gen():
        loop = asyncio.get_event_loop()
        while True:
            kind, payload = await loop.run_in_executor(None, q.get)
            if kind == "__done__":
                break
            yield f"event: {kind}\ndata: {json.dumps(payload, default=str)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream", headers=_SSE_HEADERS)


@app.get("/api/modelcompare/expect")
def api_modelcompare_get_expect(case_id: int):
    """The per-case expected recommendation ({mode:entity,options:[...]} | {mode:none} | null)."""
    return JSONResponse({"case_id": case_id, "spec": model_compare.get_expect(int(case_id))})


@app.post("/api/modelcompare/expect")
async def api_modelcompare_set_expect(request: Request):
    body = await request.json()
    cid = body.get("case_id")
    if not cid:
        return JSONResponse({"error": "case_id required"}, status_code=400)
    model_compare.put_expect(int(cid), body.get("spec"))
    return JSONResponse({"ok": True})


@app.get("/api/trademark-search")
def api_trademark_search(q: str = "", mode: str = "name"):
    """Bizapedia US trademark search by mark name or owner (port of bizapedia_tm.php)."""
    q = (q or "").strip()
    mode = mode if mode in ("name", "owner") else "name"
    if not q:
        return JSONResponse({"query": q, "mode": mode, "results": [], "error": None})
    try:
        out = _tools().search_trademarks(q, mode)
    except Exception as e:  # noqa: BLE001
        out = {"results": [], "error": str(e)}
    return JSONResponse({"query": q, "mode": mode, **out})


@app.get("/api/validate")
def api_validate(entity_name: str = "", registry_id: str = "", country: str = "", state: str = ""):
    """Registry validation (port of validate.php Phase-7 logic). name+registry_id+country[+state]."""
    entity_name = (entity_name or "").strip()
    registry_id = (registry_id or "").strip()
    country = (country or "").strip().upper()
    state = (state or "").strip().upper()
    if not registry_id or not country:
        return JSONResponse({"error": "registry_id and country are required"})

    t = _tools()
    registry_name = registry_status = source = raw_data = None
    is_branch = is_fictitious = False
    domestic_state = fictitious_owner = None

    # US → Bizapedia
    if country == 'US' and state:
        biz = t.lookup_bizapedia_by_file_number(registry_id, state)
        if biz:
            registry_name = biz.get('EntityName')
            registry_status = biz.get('FilingStatus')
            source = 'Bizapedia'
            raw_data = biz
            entity_type = (biz.get('EntityType') or '').upper()
            domestic_state = biz.get('DomesticJurisdictionPostalAbbreviation')
            if 'FOREIGN' in entity_type or 'OUT OF STATE' in entity_type:
                is_branch = True
            if 'FICTITIOUS' in entity_type:
                is_fictitious = True
                for p in (biz.get('Principals') or []):
                    if (p.get('Titles') or '').lower() == 'owner' and p.get('PrincipalName'):
                        fictitious_owner = p['PrincipalName']
                        break

    # UK → Companies House
    if country == 'GB' and not registry_name:
        ch = t.lookup_companies_house_by_number(registry_id)
        if ch:
            registry_name = ch.get('company_name')
            registry_status = ch.get('company_status')
            source = 'Companies House'
            raw_data = ch

    # Europe → NorthData
    if country in NORTHDATA_COUNTRIES and not registry_name:
        nd = t.validate_northdata_entity(entity_name, registry_id, country)
        if nd:
            full = re.sub(r'\s*\([^)]*\)\s*$', '', nd.get('name') or '')
            parts = [x.strip() for x in full.split(',')]
            registry_name = ', '.join(parts[:-2]) if len(parts) >= 3 else parts[0]
            registry_status = nd.get('status') or 'unknown'
            source = 'NorthData'
            raw_data = nd
            if not nd.get('country_match'):
                registry_name = None

    # Singapore → ACRA (data.gov.sg open dataset); registry_id is the UEN, status tells us active
    if country == 'SG' and not registry_name:
        sg = t.lookup_singapore_by_uen(registry_id)
        if sg:
            registry_name = sg.get('name')
            registry_status = sg.get('status')
            source = 'ACRA (Singapore)'
            raw_data = sg

    if not registry_name:
        return JSONResponse({"result": False, "status": "not_found",
                             "message": f'Registry ID "{registry_id}" not found in ' + (source or 'registry'),
                             "source": source, "raw": raw_data})

    norm_llm = re.sub(r'[^A-Z0-9 ]', '', entity_name.upper())
    norm_reg = re.sub(r'[^A-Z0-9 ]', '', (registry_name or '').upper())
    name_match = (not entity_name) or norm_llm == norm_reg
    # 'registered'/'live'/'existing' are ACRA (Singapore) active statuses; the rest cover US/UK/EU
    status_ok = (registry_status or '').lower() in ('active', 'unknown', 'registered', 'live', 'existing')
    reg_id_ok = (raw_data or {}).get('registry_id_match') is not False

    # link back out to the actual public register (shown on the validation result page)
    registry_url = None
    if source == 'Companies House' and registry_id:
        registry_url = f"https://find-and-update.company-information.service.gov.uk/company/{registry_id}"
    elif source == 'NorthData':
        registry_url = (raw_data or {}).get('url')
    elif source == 'Bizapedia':
        registry_url = (raw_data or {}).get('BizapediaUrl') or (raw_data or {}).get('Url')
    elif source == 'ACRA (Singapore)':
        registry_url = "https://www.bizfile.gov.sg/"     # official ACRA search (per-UEN profile is paid)

    base = {"registry_name": registry_name, "registry_status": registry_status, "source": source,
            "registry_url": registry_url,
            "name_match": name_match, "registry_id_match": reg_id_ok,
            "name_normalised": {"input": norm_llm, "registry": norm_reg}, "raw": raw_data,
            "is_branch": is_branch, "is_fictitious": is_fictitious}
    if is_branch:
        base["domestic_state"] = domestic_state
    if is_fictitious:
        base["fictitious_owner"] = fictitious_owner

    if not name_match:
        return JSONResponse({**base, "result": False, "status": "name_mismatch",
                             "message": f'Name mismatch: input "{entity_name}" but registry has "{registry_name}"'})
    if not reg_id_ok:
        return JSONResponse({**base, "result": False, "status": "registry_id_mismatch",
                             "message": f'Entity "{registry_name}" found in {source} but registry ID "{registry_id}" not found on page'})
    if is_fictitious:
        owner_msg = f" Owner: {fictitious_owner}." if fictitious_owner else ""
        owner_lookup = None
        if fictitious_owner and country == 'US' and state:
            owner_results = t.search_bizapedia(fictitious_owner)
            if owner_results:
                owner_lookup = [{
                    'EntityName': r.get('EntityName') or '',
                    'FileNumber': r.get('FileNumber') or '',
                    'FilingStatus': r.get('FilingStatus') or '',
                    'EntityType': r.get('EntityType') or '',
                    'FilingJurisdiction': r.get('FilingJurisdictionPostalAbbreviation') or '',
                    'DomesticJurisdiction': r.get('DomesticJurisdictionPostalAbbreviation') or '',
                } for r in owner_results if 'FICTITIOUS' not in (r.get('EntityType') or '').upper()]
        extra = {"result": False, "status": "fictitious_name",
                 "message": f"This is a fictitious name (trade name) registration, not a legal entity.{owner_msg} Look up the owning entity instead."}
        if owner_lookup is not None:
            extra["owner_registry_results"] = owner_lookup
        return JSONResponse({**base, **extra})
    if is_branch:
        return JSONResponse({**base, "result": False, "status": "branch_registration",
                             "message": f"This is a branch (Foreign) registration in {state}. Home jurisdiction is {domestic_state}. Use the domestic filing instead."})
    if not status_ok:
        return JSONResponse({**base, "result": False, "status": "name_match_bad_status",
                             "message": f'Name and registry ID match but status is "{registry_status}" (not active) in {source}'})
    return JSONResponse({**base, "result": True, "status": "verified",
                         "message": f'Verified: "{registry_name}" is {registry_status} in {source}'
                                    + (" (registry ID confirmed on page)" if source == 'NorthData' else "")})


# ── LinkedIn Finder ───────────────────────────────────────────────────────────
# Google (Bright Data SERP) → linkedin.com/company URL → LinkedIn Organization data
# (employees the headline field). Same flow the entity pipeline uses; results cached in
# Postgres so repeat lookups don't re-hit Bright Data (which costs per call).
def _norm_query(q: str) -> str:
    """Normalize the user input: a URL collapses to its bare domain; a name is trimmed/lowered."""
    q = (q or "").strip()
    if not q:
        return ""
    if "://" in q or q.lower().startswith("www."):
        host = urlparse(q if "://" in q else "http://" + q).hostname or q
        return re.sub(r'^www\.', '', host).lower()
    if "." in q and " " not in q:                 # looks like a bare domain
        return re.sub(r'^www\.', '', q).lower()
    return q.lower()


@app.get("/api/linkedin")
def api_linkedin(q: str = "", refresh: str = ""):
    """Find a company's LinkedIn page + employee count from a domain or name."""
    query = _norm_query(q)
    if not query:
        return JSONResponse({"error": "Enter a company domain or name."})

    if not refresh:
        cached = linkedin_cache.get_latest(query)
        if cached:
            return JSONResponse(cached)

    t = _tools()
    try:
        linkedin_url = t.find_linkedin_url(query)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"query": query, "error": f"Google search failed: {e}"})

    result = {
        "query": query,
        "linkedin_url": linkedin_url,
        "name": None, "employees": None, "website": None, "address": None,
        "description": None, "slogan": None, "org": None,
        "from_cache": False,
    }

    if linkedin_url:
        try:
            data = t.linkedin_company_data(linkedin_url)
        except Exception as e:  # noqa: BLE001
            data = None
            result["linkedin_error"] = str(e)
        if data:
            for k in ("name", "employees", "website", "address", "address_locality",
                      "address_country", "description", "slogan", "org"):
                result[k] = data.get(k)

    if not result["linkedin_url"]:
        result["error"] = "No LinkedIn company page found for this company."
    elif result["employees"] is None and result["name"] is None:
        result["error"] = "Found the LinkedIn page but couldn't read its company data."

    # Only cache genuine successes — a failed/empty lookup should retry next time, not stick.
    if not result.get("error"):
        try:
            linkedin_cache.save(query, result)
        except Exception as e:  # noqa: BLE001
            print(f"[linkedin_cache] save failed: {e}")
    result["api_calls"] = t.get_api_calls()          # added after save so counts aren't persisted
    result["cost"] = _bd_cost(t)
    return JSONResponse(result)


@app.get("/api/linkedin/by-url")
def api_linkedin_by_url(url: str = "", refresh: str = ""):
    """Fetch a company's LinkedIn page + employee count from the EXACT company URL (no Google
    step) — so a precise URL isn't mis-resolved to a similarly-named company. Cached by slug."""
    import re as _re
    url = (url or "").strip()
    if not url:
        return JSONResponse({"error": "Enter a LinkedIn company URL."})
    m = _re.search(r"/company/([^/?#]+)", url)
    slug = (m.group(1) if m else url).lower()
    if not refresh:
        cached = linkedin_cache.get_latest(slug)
        if cached:
            # Only trust a cache hit whose linkedin_url slug EXACTLY matches the requested one —
            # the query-keyed cache can hold a Google-mis-resolved entry under this slug.
            cu = cached.get("linkedin_url") or ""
            mc = _re.search(r"/company/([^/?#]+)", cu)
            if (mc.group(1).lower() if mc else "") == slug:
                return JSONResponse(cached)
    t = _tools()
    result = {"query": slug, "linkedin_url": url, "name": None, "employees": None,
              "website": None, "address": None, "description": None, "org": None, "from_cache": False}
    try:
        data = t.linkedin_company_data(url)
    except Exception as e:  # noqa: BLE001
        data = None
        result["linkedin_error"] = str(e)
    if data:
        for k in ("name", "employees", "website", "address", "address_locality",
                  "address_country", "description", "slogan", "org"):
            result[k] = data.get(k)
    if result["employees"] is None and result["name"] is None:
        result["error"] = "Couldn't read the LinkedIn company data for that URL."
    if not result.get("error"):
        try:
            linkedin_cache.save(slug, result)
        except Exception as e:  # noqa: BLE001
            print(f"[linkedin_cache] save failed: {e}")
    result["api_calls"] = t.get_api_calls()
    result["cost"] = _bd_cost(t)
    return JSONResponse(result)


# Bright Data Web Unlocker est. cost per request (marketplace ~$1.5/1k successful reqs → $0.0015 each).
BRIGHTDATA_REQ_RATE = 0.0015

# Targeted role qualifiers appended to `site:linkedin.com/in/ "Company" <term>` to surface decision-makers.
# (label, google-term) — multi-word terms quoted so they match as a phrase.
LP_ROLES = [
    ("CXO", "CXO"), ("CEO", "CEO"), ("founder", "founder"),
    ("corporate finance", '"corporate finance"'), ("corporate development", '"corporate development"'),
    ("CFO", "CFO"), ("managing director", '"managing director"'), ("president", "president"),
    ("co-founder", "co-founder"), ("partner", "partner"), ("director", "director"),
    ("board member", '"board member"'), ("chairman", "chairman"),
    ("Head of Corporate Finance", '"Head of Corporate Finance"'), ("Finance Director", '"Finance Director"'),
]


def _bd_cost(t):
    n = t.get_api_calls().get("brightdata", 0)
    return {"brightdata_calls": n, "rate": BRIGHTDATA_REQ_RATE, "usd": round(n * BRIGHTDATA_REQ_RATE, 4)}


def _lp_key(inp):
    """Cache key for a LinkedIn-Profiles report — a LinkedIn company URL keys on its slug, a website
    on its bare domain (so re-running the same input is instant + free)."""
    m = re.search(r"/company/([^/?#]+)", (inp or "").lower())
    return ("co:" + m.group(1).rstrip("/")) if m else ("web:" + _norm_query(inp))


@app.get("/api/linkedin-profiles")
def api_linkedin_profiles(input: str = ""):
    """Find employee LinkedIn profiles for a company. Input = a website URL OR a LinkedIn *company* URL.
    1) website → resolve its LinkedIn company page (skipped if a LinkedIn company URL is given)
    2) read the company NAME off that LinkedIn page
    3) Google `site:linkedin.com/in/ "Company Name"` via Bright Data → employee profile hits
    Returns the per-step trail, the profile results, and Bright Data call count + est. cost."""
    inp = (input or "").strip()
    if not inp:
        return JSONResponse({"error": "Enter a website URL or a LinkedIn company URL."})
    t = _tools()
    steps, low, company_url = [], inp.lower(), None

    # ── Steps 1–2: get the company's LinkedIn page ──
    if "linkedin.com/company/" in low:
        company_url = inp if inp.startswith("http") else "https://" + inp
        steps.append({"n": 1, "label": "Input is a LinkedIn company URL — skipped website resolution",
                      "ok": True, "result": company_url})
    elif "linkedin.com/in/" in low:
        return JSONResponse({"input": inp, "steps": steps,
                             "error": "That's a personal profile URL. Enter a company website or a LinkedIn company URL."})
    else:
        q = _norm_query(inp)
        step = {"n": 1, "label": "Website → find LinkedIn company page (Bright Data Google)", "detail": q}
        try:
            company_url = t.find_linkedin_url(q)
        except Exception as e:  # noqa: BLE001
            step["error"] = str(e)
        step["ok"] = bool(company_url); step["result"] = company_url
        steps.append(step)
        if not company_url:
            return JSONResponse({"input": inp, "steps": steps, "error": "No LinkedIn company page found for that website.",
                                 "api_calls": t.get_api_calls(), "cost": _bd_cost(t)})

    # ── Step 3: company name from the LinkedIn page ──
    step = {"n": 2, "label": "Read company name from LinkedIn (Bright Data)", "detail": company_url}
    company_name = employees = None
    try:
        data = t.linkedin_company_data(company_url)
    except Exception as e:  # noqa: BLE001
        data = None; step["error"] = str(e)
    if data:
        company_name, employees = data.get("name"), data.get("employees")
    step["ok"] = bool(company_name); step["result"] = company_name
    steps.append(step)
    if not company_name:
        return JSONResponse({"input": inp, "company_url": company_url, "steps": steps,
                             "error": "Found the LinkedIn page but couldn't read the company name.",
                             "api_calls": t.get_api_calls(), "cost": _bd_cost(t)})

    # ── Step 4: Google site:linkedin.com/in/ "Company Name" ──
    query = 'site:linkedin.com/in/ "%s"' % company_name
    step = {"n": 3, "label": "Google people search (Bright Data)", "detail": query}
    profiles = []
    try:
        html = t._google_serp_html(query)
        seen = set()
        for title, url in t._parse_serp_organic(html or ""):
            if "linkedin.com/in/" not in url.lower():
                continue
            clean = url.split("?")[0].rstrip("/")
            if clean in seen:
                continue
            seen.add(clean)
            name = re.split(r"\s[–|\-]\s", title)[0].strip()
            profiles.append({"name": name, "title": title, "url": clean})
        step["ok"] = True
    except Exception as e:  # noqa: BLE001
        step["error"] = str(e); step["ok"] = False
    step["result"] = "%d profiles" % len(profiles)
    steps.append(step)

    return JSONResponse({"input": inp, "company_url": company_url, "company_name": company_name,
                         "employees": employees, "query": query, "profiles": profiles, "steps": steps,
                         "api_calls": t.get_api_calls(), "cost": _bd_cost(t)})


@app.get("/api/linkedin-profiles/stream")
def api_linkedin_profiles_stream(input: str = "", refresh: str = ""):
    """Same flow as /api/linkedin-profiles but STREAMS a live log (SSE) — one `log` event per step as
    it happens, a `result` event with the full payload, then `done`. Cached per input: a repeat run
    replays the stored report instantly (no Bright Data calls) unless refresh=1."""
    def sse(event, data):
        return "event: %s\ndata: %s\n\n" % (event, json.dumps(data, default=str))

    def gen():
        inp = (input or "").strip()
        if not inp:
            yield sse("fail", {"error": "Enter a website URL or a LinkedIn company URL."}); yield sse("done", {}); return
        key = _lp_key(inp)
        # Load the latest saved report. A normal run replays it (no Bright Data calls). A re-run
        # REUSES the already-resolved company page + name (Steps 1-2 are stable — the company's
        # LinkedIn page doesn't change) and only re-fetches the employee profiles (Steps 3-4), so a
        # transient Step-1 Google lookup failure can never wipe a run we resolved once before.
        try:
            cached = linkedin_cache.report_get_latest(key)
        except Exception:  # noqa: BLE001
            cached = None
        if cached and not refresh:
            yield sse("log", {"key": "cache", "msg": "Loaded from cache — original run %s · no Bright Data calls (tick “re-run” to refresh)" % (cached.get("cached_at") or "").replace("T", " ")[:19], "ok": True})
            yield sse("result", cached)
            yield sse("done", {})
            return
        t = _tools()
        low, company_url = inp.lower(), None
        company_name = employees = None
        reuse = bool(refresh and cached and cached.get("company_url") and cached.get("company_name"))
        try:
            if reuse:
                # Re-run: skip the fragile live company-page lookup; reuse what we resolved before.
                company_url = cached.get("company_url")
                company_name = cached.get("company_name")
                employees = cached.get("employees")
                yield sse("log", {"key": "s1", "step": 1, "msg": "Website → LinkedIn company page (reused from last run — re-run only re-searches employees)", "ok": True, "result": company_url})
                yield sse("log", {"key": "s2", "step": 2, "msg": "Company name (reused from last run)", "ok": True, "result": (company_name or "") + (" · %s employees" % employees if employees else "")})
            else:
                # ── Step 1: company LinkedIn page ──
                if "linkedin.com/company/" in low:
                    company_url = inp if inp.startswith("http") else "https://" + inp
                    yield sse("log", {"key": "s1", "step": 1, "msg": "Input is a LinkedIn company URL — skipped resolution", "ok": True, "result": company_url})
                elif "linkedin.com/in/" in low:
                    yield sse("fail", {"error": "That's a personal profile URL. Enter a website or a LinkedIn company URL."}); yield sse("done", {}); return
                else:
                    q = _norm_query(inp)
                    yield sse("log", {"key": "s1", "step": 1, "msg": "Website → find LinkedIn company page", "detail": q, "running": True})
                    company_url = t.find_linkedin_url(q)
                    if not company_url:
                        yield sse("log", {"key": "s1", "step": 1, "msg": "Website → find LinkedIn company page", "ok": False, "result": "no company page found"})
                        yield sse("result", {"input": inp, "error": "No LinkedIn company page found for that website.", "cost": _bd_cost(t), "api_calls": t.get_api_calls()}); yield sse("done", {}); return
                    yield sse("log", {"key": "s1", "step": 1, "msg": "Website → find LinkedIn company page", "ok": True, "result": company_url})

                # ── Step 2: company name ──
                yield sse("log", {"key": "s2", "step": 2, "msg": "Read company name from LinkedIn", "detail": company_url, "running": True})
                data = t.linkedin_company_data(company_url)
                company_name = data.get("name") if data else None
                employees = data.get("employees") if data else None
                if not company_name:
                    yield sse("log", {"key": "s2", "step": 2, "msg": "Read company name from LinkedIn", "ok": False, "result": "couldn't read name"})
                    yield sse("result", {"input": inp, "company_url": company_url, "error": "Couldn't read the company name.", "cost": _bd_cost(t), "api_calls": t.get_api_calls()}); yield sse("done", {}); return
                yield sse("log", {"key": "s2", "step": 2, "msg": "Read company name from LinkedIn", "ok": True, "result": company_name + (" · %s employees" % employees if employees else "")})

            # ── Step 3: Google people search — broad (30) + 15 role searches, shown as substeps ──
            base = 'site:linkedin.com/in/ "%s"' % company_name
            profiles = {}   # clean_url -> {name, title, description, followers, url, hits}

            def search_one(qstr, pages):
                # PURE fetch (no shared-state writes) → safe to run in a thread; merge happens in gen
                out = []
                for pg in range(pages):
                    got = t._google_serp_json(qstr, start=pg * 10)
                    ph = 0
                    for o in got:
                        if "linkedin.com/in/" in (o.get("link") or "").lower():
                            ph += 1; out.append(o)
                    if pg > 0 and ph == 0:
                        break   # Google exhausted
                return out

            def nice_of(lbl):
                return "all employees — everyone at the company (3 pages)" if lbl == "all employees" else ('“%s”' % lbl)

            searches = [("all employees", base, 3)] + [(lbl, base + " " + term, 1) for lbl, term in LP_ROLES]
            yield sse("log", {"key": "s3", "step": 3, "parent": True, "msg": "Searching Google (%d queries, in parallel)" % len(searches), "running": True})
            for lbl, qstr, pages in searches:   # pre-list substeps in fixed order (running)
                yield sse("log", {"key": "s3." + lbl, "sub": True, "msg": nice_of(lbl), "detail": qstr, "running": True})

            # run all searches concurrently; merge + emit each substep as it completes (~15s vs ~80s)
            with ThreadPoolExecutor(max_workers=6) as ex:
                futs = {ex.submit(search_one, qstr, pages): lbl for lbl, qstr, pages in searches}
                for fut in as_completed(futs):
                    lbl = futs[fut]
                    try:
                        results = fut.result()
                    except Exception:  # noqa: BLE001
                        results = []
                    new = 0
                    for o in results:
                        clean = (o.get("link") or "").split("?")[0].rstrip("/")
                        if clean not in profiles:
                            title = o.get("title") or ""
                            nm = re.split(r"\s[–|\-]\s", title)[0].strip()
                            profiles[clean] = {"name": nm, "title": title,
                                               "description": o.get("description") or "",
                                               "followers": o.get("display_link") or "",
                                               "url": clean, "hits": []}
                            new += 1
                        if lbl not in profiles[clean]["hits"]:
                            profiles[clean]["hits"].append(lbl)
                    c = _bd_cost(t)
                    yield sse("log", {"key": "s3." + lbl, "sub": True, "msg": nice_of(lbl), "ok": True,
                                      "result": "%d unique · %d new · $%s" % (len(profiles), new, c["usd"])})

            ranked = sorted(profiles.values(), key=lambda p: (-len(p["hits"]), p["name"].lower()))
            search_cost = _bd_cost(t)
            yield sse("log", {"key": "s3", "step": 3, "parent": True, "ok": True,
                              "msg": "Searching Google (%d queries)" % len(searches),
                              "result": "%d unique profiles · %d Bright Data calls · ~$%s" % (len(ranked), search_cost["brightdata_calls"], search_cost["usd"])})

            # Emit the full ranked list NOW — it renders immediately; verified titles stream onto
            # the cards afterwards (Step 4). Cost shown here is the search-only cost so far.
            report = {"input": inp, "company_url": company_url, "company_name": company_name,
                      "employees": employees, "base_query": base, "searches": len(searches),
                      "profiles": ranked, "cost": search_cost, "api_calls": t.get_api_calls()}
            yield sse("result", report)

            # ── Step 4: verify the top 20 via the LinkedIn people dataset (gd_l1viktl72bvl7bjuj0) —
            # ONE batched job returns each profile's structured CURRENT company. Far more reliable
            # than the page <title> (which conflates location/education), and one job avoids the
            # per-request rate-limiting of many Web Unlocker calls. LinkedIn masks the role/title
            # for logged-out scraping (position=null), so the role stays from the Google SERP above;
            # this step confirms whether each person is *still at the company*. ──
            top = ranked[:20]
            cn_l = (company_name or "").strip().lower()
            yield sse("log", {"key": "s4", "step": 4, "parent": True, "running": True,
                              "msg": "Checking which of the top %d are still at the company (LinkedIn dataset)…" % len(top)})
            # Split into small parallel dataset jobs so we can stream real progress ("N/20 checked")
            # and finish faster than one big ~135s batch. A chunk failing only affects its members.
            CH = 4
            chunks = [top[i:i + CH] for i in range(0, len(top), CH)]

            def _run_chunk(chunk):
                return chunk, t.linkedin_profiles_dataset([p["url"] for p in chunk])

            def _apply(p, row):
                if row:
                    cc, ctitle = t.row_current_company(row)
                    if cc:
                        p["current_company"] = cc
                    if ctitle:
                        p["cur_title"] = ctitle
                    if row.get("city"):
                        p["ds_city"] = row.get("city")
                cc_l = (p.get("current_company") or "").strip().lower()
                p["at_company"] = bool(cc_l and cn_l and (cn_l in cc_l or cc_l in cn_l))

            checked = 0
            with ThreadPoolExecutor(max_workers=3) as ex:
                futs = [ex.submit(_run_chunk, c) for c in chunks]
                pending = set(futs)
                waited = 0
                while pending:
                    ready = [f for f in pending if f.done()]
                    if not ready:                        # spinner tick — keeps the SSE stream alive
                        time.sleep(2); waited += 2
                        yield sse("verify", {"progress": True, "checked": checked,
                                             "total": len(top), "elapsed": waited, "cost": _bd_cost(t)})
                        continue
                    for fut in ready:
                        pending.discard(fut)
                        try:
                            chunk, ds = fut.result()
                        except Exception:  # noqa: BLE001
                            chunk, ds = [], {}
                        for p in chunk:
                            _apply(p, ds.get(t._slug_of(p["url"])))
                            checked += 1
                            yield sse("verify", {"url": p["url"], "current_company": p.get("current_company"),
                                                 "cur_title": p.get("cur_title"), "at_company": p.get("at_company"),
                                                 "city": p.get("ds_city"), "checked": checked,
                                                 "total": len(top), "cost": _bd_cost(t)})

            # TOP 12 = the 12 highest-ranked people STILL at the company — skip movers / no-company.
            picked = 0
            for p in ranked:
                is_top = bool(p.get("at_company")) and picked < 12
                p["top12"] = is_top
                if is_top:
                    picked += 1
            yield sse("rerank", {"top12": [p["url"] for p in ranked if p.get("top12")]})

            cost = _bd_cost(t)
            at_n = sum(1 for p in top if p.get("at_company"))
            got_n = sum(1 for p in top if p.get("current_company"))
            yield sse("log", {"key": "s4", "step": 4, "parent": True, "ok": True,
                              "msg": "Verifying top %d" % len(top),
                              "result": "%d still at %s · %d/%d with a listed current company · %d Bright Data calls total · ~$%s" % (
                                  at_n, company_name or "company", got_n, len(top), cost["brightdata_calls"], cost["usd"])})

            # persist the enriched report (profiles now carry page_title; final cost incl. verify)
            report["cost"] = cost
            try:
                linkedin_cache.report_save(key, report)
            except Exception as e:  # noqa: BLE001
                print(f"[linkedin_cache] report_save failed: {e}")
        except Exception as e:  # noqa: BLE001
            yield sse("fail", {"error": str(e)})
        yield sse("done", {})

    return StreamingResponse(gen(), media_type="text/event-stream", headers=_SSE_HEADERS)


@app.get("/api/linkedin-profiles/history")
def api_linkedin_profiles_history(limit: int = 50):
    return JSONResponse({"history": linkedin_cache.report_history(limit)})


@app.get("/api/linkedin/history")
def api_linkedin_history(limit: int = 100):
    return JSONResponse({"history": linkedin_cache.history(limit)})


PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Entity Lookup</title>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body { font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; background:#ffffff; color:#333; }
  .header { background:#ffffff; color:#1a1a2e; padding:20px 28px; border-bottom:1px solid #e4e7ec; }
  .header h1 { font-size:20px; font-weight:600; }
  .header p { font-size:13px; color:#5c6675; margin-top:4px; }
  .content { padding:24px 28px; }
  .search-form { display:flex; gap:12px; max-width:800px; margin-bottom:20px; }
  .search-input { flex:1; padding:12px 16px; font-size:15px; border:2px solid #e0e0e0; border-radius:8px; outline:none; }
  .search-input:focus { border-color:#4a90d9; }
  .search-btn { padding:12px 28px; background:#4a90d9; color:#fff; border:none; border-radius:8px; font-size:15px; font-weight:600; cursor:pointer; }
  .search-btn:hover { background:#3a7bc8; }
  .meta-line { font-size:13px; color:#666; margin:4px 0 14px; min-height:18px; }
  .report-card { background:#fff; border-radius:12px; border:2px solid #e0e0e0; overflow:hidden; max-width:900px; margin-bottom:20px; }
  .report-card.conf-high { border-color:#27ae60; } .report-card.conf-medium { border-color:#f39c12; }
  .report-card.conf-low { border-color:#e67e22; } .report-card.conf-insufficient { border-color:#e74c3c; }
  .report-header { padding:24px; border-bottom:1px solid #f0f0f0; }
  .report-entity { font-size:22px; font-weight:700; color:#1a1a2e; }
  .report-meta { display:flex; gap:16px; margin-top:10px; font-size:13px; color:#666; flex-wrap:wrap; align-items:center; }
  .report-meta span, .report-meta a { display:inline-flex; align-items:center; gap:4px; }
  .cost-badge { background:#d4edda; color:#155724; padding:2px 8px; border-radius:4px; font-weight:700; }
  .badge { display:inline-block; padding:3px 10px; border-radius:4px; font-size:11px; font-weight:700; text-transform:uppercase; }
  .badge-high { background:#d4edda; color:#155724; } .badge-medium { background:#fff3cd; color:#856404; }
  .badge-low { background:#ffeeba; color:#856404; } .badge-insufficient { background:#f8d7da; color:#721c24; }
  .badge-neutral { background:#e2e3e5; color:#383d41; }
  .report-body { padding:24px; }
  .report-section { margin-bottom:20px; }
  .report-section h3 { font-size:13px; font-weight:600; color:#888; text-transform:uppercase; letter-spacing:.5px; margin-bottom:8px; }
  .report-row { display:flex; padding:4px 0; font-size:14px; }
  .report-label { width:140px; color:#888; flex-shrink:0; }
  .report-value { color:#333; }
  .report-note { font-size:13px; color:#555; line-height:1.6; background:#f8f8fc; padding:12px; border-radius:6px; }
  .evidence-item { font-size:13px; padding:6px 0; border-bottom:1px solid #f5f5f5; }
  .evidence-item:last-child { border-bottom:none; }
  .evidence-step { font-weight:600; }
  .evidence-link { color:#4a90d9; text-decoration:none; }
  .report-timing { margin-top:20px; padding-top:16px; border-top:1px solid #eee; font-size:12px; color:#888; line-height:1.7; }
  .progress-log { margin-top:8px; background:#fff; border-radius:12px; border:1px solid #e0e0e0; overflow:hidden; max-width:900px; }
  .progress-log-header { padding:14px 20px; font-size:14px; font-weight:600; border-bottom:1px solid #e0e0e0; background:#f8f8fc; }
  .progress-log-body { padding:0; max-height:640px; overflow-y:auto; }
  .log-entry { display:flex; gap:10px; padding:6px 20px; border-bottom:1px solid #f5f5f5; font-size:12px; font-family:'SF Mono','Fira Code',monospace; line-height:1.6; }
  .log-time { color:#888; width:50px; flex-shrink:0; text-align:right; }
  .log-phase { width:90px; flex-shrink:0; font-weight:600; text-transform:uppercase; font-size:10px; padding-top:2px; }
  .log-phase-start{color:#4a90d9;} .log-phase-phase{color:#1a1a2e;} .log-phase-fetch{color:#8b5cf6;}
  .log-phase-extract{color:#d97706;} .log-phase-llm{color:#d97706;} .log-phase-registry{color:#059669;}
  .log-phase-google{color:#be185d;} .log-phase-ch{color:#059669;} .log-phase-sec{color:#0369a1;}
  .log-phase-edgar{color:#6d28d9;} .log-phase-delaware{color:#b45309;} .log-phase-bizapedia{color:#0891b2;}
  .log-phase-northdata{color:#be185d;} .log-phase-crossref{color:#0d9488;} .log-phase-validate{color:#7c3aed;}
  .log-phase-brightdata{color:#e67e22;} .log-phase-done{color:#27ae60;} .log-phase-warning{color:#dc2626;}
  .log-json { background:#1e1e2e; color:#a6e3a1; padding:10px 14px; border-radius:6px; font-size:11.5px; line-height:1.5; margin:6px 0 2px; overflow-x:auto; white-space:pre; }
  .log-msg { color:#333; flex:1; word-break:break-word; white-space:pre-wrap; }
  .log-phase-header { background:#f0f0f5; padding:8px 20px; font-size:13px; font-weight:700; color:#1a1a2e; border-bottom:1px solid #e0e0e0; border-top:1px solid #e0e0e0; letter-spacing:.3px; }
  .log-expandable { margin-top:4px; }
  .log-expandable summary { cursor:pointer; font-size:11px; color:#4a90d9; font-weight:600; }
  .log-expandable pre { margin-top:4px; padding:10px 12px; background:#1a1a2e; color:#c9d1d9; border-radius:6px; font-size:11px; line-height:1.5; overflow-x:auto; max-height:400px; overflow-y:auto; white-space:pre-wrap; word-break:break-word; }
  .http-2xx{color:#16a34a;font-weight:700;} .http-3xx{color:#2563eb;font-weight:700;} .http-4xx{color:#dc2626;font-weight:700;}
  .http-5xx{color:#7c3aed;font-weight:700;} .http-0{color:#991b1b;font-weight:700;} .tag-browserbase{color:#d97706;font-weight:700;}
  .spin { display:inline-block; width:12px; height:12px; border:2px solid #ddd; border-top-color:#4a90d9; border-radius:50%; animation:sp .7s linear infinite; vertical-align:middle; }
  @keyframes sp { to { transform:rotate(360deg); } }
</style></head><body>
<div class="header"><h1>Entity Lookup</h1><p>Identify the contracting legal entity from a company website</p></div>
<div class="content">
  <form class="search-form" onsubmit="return go(event)">
    <input class="search-input" id="url" type="text" placeholder="https://www.example.com/" value="__URL__">
    <label style="display:flex;align-items:center;gap:6px;color:#666;font-size:13px;white-space:nowrap"><input type="checkbox" id="refresh"> Refresh</label>
    <button class="search-btn" type="submit">Look Up</button>
  </form>
  <div class="meta-line" id="meta"></div>
  <div id="report"></div>
  <div class="progress-log" id="logwrap" style="display:none">
    <div class="progress-log-header">Progress Log</div>
    <div class="progress-log-body" id="log"></div>
  </div>
  <div id="history" style="margin-top:26px"></div>
</div>
<script>
function esc(s){ return (s==null?'':String(s)).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
// port of colorizeLogMsg: JSON blocks -> pre.log-json; HTTP codes colorized; browserbase tag
function colorize(msg){
  msg = msg || '';
  // pretty-print fenced or bare JSON blocks
  msg = msg.replace(/```json\s*([\s\S]*?)```|(\{[\s\S]*\}|\[[\s\S]*\])/g, function(m, a, b){
    var raw = a || b; try { var d = JSON.parse(raw); return '<pre class="log-json">'+esc(JSON.stringify(d,null,2))+'</pre>'; } catch(e){ return esc(m); }
  });
  if (msg.indexOf('<pre class="log-json">') === -1) msg = esc(msg);
  msg = msg.replace(/HTTP (\d{3})/g, function(m,c){ c=+c; var cls = c>=500?'http-5xx':c>=400?'http-4xx':c>=300?'http-3xx':c>=200?'http-2xx':'http-0'; return '<span class="'+cls+'">HTTP '+c+'</span>'; });
  msg = msg.replace(/\b(Bright Data|Browserbase|Web Unlocker|Wayback)\b/g, '<span class="tag-browserbase">$1</span>');
  return msg;
}
function renderExpandable(detail){
  if(!detail || !detail.expandable || !detail.sections) return '';
  var h = '<div class="log-expandable">';
  detail.sections.forEach(function(s){
    var label = esc(s.label||'Details'); var raw = s.content||''; var content;
    try { content = esc(JSON.stringify(JSON.parse(raw), null, 2)); } catch(e){ content = esc(raw); }
    h += '<details><summary>'+label+'</summary><pre class="log-json">'+content+'</pre></details>';
  });
  return h + '</div>';
}
function renderEntry(e){
  if(e.phase === 'phase') return '<div class="log-phase-header">'+esc(e.message)+'</div>';
  var t = (Math.round((e.time||0)*10)/10).toFixed(1);
  var ph = esc(e.phase);
  return '<div class="log-entry"><span class="log-time">'+t+'s</span>'
    + '<span class="log-phase log-phase-'+ph+'">'+ph+'</span>'
    + '<span class="log-msg">'+colorize(e.message)+renderExpandable(e.detail)+'</span></div>';
}
// number_format(n) with thousands separators (PHP number_format($n))
function nf(n){ n = Number(n||0); return n.toLocaleString('en-US'); }
function r1(n){ return (Math.round((Number(n)||0)*10)/10).toFixed(1); }
function money(n){ return '$'+(Number(n)||0).toFixed(2); }
// registry_validation status -> {cls,label}. Faithful to the PHP match() maps.
function rvInfo(st){
  var cls = st==='verified'?'badge-high':st==='name_match_bad_status'?'badge-low':'badge-insufficient';
  var lab = {verified:'Registry Verified',name_match_bad_status:'Inactive in Registry',name_mismatch:'Registry Mismatch',
             fictitious_name:'Fictitious Name',branch_registration:'Branch Registration'}[st]||'Not Found in Registry';
  return {cls:cls,label:lab};
}
// One report-card (used for the main report and the dimmed original-analysis card).
function renderReport(rep, meta, url){
  rep = rep||{}; meta = meta||{};
  var ent = rep.recommended_entity; var name = (ent && ent.legal_entity_name) || 'No match found';
  var conf = rep.confidence || 'insufficient';
  var cost = meta.cost_usd || 0;

  // ── header meta line ─────────────────────────────────────────────
  var metaHtml = '';
  if(ent){
    metaHtml += '<span>'+esc(ent.jurisdiction_description||ent.jurisdiction||'')+'</span>';
    if(ent.registry_id) metaHtml += '<span>'+esc(ent.registry_id)+'</span>';
    var rv = rep.registry_validation;
    if(rv){ var i=rvInfo(rv.status||''); var ttl=esc(rv.message||'');
      metaHtml += rv.validation_url
        ? '<a href="'+esc(rv.validation_url)+'" target="_blank" class="badge '+i.cls+'" title="'+ttl+'" style="text-decoration:none;">'+esc(i.label)+'</a>'
        : '<span class="badge '+i.cls+'" title="'+ttl+'">'+esc(i.label)+'</span>'; }
    if(rep.validation_warning) metaHtml += '<span class="badge badge-insufficient" title="'+esc(rep.validation_warning)+'">⚠ Validation Failed</span>';
  }
  metaHtml += '<span class="cost-badge">'+money(cost)+'</span>';
  metaHtml += '<span>'+r1(meta.total_time_s)+'s</span>';
  metaHtml += '<span>'+nf(meta.input_tokens)+' in / '+nf(meta.output_tokens)+' out tokens</span>';
  // API usage — total (visible) with a full per-source breakdown on hover; the detailed
  // per-source list is also spelled out in the timing footer at the bottom of the card.
  var SVC_LABELS={sec:'SEC/EDGAR',nzco:'NZ Companies Office',acra:'Singapore (ACRA)',companies_house:'Companies House',northdata:'NorthData',delaware:'Delaware',bizapedia:'Bizapedia',opencorporates:'OpenCorporates',google:'Google',linkedin:'LinkedIn',yahoo:'Yahoo Finance',brightdata:'Bright Data',browserbase:'Browserbase',scraping_browser:'Scraping Browser',http:'HTTP',whois:'WHOIS',wayback:'Wayback',claude:'Claude',openai:'OpenAI'};
  function svcLabel(k){return SVC_LABELS[k]||k;}
  var usage0 = meta.usage||null; var ac0 = meta.api_calls||{};
  if(usage0){
    var kv=function(o){return Object.keys(o||{}).map(function(k){return svcLabel(k)+' '+o[k];}).join(' · ');};
    var cch0 = (usage0.cached&&Object.keys(usage0.cached).length)?('\ncached: '+kv(usage0.cached)):'';
    var tip0 = 'Sources: '+(kv(usage0.sources)||'—')+'\nTransport: '+(kv(usage0.transport)||'—')
             + (Object.keys(usage0.llm||{}).length?('\nLLM: '+kv(usage0.llm)):'') + cch0;
    if(usage0.total) metaHtml += '<span class="cost-badge" title="'+esc(tip0)+'">'+usage0.total+' tool call'+(usage0.total===1?'':'s')+'</span>';
  } else {
    var acParts0=[]; var acTotal0=0;
    for(var svc0 in ac0){ if(ac0[svc0]>0){ acParts0.push(svcLabel(svc0)+' '+ac0[svc0]); acTotal0+=ac0[svc0]; } }
    if(acTotal0) metaHtml += '<span class="cost-badge" title="'+esc(acParts0.join(' · '))+'">'+acTotal0+' API call'+(acTotal0===1?'':'s')+'</span>';
  }
  if(meta.model) metaHtml += '<span>'+esc(meta.model)+'</span>';
  metaHtml += '<a href="api/lookup?url='+encodeURIComponent(url||'')+'" target="_blank" class="evidence-link">View API</a>';

  // ── body sections ────────────────────────────────────────────────
  var body = '';
  if(rep.note) body += '<div class="report-section"><div class="report-note">'+esc(rep.note)+'</div></div>';
  if(ent){
    body += '<div class="report-section"><h3>Entity Details</h3>'
      + row('Name', esc(ent.legal_entity_name))
      + row('Jurisdiction', esc(ent.jurisdiction_description||ent.jurisdiction||'—'))
      + row('Registry ID', esc(ent.registry_id||'—')+(ent.jurisdiction_state?' ('+esc(ent.jurisdiction_state)+')':''))
      + row('Address', esc(ent.address||'—'))
      + row('Source', '<a href="'+esc(ent.source_url||'#')+'" target="_blank" class="evidence-link">'+esc(ent.source||'—')+'</a>')
      + '</div>';
  }
  // Forward Evidence
  var fe = rep.evidence_forward||[];
  if(fe.length){ var s='<div class="report-section"><h3>Forward Evidence ('+fe.length+')</h3>';
    fe.forEach(function(ev){ s += '<div class="evidence-item"><span class="evidence-step">'+esc(ev.step||'')+'</span>'
      + '<span> — '+esc(ev.description||'')+'</span>'
      + (ev.source_url?' <a href="'+esc(ev.source_url)+'" target="_blank" class="evidence-link">[src]</a>':'')+'</div>'; });
    body += s+'</div>'; }
  // Reverse Validation
  var re = rep.evidence_reverse||[];
  if(re.length){ var s='<div class="report-section"><h3>Reverse Validation ('+re.length+')</h3>';
    re.forEach(function(ev){ var str=ev.strength||'none'; s += '<div class="evidence-item"><span class="evidence-step">'+esc(ev.step||'')+'</span>'
      + ' <span class="badge badge-'+esc(str)+'">'+esc(ev.strength||'—')+'</span>'
      + '<span> — '+esc(ev.description||'')+'</span></div>'; });
    body += s+'</div>'; }
  // Key People
  var kp = rep.key_people||[];
  if(kp.length){ var s='<div class="report-section"><h3>Key People ('+kp.length+')</h3>';
    kp.forEach(function(p){ s += '<div class="evidence-item">'+esc(p.name||'')+' — '+esc(p.role||'')+'</div>'; });
    body += s+'</div>'; }
  // Contractable Affiliates
  var ca = rep.contractable_affiliates||[];
  if(ca.length){ var s='<div class="report-section"><h3>Contractable Affiliates ('+ca.length+')</h3>';
    ca.forEach(function(a){
      var line = '<div class="evidence-item"><strong>'+esc(a.legal_entity_name||'')+'</strong>';
      if(a.registry_validated){
        line += a.validation_url
          ? ' <a href="'+esc(a.validation_url)+'" target="_blank" class="badge badge-high" style="text-decoration:none;">Registry Verified</a>'
          : ' <span class="badge badge-high">Registry Verified</span>';
      } else {
        var fl = {inactive:'Inactive in Registry',
                  name_mismatch:'Registry Name Mismatch'+(a.registry_name?' ("'+esc(a.registry_name)+'")':''),
                  not_found:'Not Found in Registry',no_registry_id:'No Registry ID'}[a.validation_status||'']||'Validation Failed';
        line += a.validation_url
          ? ' <a href="'+esc(a.validation_url)+'" target="_blank" class="badge badge-insufficient" style="text-decoration:none;">'+fl+'</a>'
          : ' <span class="badge badge-insufficient">'+fl+'</span>';
      }
      if(a.jurisdiction_country) line += ' <span class="badge badge-neutral">'+esc(a.jurisdiction_country)+(a.jurisdiction_state?'/'+esc(a.jurisdiction_state):'')+'</span>';
      if(a.registry_id) line += ' <span style="color:#666;"> — #'+esc(a.registry_id)+'</span>';
      if(a.validation_source) line += ' <span style="color:#666;font-size:0.85em;"> ('+esc(a.validation_source)+')</span>';
      if(a.role) line += '<div style="color:#888;margin-left:1em;font-size:0.9em;">'+esc(a.role)+'</div>';
      s += line+'</div>';
    });
    body += s+'</div>'; }
  // Other Entities Considered
  var oe = rep.other_entities||[];
  if(oe.length){ var s='<div class="report-section"><h3>Other Entities Considered ('+oe.length+')</h3>';
    oe.forEach(function(o){
      var line='<div class="evidence-item"><strong>'+esc(o.legal_entity_name||'')+'</strong>';
      if(o.jurisdiction_country) line += ' <span class="badge badge-neutral">'+esc(o.jurisdiction_country)+(o.jurisdiction_state?'/'+esc(o.jurisdiction_state):'')+'</span>';
      if(o.registry_id) line += ' <span style="color:#666;"> — #'+esc(o.registry_id)+'</span>';
      if(o.why_not_recommended) line += '<div style="color:#888;margin-left:1em;font-size:0.9em;">'+esc(o.why_not_recommended)+'</div>';
      if(o.verify_url) line += ' <a href="'+esc(o.verify_url)+'" target="_blank" class="evidence-link" style="margin-left:1em;">[verify]</a>';
      s += line+'</div>';
    });
    body += s+'</div>'; }
  // Tool usage — always-visible, grouped breakdown (the header pill only shows the total on hover).
  if(usage0){
    var kvg=function(o){ o=o||{}; var ks=Object.keys(o); return ks.length?ks.map(function(k){return svcLabel(k)+' '+o[k];}).join(' · '):null; };
    var us='<div class="report-section"><h3>Tool usage — '+usage0.total+' call'+(usage0.total===1?'':'s')+'</h3>';
    var srcs=kvg(usage0.sources), trans=kvg(usage0.transport), llms=kvg(usage0.llm), cch=kvg(usage0.cached);
    if(srcs)  us += row('Sources', esc(srcs));
    if(trans) us += row('Transport', esc(trans));
    if(llms)  us += row('LLM', esc(llms));
    if(cch)   us += row('Cached', esc(cch));
    body += us+'</div>';
  } else if(Object.keys(ac0).some(function(k){return ac0[k]>0;})){
    var flat=Object.keys(ac0).filter(function(k){return ac0[k]>0;}).map(function(k){return svcLabel(k)+' '+ac0[k];}).join(' · ');
    var tot=Object.keys(ac0).reduce(function(a,k){return a+(ac0[k]>0?ac0[k]:0);},0);
    body += '<div class="report-section"><h3>Tool usage — '+tot+' call'+(tot===1?'':'s')+'</h3>'+row('Calls', esc(flat))+'</div>';
  }
  // Timing / cost / api-calls footer
  var pt = meta.phase_times||{};
  var timing = 'Completed in '+r1(meta.total_time_s)+'s (fetch: '+r1(pt.fetch)+'s, extract: '+r1(pt.extraction)
    + 's, registries: '+r1(pt.registries)+'s, analysis: '+r1(pt.analysis)+'s'
    + (pt.reanalysis?', reanalysis: '+r1(pt.reanalysis)+'s':'')+')'
    + ' | Cost: '+money(cost)+' ('+nf(meta.input_tokens)+' input + '+nf(meta.output_tokens)+' output tokens)';
  var ac = meta.api_calls||{}; var parts=[];
  for(var svc in ac){ if(ac[svc]>0) parts.push(ac[svc]+' '+svcLabel(svc)); }
  if(parts.length) timing += ' | API calls: '+esc(parts.join(', '));
  body += '<div class="report-timing">'+timing+'</div>';

  var main = '<div class="report-card conf-'+esc(conf)+'"><div class="report-header">'
    + '<div class="report-entity">'+esc(name)+' <span class="badge badge-'+esc(conf)+'">'+esc(conf)+'</span></div>'
    + '<div class="report-meta">'+metaHtml+'</div></div>'
    + '<div class="report-body">'+body+'</div></div>';

  // ── Original Analysis card (before re-analysis) ──────────────────
  if(rep.original_report){
    var orep=rep.original_report; var oent=orep.recommended_entity; var oconf=orep.confidence||'insufficient';
    var oname=(oent&&oent.legal_entity_name)||'No match found';
    var ometa='';
    if(oent){
      ometa += '<span>'+esc(oent.jurisdiction_description||'')+'</span>';
      if(oent.registry_id) ometa += '<span>'+esc(oent.registry_id)+'</span>';
      var orv=orep.registry_validation;
      if(orv){ var oi=rvInfo(orv.status||''); ometa += '<span class="badge '+oi.cls+'" title="'+esc(orv.message||'')+'">'+esc(oi.label)+'</span>'; }
    }
    var obody='';
    if(orep.note) obody += '<div class="report-section"><div class="report-note">'+esc(orep.note)+'</div></div>';
    if(oent){ obody += '<div class="report-section"><h3>Entity Details</h3>'
      + row('Name', esc(oent.legal_entity_name))
      + row('Registry ID', esc(oent.registry_id||'—')+(oent.jurisdiction_state?' ('+esc(oent.jurisdiction_state)+')':''))
      + row('Source', esc(oent.source||'—'))
      + '</div>'; }
    main += '<div class="report-card conf-'+esc(oconf)+'" style="opacity:0.7;margin-top:16px;"><div class="report-header">'
      + '<div class="report-entity"><span style="font-size:11px;text-transform:uppercase;color:#999;letter-spacing:1px;">Original Analysis (before re-analysis)</span><br>'
      + esc(oname)+' <span class="badge badge-'+esc(oconf)+'">'+esc(oconf)+'</span></div>'
      + '<div class="report-meta">'+ometa+'</div></div>'
      + '<div class="report-body">'+obody+'</div></div>';
  }
  return main;
}
function row(label, valueHtml){ return '<div class="report-row"><span class="report-label">'+label+'</span><span class="report-value">'+valueHtml+'</span></div>'; }
function go(e){
  if(e) e.preventDefault();
  var u=document.getElementById('url').value.trim(); if(!u) return false;
  history.replaceState(null,'','live?url='+encodeURIComponent(u));
  document.getElementById('log').innerHTML=''; document.getElementById('report').innerHTML='';
  document.getElementById('logwrap').style.display='block';
  document.getElementById('meta').innerHTML='<span class="spin"></span> researching…';
  var rf=(document.getElementById('refresh')&&document.getElementById('refresh').checked)?'&refresh=1':'';
  var es=new EventSource('lookup/stream?url='+encodeURIComponent(u)+rf);
  es.addEventListener('log', function(ev){ var e=JSON.parse(ev.data); var d=document.getElementById('log'); d.insertAdjacentHTML('beforeend', renderEntry(e)); d.scrollTop=d.scrollHeight; });
  es.addEventListener('result', function(ev){ var r=JSON.parse(ev.data); document.getElementById('report').innerHTML=renderReport(r.report||{}, r.meta||{}, u);
    var m=r.meta||{}; document.getElementById('meta').innerHTML='Done · '+(m.total_time_s||'?')+'s · $'+(m.cost_usd||'?')+' · '+(m.input_tokens||0)+'/'+(m.output_tokens||0)+' tokens · '+(m.model||''); es.close(); loadHistory(); });
  es.addEventListener('error', function(ev){ try{ document.getElementById('meta').innerHTML='<span style="color:#dc2626">Error: '+esc(JSON.parse(ev.data))+'</span>'; }catch(_){ document.getElementById('meta').innerHTML='<span style="color:#dc2626">Stream error</span>'; } es.close(); });
  es.addEventListener('done', function(){ es.close(); });
  return false;
}
var CONF2={high:['#d4edda','#155724'],medium:['#fff3cd','#856404'],low:['#ffeeba','#856404'],insufficient:['#f8d7da','#721c24']};
function fmtWhen(iso){ if(!iso) return ''; try{ return new Date(iso).toLocaleString(); }catch(e){ return iso; } }
function openLookup(url){ document.getElementById('url').value=url; var rf=document.getElementById('refresh'); if(rf) rf.checked=false; window.scrollTo(0,0); go(null); }
function loadHistory(){
  fetch('history').then(function(r){return r.json();}).then(function(d){
    var rows=(d&&d.lookups)||[]; var h=document.getElementById('history');
    if(!rows.length){ h.innerHTML=''; return; }
    var out='<div style="font-size:13px;font-weight:600;color:#888;text-transform:uppercase;letter-spacing:.5px;margin-bottom:10px">Recent lookups ('+rows.length+')</div>';
    out+='<div style="border:1px solid #e0e0e0;border-radius:10px;overflow:hidden;background:#fff;max-width:960px"><table style="width:100%;border-collapse:collapse;font-size:13px"><thead><tr style="background:#f8f8fc;color:#888;text-align:left"><th style="padding:8px 14px;font-weight:600">Website</th><th style="padding:8px 14px;font-weight:600">Entity</th><th style="padding:8px 14px;font-weight:600">Conf</th><th style="padding:8px 14px;font-weight:600;text-align:right">Cost</th><th style="padding:8px 14px;font-weight:600;white-space:nowrap">When</th></tr></thead><tbody>';
    rows.forEach(function(x){
      var cb=CONF2[x.confidence]||CONF2.insufficient;
      var badge=x.confidence?'<span style="display:inline-block;padding:1px 8px;border-radius:4px;font-size:10px;font-weight:700;text-transform:uppercase;background:'+cb[0]+';color:'+cb[1]+'">'+esc(x.confidence)+'</span>':'';
      var arg=JSON.stringify(x.url).replace(/"/g,'&quot;');
      out+='<tr style="border-top:1px solid #f0f0f0;cursor:pointer" onmouseover="this.style.background=\'#f6f9ff\'" onmouseout="this.style.background=\'\'" onclick="openLookup('+arg+')">'
        +'<td style="padding:8px 14px;color:#4a90d9">'+esc(x.domain||x.url)+'</td>'
        +'<td style="padding:8px 14px">'+esc(x.entity_name||'—')+(x.jurisdiction?' <span style="color:#aaa">· '+esc(x.jurisdiction)+'</span>':'')+'</td>'
        +'<td style="padding:8px 14px">'+badge+'</td>'
        +'<td style="padding:8px 14px;text-align:right;color:#888">'+(x.cost_usd!=null?'$'+x.cost_usd:'')+'</td>'
        +'<td style="padding:8px 14px;color:#888;white-space:nowrap">'+esc(fmtWhen(x.created_at))+'</td></tr>';
    });
    out+='</tbody></table></div>';
    h.innerHTML=out;
  }).catch(function(){});
}
loadHistory();
(function(){ var p=new URLSearchParams(location.search); var u=p.get('url'); if(u){ document.getElementById('url').value=u; go(null); } })();
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def home():
    return PAGE.replace("__URL__", "")


@app.get("/live", response_class=HTMLResponse)
def live(url: str = ""):
    return PAGE.replace("__URL__", url.replace('"', "&quot;"))
