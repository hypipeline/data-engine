"""Diagnostic: for each company, fetch the NorthData page and report whether the PARENT is
visible as a NAMED node in the ownership graph, or only as an unnamed relationship id-link."""
import re
import urllib.parse
import pathlib

from config import load_config
from tools import LookupTools
import northdata_structure as ns

MARK = 'aria-label="Network"'
FIX = pathlib.Path(__file__).resolve().parent.parent / "tests" / "fixtures"

CASES = [
    ("audi",       "Audi AG"),
    ("rollsroyce", "Rolls-Royce Motor Cars Limited"),
    ("nestle_de",  "Nestlé Deutschland AG"),
    ("adidas",     "adidas AG"),
]


def fetch(t, name):
    url = "https://www.northdata.com/" + urllib.parse.quote_plus(name)
    cap = {}
    orig = t._parse_network_svg
    t._parse_network_svg = lambda h, u='': (cap.__setitem__('html', h), orig(h, u))[1]
    t.northdata_network(url)
    t._parse_network_svg = orig
    return cap.get('html', '')


def analyse(slug, name, html):
    print(f"\n{'='*70}\n{name}")
    if MARK not in html:
        print("  NO network SVG at all.")
        return
    (FIX / f"{slug}_network.html").write_text(html)
    net = ns.parse_network(html)
    root = next((i for i, n in net['nodes'].items() if n['root']), None)
    # NAMED parent in the graph? an edge from root whose label mentions 'parent' to a named node
    named_parents = []
    for e in net['edges']:
        if e['source'] == root and 'parent' in e['label'].lower():
            named_parents.append((net['nodes'].get(e['target'], {}).get('name', '?'), e['label']))
    # summary id-only parent links (name NOT present)
    flat = re.sub(r'>\s+<', '><', html)
    idlinks = re.findall(r'<a href="/\?id=(\d+)"><span>(Ultimate parent|Direct parent)</span>', flat)
    res = ns.resolve(html)
    print(f"  graph nodes: {len(net['nodes'])}   named parent nodes in graph: {len(named_parents)}")
    for nm, lab in named_parents:
        print(f"      NAMED in graph: {nm}  [{lab}]")
    print(f"  unnamed summary parent id-links: {len(idlinks)}")
    for eid, lab in idlinks:
        print(f"      id-only (NO NAME): {lab} -> id={eid}")
    print(f"  RESOLVER ultimate_parent: {res['ultimate_parent']}  is_top={res['is_top_itself']}")
    verdict = ("PARENT VISIBLE (named in graph)" if named_parents
               else ("PARENT HIDDEN (only unnamed id-link)" if idlinks
                     else "NO PARENT SHOWN (looks like a top / TopCo)"))
    print(f"  >>> {verdict}")


if __name__ == "__main__":
    t = LookupTools(load_config())
    for slug, name in CASES:
        try:
            analyse(slug, name, fetch(t, name))
        except Exception as e:  # noqa: BLE001
            print(f"\n{name}: ERROR {e}")
