"""
Entity Lookup Report — JSON schema and HTML renderer.

The agent produces a structured JSON report. This module:
1. Defines the schema as dataclasses (for validation / type hints)
2. Renders JSON → HTML for human review
"""

from __future__ import annotations
import json
from dataclasses import dataclass, field, asdict
from typing import Optional


# ── JSON Schema (as dataclasses) ──────────────────────────────────────────────

@dataclass
class EntityInfo:
    legal_entity_name: str
    source: str                        # e.g. "UK Companies House", "SEC EDGAR"
    source_url: str                    # clickable link to registry page
    jurisdiction: str                  # e.g. "DE", "US-DE", "GB"
    registry_id: Optional[str] = None  # e.g. "HRB 6684", "File 2125720"
    regulatory_status: Optional[str] = None  # e.g. "Active", "Dissolved"
    address: Optional[str] = None
    relationship_to_website_entity: Optional[str] = None  # only on recommended_entity
    former_names: Optional[list[dict]] = None  # [{name, from, to, source, source_url}]


@dataclass
class EvidenceStep:
    step: str              # e.g. "url_to_candidate", "candidate_to_registry"
    description: str       # human-readable narrative
    source: str            # e.g. "WHOIS", "Companies House"
    source_url: Optional[str] = None
    quality: str = "verified"  # verified | inferred | unavailable
    strength: Optional[str] = None  # strong | moderate | weak (reverse only)


@dataclass
class SubstanceFactor:
    description: str
    result: str            # pass | fail | unknown
    source_url: Optional[str] = None


@dataclass
class OtherEntity:
    legal_entity_name: str
    jurisdiction: Optional[str] = None
    registry_id: Optional[str] = None
    why_not_recommended: str = ""
    verify_url: Optional[str] = None


@dataclass
class CorporateStructureNode:
    legal_entity_name: str
    jurisdiction: Optional[str] = None
    registry_id: Optional[str] = None
    source_url: Optional[str] = None
    role: Optional[str] = None         # e.g. "subsidiary", "division", "spun-off"
    is_recommended: bool = False
    children: list[CorporateStructureNode] = field(default_factory=list)


@dataclass
class KeyPerson:
    name: str
    role: str                          # e.g. "CEO", "Founder", "Supervisory Board Chair"
    source_url: Optional[str] = None


@dataclass
class EntityLookupReport:
    input_url: str
    date: str                          # ISO date e.g. "2026-06-04"
    report_id: str

    # Core recommendation
    recommended_entity: Optional[EntityInfo] = None
    website_entity: Optional[EntityInfo] = None
    confidence: str = "insufficient"   # high | medium | low | insufficient
    note: str = ""                     # plain English summary

    # Evidence
    evidence_forward: list[EvidenceStep] = field(default_factory=list)
    evidence_reverse: list[EvidenceStep] = field(default_factory=list)

    # Substance
    substance_score: int = 0
    substance_band: str = "insufficient"
    substance_factors: list[SubstanceFactor] = field(default_factory=list)

    # Structure
    corporate_structure: Optional[CorporateStructureNode] = None
    key_people: list[KeyPerson] = field(default_factory=list)
    other_entities: list[OtherEntity] = field(default_factory=list)

    # Provenance
    sources_used: list[dict] = field(default_factory=list)  # [{name, url}]

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False)

    def to_html(self) -> str:
        return render_html(self)


# ── HTML Renderer ─────────────────────────────────────────────────────────────

def _esc(text: str) -> str:
    """HTML-escape."""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _link(url: str | None, text: str) -> str:
    if not url:
        return _esc(text)
    return f'<a href="{_esc(url)}" target="_blank">{_esc(text)}</a>'


def _badge(band: str) -> str:
    return f'<span class="badge badge-{_esc(band)}">{_esc(band.upper())}</span>'


def _quality(q: str) -> str:
    cls = {"verified": "q-verified", "inferred": "q-inferred", "unavailable": "q-unavailable"}.get(q, "q-unavailable")
    return f'<span class="quality {cls}">{_esc(q.upper())}</span>'


def _strength(s: str | None) -> str:
    if not s:
        return ""
    cls = {"strong": "strong", "moderate": "moderate", "weak": "weak", "none": "none"}.get(s, "none")
    return f'<span class="rv-strength {cls}">{_esc(s.upper())}</span>'


def _factor_icon(result: str) -> tuple[str, str]:
    if result == "pass":
        return "✓", "factor-pass"
    elif result == "fail":
        return "✗", "factor-fail"
    return "?", "factor-unknown"


def _render_structure_tree(node: CorporateStructureNode, depth: int = 0) -> str:
    """Render corporate structure as indented tree."""
    lines = []
    prefix = ""
    if depth > 0:
        prefix = "&nbsp;&nbsp;&nbsp;" * (depth - 1) + "├── "

    name_html = _link(node.source_url, node.legal_entity_name)
    extra = []
    if node.jurisdiction:
        extra.append(_esc(node.jurisdiction))
    if node.role:
        extra.append(_esc(node.role))
    extra_str = f' ({", ".join(extra)})' if extra else ""

    if node.is_recommended:
        lines.append(f'{prefix}<span class="recommended">{name_html}{extra_str}</span> ← recommended')
    else:
        lines.append(f'{prefix}{name_html}{extra_str}')

    for child in node.children:
        lines.append(_render_structure_tree(child, depth + 1))

    return "<br>".join(lines)


def render_html(report: EntityLookupReport) -> str:
    """Render an EntityLookupReport to a standalone HTML page."""
    r = report
    json_str = r.to_json()

    # Recommendation section
    if r.recommended_entity:
        e = r.recommended_entity
        rec_entity = _link(e.source_url, e.legal_entity_name)
        rec_jurisdiction = _esc(e.jurisdiction or "Unknown")
        if e.registry_id:
            rec_jurisdiction += f" ({_esc(e.registry_id)})"
        rec_status = _esc(e.regulatory_status or "Unknown")
        rec_address = _esc(e.address or "—")
    else:
        rec_entity = "None — unable to identify a registered legal entity"
        rec_jurisdiction = "Unknown"
        rec_status = "Unknown"
        rec_address = "—"

    # Former names
    former_names_html = ""
    if r.recommended_entity and r.recommended_entity.former_names:
        items = []
        for fn in r.recommended_entity.former_names:
            item = f'"{_esc(fn.get("name", ""))}"'
            if fn.get("from") or fn.get("to"):
                item += f' ({_esc(fn.get("from", "?"))} → {_esc(fn.get("to", "?"))})'
            if fn.get("source_url"):
                item += f' — {_link(fn["source_url"], fn.get("source", "source"))}'
            items.append(item)
        former_names_html = "<br>".join(items)

    # Evidence forward
    forward_html = ""
    for i, step in enumerate(r.evidence_forward, 1):
        link_html = ""
        if step.source_url:
            link_html = f'<div class="step-link">{_link(step.source_url, "→ " + step.source)}</div>'
        forward_html += f'''
        <div class="evidence-step">
          <div class="step-header"><span class="step-num">{i}</span> {_esc(step.step)} {_quality(step.quality)}</div>
          <div class="step-body">{_esc(step.description)}{link_html}</div>
        </div>'''

    # Evidence reverse
    reverse_html = ""
    for step in r.evidence_reverse:
        rv_cls = ""
        if step.strength in ("moderate", "weak"):
            rv_cls = " rv-weak"
        elif step.strength == "strong":
            rv_cls = " rv-pass"
        reverse_html += f'''
        <div class="rv-item{rv_cls}">
          <span class="rv-label">{_esc(step.step)}</span>
          <span class="rv-detail">{_esc(step.description)}</span>
          {_strength(step.strength)}
        </div>'''

    # Substance factors
    factors_html = ""
    for f in r.substance_factors:
        icon, cls = _factor_icon(f.result)
        link = ""
        if f.source_url:
            link = f'<div class="factor-link"><a href="{_esc(f.source_url)}" target="_blank">source</a></div>'
        factors_html += f'''
        <div class="factor {cls}">
          <div class="factor-icon">{icon}</div>
          <div class="factor-text">{_esc(f.description)}</div>
          {link}
        </div>'''

    # Corporate structure
    structure_html = ""
    if r.corporate_structure:
        structure_html = f'''
    <div class="section">
      <div class="section-title">Corporate Structure</div>
      <div class="tree">{_render_structure_tree(r.corporate_structure)}</div>
      {_render_key_people(r.key_people)}
    </div>'''

    # Other entities
    others_html = ""
    if r.other_entities:
        rows = ""
        for oe in r.other_entities:
            meta = []
            if oe.jurisdiction:
                meta.append(_esc(oe.jurisdiction))
            if oe.registry_id:
                meta.append(_esc(oe.registry_id))
            meta_str = f'<br><span style="color:#888;font-size:12px;">{" · ".join(meta)}</span>' if meta else ""
            verify = _link(oe.verify_url, "verify →") if oe.verify_url else ""
            rows += f'''
            <tr>
              <td><strong>{_esc(oe.legal_entity_name)}</strong>{meta_str}</td>
              <td>{_esc(oe.why_not_recommended)}</td>
              <td>{verify}</td>
            </tr>'''
        others_html = f'''
    <div class="section">
      <div class="section-title">Other Entities Considered</div>
      <table class="considered-table">
        <thead><tr><th>Entity</th><th>Why Not Recommended</th><th></th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>'''

    # Sources
    sources_html = ""
    for s in r.sources_used:
        name = _esc(s.get("name", ""))
        url = s.get("url")
        if url:
            sources_html += f'<li>{name} — <a href="{_esc(url)}" target="_blank">{_esc(url)}</a></li>'
        else:
            sources_html += f"<li>{name}</li>"

    # Note
    note_cls = "note-warning" if r.confidence == "insufficient" else ""
    note_html = f'<div class="note {note_cls}">{_esc(r.note)}</div>' if r.note else ""

    # Score bar class
    score_cls = f"score-{r.substance_band}" if r.substance_band in ("high", "medium", "low") else "score-insufficient"

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Entity Lookup Report — {_esc(r.input_url)}</title>
{_CSS}
</head>
<body>

<div class="report">

  <div class="header">
    <h1>Entity Lookup Report</h1>
    <div class="meta">
      Input: {_link(r.input_url, r.input_url)} &nbsp;·&nbsp; {_esc(r.date)}
      <button class="json-toggle" onclick="document.getElementById('json-overlay').classList.toggle('visible')">View Response</button>
    </div>
  </div>

  <div class="section recommendation">
    <div class="section-title">Recommendation</div>
    <div class="rec-grid">
      <span class="rec-label">Entity</span>
      <span class="rec-value">{rec_entity}</span>
      <span class="rec-label">Jurisdiction</span>
      <span class="rec-value">{rec_jurisdiction}</span>
      <span class="rec-label">Status</span>
      <span class="rec-value">{rec_status}</span>
      <span class="rec-label">Address</span>
      <span class="rec-value">{rec_address}</span>
      <span class="rec-label">Confidence</span>
      <span class="rec-value">{_badge(r.confidence)}</span>
    </div>
    {note_html}
  </div>

  <div class="section">
    <div class="section-title">Evidence Chain (Forward)</div>
    {forward_html}
  </div>

  <div class="section">
    <div class="section-title">Reverse Validation (Entity → URL)</div>
    {reverse_html if reverse_html else '<p style="font-size:13px;color:#888;">No reverse validation possible — no registered entity found.</p>'}
  </div>

  {structure_html}

  <div class="section">
    <div class="section-title">Substance Assessment</div>
    <div class="score-bar-container {score_cls}">
      <div class="score-bar-label">
        <span><strong>{r.substance_score} / 100</strong></span>
        {_badge(r.substance_band)}
      </div>
      <div class="score-bar"><div class="score-bar-fill" style="width:{r.substance_score}%;"></div></div>
    </div>
    {factors_html}
  </div>

  {others_html}

  <div class="section">
    <div class="section-title">Sources Used</div>
    <ul class="sources">{sources_html}</ul>
  </div>

  <div class="footer">
    <span>Entity Lookup v2 — Automated research, human review required</span>
    <span>{_esc(r.report_id)}</span>
  </div>

</div>

<div id="json-overlay" class="json-overlay">
  <div class="json-panel">
    <div class="json-header">
      <span>JSON Response</span>
      <button onclick="document.getElementById('json-overlay').classList.remove('visible')">✕</button>
    </div>
    <pre class="json-body">{_esc(json_str)}</pre>
  </div>
</div>

</body>
</html>'''


def _render_key_people(people: list[KeyPerson]) -> str:
    if not people:
        return ""
    lines = []
    for p in people:
        name = f"<strong>{_esc(p.name)}</strong>"
        if p.source_url:
            name += f' ({_link(p.source_url, "source")})'
        lines.append(f"{_esc(p.role)}: {name}")
    return f'<div class="key-person">{"<br>".join(lines)}</div>'


# ── CSS ───────────────────────────────────────────────────────────────────────

_CSS = '''<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif; background: #f5f5f5; color: #1a1a1a; line-height: 1.5; }
  .report { max-width: 820px; margin: 40px auto; background: #fff; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); overflow: hidden; }
  .header { background: #1a1a2e; color: #fff; padding: 24px 32px; display: flex; justify-content: space-between; align-items: flex-start; }
  .header h1 { font-size: 18px; font-weight: 600; letter-spacing: 0.5px; text-transform: uppercase; margin-bottom: 4px; }
  .header .meta { font-size: 13px; color: #8a8aaf; }
  .header .meta a { color: #8a8aaf; }
  .json-toggle { background: transparent; border: 1px solid #555; color: #8a8aaf; font-size: 11px; padding: 4px 10px; border-radius: 3px; cursor: pointer; margin-left: 12px; }
  .json-toggle:hover { border-color: #aaa; color: #fff; }
  .section { padding: 24px 32px; border-bottom: 1px solid #eee; }
  .section:last-child { border-bottom: none; }
  .section-title { font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 1px; color: #888; margin-bottom: 16px; }
  .recommendation { background: #fafbfc; }
  .rec-grid { display: grid; grid-template-columns: 100px 1fr; gap: 6px 16px; font-size: 14px; }
  .rec-label { color: #666; font-weight: 500; }
  .rec-value { font-weight: 600; }
  .rec-value a { color: #1a1a2e; text-decoration: underline; text-decoration-color: #ccc; text-underline-offset: 2px; }
  .rec-value a:hover { text-decoration-color: #1a1a2e; }
  .badge { display: inline-block; padding: 2px 10px; border-radius: 3px; font-size: 11px; font-weight: 700; letter-spacing: 0.5px; }
  .badge-high { background: #e6f4ea; color: #1e7e34; }
  .badge-medium { background: #fff3cd; color: #856404; }
  .badge-low { background: #fde8e8; color: #c53030; }
  .badge-insufficient { background: #eee; color: #666; }
  .quality { display: inline-block; padding: 1px 6px; border-radius: 2px; font-size: 10px; font-weight: 600; letter-spacing: 0.3px; margin-left: 4px; }
  .q-verified { background: #e6f4ea; color: #1e7e34; }
  .q-inferred { background: #fff3cd; color: #856404; }
  .q-unavailable { background: #fde8e8; color: #c53030; }
  .note { margin-top: 16px; padding: 12px 16px; background: #f8f9fa; border-left: 3px solid #dee2e6; font-size: 13px; color: #555; line-height: 1.6; }
  .note.note-warning { border-left-color: #c53030; background: #fef5f5; }
  .evidence-step { margin-bottom: 20px; }
  .evidence-step:last-child { margin-bottom: 0; }
  .step-header { font-size: 13px; font-weight: 600; color: #333; margin-bottom: 4px; }
  .step-num { display: inline-block; width: 20px; height: 20px; background: #1a1a2e; color: #fff; border-radius: 50%; text-align: center; line-height: 20px; font-size: 11px; font-weight: 700; margin-right: 6px; }
  .step-body { font-size: 13px; color: #555; margin-left: 28px; line-height: 1.6; }
  .step-link { display: block; margin-top: 4px; }
  .step-link a { font-size: 12px; color: #1a56db; word-break: break-all; }
  .step-link a:hover { text-decoration: underline; }
  .tree { font-size: 13px; font-family: 'SF Mono', 'Fira Code', monospace; line-height: 1.8; }
  .tree a { color: #1a56db; text-decoration: none; }
  .tree a:hover { text-decoration: underline; }
  .tree .recommended { background: #e6f4ea; padding: 2px 6px; border-radius: 3px; font-weight: 600; }
  .key-person { margin-top: 12px; font-size: 13px; color: #666; }
  .factor { display: flex; align-items: flex-start; gap: 8px; padding: 6px 0; font-size: 13px; }
  .factor-icon { flex-shrink: 0; width: 18px; text-align: center; font-size: 14px; }
  .factor-pass .factor-icon { color: #1e7e34; }
  .factor-fail .factor-icon { color: #c53030; }
  .factor-unknown .factor-icon { color: #856404; }
  .factor-text { flex: 1; }
  .factor-link { flex-shrink: 0; }
  .factor-link a { font-size: 11px; color: #1a56db; text-decoration: none; padding: 2px 6px; border: 1px solid #d0d7de; border-radius: 3px; }
  .factor-link a:hover { background: #f0f4ff; }
  .score-bar-container { margin-bottom: 16px; }
  .score-bar-label { display: flex; justify-content: space-between; font-size: 13px; margin-bottom: 4px; }
  .score-bar { height: 8px; background: #eee; border-radius: 4px; overflow: hidden; }
  .score-bar-fill { height: 100%; border-radius: 4px; }
  .score-high .score-bar-fill { background: #1e7e34; }
  .score-medium .score-bar-fill { background: #d69e2e; }
  .score-low .score-bar-fill { background: #c53030; }
  .score-insufficient .score-bar-fill { background: #999; }
  .rv-item { display: flex; align-items: flex-start; gap: 8px; padding: 8px 12px; margin-bottom: 6px; border-radius: 4px; border-left: 3px solid #c53030; background: #fef5f5; font-size: 13px; }
  .rv-item.rv-pass { background: #f8faf8; border-left-color: #1e7e34; }
  .rv-item.rv-weak { background: #fffdf5; border-left-color: #d69e2e; }
  .rv-label { font-weight: 600; min-width: 110px; flex-shrink: 0; }
  .rv-detail { flex: 1; color: #555; }
  .rv-strength { flex-shrink: 0; font-size: 11px; font-weight: 600; }
  .rv-strength.strong { color: #1e7e34; }
  .rv-strength.moderate { color: #1a56db; }
  .rv-strength.weak { color: #856404; }
  .rv-strength.none { color: #c53030; }
  .considered-table { width: 100%; font-size: 13px; border-collapse: collapse; }
  .considered-table th { text-align: left; font-weight: 600; color: #666; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; padding: 8px 12px; border-bottom: 2px solid #eee; }
  .considered-table td { padding: 10px 12px; border-bottom: 1px solid #f0f0f0; vertical-align: top; }
  .considered-table tr:last-child td { border-bottom: none; }
  .considered-table a { color: #1a56db; text-decoration: none; font-size: 12px; }
  .considered-table a:hover { text-decoration: underline; }
  .sources { list-style: none; }
  .sources li { padding: 3px 0; font-size: 13px; color: #666; }
  .sources li::before { content: '•'; margin-right: 8px; color: #999; }
  .sources li a { color: #1a56db; font-size: 12px; }
  .footer { padding: 16px 32px; background: #fafbfc; border-top: 1px solid #eee; font-size: 11px; color: #999; display: flex; justify-content: space-between; }

  /* JSON overlay */
  .json-overlay { position: fixed; top: 0; right: 0; bottom: 0; width: 50%; background: #1a1a2e; transform: translateX(100%); transition: transform 0.25s ease; z-index: 1000; display: flex; flex-direction: column; }
  .json-overlay.visible { transform: translateX(0); }
  .json-panel { display: flex; flex-direction: column; height: 100%; }
  .json-header { display: flex; justify-content: space-between; align-items: center; padding: 16px 24px; border-bottom: 1px solid #333; color: #fff; font-size: 14px; font-weight: 600; }
  .json-header button { background: none; border: none; color: #888; font-size: 18px; cursor: pointer; padding: 4px 8px; }
  .json-header button:hover { color: #fff; }
  .json-body { flex: 1; overflow: auto; padding: 24px; font-family: 'SF Mono', 'Fira Code', monospace; font-size: 12px; line-height: 1.6; color: #c9d1d9; white-space: pre-wrap; word-break: break-word; }
</style>'''
