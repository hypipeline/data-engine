"""
Client for the entity-lookup PHP sidecar (Data Engine "entity" source).

The sidecar's JSON API is asynchronous: `GET /index.php?format=json&url=...` either
returns the finished result (200) or, if the lookup is not yet cached, kicks off a
background job and returns 202 {"status":"processing"}. Callers must poll the same URL
until it returns {"status":"complete", ...}. This module wraps that kick-off + poll loop
into one call, shared by the FastAPI router (api.py) and the Streamlit view (app.py).

Complete payload shape (from lookup.php):
    {
      "status": "complete", "from_cache": bool, "cached_at": ...,
      "report": { recommended_entity, website_entity, confidence, evidence_chain, ... },
      "meta":   { total_time_s, model, cost_usd, input_tokens, output_tokens, api_calls, phase_times },
      "progress_log": [...],
      "embed_html": "<...prebuilt report HTML...>"
    }
"""
import os
import time
import httpx

ENTITY_BASE = os.environ.get("ENTITY_BASE_URL", "http://entity").rstrip("/")


def _get(url: str, refresh: bool = False, model: str | None = None, timeout: float = 35.0):
    params = {"format": "json", "url": url}
    if refresh:
        params["refresh"] = "1"
    if model:
        params["model"] = model
    return httpx.get(f"{ENTITY_BASE}/index.php", params=params, timeout=timeout)


def health() -> bool:
    """True if the sidecar is reachable and responding (400 for missing url still counts)."""
    try:
        r = httpx.get(f"{ENTITY_BASE}/index.php", params={"format": "json"}, timeout=8.0)
        return r.status_code in (200, 202, 400)
    except Exception:
        return False


def lookup(url: str, refresh: bool = False, model: str | None = None,
           max_wait: float = 180.0, poll_every: float = 4.0, on_poll=None):
    """
    Kick off (if needed) and poll the sidecar until the lookup completes or max_wait elapses.

    Returns (payload, http_status):
      * (result_dict, 200)  — complete
      * ({"status":"error",...}, 400) — invalid/missing URL
      * ({"status":"processing",...}, 202) — still running after max_wait; caller may retry
    `on_poll(elapsed_seconds)` is called on each poll iteration (for progress UIs).
    """
    start = time.monotonic()
    deadline = start + max_wait
    first = True
    last_202 = {"status": "processing"}
    while True:
        # only send refresh on the very first request, else every poll re-triggers the job
        r = _get(url, refresh=(refresh and first), model=model)
        if r.status_code == 200:
            return r.json(), 200
        if r.status_code == 400:
            try:
                return r.json(), 400
            except Exception:
                return {"status": "error", "error": "invalid url"}, 400
        # 202 processing
        first = False
        try:
            last_202 = r.json()
        except Exception:
            pass
        if on_poll:
            try:
                on_poll(time.monotonic() - start)
            except Exception:
                pass
        if time.monotonic() >= deadline:
            return last_202, 202
        time.sleep(poll_every)
