"""
Entity Lookup Server

FastAPI app that:
1. Takes a URL via web form or POST /lookup
2. Runs the pipeline (free HTTP scraping)
3. Sends pre-gathered data to Claude API for reasoning
4. Returns rendered HTML report with View Input / View Output panels

Usage:
    python server.py
    # then open http://localhost:8000
"""

import asyncio
import json
import os
import time
from datetime import date
from urllib.parse import urlparse

import anthropic
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
import uvicorn

from pipeline import lookup_entity

app = FastAPI(title="Entity Lookup")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

SYSTEM_PROMPT = """You are an entity lookup agent. Given a URL and pre-gathered research data, produce a structured JSON report identifying the best legal entity for contracting.

RULES:
- Every claim needs a clickable source link. No link = no claim.
- Return the exact legal entity name as it appears in the authoritative source.
- Prefer the TopCo / ultimate parent where ownership link is verifiable.
- Reverse validation is mandatory: name matching, address matching, people matching.
- Forward evidence alone = LOW confidence maximum.
- If not highly confident, return null rather than guessing.
- Name changes must cite specific source, exact names, date, and flag discrepancies.
- North Data website/email fields are LOW reliability — must be independently verified.
- Registry IDs, addresses, directors, ownership from North Data = HIGH reliability.

CONFIDENCE LEVELS:
- high: Entity confirmed in official register, bidirectional evidence (2+ strong reverse signals), active status
- medium: Entity confirmed but caveats exist (complex structure, one-directional evidence only)
- low: Entity identified but significant uncertainty
- insufficient: Cannot recommend — abstain. No registry match, or only company-controlled sources.

OUTPUT: Return ONLY valid JSON (no markdown fences, no explanation). Schema:
{
  "input_url": "string",
  "date": "YYYY-MM-DD",
  "report_id": "string",
  "recommended_entity": {
    "legal_entity_name": "string",
    "source": "string",
    "source_url": "string",
    "jurisdiction": "string",
    "registry_id": "string|null",
    "regulatory_status": "string|null",
    "address": "string|null",
    "relationship_to_website_entity": "string|null",
    "former_names": [{"name":"string","from":"string","to":"string","source":"string","source_url":"string"}]
  } | null,
  "website_entity": { same shape as recommended_entity } | null,
  "confidence": "high|medium|low|insufficient",
  "note": "Plain English summary — lead with the answer, then caveats. Every factual claim must reference a source from the data.",
  "evidence_forward": [
    {"step":"string","description":"string","source":"string","source_url":"string|null","quality":"verified|inferred|unavailable"}
  ],
  "evidence_reverse": [
    {"step":"Name Match|Address Match|People Match|Domain in Filings","description":"string","source":"string","source_url":"string|null","quality":"string","strength":"strong|moderate|weak|none"}
  ],
  "substance_score": 0-100,
  "substance_band": "high|medium|low|insufficient",
  "substance_factors": [
    {"description":"string","result":"pass|fail|unknown","source_url":"string|null"}
  ],
  "corporate_structure": {
    "legal_entity_name":"string","jurisdiction":"string|null","source_url":"string|null",
    "is_recommended":true,"role":"string|null",
    "children":[{ same shape, recursive }]
  } | null,
  "key_people": [{"name":"string","role":"string","source_url":"string|null"}],
  "other_entities": [
    {"legal_entity_name":"string","jurisdiction":"string|null","registry_id":"string|null","why_not_recommended":"string","verify_url":"string|null"}
  ],
  "sources_used": [{"name":"string","url":"string|null"}]
}"""


def _domain(url: str) -> str:
    return urlparse(url).netloc.replace("www.", "")


async def do_lookup(url: str) -> dict:
    """Run the full lookup: pipeline + Claude API reasoning."""
    # Step 1: Pipeline. Fast HTTP scrapers (SEC EDGAR, Companies House, North Data) run by
    # default. The Browserbase-backed registry scrapers (DE DOS, Ontario, OpenCorporates)
    # are slow/flaky, so they're opt-in via ENTITY_USE_BROWSER=1.
    use_browser = os.environ.get("ENTITY_USE_BROWSER", "").lower() in ("1", "true", "yes")
    t0 = time.time()
    pipeline_result = await lookup_entity(
        url,
        browserbase_api_key=(os.environ.get("BROWSERBASE_API_KEY") or None) if use_browser else None,
        browserbase_project_id=(os.environ.get("BROWSERBASE_PROJECT_ID") or None) if use_browser else None,
        companies_house_api_key=os.environ.get("COMPANIES_HOUSE_API_KEY") or None,
    )
    pipeline_time = time.time() - t0

    pipeline_data = json.dumps(
        pipeline_result.__dict__ if hasattr(pipeline_result, "__dict__") else pipeline_result,
        indent=2, default=str,
    )

    user_message = f"""Analyze this URL and produce the entity lookup JSON report.

INPUT URL: {url}

PRE-GATHERED DATA (from automated scrapers — WHOIS, website extraction, SEC EDGAR, North Data, Companies House):
{pipeline_data}

Based on this data, produce the structured JSON report. Use source URLs from the pipeline data where available. If data is insufficient for a confident recommendation, set confidence to "insufficient" and explain what's missing in the note."""

    # Step 2: Claude API (run the sync SDK call off the event loop so progress can stream)
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    print("[reasoning] sending gathered data to Claude...")
    t1 = time.time()
    response = await asyncio.to_thread(
        client.messages.create,
        model="claude-sonnet-4-6",
        max_tokens=8192,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    api_time = time.time() - t1

    usage = response.usage
    input_tokens = usage.input_tokens
    output_tokens = usage.output_tokens
    sonnet_cost = (input_tokens * 3.0 / 1_000_000) + (output_tokens * 15.0 / 1_000_000)
    haiku_cost = (input_tokens * 0.80 / 1_000_000) + (output_tokens * 4.0 / 1_000_000)

    response_text = response.content[0].text.strip()
    # Strip markdown fences if present
    if response_text.startswith("```"):
        response_text = response_text.split("\n", 1)[1]
        if response_text.endswith("```"):
            response_text = response_text[:-3]
        response_text = response_text.strip()

    try:
        report_json = json.loads(response_text)
    except json.JSONDecodeError:
        report_json = {
            "input_url": url,
            "date": str(date.today()),
            "report_id": "ERROR",
            "recommended_entity": None,
            "website_entity": None,
            "confidence": "insufficient",
            "note": f"Claude returned invalid JSON. Raw response: {response_text[:500]}",
            "evidence_forward": [],
            "evidence_reverse": [],
            "substance_score": 0,
            "substance_band": "insufficient",
            "substance_factors": [],
            "corporate_structure": None,
            "key_people": [],
            "other_entities": [],
            "sources_used": [],
        }

    # Build the input payload for display
    input_payload = {
        "system_prompt": SYSTEM_PROMPT,
        "user_message": user_message,
        "model": "claude-sonnet-4-6",
        "max_tokens": 8192,
    }

    # Save report
    domain = _domain(url)
    report_path = f"reports/{domain}.json"
    os.makedirs("reports", exist_ok=True)
    with open(report_path, "w") as f:
        json.dump(report_json, f, indent=2, ensure_ascii=False)

    return {
        "report": report_json,
        "input_payload": input_payload,
        "meta": {
            "pipeline_time_s": round(pipeline_time, 1),
            "api_time_s": round(api_time, 1),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "sonnet_cost_usd": round(sonnet_cost, 4),
            "haiku_cost_usd": round(haiku_cost, 4),
        },
    }


# Import the rendering helpers
from report_schema import _esc, _link, _badge, _quality, _strength
from viewer import _render_report_card


def render_page(result: dict | None = None, loading: bool = False, url: str = "") -> str:
    """Render the full page HTML."""

    report_html = ""
    header_meta = ""
    input_json = "{}"
    output_json = "{}"
    meta_bar = ""

    if result:
        report_html = _render_report_card(result["report"])
        r = result["report"]
        header_meta = f'Input: <a href="{_esc(r.get("input_url", ""))}" target="_blank">{_esc(r.get("input_url", ""))}</a> · {_esc(r.get("date", ""))}'
        input_json = json.dumps(result["input_payload"], indent=2, ensure_ascii=False)
        output_json = json.dumps(result["report"], indent=2, ensure_ascii=False)
        m = result["meta"]
        meta_bar = f'''
        <div class="meta-bar">
          <span>Pipeline: {m["pipeline_time_s"]}s</span>
          <span>API: {m["api_time_s"]}s</span>
          <span>Tokens: {m["input_tokens"]:,} in / {m["output_tokens"]:,} out</span>
          <span>Cost: ${m["sonnet_cost_usd"]:.4f} (Sonnet) / ${m["haiku_cost_usd"]:.4f} (Haiku)</span>
        </div>'''

    loading_cls = " loading" if loading else ""

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Entity Lookup</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif; background: #f0f0f0; color: #1a1a1a; line-height: 1.5; }}

  /* Search bar */
  .search-bar {{ background: #1a1a2e; padding: 16px 32px; display: flex; gap: 12px; align-items: center; position: sticky; top: 0; z-index: 100; }}
  .search-bar form {{ display: flex; gap: 12px; flex: 1; max-width: 820px; margin: 0 auto; }}
  .search-bar input {{ flex: 1; padding: 10px 16px; border: 1px solid #333; border-radius: 6px; background: #2a2a4e; color: #fff; font-size: 14px; outline: none; }}
  .search-bar input::placeholder {{ color: #666; }}
  .search-bar input:focus {{ border-color: #4a90d9; }}
  .search-bar button {{ padding: 10px 24px; background: #4a90d9; color: #fff; border: none; border-radius: 6px; font-size: 14px; font-weight: 600; cursor: pointer; white-space: nowrap; }}
  .search-bar button:hover {{ background: #3a7bc8; }}
  .search-bar button:disabled {{ background: #555; cursor: not-allowed; }}

  /* Meta bar */
  .meta-bar {{ max-width: 820px; margin: 0 auto; padding: 8px 32px; display: flex; gap: 24px; font-size: 11px; color: #888; background: #e8e8e8; border-bottom: 1px solid #ddd; }}

  /* Report */
  .report {{ max-width: 820px; margin: 0 auto; background: #fff; box-shadow: 0 1px 3px rgba(0,0,0,0.1); overflow: hidden; }}
  .report-header {{ background: #1a1a2e; color: #fff; padding: 20px 32px; display: flex; justify-content: space-between; align-items: center; }}
  .report-header h2 {{ font-size: 16px; font-weight: 600; letter-spacing: 0.5px; text-transform: uppercase; }}
  .report-header .meta {{ font-size: 12px; color: #8a8aaf; }}
  .report-header .meta a {{ color: #8a8aaf; }}
  .panel-toggles {{ display: flex; gap: 8px; }}
  .panel-toggle {{ background: transparent; border: 1px solid #555; color: #8a8aaf; font-size: 11px; padding: 4px 10px; border-radius: 3px; cursor: pointer; }}
  .panel-toggle:hover {{ border-color: #aaa; color: #fff; }}
  .panel-toggle.active {{ border-color: #4a90d9; color: #4a90d9; }}

  /* Sections (from report_schema) */
  .section {{ padding: 24px 32px; border-bottom: 1px solid #eee; }}
  .section:last-child {{ border-bottom: none; }}
  .section-title {{ font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 1px; color: #888; margin-bottom: 16px; }}
  .recommendation {{ background: #fafbfc; }}
  .rec-grid {{ display: grid; grid-template-columns: 100px 1fr; gap: 6px 16px; font-size: 14px; }}
  .rec-label {{ color: #666; font-weight: 500; }}
  .rec-value {{ font-weight: 600; }}
  .rec-value a {{ color: #1a1a2e; text-decoration: underline; text-decoration-color: #ccc; text-underline-offset: 2px; }}
  .badge {{ display: inline-block; padding: 2px 10px; border-radius: 3px; font-size: 11px; font-weight: 700; letter-spacing: 0.5px; }}
  .badge-high {{ background: #e6f4ea; color: #1e7e34; }}
  .badge-medium {{ background: #fff3cd; color: #856404; }}
  .badge-low {{ background: #fde8e8; color: #c53030; }}
  .badge-insufficient {{ background: #eee; color: #666; }}
  .quality {{ display: inline-block; padding: 1px 6px; border-radius: 2px; font-size: 10px; font-weight: 600; letter-spacing: 0.3px; margin-left: 4px; }}
  .q-verified {{ background: #e6f4ea; color: #1e7e34; }}
  .q-inferred {{ background: #fff3cd; color: #856404; }}
  .q-unavailable {{ background: #fde8e8; color: #c53030; }}
  .note {{ margin-top: 16px; padding: 12px 16px; background: #f8f9fa; border-left: 3px solid #dee2e6; font-size: 13px; color: #555; line-height: 1.6; }}
  .note.note-warning {{ border-left-color: #c53030; background: #fef5f5; }}
  .evidence-step {{ margin-bottom: 20px; }}
  .evidence-step:last-child {{ margin-bottom: 0; }}
  .step-header {{ font-size: 13px; font-weight: 600; color: #333; margin-bottom: 4px; }}
  .step-num {{ display: inline-block; width: 20px; height: 20px; background: #1a1a2e; color: #fff; border-radius: 50%; text-align: center; line-height: 20px; font-size: 11px; font-weight: 700; margin-right: 6px; }}
  .step-body {{ font-size: 13px; color: #555; margin-left: 28px; line-height: 1.6; }}
  .step-link {{ display: block; margin-top: 4px; }}
  .step-link a {{ font-size: 12px; color: #1a56db; word-break: break-all; }}
  .tree {{ font-size: 13px; font-family: 'SF Mono', 'Fira Code', monospace; line-height: 1.8; }}
  .tree a {{ color: #1a56db; text-decoration: none; }}
  .tree .recommended {{ background: #e6f4ea; padding: 2px 6px; border-radius: 3px; font-weight: 600; }}
  .key-person {{ margin-top: 12px; font-size: 13px; color: #666; }}
  .factor {{ display: flex; align-items: flex-start; gap: 8px; padding: 6px 0; font-size: 13px; }}
  .factor-icon {{ flex-shrink: 0; width: 18px; text-align: center; font-size: 14px; }}
  .factor-pass .factor-icon {{ color: #1e7e34; }}
  .factor-fail .factor-icon {{ color: #c53030; }}
  .factor-unknown .factor-icon {{ color: #856404; }}
  .factor-text {{ flex: 1; }}
  .factor-link {{ flex-shrink: 0; }}
  .factor-link a {{ font-size: 11px; color: #1a56db; text-decoration: none; padding: 2px 6px; border: 1px solid #d0d7de; border-radius: 3px; }}
  .score-bar-container {{ margin-bottom: 16px; }}
  .score-bar-label {{ display: flex; justify-content: space-between; font-size: 13px; margin-bottom: 4px; }}
  .score-bar {{ height: 8px; background: #eee; border-radius: 4px; overflow: hidden; }}
  .score-bar-fill {{ height: 100%; border-radius: 4px; }}
  .score-high .score-bar-fill {{ background: #1e7e34; }}
  .score-medium .score-bar-fill {{ background: #d69e2e; }}
  .score-low .score-bar-fill {{ background: #c53030; }}
  .score-insufficient .score-bar-fill {{ background: #999; }}
  .rv-item {{ display: flex; align-items: flex-start; gap: 8px; padding: 8px 12px; margin-bottom: 6px; border-radius: 4px; border-left: 3px solid #c53030; background: #fef5f5; font-size: 13px; }}
  .rv-item.rv-pass {{ background: #f8faf8; border-left-color: #1e7e34; }}
  .rv-item.rv-weak {{ background: #fffdf5; border-left-color: #d69e2e; }}
  .rv-label {{ font-weight: 600; min-width: 110px; flex-shrink: 0; }}
  .rv-detail {{ flex: 1; color: #555; }}
  .rv-strength {{ flex-shrink: 0; font-size: 11px; font-weight: 600; }}
  .rv-strength.strong {{ color: #1e7e34; }}
  .rv-strength.moderate {{ color: #1a56db; }}
  .rv-strength.weak {{ color: #856404; }}
  .rv-strength.none {{ color: #c53030; }}
  .considered-table {{ width: 100%; font-size: 13px; border-collapse: collapse; }}
  .considered-table th {{ text-align: left; font-weight: 600; color: #666; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; padding: 8px 12px; border-bottom: 2px solid #eee; }}
  .considered-table td {{ padding: 10px 12px; border-bottom: 1px solid #f0f0f0; vertical-align: top; }}
  .considered-table a {{ color: #1a56db; text-decoration: none; font-size: 12px; }}
  .sources {{ list-style: none; }}
  .sources li {{ padding: 3px 0; font-size: 13px; color: #666; }}
  .sources li::before {{ content: '•'; margin-right: 8px; color: #999; }}
  .sources li a {{ color: #1a56db; font-size: 12px; }}

  /* JSON panels */
  .json-overlay {{ position: fixed; top: 0; right: 0; bottom: 0; width: 50%; background: #1a1a2e; transform: translateX(100%); transition: transform 0.25s ease; z-index: 1000; display: flex; flex-direction: column; }}
  .json-overlay.visible {{ transform: translateX(0); }}
  .json-panel-header {{ display: flex; justify-content: space-between; align-items: center; padding: 16px 24px; border-bottom: 1px solid #333; color: #fff; font-size: 14px; font-weight: 600; }}
  .json-panel-header button {{ background: none; border: none; color: #888; font-size: 18px; cursor: pointer; padding: 4px 8px; }}
  .json-panel-header button:hover {{ color: #fff; }}
  .json-tabs {{ display: flex; border-bottom: 1px solid #333; }}
  .json-tab {{ padding: 8px 20px; color: #666; font-size: 12px; font-weight: 600; cursor: pointer; border-bottom: 2px solid transparent; }}
  .json-tab:hover {{ color: #aaa; }}
  .json-tab.active {{ color: #4a90d9; border-bottom-color: #4a90d9; }}
  .json-content {{ flex: 1; overflow: auto; padding: 24px; font-family: 'SF Mono', 'Fira Code', monospace; font-size: 12px; line-height: 1.6; color: #c9d1d9; white-space: pre-wrap; word-break: break-word; }}

  /* Loading */
  .loading-overlay {{ display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.3); z-index: 200; justify-content: center; align-items: center; }}
  .loading-overlay.visible {{ display: flex; }}
  .loading-box {{ background: #fff; padding: 32px 48px; border-radius: 8px; text-align: center; box-shadow: 0 4px 20px rgba(0,0,0,0.2); }}
  .loading-box .spinner {{ width: 32px; height: 32px; border: 3px solid #eee; border-top-color: #4a90d9; border-radius: 50%; animation: spin 0.8s linear infinite; margin: 0 auto 16px; }}
  @keyframes spin {{ to {{ transform: rotate(360deg); }} }}

  .empty-state {{ max-width: 820px; margin: 80px auto; text-align: center; color: #888; }}
  .empty-state h2 {{ font-size: 20px; font-weight: 600; color: #666; margin-bottom: 8px; }}
  .empty-state p {{ font-size: 14px; }}
</style>
</head>
<body>

<div class="search-bar">
  <form id="lookup-form" onsubmit="doLookup(event)">
    <input type="url" id="url-input" name="url" placeholder="Enter company website URL (e.g. https://www.siemens.com/)" value="{_esc(url)}" required />
    <button type="submit" id="submit-btn">Lookup</button>
  </form>
</div>

{meta_bar}

{f"""
<div class="report">
  <div class="report-header">
    <div>
      <h2>Entity Lookup Report</h2>
      <div class="meta">{header_meta}</div>
    </div>
    <div class="panel-toggles">
      <button class="panel-toggle" onclick="showPanel('input')">View Input</button>
      <button class="panel-toggle" onclick="showPanel('output')">View Output</button>
    </div>
  </div>
  {report_html}
</div>
""" if result else """
<div class="empty-state">
  <h2>Entity Lookup</h2>
  <p>Enter a company website URL above to identify the best legal entity for contracting.</p>
</div>
"""}

<div id="json-overlay" class="json-overlay">
  <div style="display:flex;flex-direction:column;height:100%;">
    <div class="json-panel-header">
      <span id="panel-title">JSON</span>
      <button onclick="hidePanel()">✕</button>
    </div>
    <div class="json-tabs">
      <div class="json-tab active" id="tab-input" onclick="switchTab('input')">Input (sent to Claude)</div>
      <div class="json-tab" id="tab-output" onclick="switchTab('output')">Output (Claude response)</div>
    </div>
    <div class="json-content" id="json-content"></div>
  </div>
</div>

<div id="loading-overlay" class="loading-overlay">
  <div class="loading-box">
    <div class="spinner"></div>
    <div>Running entity lookup...</div>
    <div style="font-size:12px;color:#888;margin-top:8px;">Pipeline + Claude API (~60s)</div>
  </div>
</div>

<script>
const inputJson = {json.dumps(input_json)};
const outputJson = {json.dumps(output_json)};
let currentTab = 'input';

function doLookup(e) {{
  e.preventDefault();
  const url = document.getElementById('url-input').value;
  if (!url) return;
  document.getElementById('submit-btn').disabled = true;
  document.getElementById('loading-overlay').classList.add('visible');
  window.location.href = '/lookup?url=' + encodeURIComponent(url);
}}

function showPanel(tab) {{
  currentTab = tab;
  document.getElementById('json-overlay').classList.add('visible');
  switchTab(tab);
}}

function hidePanel() {{
  document.getElementById('json-overlay').classList.remove('visible');
}}

function switchTab(tab) {{
  currentTab = tab;
  document.getElementById('tab-input').classList.toggle('active', tab === 'input');
  document.getElementById('tab-output').classList.toggle('active', tab === 'output');
  const content = tab === 'input' ? inputJson : outputJson;
  try {{
    document.getElementById('json-content').textContent = JSON.stringify(JSON.parse(content), null, 2);
  }} catch {{
    document.getElementById('json-content').textContent = content;
  }}
}}

document.addEventListener('keydown', (e) => {{
  if (e.key === 'Escape') hidePanel();
}});
</script>

</body>
</html>'''


@app.get("/", response_class=HTMLResponse)
async def home():
    return render_page()


@app.get("/lookup", response_class=HTMLResponse)
async def lookup_get(url: str):
    result = await do_lookup(url)
    return render_page(result=result, url=url)


@app.post("/api/lookup")
async def lookup_api(request: Request):
    body = await request.json()
    url = body.get("url", "")
    if not url:
        return JSONResponse({"error": "url is required"}, status_code=400)
    result = await do_lookup(url)
    return JSONResponse(result)


# --- Streaming ("chatty") lookup: live pipeline log via SSE, then the rendered report ---
@app.get("/lookup/stream")
async def lookup_stream(url: str):
    """Server-Sent Events: streams each pipeline progress line as it happens, then a
    'result' event with the rendered report HTML. do_lookup's print() output is captured
    and relayed live; the Claude call runs off-loop so logs keep flowing."""
    import contextlib

    queue: asyncio.Queue = asyncio.Queue()

    class _QueueWriter:
        def write(self, s):
            for line in s.splitlines():
                if line.strip():
                    queue.put_nowait(("log", line.rstrip()))
        def flush(self):
            pass

    async def run():
        try:
            with contextlib.redirect_stdout(_QueueWriter()):
                result = await do_lookup(url)
            queue.put_nowait(("result", result))
        except Exception as e:  # noqa: BLE001
            queue.put_nowait(("error", str(e)))
        finally:
            queue.put_nowait(("__done__", None))

    async def gen():
        task = asyncio.create_task(run())
        try:
            while True:
                kind, payload = await queue.get()
                if kind == "log":
                    yield f"event: log\ndata: {json.dumps(payload)}\n\n"
                elif kind == "result":
                    html = _render_report_card(payload["report"])
                    yield "event: result\ndata: " + json.dumps(
                        {"html": html, "meta": payload["meta"]}) + "\n\n"
                elif kind == "error":
                    yield f"event: error\ndata: {json.dumps(payload)}\n\n"
                else:  # __done__
                    yield "event: done\ndata: {}\n\n"
                    break
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/live", response_class=HTMLResponse)
async def live(url: str = ""):
    """A self-contained page: URL form + a live progress log that streams as the lookup
    runs, then renders the report inline. This is the 'chatty' interface."""
    from report_schema import _CSS
    safe_url = _esc(url)
    autostart = "true" if url else "false"
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Entity Lookup</title>{_CSS}
<style>
  body {{ margin:0; padding:20px; font-family:-apple-system,Segoe UI,Roboto,sans-serif; background:#0f1117; color:#e6e6e6; }}
  .row {{ display:flex; gap:8px; margin-bottom:14px; }}
  input[type=text] {{ flex:1; padding:10px 12px; border-radius:8px; border:1px solid #333; background:#1a1d27; color:#eee; font-size:14px; }}
  button {{ padding:10px 18px; border:0; border-radius:8px; background:#2e8b6f; color:#fff; font-weight:600; cursor:pointer; }}
  #log {{ background:#12151d; border:1px solid #232838; border-radius:8px; padding:12px 14px; font-family:ui-monospace,Menlo,monospace;
          font-size:12px; line-height:1.55; white-space:pre-wrap; max-height:260px; overflow-y:auto; color:#a6e3a1; }}
  #log .r {{ color:#f0c674; }}  #log .d {{ color:#8ab4f8; }}
  #report {{ margin-top:18px; background:#fff; color:#111; border-radius:10px; overflow:hidden; }}
  .muted {{ color:#8a8f9c; font-size:12px; }}
</style></head><body>
<form class="row" onsubmit="go(event)">
  <input id="url" type="text" placeholder="https://www.example.com/" value="{safe_url}">
  <button type="submit">Look up</button>
</form>
<div id="log" class="muted">Enter a company website URL and press Look up.</div>
<div id="meta" class="muted" style="margin-top:8px"></div>
<div id="report"></div>
<script>
function go(e){{ if(e) e.preventDefault(); var u=document.getElementById('url').value.trim(); if(!u) return;
  history.replaceState(null,'', '/live?url='+encodeURIComponent(u)); start(u); }}
function line(t){{ var l=document.getElementById('log'); var cls=''; if(t.indexOf('[reasoning]')>=0)cls='r'; else if(/^\\s*\\[\\d/.test(t))cls='d';
  l.classList.remove('muted'); l.innerHTML += '<span class="'+cls+'">'+t.replace(/</g,'&lt;')+'</span>\\n'; l.scrollTop=l.scrollHeight; }}
function start(u){{
  document.getElementById('log').innerHTML=''; document.getElementById('report').innerHTML=''; document.getElementById('meta').innerHTML='';
  line('Starting lookup for '+u+' ...');
  var es=new EventSource('/lookup/stream?url='+encodeURIComponent(u));
  es.addEventListener('log', function(ev){{ line(JSON.parse(ev.data)); }});
  es.addEventListener('result', function(ev){{ var d=JSON.parse(ev.data); document.getElementById('report').innerHTML=d.html;
    var m=d.meta||{{}}; document.getElementById('meta').innerHTML='pipeline '+(m.pipeline_time_s||'?')+'s · reasoning '+(m.api_time_s||'?')+'s · $'+(m.sonnet_cost_usd||'?');
    es.close(); }});
  es.addEventListener('error', function(ev){{ try{{line('ERROR: '+JSON.parse(ev.data));}}catch(e){{}} es.close(); }});
  es.addEventListener('done', function(){{ es.close(); }});
}}
if({autostart}) start("{safe_url}");
</script></body></html>"""


if __name__ == "__main__":
    if not ANTHROPIC_API_KEY:
        print("ERROR: Set ANTHROPIC_API_KEY environment variable")
        exit(1)
    print("Starting Entity Lookup server at http://localhost:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000)
