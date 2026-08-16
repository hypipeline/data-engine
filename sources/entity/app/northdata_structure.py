"""
NorthData corporate-structure resolver — PURE (no network I/O).

Input: the rendered HTML of a NorthData entity page (the same HTML the Browserbase
fetch returns; also a saved .mhtml fixture once decoded). Output: a resolved view of
the ownership network, in particular the *current* ultimate parent.

Why this exists: the flat parser in tools_northdata.py dumps every edge label as-is,
which produced FIVE "ultimate parents" for Quest Global when there is really one. Three
signals fix that, all present in the SVG and all ignored by the flat parser:

  1. data-old="true"      → the edge is HISTORICAL (a former relationship). Exclude from
                            the current structure.
  2. data-head/-tail      → the ARROWHEAD end. For shareholding/parent edges the arrowhead
                            sits on the OWNED/child end (verified against unambiguous edges:
                            the "sole"/"100%" arrows point at the small local subsidiary —
                            España, Poland, Romania). So direction is NOT source→target; it
                            is read from the arrowhead.
  3. root "Ultimate parent" pointer edges are a SEPARATE arrow type: from the root they
                            point up to a candidate ultimate parent. A candidate is only the
                            real current ultimate parent if (a) it is current, (b) nothing
                            currently owns it, and (c) it anchors a live downward chain
                            (tie-breaker when several qualify).

Everything here is deterministic and unit-tested against a real saved page
(tests/fixtures/questglobal_network.html), so accuracy can't silently regress.
"""
import html as _html
import re

_NODE_RE = re.compile(r'<a class="node"[^>]*>')
_LINK_RE = re.compile(r'<g class="link"[^>]*>')
_SVG_RE = re.compile(r'<svg[^>]*aria-label="Network"[^>]*>(.*?)</svg>', re.S)


def _attr(tag: str, key: str) -> str:
    m = re.search(key + r'="([^"]*)"', tag)
    return _html.unescape(m.group(1)) if m else ''


def parse_network(html: str) -> dict | None:
    """Extract nodes and edges from the network SVG. Returns None if no graph is present."""
    svg_m = _SVG_RE.search(html or '')
    if not svg_m:
        return None
    svg = svg_m.group(1)

    nodes = {}
    for tag in _NODE_RE.findall(svg):
        nid = _attr(tag, 'data-id')
        if not nid:
            continue
        nodes[nid] = {
            'id': nid,
            'name': _attr(tag, 'data-text'),
            'desc': _attr(tag, 'data-description'),
            'root': _attr(tag, 'data-root') != '',
            'warning': _attr(tag, 'data-warning'),          # e.g. dissolved (†) markers
        }

    edges = []
    for tag in _LINK_RE.findall(svg):
        s, t = _attr(tag, 'data-source-id'), _attr(tag, 'data-target-id')
        if not s or not t:
            continue
        edges.append({
            'source': s, 'target': t,
            'label': _attr(tag, 'data-description'),
            'old': _attr(tag, 'data-old') == 'true',        # historical edge
            'head': _attr(tag, 'data-head'),                 # 'arrow' → arrowhead at target
            'tail': _attr(tag, 'data-tail'),                 # 'arrow' → arrowhead at source
        })
    return {'nodes': nodes, 'edges': edges}


def _owner_owned(edge: dict):
    """For a shareholding/parent edge, return (owner_id, owned_id) using the arrowhead.
    The arrowhead marks the OWNED/child end. Returns None for undirected edges
    (Address / Merger) — those are not ownership."""
    if edge['head'] == 'arrow':
        return edge['source'], edge['target']       # arrowhead at target → owned=target
    if edge['tail'] == 'arrow':
        return edge['target'], edge['source']        # arrowhead at source → owned=source
    return None


def resolve(html: str) -> dict:
    """Resolve the current ultimate parent (and the surrounding structure) from a NorthData
    entity page. See module docstring for the three signals. Returns a structured dict; when
    no graph exists, {'has_network': False}."""
    net = parse_network(html)
    if not net:
        return {'has_network': False}
    nodes, edges = net['nodes'], net['edges']
    name = lambda i: nodes.get(i, {}).get('name', f'#{i}')

    root_id = next((i for i, n in nodes.items() if n['root']), None)
    root_name = name(root_id) if root_id else None

    # 1+2. Resolve EVERY ownership edge by its arrowhead (the owned/child end) into a current
    #      ownership graph. The arrowhead gives direction for every edge alike — an explicit
    #      "Ultimate parent" arrow AND a controlling shareholding (">=75%", "Direct parent", ...).
    #      So a parent expressed either way lands in current_owned_by. [Quest Global: the
    #      "Ultimate parent" arrows point DOWN from Services PTE, so it owns them, not the reverse.]
    current_owned_by = {}        # owned_id -> set(owner_id)   (current edges only)
    current_owns = {}            # owner_id -> set(owned_id)
    stakes = []
    for e in edges:
        oo = _owner_owned(e)
        if not oo:
            continue             # Address / Merger → not directed ownership
        owner, owned = oo
        stakes.append({'owner': name(owner), 'owned': name(owned), 'label': e['label'], 'old': e['old']})
        if not e['old']:
            current_owned_by.setdefault(owned, set()).add(owner)
            current_owns.setdefault(owner, set()).add(owned)

    # 3. Walk UP the current ownership chain from the root to its topmost owner(s). The root is the
    #    TopCo only if NOTHING currently owns it. This catches a parent given as a controlling
    #    shareholding, not just as an explicit "Ultimate parent" pointer. [ABCA: Abca Systems Group
    #    Ltd. owns Abca Systems Ltd. at >=75%, so Group is the ultimate parent — not the root.]
    root_owners = current_owned_by.get(root_id, set())
    tops_ids, intermediates, seen, frontier = set(), set(), {root_id}, set(root_owners)
    while frontier:
        nid = frontier.pop()
        if nid in seen:
            continue
        seen.add(nid)
        ups = current_owned_by.get(nid, set()) - seen          # who owns this owner (excl. cycles)
        if ups:
            intermediates.add(nid)
            frontier |= ups
        else:
            tops_ids.add(nid)                                  # nobody owns it → a current top

    tops = [{'id': t, 'name': name(t),
             'controls': sorted(name(k) for k in current_owns.get(t, ()))} for t in tops_ids]
    not_top = [{'name': name(i), 'owned_by': sorted(name(a) for a in current_owned_by.get(i, ()))}
               for i in intermediates]

    # Tie-breaker: prefer a top that anchors a live downward chain (most current holdings).
    anchored = [t for t in tops if t['controls']]
    ranked = sorted(tops, key=lambda t: -len(t['controls']))
    ultimate = (anchored[0] if len(anchored) == 1 else (ranked[0] if ranked else None))

    # Former parents: entities that owned the root in a HISTORICAL edge and don't own it now.
    former_ids = set()
    for e in edges:
        if not e['old']:
            continue
        oo = _owner_owned(e)
        if oo and oo[1] == root_id and oo[0] not in root_owners:
            former_ids.add(oo[0])
    former_parents = [name(i) for i in former_ids]

    dissolved = [n['name'] for n in nodes.values() if n['warning']]

    return {
        'has_network': True,
        'target': root_name,
        'ultimate_parent': ultimate['name'] if ultimate else None,
        'is_top_itself': (not root_owners),              # nothing currently owns the root → TopCo
        'ultimate_candidates': tops,                     # current tops (may be >1 before tie-break)
        'excluded_not_top': not_top,                     # intermediate owners (themselves owned)
        'former_parents': former_parents,
        'stakes': stakes,
        'dissolved': dissolved,
    }


def format_for_llm(res: dict) -> str:
    """Render the resolution as unambiguous, fully-qualified statements for an LLM reader."""
    if not res.get('has_network'):
        return "No NorthData ownership network available for this entity."
    L = [f"CORPORATE STRUCTURE — {res['target']}", "SOURCE: NorthData ownership graph", ""]
    if res['ultimate_parent']:
        L.append(f"ULTIMATE PARENT (current): {res['ultimate_parent']} is the ultimate parent of {res['target']}.")
    elif res['is_top_itself']:
        L.append(f"ULTIMATE PARENT: {res['target']} appears to be the ultimate parent / TopCo — "
                 f"no current parent entity sits above it.")
    else:
        L.append("ULTIMATE PARENT: could not be resolved to a single entity (see candidates).")
    if len(res['ultimate_candidates']) > 1:
        L.append("  Note: multiple current tops before tie-break: "
                 + "; ".join(c['name'] for c in res['ultimate_candidates']))
    if res['excluded_not_top']:
        L.append("")
        for c in res['excluded_not_top']:
            L.append(f"NOT ultimate — {c['name']} is itself owned by {', '.join(c['owned_by'])}.")
    if res['former_parents']:
        L.append("")
        L.append("FORMER ultimate parents (historical — ignore for current control): "
                 + "; ".join(res['former_parents']) + ".")
    cur_stakes = [s for s in res['stakes'] if not s['old']]
    if cur_stakes:
        L.append("")
        L.append("CURRENT HOLDINGS:")
        for s in cur_stakes:
            L.append(f"- {s['owner']} — {s['label']} — {s['owned']}.")
    if res['dissolved']:
        L.append("")
        L.append("DISSOLVED/MERGED entities in the graph: " + "; ".join(res['dissolved']) + ".")
    return "\n".join(L)
