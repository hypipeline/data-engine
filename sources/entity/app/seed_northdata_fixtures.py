"""One-off: fetch NorthData entity pages via Browserbase, cache raw HTML as test fixtures,
and print the resolver's ultimate-parent verdict vs known truth. Run: python3 seed_northdata_fixtures.py [slug]"""
import sys
import time
import urllib.parse
import pathlib

from config import load_config
from tools import LookupTools
import northdata_structure as ns

MARK = 'aria-label="Network"'
FIX = pathlib.Path(__file__).resolve().parent.parent / "tests" / "fixtures"

# slug -> (search name, known ultimate parent or None for TopCo)
CASES = {
    "audi":       ("Audi AG", "Volkswagen AG"),
    "rollsroyce": ("Rolls-Royce Motor Cars Limited", "BMW AG"),
    "nestle_de":  ("Nestlé Deutschland AG", "Nestlé S.A."),
    "adidas":     ("adidas AG", None),   # TopCo — must NOT invent a parent
}


def seed(slug, name, known):
    t = LookupTools(load_config())
    url = "https://www.northdata.com/" + urllib.parse.quote_plus(name)
    cap = {}
    orig = t._parse_network_svg

    def wrap(html, u=''):
        cap['html'] = html
        return orig(html, u)
    t._parse_network_svg = wrap

    tic = time.time()
    netstr = t.northdata_network(url)
    dt = time.time() - tic
    html = cap.get('html', '')
    has = MARK in html
    print(f"\n[{slug}] {name}")
    print(f"   url: {url}")
    print(f"   fetch {dt:.0f}s | html {len(html)} chars | SVG present: {has}")
    if not has:
        print(f"   -> NO GRAPH. netstr: {netstr[:100]}")
        return False
    p = FIX / f"{slug}_network.html"
    p.write_text(html)
    res = ns.resolve(html)
    up = res['ultimate_parent']
    print(f"   SAVED {p.name}")
    print(f"   target: {res['target']}")
    print(f"   RESOLVED ultimate parent: {up}  (is_top={res['is_top_itself']})")
    print(f"   current tops: {[c['name'] for c in res['ultimate_candidates']]}")
    print(f"   former: {res['former_parents']}")
    # verdict vs known
    if known is None:
        ok = res['is_top_itself'] or up is None
        print(f"   KNOWN: TopCo (no parent)  -> {'PASS' if ok else 'FAIL'}")
    else:
        norm = lambda s: (s or '').lower().replace('.', '').replace(',', '')
        ok = up and (norm(known)[:12] in norm(up) or norm(up)[:12] in norm(known))
        print(f"   KNOWN: {known}  -> {'PASS' if ok else 'FAIL'}")
    return True


if __name__ == "__main__":
    which = sys.argv[1:] or list(CASES.keys())
    for slug in which:
        name, known = CASES[slug]
        try:
            seed(slug, name, known)
        except Exception as e:  # noqa: BLE001
            print(f"[{slug}] ERROR: {e}")
