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

from config import load_config
from agent import EntityLookup

app = FastAPI(title="Entity Lookup v3b")
CONFIG = load_config()


def _run_lookup(url: str, q: "queue.Queue"):
    def progress(entry):
        q.put(("log", entry))
    try:
        agent = EntityLookup(CONFIG, progress_callback=progress)
        result = agent.run(url)
        q.put(("result", result))
    except Exception as e:  # noqa: BLE001
        import traceback
        q.put(("error", f"{e}\n{traceback.format_exc()}"))
    finally:
        q.put(("__done__", None))


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/lookup/stream")
async def lookup_stream(url: str):
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
    if not url:
        return JSONResponse({"error": "url is required"}, status_code=400)
    q: "queue.Queue" = queue.Queue()
    loop = asyncio.get_event_loop()
    result_holder = {}

    def run():
        agent = EntityLookup(CONFIG, progress_callback=None)
        result_holder['r'] = agent.run(url)

    await loop.run_in_executor(None, run)
    return JSONResponse(result_holder.get('r', {}))


PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Entity Lookup</title>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body { font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; background:#f5f5f7; color:#333; }
  .header { background:#1a1a2e; color:#fff; padding:20px 28px; }
  .header h1 { font-size:20px; font-weight:600; }
  .header p { font-size:13px; color:#8a8aaf; margin-top:4px; }
  .content { padding:24px 28px; }
  .search-form { display:flex; gap:12px; max-width:800px; margin-bottom:20px; }
  .search-input { flex:1; padding:12px 16px; font-size:15px; border:2px solid #e0e0e0; border-radius:8px; outline:none; }
  .search-input:focus { border-color:#4a90d9; }
  .search-btn { padding:12px 28px; background:#4a90d9; color:#fff; border:none; border-radius:8px; font-size:15px; font-weight:600; cursor:pointer; }
  .search-btn:hover { background:#3a7bc8; }
  .meta-line { font-size:13px; color:#666; margin:4px 0 14px; min-height:18px; }
  .report-card { background:#fff; border-radius:12px; border:2px solid #e0e0e0; overflow:hidden; max-width:900px; margin-bottom:20px; }
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
    <button class="search-btn" type="submit">Look Up</button>
  </form>
  <div class="meta-line" id="meta"></div>
  <div id="report"></div>
  <div class="progress-log" id="logwrap" style="display:none">
    <div class="progress-log-header">Progress Log</div>
    <div class="progress-log-body" id="log"></div>
  </div>
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
var CONF = {high:'#27ae60',medium:'#f39c12',low:'#e67e22',insufficient:'#e74c3c'};
var CBG = {high:['#d4edda','#155724'],medium:['#fff3cd','#856404'],low:['#ffeeba','#856404'],insufficient:['#f8d7da','#721c24']};
function renderReport(rep, url){
  var ent = rep.recommended_entity; var name = ent && ent.legal_entity_name || 'No match found';
  var conf = rep.confidence || 'insufficient'; var border = CONF[conf]||CONF.insufficient; var cb = CBG[conf]||CBG.insufficient;
  var rows='';
  if(ent){ var f = {'Jurisdiction': ent.jurisdiction_description||ent.jurisdiction||'—',
    'Registry ID': (ent.registry_id||'—')+(ent.jurisdiction_state?' ('+ent.jurisdiction_state+')':''),
    'Address': ent.address||'—','Source': ent.source||'—'};
    for(var k in f) rows += '<div style="display:flex;padding:4px 0;font-size:14px;"><span style="width:140px;color:#888;flex-shrink:0;">'+k+'</span><span style="color:#333;">'+esc(f[k])+'</span></div>'; }
  var note = rep.note ? '<div style="font-size:13px;color:#555;line-height:1.6;background:#f8f8fc;padding:12px;border-radius:6px;margin-bottom:16px;">'+esc(rep.note)+'</div>' : '';
  var rv = rep.registry_validation, rvBadge='';
  if(rv){ var st=rv.status||''; var lab={verified:'Registry Verified',name_match_bad_status:'Inactive in Registry',name_mismatch:'Registry Mismatch',fictitious_name:'Fictitious Name',branch_registration:'Branch Registration'}[st]||'Not Found in Registry';
    var col = st==='verified'?['#d4edda','#155724']:st==='name_match_bad_status'?['#ffeeba','#856404']:['#f8d7da','#721c24'];
    rvBadge='<span style="display:inline-block;padding:3px 10px;border-radius:4px;font-size:11px;font-weight:700;text-transform:uppercase;background:'+col[0]+';color:'+col[1]+';margin-left:6px;">'+esc(lab)+'</span>'; }
  return '<div class="report-card" style="border-color:'+border+';"><div style="padding:20px;border-bottom:1px solid #f0f0f0;">'
    + '<div style="font-size:20px;font-weight:700;color:#1a1a2e;">'+esc(name)
    + ' <span style="display:inline-block;padding:3px 10px;border-radius:4px;font-size:11px;font-weight:700;text-transform:uppercase;background:'+cb[0]+';color:'+cb[1]+';">'+esc(conf)+'</span>'+rvBadge+'</div></div>'
    + '<div style="padding:20px;">'+note+rows+'</div></div>';
}
function go(e){
  if(e) e.preventDefault();
  var u=document.getElementById('url').value.trim(); if(!u) return false;
  history.replaceState(null,'','live?url='+encodeURIComponent(u));
  document.getElementById('log').innerHTML=''; document.getElementById('report').innerHTML='';
  document.getElementById('logwrap').style.display='block';
  document.getElementById('meta').innerHTML='<span class="spin"></span> researching…';
  var es=new EventSource('lookup/stream?url='+encodeURIComponent(u));
  es.addEventListener('log', function(ev){ var e=JSON.parse(ev.data); var d=document.getElementById('log'); d.insertAdjacentHTML('beforeend', renderEntry(e)); d.scrollTop=d.scrollHeight; });
  es.addEventListener('result', function(ev){ var r=JSON.parse(ev.data); document.getElementById('report').innerHTML=renderReport(r.report||{}, u);
    var m=r.meta||{}; document.getElementById('meta').innerHTML='Done · '+(m.total_time_s||'?')+'s · $'+(m.cost_usd||'?')+' · '+(m.input_tokens||0)+'/'+(m.output_tokens||0)+' tokens · '+(m.model||''); es.close(); });
  es.addEventListener('error', function(ev){ try{ document.getElementById('meta').innerHTML='<span style="color:#dc2626">Error: '+esc(JSON.parse(ev.data))+'</span>'; }catch(_){ document.getElementById('meta').innerHTML='<span style="color:#dc2626">Stream error</span>'; } es.close(); });
  es.addEventListener('done', function(){ es.close(); });
  return false;
}
(function(){ var p=new URLSearchParams(location.search); var u=p.get('url'); if(u){ document.getElementById('url').value=u; go(null); } })();
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def home():
    return PAGE.replace("__URL__", "")


@app.get("/live", response_class=HTMLResponse)
def live(url: str = ""):
    return PAGE.replace("__URL__", url.replace('"', "&quot;"))
