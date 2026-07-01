"""
Multi-report HTML viewer.

Reads all JSON reports from reports/ directory and generates a single
index.html with sidebar navigation and the JSON panel.

Usage:
    python viewer.py              # generates index.html and opens it
    python viewer.py --no-open    # generates without opening
"""

from __future__ import annotations
import json
import glob
import os
import sys
from urllib.parse import urlparse

from report_schema import _CSS, _esc, _link, _badge, _quality, _strength, _factor_icon


def _domain(url: str) -> str:
    """Extract domain from URL for display."""
    return urlparse(url).netloc.replace("www.", "")


def _render_report_card(data: dict) -> str:
    """Render a single report as an HTML card (the main content area)."""
    r = data
    entity = r.get("recommended_entity")

    # Recommendation
    if entity:
        rec_entity = _link(entity["source_url"], entity["legal_entity_name"])
        rec_jurisdiction = _esc(entity.get("jurisdiction") or "Unknown")
        if entity.get("registry_id"):
            rec_jurisdiction += f' ({_esc(entity["registry_id"])})'
        rec_status = _esc(entity.get("regulatory_status") or "Unknown")
        rec_address = _esc(entity.get("address") or "—")
    else:
        rec_entity = "None — unable to identify a registered legal entity"
        rec_jurisdiction = "Unknown"
        rec_status = "Unknown"
        rec_address = "—"

    confidence = r.get("confidence", "insufficient")
    note = r.get("note", "")
    note_cls = "note-warning" if confidence == "insufficient" else ""
    note_html = f'<div class="note {note_cls}">{_esc(note)}</div>' if note else ""

    # Former names
    former_html = ""
    if entity and entity.get("former_names"):
        items = []
        for fn in entity["former_names"]:
            item = f'"{_esc(fn.get("name", ""))}"'
            if fn.get("from") or fn.get("to"):
                item += f' ({_esc(fn.get("from", "?"))} → {_esc(fn.get("to", "?"))})'
            if fn.get("source_url"):
                item += f' — {_link(fn["source_url"], fn.get("source", "source"))}'
            items.append(f"<li>{item}</li>")
        former_html = f'''
        <div style="margin-top:8px;font-size:12px;color:#666;">
          <strong>Former names:</strong>
          <ul style="margin:4px 0 0 16px;">{"".join(items)}</ul>
        </div>'''

    # Forward evidence
    forward_html = ""
    for i, step in enumerate(r.get("evidence_forward", []), 1):
        link_html = ""
        if step.get("source_url"):
            link_html = f'<div class="step-link">{_link(step["source_url"], "→ " + step.get("source", ""))}</div>'
        forward_html += f'''
        <div class="evidence-step">
          <div class="step-header"><span class="step-num">{i}</span> {_esc(step.get("step", ""))} {_quality(step.get("quality", "unavailable"))}</div>
          <div class="step-body">{_esc(step.get("description", ""))}{link_html}</div>
        </div>'''

    # Reverse evidence
    reverse_html = ""
    for step in r.get("evidence_reverse", []):
        strength = step.get("strength", "none")
        rv_cls = ""
        if strength in ("moderate", "weak"):
            rv_cls = " rv-weak"
        elif strength == "strong":
            rv_cls = " rv-pass"
        reverse_html += f'''
        <div class="rv-item{rv_cls}">
          <span class="rv-label">{_esc(step.get("step", ""))}</span>
          <span class="rv-detail">{_esc(step.get("description", ""))}</span>
          {_strength(strength)}
        </div>'''
    if not reverse_html:
        reverse_html = '<p style="font-size:13px;color:#888;">No reverse validation possible — no registered entity found.</p>'

    # Corporate structure
    structure_html = ""
    if r.get("corporate_structure"):
        tree = _render_structure_dict(r["corporate_structure"])
        people_html = ""
        if r.get("key_people"):
            lines = []
            for p in r["key_people"]:
                name = f'<strong>{_esc(p["name"])}</strong>'
                if p.get("source_url"):
                    name += f' ({_link(p["source_url"], "source")})'
                lines.append(f'{_esc(p["role"])}: {name}')
            people_html = f'<div class="key-person">{"<br>".join(lines)}</div>'
        structure_html = f'''
    <div class="section">
      <div class="section-title">Corporate Structure</div>
      <div class="tree">{tree}</div>
      {people_html}
    </div>'''

    # Substance
    sub_score = r.get("substance_score", 0)
    sub_band = r.get("substance_band", "insufficient")
    score_cls = f"score-{sub_band}" if sub_band in ("high", "medium", "low") else "score-insufficient"
    factors_html = ""
    for f in r.get("substance_factors", []):
        result = f.get("result", "unknown")
        icons = {"pass": ("✓", "factor-pass"), "fail": ("✗", "factor-fail")}
        icon, cls = icons.get(result, ("?", "factor-unknown"))
        link = ""
        if f.get("source_url"):
            link = f'<div class="factor-link"><a href="{_esc(f["source_url"])}" target="_blank">source</a></div>'
        factors_html += f'''
        <div class="factor {cls}">
          <div class="factor-icon">{icon}</div>
          <div class="factor-text">{_esc(f.get("description", ""))}</div>
          {link}
        </div>'''

    # Other entities
    others_html = ""
    if r.get("other_entities"):
        rows = ""
        for oe in r["other_entities"]:
            meta = []
            if oe.get("jurisdiction"):
                meta.append(_esc(oe["jurisdiction"]))
            if oe.get("registry_id"):
                meta.append(_esc(oe["registry_id"]))
            meta_str = f'<br><span style="color:#888;font-size:12px;">{" · ".join(meta)}</span>' if meta else ""
            verify = _link(oe.get("verify_url"), "verify →") if oe.get("verify_url") else ""
            rows += f'''
            <tr>
              <td><strong>{_esc(oe.get("legal_entity_name", ""))}</strong>{meta_str}</td>
              <td>{_esc(oe.get("why_not_recommended", ""))}</td>
              <td>{verify}</td>
            </tr>'''
        others_html = f'''
    <div class="section">
      <div class="section-title">Other Entities Considered</div>
      <table class="considered-table"><thead><tr><th>Entity</th><th>Why Not Recommended</th><th></th></tr></thead><tbody>{rows}</tbody></table>
    </div>'''

    # Sources
    sources_html = ""
    for s in r.get("sources_used", []):
        name = _esc(s.get("name", ""))
        url = s.get("url")
        if url:
            sources_html += f'<li>{name} — <a href="{_esc(url)}" target="_blank">{_esc(url)}</a></li>'
        else:
            sources_html += f"<li>{name}</li>"

    return f'''
    <div class="section recommendation">
      <div class="section-title">Recommendation</div>
      <div class="rec-grid">
        <span class="rec-label">Entity</span><span class="rec-value">{rec_entity}</span>
        <span class="rec-label">Jurisdiction</span><span class="rec-value">{rec_jurisdiction}</span>
        <span class="rec-label">Status</span><span class="rec-value">{rec_status}</span>
        <span class="rec-label">Address</span><span class="rec-value">{rec_address}</span>
        <span class="rec-label">Confidence</span><span class="rec-value">{_badge(confidence)}</span>
      </div>
      {note_html}
      {former_html}
    </div>
    <div class="section"><div class="section-title">Evidence Chain (Forward)</div>{forward_html}</div>
    <div class="section"><div class="section-title">Reverse Validation (Entity → URL)</div>{reverse_html}</div>
    {structure_html}
    <div class="section">
      <div class="section-title">Substance Assessment</div>
      <div class="score-bar-container {score_cls}">
        <div class="score-bar-label"><span><strong>{sub_score} / 100</strong></span>{_badge(sub_band)}</div>
        <div class="score-bar"><div class="score-bar-fill" style="width:{sub_score}%;"></div></div>
      </div>
      {factors_html}
    </div>
    {others_html}
    <div class="section"><div class="section-title">Sources Used</div><ul class="sources">{sources_html}</ul></div>
    '''


def _render_structure_dict(node: dict, depth: int = 0) -> str:
    lines = []
    prefix = ""
    if depth > 0:
        prefix = "&nbsp;&nbsp;&nbsp;" * (depth - 1) + "├── "

    name_html = _link(node.get("source_url"), node["legal_entity_name"])
    extra = []
    if node.get("jurisdiction"):
        extra.append(_esc(node["jurisdiction"]))
    if node.get("role"):
        extra.append(_esc(node["role"]))
    extra_str = f' ({", ".join(extra)})' if extra else ""

    if node.get("is_recommended"):
        lines.append(f'{prefix}<span class="recommended">{name_html}{extra_str}</span> ← recommended')
    else:
        lines.append(f'{prefix}{name_html}{extra_str}')

    for child in node.get("children", []):
        lines.append(_render_structure_dict(child, depth + 1))

    return "<br>".join(lines)


def build_index(report_dir: str = "reports", output: str = "index.html") -> str:
    """Build multi-report index.html from all JSONs in report_dir."""
    paths = sorted(glob.glob(f"{report_dir}/*.json"))
    if not paths:
        raise FileNotFoundError(f"No JSON files in {report_dir}/")

    reports = []
    for path in paths:
        with open(path) as f:
            data = json.load(f)
        with open(path) as f:
            raw_json = f.read()
        reports.append({"data": data, "json": raw_json, "file": os.path.basename(path)})

    # Build sidebar items and report panels
    sidebar_items = ""
    panels = ""
    json_panels = ""
    for i, r in enumerate(reports):
        d = r["data"]
        domain = _domain(d["input_url"])
        conf = d.get("confidence", "insufficient")
        entity_name = d["recommended_entity"]["legal_entity_name"] if d.get("recommended_entity") else "No match"
        active = " active" if i == 0 else ""
        visible = "" if i == 0 else " style=\"display:none;\""

        sidebar_items += f'''
        <div class="sidebar-item{active}" data-index="{i}" onclick="showReport({i})">
          <div class="sidebar-domain">{_esc(domain)}</div>
          <div class="sidebar-entity">{_esc(entity_name)}</div>
          <span class="badge badge-{conf}" style="margin-top:4px;">{_esc(conf.upper())}</span>
        </div>'''

        card_html = _render_report_card(d)
        panels += f'''<div class="report-panel" id="panel-{i}"{visible}>{card_html}</div>'''
        json_panels += f'''<pre class="json-body" id="json-{i}"{visible}>{_esc(r["json"])}</pre>'''

    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Entity Lookup — Reports</title>
{_CSS}
<style>
  body {{ display: flex; height: 100vh; overflow: hidden; background: #f0f0f0; }}
  .sidebar {{ width: 260px; background: #1a1a2e; color: #fff; overflow-y: auto; flex-shrink: 0; padding: 16px 0; }}
  .sidebar-header {{ padding: 12px 20px 16px; font-size: 13px; font-weight: 700; text-transform: uppercase; letter-spacing: 1px; color: #666; border-bottom: 1px solid #2a2a4e; margin-bottom: 8px; }}
  .sidebar-item {{ padding: 12px 20px; cursor: pointer; border-left: 3px solid transparent; transition: all 0.15s; }}
  .sidebar-item:hover {{ background: #2a2a4e; }}
  .sidebar-item.active {{ background: #2a2a4e; border-left-color: #4a90d9; }}
  .sidebar-domain {{ font-size: 14px; font-weight: 600; color: #e0e0e0; }}
  .sidebar-entity {{ font-size: 11px; color: #8a8aaf; margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .main {{ flex: 1; overflow-y: auto; }}
  .main .report {{ margin: 0; border-radius: 0; box-shadow: none; min-height: 100vh; }}
  .main .header {{ position: sticky; top: 0; z-index: 10; }}
  .json-toggle {{ background: transparent; border: 1px solid #555; color: #8a8aaf; font-size: 11px; padding: 4px 10px; border-radius: 3px; cursor: pointer; }}
  .json-toggle:hover {{ border-color: #aaa; color: #fff; }}
  .json-overlay {{ position: fixed; top: 0; right: 0; bottom: 0; width: 50%; background: #1a1a2e; transform: translateX(100%); transition: transform 0.25s ease; z-index: 1000; display: flex; flex-direction: column; }}
  .json-overlay.visible {{ transform: translateX(0); }}
  .json-panel {{ display: flex; flex-direction: column; height: 100%; }}
  .json-header {{ display: flex; justify-content: space-between; align-items: center; padding: 16px 24px; border-bottom: 1px solid #333; color: #fff; font-size: 14px; font-weight: 600; }}
  .json-header button {{ background: none; border: none; color: #888; font-size: 18px; cursor: pointer; padding: 4px 8px; }}
  .json-header button:hover {{ color: #fff; }}
  .json-body {{ flex: 1; overflow: auto; padding: 24px; font-family: 'SF Mono', 'Fira Code', monospace; font-size: 12px; line-height: 1.6; color: #c9d1d9; white-space: pre-wrap; word-break: break-word; }}
  .report-count {{ padding: 8px 20px; font-size: 11px; color: #555; }}
</style>
</head>
<body>

<div class="sidebar">
  <div class="sidebar-header">Reports</div>
  {sidebar_items}
  <div class="report-count">{len(reports)} report{"s" if len(reports) != 1 else ""}</div>
</div>

<div class="main">
  <div class="report">
    <div class="header" id="report-header">
      <div>
        <h1>Entity Lookup Report</h1>
        <div class="meta" id="report-meta"></div>
      </div>
      <button class="json-toggle" onclick="toggleJson()">View Response</button>
    </div>
    {panels}
  </div>
</div>

<div id="json-overlay" class="json-overlay">
  <div class="json-panel">
    <div class="json-header">
      <span>JSON Response</span>
      <button onclick="toggleJson()">✕</button>
    </div>
    <div id="json-container">
      {json_panels}
    </div>
  </div>
</div>

<script>
const reportData = {json.dumps([r["data"] for r in reports])};
let currentIndex = 0;

function showReport(idx) {{
  // Hide all panels
  document.querySelectorAll('.report-panel').forEach(p => p.style.display = 'none');
  document.querySelectorAll('.json-body').forEach(p => p.style.display = 'none');
  document.querySelectorAll('.sidebar-item').forEach(s => s.classList.remove('active'));

  // Show selected
  document.getElementById('panel-' + idx).style.display = 'block';
  document.getElementById('json-' + idx).style.display = 'block';
  document.querySelectorAll('.sidebar-item')[idx].classList.add('active');

  // Update header
  const r = reportData[idx];
  document.getElementById('report-meta').innerHTML =
    'Input: <a href="' + r.input_url + '" target="_blank">' + r.input_url + '</a> &nbsp;·&nbsp; ' + r.date;

  currentIndex = idx;

  // Scroll main to top
  document.querySelector('.main').scrollTop = 0;
}}

function toggleJson() {{
  document.getElementById('json-overlay').classList.toggle('visible');
}}

// Init
showReport(0);

// Keyboard navigation
document.addEventListener('keydown', (e) => {{
  if (e.key === 'ArrowDown' && currentIndex < reportData.length - 1) {{
    showReport(currentIndex + 1);
  }} else if (e.key === 'ArrowUp' && currentIndex > 0) {{
    showReport(currentIndex - 1);
  }} else if (e.key === 'Escape') {{
    document.getElementById('json-overlay').classList.remove('visible');
  }}
}});
</script>

</body>
</html>'''

    with open(output, "w") as f:
        f.write(html)
    return output


if __name__ == "__main__":
    out = build_index()
    print(f"Built {out} with reports from reports/")
    if "--no-open" not in sys.argv:
        os.system(f"open {out}")
