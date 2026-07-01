"""
Client for the entity-lookup service (Data Engine "entity" source) — the Python
FastAPI app in sources/entity/app/. Shared by the Data Engine API (api.py) and the
Streamlit dashboard (app.py).

The Python server is synchronous: `POST /api/lookup {url}` runs the pipeline (WHOIS +
website extraction + SEC EDGAR + Companies House + North Data + Browserbase registries)
then Claude reasons it into a report, and returns:
    { "report": {...}, "input_payload": {...}, "meta": {pipeline_time_s, api_time_s,
      input_tokens, output_tokens, sonnet_cost_usd, haiku_cost_usd} }
A lookup takes ~1-2 min. `GET /lookup?url=` returns the app's own rendered HTML report
page (used by the dashboard as an embedded native view).
"""
import os
from urllib.parse import quote
import httpx

# in-cluster base (api/web -> entity container)
ENTITY_BASE = os.environ.get("ENTITY_BASE_URL", "http://entity:8000").rstrip("/")
# browser-facing base for the embedded native UI (iframe); local debug port by default,
# in prod a Caddy-routed path on the same origin.
ENTITY_PUBLIC_BASE = os.environ.get("ENTITY_PUBLIC_BASE", "http://localhost:9090").rstrip("/")


def health() -> bool:
    """True if the entity server is reachable."""
    try:
        r = httpx.get(f"{ENTITY_BASE}/", timeout=8.0)
        return r.status_code == 200
    except Exception:
        return False


def lookup(url: str, timeout: float = 220.0):
    """
    Run a lookup (blocking ~1-2 min). Returns (payload, http_status):
      * (result_dict, 200) — {report, input_payload, meta}
      * ({"error": ...}, 400) — invalid/missing URL
    Raises httpx errors on transport/5xx failures.
    """
    r = httpx.post(f"{ENTITY_BASE}/api/lookup", json={"url": url}, timeout=timeout)
    if r.status_code == 400:
        try:
            return r.json(), 400
        except Exception:
            return {"error": "bad request"}, 400
    r.raise_for_status()
    return r.json(), 200


def ui_url(url: str, public: bool = True) -> str:
    """The entity app's own report page for a URL (for embedding as an iframe)."""
    base = ENTITY_PUBLIC_BASE if public else ENTITY_BASE
    return f"{base}/lookup?url={quote(url, safe='')}"
