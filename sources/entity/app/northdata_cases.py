"""NorthData test cases — two distinct layers.

PARSER cases (CASES / run_case): deterministic, fixture-based unit tests of the ownership-graph
extractors (flat parser + direction-aware resolver) vs known corporate-structure truth. Owned by
pytest (tests/test_northdata_cases.py); zero network I/O.

INTEGRATION cases (DB-backed, editable): input is a stage-1 LLM NAME LIST — exactly what the
pipeline feeds NorthData. run_resolution_case runs the LIVE search → resolve → pick path (where
the adidas→Airbus bug lived) and grades which entity we'd graph + that fuzzy traps are rejected by
the match-guard. These are the ones surfaced (and edited) in the UI at /entity/tools/northdata.
"""
import json
import os
import re
import pathlib
from contextlib import closing

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except Exception:  # pragma: no cover
    psycopg2 = None

# Compact SVG-only fixtures live INSIDE the app dir (Docker build context = sources/entity/app with
# `COPY . .`), so they ship in the runtime container. Both extractors key off the same
# <svg aria-label="Network"> element (see tests/fixtures/*_network.html for the full originals).
_FIX = pathlib.Path(__file__).resolve().parent / "network_fixtures"

# ── PARSER cases (pytest-owned; deterministic) ──────────────────────────────────
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


def _owned_by_block(flat: str) -> str:
    if "OWNED BY" not in flat:
        return ""
    return flat.split("OWNED BY", 1)[1].split("Conclusion", 1)[0]


def list_cases() -> list:
    return [{"slug": c["slug"], "name": c["name"], "url": c["url"],
             "fixture": c["fixture"], "fixture_exists": (_FIX / c["fixture"]).exists(),
             "expect": c["expect"], "note": c["note"]} for c in CASES]


def run_case(config: dict, slug: str) -> dict:
    """Parse the fixture with both extractors and grade against known truth (no network I/O)."""
    from tools import LookupTools
    import northdata_structure as ns
    case = next((c for c in CASES if c["slug"] == slug), None)
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
    checks = [{"label": f"root is {exp['root']}", "ok": exp["root"].lower() in flat.lower()}]
    up = exp.get("ultimate_parent")
    if up is None:
        checks.append({"label": "TopCo — no parent above (resolver)",
                       "ok": bool(resolved.get("is_top_itself")),
                       "detail": {"resolver_ultimate_parent": resolved.get("ultimate_parent"),
                                  "is_top_itself": resolved.get("is_top_itself")}})
    else:
        in_res = up.lower() in (resolved.get("ultimate_parent") or "").lower()
        in_flat = up.lower() in _owned_by_block(flat).lower()
        checks.append({"label": f"parent '{up}' surfaced", "ok": bool(in_res or in_flat),
                       "detail": {"in_resolver": in_res, "in_flat_owned_by": in_flat,
                                  "resolver_ultimate_parent": resolved.get("ultimate_parent")}})
    overall = all(c["ok"] for c in checks)
    return {"slug": slug, "case": case, "ok": overall, "checks": checks,
            "flat": flat, "resolved": resolved,
            "resolver_summary": {"ultimate_parent": resolved.get("ultimate_parent"),
                                 "is_top_itself": resolved.get("is_top_itself"),
                                 "former_parents": resolved.get("former_parents"),
                                 "has_network": resolved.get("has_network")}}


# ══ INTEGRATION cases (DB-backed & editable; LIVE search→resolve→pick) ═══════════
_RESOLUTION_SEED = [
    {"name": "adidas.com — stage-1 name list",
     "names": ["adidas AG", "adidas America, Inc.", "adidas"],
     "primary_name": "adidas AG",
     "expect_target": "adidas AG",
     "expect_reject": ["adidas America"],
     "note": "The LLM proposed 'adidas America, Inc.', which NorthData fuzzy-matches to Airbus/Asahi/"
             "Geodis America Inc. — the guard must reject it so we graph adidas AG, not the wrong company."},
]

_DSN = os.environ.get("DATABASE_URL")


def enabled() -> bool:
    return bool(_DSN and psycopg2)


def _conn():
    return psycopg2.connect(_DSN)


def ensure_schema() -> None:
    if not enabled():
        return
    with closing(_conn()) as c:
        with c.cursor() as cur:
            cur.execute("""
                CREATE SCHEMA IF NOT EXISTS entity;
                CREATE TABLE IF NOT EXISTS entity.northdata_resolution_cases (
                    id            bigserial PRIMARY KEY,
                    name          text NOT NULL,
                    names         jsonb NOT NULL DEFAULT '[]',
                    primary_name  text,
                    expect_target text,
                    expect_reject jsonb NOT NULL DEFAULT '[]',
                    note          text,
                    last_result   jsonb,
                    last_run_at   timestamptz
                );
            """)
        c.commit()
    _seed_resolution_if_empty()


def _seed_resolution_if_empty():
    try:
        if not list_resolution_cases():
            for c in _RESOLUTION_SEED:
                add_resolution_case(c)
    except Exception as e:  # noqa: BLE001
        print(f"[northdata] resolution seed skipped: {e}")


def _res_row(r):
    return {"id": r["id"], "name": r["name"], "names": r.get("names") or [],
            "primary": r.get("primary_name"), "note": r.get("note"),
            "expect": {"target": r.get("expect_target"), "reject": r.get("expect_reject") or []},
            "last_result": r.get("last_result"),
            "last_run_at": r["last_run_at"].isoformat() if r.get("last_run_at") else None}


def list_resolution_cases() -> list:
    if not enabled():
        return []
    with closing(_conn()) as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM entity.northdata_resolution_cases ORDER BY id")
            return [_res_row(r) for r in cur.fetchall()]


def get_resolution_case(cid: int):
    if not enabled():
        return None
    with closing(_conn()) as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM entity.northdata_resolution_cases WHERE id=%s", (cid,))
            r = cur.fetchone()
            return _res_row(r) if r else None


def _case_fields(case: dict):
    return (case.get("name") or "unnamed",
            json.dumps([n for n in (case.get("names") or []) if str(n).strip()]),
            case.get("primary_name") or case.get("primary"),
            case.get("expect_target") or (case.get("expect") or {}).get("target"),
            json.dumps(case.get("expect_reject") or (case.get("expect") or {}).get("reject") or []),
            case.get("note"))


def add_resolution_case(case: dict) -> int:
    with closing(_conn()) as c:
        with c.cursor() as cur:
            cur.execute(
                "INSERT INTO entity.northdata_resolution_cases "
                "(name, names, primary_name, expect_target, expect_reject, note) "
                "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id", _case_fields(case))
            cid = cur.fetchone()[0]
        c.commit()
    return cid


def update_resolution_case(cid: int, case: dict) -> None:
    """Overwrite a case in place — editing changes what pass/fail means, so clear the last result."""
    with closing(_conn()) as c:
        with c.cursor() as cur:
            cur.execute(
                "UPDATE entity.northdata_resolution_cases SET name=%s, names=%s, primary_name=%s, "
                "expect_target=%s, expect_reject=%s, note=%s, last_result=NULL, last_run_at=NULL WHERE id=%s",
                _case_fields(case) + (cid,))
        c.commit()


def delete_resolution_case(cid: int) -> None:
    with closing(_conn()) as c:
        with c.cursor() as cur:
            cur.execute("DELETE FROM entity.northdata_resolution_cases WHERE id=%s", (cid,))
        c.commit()


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


def run_resolution_case(config: dict, cid: int) -> dict:
    """Run the LIVE search → resolve → pick path for a stored name list and grade the chosen target."""
    from tools import LookupTools
    case = get_resolution_case(int(cid))
    if not case:
        return {"error": f"unknown resolution case #{cid}"}
    tools = LookupTools(config)
    resolutions = []
    for nm in case["names"]:
        try:
            raw = tools.search_northdata(nm)                # LIVE — capture the raw NorthData output
            r = dict(getattr(tools, "_nd_last_resolution", None) or {})
            r["raw_search"] = raw
        except Exception as e:  # noqa: BLE001
            r = {"error": f"{type(e).__name__}: {e}"}
        r["search_name"] = nm
        resolutions.append(r)
    targets = [r for r in resolutions if r.get("url")]
    winner = _select_target(targets, case.get("primary") or (case["names"][0] if case["names"] else ""))
    win_name = (winner or {}).get("result_name") or (winner or {}).get("search_name")

    # Fetch the SAME ownership-graph output the live workflow feeds the LLM for the graph target
    # (phases_registry: one graph, best name-matched winner). Mirrors that logging exactly.
    network_output = None
    network_summary = None
    if winner and winner.get("url"):
        try:
            network_output = tools.northdata_network(winner["url"])
            network_summary = "Network graph loaded"
            if 'appears to be the ultimate parent' in network_output or 'TopCo' in network_output:
                network_summary = "Entity appears to be the ultimate parent (TopCo)"
            elif 'ULTIMATE PARENT (current)' in network_output or 'OWNED BY' in network_output:
                network_summary = "Parent/ownership structure identified"
        except Exception as e:  # noqa: BLE001 — live fetch; never break the grading path
            network_output = f"[network fetch failed: {type(e).__name__}: {e}]"
            network_summary = "Network fetch failed"

    exp = case["expect"]
    checks = []
    if exp.get("target"):
        checks.append({"label": f"graph target is {exp['target']}",
                       "ok": bool(winner) and exp["target"].lower() in (win_name or "").lower(),
                       "detail": {"picked": win_name, "url": (winner or {}).get("url")}})
    else:
        checks.append({"label": "no graph fetched (guard should fire)", "ok": winner is None,
                       "detail": {"picked": win_name}})
    for rj in exp.get("reject") or []:
        r = next((x for x in resolutions if rj.lower() in (x.get("search_name") or "").lower()), None)
        checks.append({"label": f"fuzzy search '{rj}' rejected by guard",
                       "ok": bool(r) and not r.get("matched"),
                       "detail": {"resolved_to": (r or {}).get("result_name"), "matched": (r or {}).get("matched")}})
    overall = all(c["ok"] for c in checks)
    try:
        with closing(_conn()) as c:
            with c.cursor() as cur:
                cur.execute("UPDATE entity.northdata_resolution_cases SET last_result=%s, last_run_at=now() WHERE id=%s",
                            (json.dumps({"ok": overall, "checks": checks, "win_name": win_name}, default=str), cid))
            c.commit()
    except Exception:  # noqa: BLE001
        pass
    return {"id": cid, "case": case, "ok": overall, "checks": checks,
            "resolutions": resolutions, "winner": winner, "win_name": win_name,
            "network_output": network_output, "network_summary": network_summary}
