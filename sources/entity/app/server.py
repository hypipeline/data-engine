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

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

import re
from urllib.parse import urlparse

from config import load_config
from agent import EntityLookup
from tools import LookupTools
import cache

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
        return JSONResponse({"query": q, "state": state, "results": [], "error": None})
    try:
        results = _tools().search_bizapedia(q)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"query": q, "state": state, "results": [], "error": str(e)})
    st = (state or "").strip().upper()
    if st:
        results = [r for r in results
                   if (r.get('FilingJurisdictionPostalAbbreviation') or '').upper() == st
                   or (r.get('DomesticJurisdictionPostalAbbreviation') or '').upper() == st]
    return JSONResponse({"query": q, "state": st, "results": results,
                         "error": None if results else f'No companies found for "{q}".'})


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

    if not registry_name:
        return JSONResponse({"result": False, "status": "not_found",
                             "message": f'Registry ID "{registry_id}" not found in ' + (source or 'registry'),
                             "source": source, "raw": raw_data})

    norm_llm = re.sub(r'[^A-Z0-9 ]', '', entity_name.upper())
    norm_reg = re.sub(r'[^A-Z0-9 ]', '', (registry_name or '').upper())
    name_match = (not entity_name) or norm_llm == norm_reg
    status_ok = (registry_status or '').lower() in ('active', 'unknown')
    reg_id_ok = (raw_data or {}).get('registry_id_match') is not False

    # link back out to the actual public register (shown on the validation result page)
    registry_url = None
    if source == 'Companies House' and registry_id:
        registry_url = f"https://find-and-update.company-information.service.gov.uk/company/{registry_id}"
    elif source == 'NorthData':
        registry_url = (raw_data or {}).get('url')
    elif source == 'Bizapedia':
        registry_url = (raw_data or {}).get('BizapediaUrl') or (raw_data or {}).get('Url')

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
  // Timing / cost / api-calls footer
  var pt = meta.phase_times||{};
  var timing = 'Completed in '+r1(meta.total_time_s)+'s (fetch: '+r1(pt.fetch)+'s, extract: '+r1(pt.extraction)
    + 's, registries: '+r1(pt.registries)+'s, analysis: '+r1(pt.analysis)+'s'
    + (pt.reanalysis?', reanalysis: '+r1(pt.reanalysis)+'s':'')+')'
    + ' | Cost: '+money(cost)+' ('+nf(meta.input_tokens)+' input + '+nf(meta.output_tokens)+' output tokens)';
  var ac = meta.api_calls||{}; var parts=[];
  for(var svc in ac){ if(ac[svc]>0) parts.push(ac[svc]+' '+svc); }
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
