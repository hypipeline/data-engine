"""NorthData ownership-network test harness — a *visible* companion to the pytest suite.

Each case pairs a saved NorthData entity page (fixture, zero network I/O) with the KNOWN
corporate-structure truth (its current ultimate parent, or None for a TopCo). Running a case
feeds the fixture through BOTH extractors and grades them against that truth:

  - flat parser  : tools._parse_network_svg()  — lists every edge; weak on direction
  - resolver     : northdata_structure.resolve() — arrowhead + data-old aware; picks ONE parent

The point is to make the disagreements between the two, and the misses vs. known truth, visible
in the UI (the same phenomenon behind the Quest Global "ultimate parent never reached the LLM"
finding). Fixture-based so it is deterministic and CI-safe; live re-fetch is a separate concern.
"""
import pathlib

# Compact SVG-only fixtures live INSIDE the app dir (the Docker build context is
# sources/entity/app with `COPY . .`), so they ship in the runtime container. Both extractors
# key off the same <svg aria-label="Network"> element, so an svg-only page is byte-equivalent
# input to the full saved page (see tests/fixtures/*_network.html for the originals).
_FIX = pathlib.Path(__file__).resolve().parent / "network_fixtures"

# slug -> case. expect.ultimate_parent: substring that must appear as the CURRENT ultimate
# parent (None => the entity is itself the TopCo and no parent must be invented).
CASES = [
    {"slug": "adidas", "name": "adidas AG",
     "url": "https://www.northdata.com/adidas+AG",
     "fixture": "adidas.svg.html",
     "expect": {"root": "adidas AG", "ultimate_parent": None},
     "note": "TopCo — the flat parser wrongly files its subsidiaries under OWNED BY; the resolver must say TopCo."},
    {"slug": "questglobal", "name": "Quest Global Services PTE. Ltd. (Singapore)",
     "url": "https://www.northdata.com/_c5070753268498432",
     "fixture": "questglobal.svg.html",
     "expect": {"root": "Quest Global Services PTE. Ltd.", "ultimate_parent": "Quest Global Engineering Solutions"},
     "note": "Five 'ultimate parent' labels on the page; resolver must land on the one current parent."},
    {"slug": "audi", "name": "AUDI AG",
     "url": "https://www.northdata.com/Audi+AG",
     "fixture": "audi.svg.html",
     "expect": {"root": "AUDI AG", "ultimate_parent": "Volkswagen"},
     "note": "Known parent Volkswagen AG — watch whether it appears as a NAMED node in the graph."},
    {"slug": "nestle_de", "name": "Nestlé Deutschland AG",
     "url": "https://www.northdata.com/Nestl%C3%A9+Deutschland+AG",
     "fixture": "nestle_de.svg.html",
     "expect": {"root": "Nestlé Deutschland AG", "ultimate_parent": "Nestlé S"},
     "note": "Known parent Nestlé S.A. — appears only as a sibling's parent, not the root's."},
]


import re

# ── Resolution cases: input is a stage-1 LLM NAME LIST (what the pipeline actually searches) ──
# These exercise the search → resolve → pick-one-target path (where the adidas→Airbus bug lived),
# NOT the parser. They run LIVE NorthData searches, so results can drift; the grade is on which
# entity we'd fetch the graph for, and that the fuzzy traps are rejected by the match-guard.
RESOLUTION_CASES = [
    {"slug": "adidas_names",
     "name": "adidas.com — stage-1 name list",
     "names": ["adidas AG", "adidas America, Inc.", "adidas"],
     "primary": "adidas AG",
     "expect": {"target": "adidas AG", "reject": ["adidas America"]},
     "note": "The LLM proposed 'adidas America, Inc.', which NorthData fuzzy-matches to Airbus/Asahi "
             "America Inc. — the guard must reject it so we graph adidas AG, not the wrong company."},
]


def _norm(s):
    return re.sub(r'[^a-z0-9]', '', (s or '').lower())


def _select_target(targets, primary):
    """Mirror of the phases_registry B selection: best name-matched entity wins; none → None."""
    matched = [t for t in targets if t.get('matched') and t.get('url')]
    if not matched:
        return None
    p = _norm(primary)

    def score(t):
        s = 100 if t.get('company_page') else 0
        b = _norm(t.get('result_name') or t.get('search_name'))
        if p and b and (p == b or p.startswith(b) or b.startswith(p)):
            s += 50
        elif p and b and (p in b or b in p):
            s += 20
        return s
    return max(matched, key=score)


def list_resolution_cases() -> list:
    return [{"slug": c["slug"], "name": c["name"], "names": c["names"],
             "primary": c["primary"], "expect": c["expect"], "note": c["note"]}
            for c in RESOLUTION_CASES]


def run_resolution_case(config: dict, slug: str) -> dict:
    """Run the LIVE search → resolve → pick path for a name list and grade the chosen graph target."""
    from tools import LookupTools
    case = next((c for c in RESOLUTION_CASES if c["slug"] == slug), None)
    if not case:
        return {"error": f"unknown resolution case '{slug}'"}
    tools = LookupTools(config)
    resolutions = []
    for nm in case["names"]:
        try:
            tools.search_northdata(nm)                      # LIVE
            r = dict(getattr(tools, "_nd_last_resolution", None) or {})
        except Exception as e:  # noqa: BLE001
            r = {"error": f"{type(e).__name__}: {e}"}
        r["search_name"] = nm
        resolutions.append(r)
    targets = [r for r in resolutions if r.get("url")]
    winner = _select_target(targets, case.get("primary") or case["names"][0])
    win_name = (winner or {}).get("result_name") or (winner or {}).get("search_name")

    exp = case["expect"]
    checks = []
    checks.append({"label": f"graph target is {exp['target']}",
                   "ok": bool(winner) and exp["target"].lower() in (win_name or "").lower(),
                   "detail": {"picked": win_name, "url": (winner or {}).get("url")}})
    for rj in exp.get("reject", []):
        r = next((x for x in resolutions if rj.lower() in (x.get("search_name") or "").lower()), None)
        checks.append({"label": f"fuzzy search '{rj}' rejected by guard",
                       "ok": bool(r) and not r.get("matched"),
                       "detail": {"resolved_to": (r or {}).get("result_name"), "matched": (r or {}).get("matched")}})
    overall = all(c["ok"] for c in checks)
    return {"slug": slug, "case": case, "ok": overall, "checks": checks,
            "resolutions": resolutions, "winner": winner, "win_name": win_name}


def _owned_by_block(flat: str) -> str:
    if "OWNED BY" not in flat:
        return ""
    return flat.split("OWNED BY", 1)[1].split("Conclusion", 1)[0]


def list_cases() -> list:
    """Lightweight list for the UI (no parsing)."""
    return [{"slug": c["slug"], "name": c["name"], "url": c["url"],
             "fixture": c["fixture"], "fixture_exists": (_FIX / c["fixture"]).exists(),
             "expect": c["expect"], "note": c["note"]} for c in CASES]


def _get(slug):
    return next((c for c in CASES if c["slug"] == slug), None)


def run_case(config: dict, slug: str) -> dict:
    """Parse the fixture with both extractors and grade against known truth."""
    from tools import LookupTools
    import northdata_structure as ns
    case = _get(slug)
    if not case:
        return {"error": f"unknown case '{slug}'"}
    fp = _FIX / case["fixture"]
    if not fp.exists():
        return {"error": f"fixture missing: {case['fixture']}", "case": case}
    html = fp.read_text()
    tools = LookupTools(config)
    flat = tools._parse_network_svg(html, case["url"])
    try:
        resolved = ns.resolve(html)
    except Exception as e:  # noqa: BLE001
        resolved = {"error": f"{type(e).__name__}: {e}"}

    exp = case["expect"]
    checks = []
    checks.append({"label": f"root is {exp['root']}",
                   "ok": exp["root"].lower() in flat.lower()})
    up = exp.get("ultimate_parent")
    if up is None:
        checks.append({"label": "TopCo — no parent above (resolver)",
                       "ok": bool(resolved.get("is_top_itself")),
                       "detail": {"resolver_ultimate_parent": resolved.get("ultimate_parent"),
                                  "is_top_itself": resolved.get("is_top_itself")}})
    else:
        in_res = up.lower() in (resolved.get("ultimate_parent") or "").lower()
        in_flat = up.lower() in _owned_by_block(flat).lower()
        checks.append({"label": f"parent '{up}' surfaced",
                       "ok": bool(in_res or in_flat),
                       "detail": {"in_resolver": in_res, "in_flat_owned_by": in_flat,
                                  "resolver_ultimate_parent": resolved.get("ultimate_parent")}})
    overall = all(c["ok"] for c in checks)
    return {"slug": slug, "case": case, "ok": overall, "checks": checks,
            "flat": flat, "resolved": resolved,
            "resolver_summary": {"ultimate_parent": resolved.get("ultimate_parent"),
                                 "is_top_itself": resolved.get("is_top_itself"),
                                 "former_parents": resolved.get("former_parents"),
                                 "has_network": resolved.get("has_network")}}
