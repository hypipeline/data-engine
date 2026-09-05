"""
TDC — grouping the accounts of one transaction.

A dossier is every account we hold of one deal: a firm's LinkedIn post, its own
write-up, and in time the other side's adviser saying the same thing from the
opposite direction. Members are never merged into one another — the dossier is an
edge set, so a wrong grouping is undone by removing an edge rather than by
reconstructing what was overwritten.

Rules live in tdc.merge_rule and are expected to multiply. Each membership records
which rule admitted it, so a rule can be measured, and retired, on its own.
"""
import re
from datetime import timedelta

from psycopg2.extras import RealDictCursor

SUFFIXES = r"\b(ltd|limited|llc|l\.l\.c|inc|incorporated|plc|gmbh|ag|nv|bv|sa|spa|" \
           r"group|holdings?|company|co|llp|lp|partners)\b"
PAIR_WINDOW = timedelta(days=90)


def norm_name(s):
    """Company names to a comparable form. Legal suffixes go: 'The Peakstone Group'
    and 'The Peakstone Group, LLC' are one firm, and keeping the suffix makes them
    two."""
    s = re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())
    s = re.sub(SUFFIXES, " ", s)
    return " ".join(s.split())


def norm_title(title, firm=None):
    """Titles minus the publisher's own name, hashtags and site furniture. What is
    left is the deal, which is what should match across two channels."""
    t = re.sub(r"#\w+", " ", title or "")
    t = re.sub(r"\s*\|\s*.*$", "", t)              # "… | The Peakstone Group"
    t = re.sub(r"[^a-z0-9 ]", " ", t.lower())
    if firm:
        for w in norm_name(firm).split():
            t = re.sub(rf"\b{re.escape(w)}\b", " ", t)
    return " ".join(t.split())


def _items(conn, version="v5"):
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT i.id, i.coverage_id, c.name AS firm, i.channel, i.title, i.url,
                   i.outlink, i.published_at, s.acquirer, s.target, s.headline,
                   s.is_deal, s.clarity
            FROM tdc.scan_item i
            JOIN tdc.coverage c ON c.id = i.coverage_id
            LEFT JOIN tdc.item_summary s
                   ON s.scan_item_id = i.id AND s.prompt_version = %s
            ORDER BY i.id""", (version,))
        return cur.fetchall()


def enabled_rules(conn):
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM tdc.merge_rule ORDER BY code")
        return cur.fetchall()


def _rejected(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT item_a, item_b FROM tdc.dossier_reject")
        return {tuple(sorted(r)) for r in cur.fetchall()}


def candidate_pairs(conn, version="v5"):
    """Every pair the enabled rules join, with the rule that did it.

    Only items a model read as a deal are considered. An unread item has nothing to
    match on but its title, and a page that is not a deal has no business being
    grouped with one.
    """
    rules = {r["code"]: r for r in enabled_rules(conn) if r["enabled"]}
    rejected = _rejected(conn)
    rows = [r for r in _items(conn, version) if r["is_deal"]]

    for r in rows:
        r["_t"] = norm_title(r["title"], r["firm"])
        r["_pair"] = ({norm_name(r["acquirer"]), norm_name(r["target"])}
                      if r["acquirer"] and r["target"] else None)

    out = []
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            if tuple(sorted((a["id"], b["id"]))) in rejected:
                continue
            code = None
            if "R1" in rules and a["outlink"] and a["outlink"] == b["outlink"]:
                code = "R1"
            elif "R2" in rules and ((a["outlink"] and a["outlink"] == b["url"])
                                    or (b["outlink"] and b["outlink"] == a["url"])):
                code = "R2"
            elif "R3" in rules and a["_t"] and a["_t"] == b["_t"]:
                code = "R3"
            elif "R5" in rules and a["_pair"] and a["_pair"] == b["_pair"]:
                # A date window only where both are dated; an undated item is not
                # evidence of a different deal.
                if (a["published_at"] and b["published_at"]
                        and abs(a["published_at"] - b["published_at"]) > PAIR_WINDOW):
                    continue
                code = "R5"
            if code:
                out.append({"a": a["id"], "b": b["id"], "rule": code,
                            "verdict": rules[code]["verdict"]})
    return out


def rebuild(conn, version="v5"):
    """Recompute every dossier from the certain pairs.

    Transitivity is allowed only through certain rules — a review-level link joining
    two groups is how one bad pair silently swallows a hundred unrelated deals.
    """
    pairs = [p for p in candidate_pairs(conn, version) if p["verdict"] == "certain"]

    parent, why = {}, {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for p in pairs:
        ra, rb = find(p["a"]), find(p["b"])
        if ra != rb:
            parent[rb] = ra
            why[p["b"]] = (p["rule"], p["a"])

    groups = {}
    for node in list(parent):
        groups.setdefault(find(node), []).append(node)
    groups = {k: v for k, v in groups.items() if len(v) > 1}

    with conn.cursor() as cur:
        cur.execute("DELETE FROM tdc.dossier_member")
        cur.execute("DELETE FROM tdc.dossier WHERE status = 'open'")
        made = 0
        for members in groups.values():
            cur.execute("INSERT INTO tdc.dossier DEFAULT VALUES RETURNING id")
            did = cur.fetchone()[0]
            made += 1
            for m in sorted(members):
                rule, against = why.get(m, (None, None))
                cur.execute("""INSERT INTO tdc.dossier_member
                                 (dossier_id, scan_item_id, rule_code, matched_item_id)
                               VALUES (%s,%s,%s,%s)""", (did, m, rule, against))
    conn.commit()
    return {"pairs": len(pairs), "dossiers": made,
            "items_grouped": sum(len(v) for v in groups.values())}


def dossiers(conn, version="v5"):
    """One row per dossier: how many accounts, how many distinct publishers, and
    whether they disagree. Publishers rather than accounts, because a firm's post
    and its own article are one voice saying a thing twice."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT d.id, d.status, d.created_at,
                   count(*)                          AS members,
                   count(DISTINCT i.coverage_id)     AS publishers,
                   -- normalised the same way the field grid normalises, or the badge
                   -- calls "The Peakstone Group" and "The Peakstone Group, LLC" a
                   -- disagreement while the grid, correctly, does not
                   count(DISTINCT tdc.norm_co(s.acquirer)) FILTER (WHERE s.acquirer IS NOT NULL)
                                                     AS acquirer_variants,
                   count(DISTINCT tdc.norm_co(s.target))   FILTER (WHERE s.target IS NOT NULL)
                                                     AS target_variants,
                   min(i.published_at)               AS first_seen,
                   max(i.published_at)               AS last_seen,
                   (array_agg(s.headline ORDER BY i.published_at DESC NULLS LAST))[1]
                                                     AS headline
            FROM tdc.dossier d
            JOIN tdc.dossier_member m ON m.dossier_id = d.id
            JOIN tdc.scan_item i ON i.id = m.scan_item_id
            LEFT JOIN tdc.item_summary s
                   ON s.scan_item_id = i.id AND s.prompt_version = %s
            GROUP BY d.id, d.status, d.created_at
            ORDER BY max(i.published_at) DESC NULLS LAST, d.id DESC""", (version,))
        return cur.fetchall()


FIELDS = ["acquirer", "target", "vendor", "consideration", "revenue", "ebitda",
          "date_hint", "sector"]


def detail(conn, dossier_id, version="v5"):
    """The members side by side, and a field grid where they can be compared. The
    grid is the point: a disagreement between two accounts is the most interesting
    thing a dossier contains, and it is invisible in any single one of them."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT m.rule_code, m.matched_item_id, i.*, c.name AS firm,
                   s.headline, s.summary, s.acquirer, s.target, s.vendor,
                   s.consideration, s.revenue, s.ebitda, s.date_hint, s.date_basis,
                   s.sector, s.advisers, s.people, s.clarity,
                   s.model, s.prompt_version, s.output_text, s.input_text,
                   s.system_prompt, s.finish_reason, s.response_meta,
                   s.prompt_tokens, s.completion_tokens, s.cost_usd, s.cost_source,
                   s.latency_ms, s.ok AS read_ok, s.error AS read_error
            FROM tdc.dossier_member m
            JOIN tdc.scan_item i ON i.id = m.scan_item_id
            JOIN tdc.coverage c ON c.id = i.coverage_id
            LEFT JOIN tdc.item_summary s
                   ON s.scan_item_id = i.id AND s.prompt_version = %s
            WHERE m.dossier_id = %s
            ORDER BY i.published_at DESC NULLS LAST, i.id""", (version, dossier_id))
        members = cur.fetchall()

    # Every field, always — including the ones no account fills in. Unknown is a
    # value, not a gap: hiding the consideration row because nobody disclosed a
    # price makes the most common fact about a mid-market deal invisible.
    grid = []
    for f in FIELDS:
        cells = [(m["id"], m[f]) for m in members]
        present = [v for _, v in cells if v]
        agree = len({norm_name(v) if f in ("acquirer", "target", "vendor") else v.lower()
                     for v in present}) <= 1
        # "cells", not "values": Jinja resolves attributes before keys, so a key
        # named after a dict method renders the method. Same trap that printed
        # "<built-in method items>" across the coverage screen.
        grid.append({"field": f, "cells": cells, "present": bool(present), "agree": agree})
    # Rows that are not simple fields, so the table is the whole reading rather than
    # the part that happens to be scalar.
    def cells_of(fn):
        return [(m["id"], fn(m)) for m in members]

    def advisers_of(m):
        out = []
        for a in (m.get("advisers") or []):
            if isinstance(a, dict) and a.get("firm"):
                bits = [a["firm"]]
                if a.get("service"):
                    bits.append(a["service"])
                bits.append(f"{a['side']}-side" if a.get("side") else "side unknown")
                out.append(" · ".join(bits))
        return "; ".join(out)

    def people_of(m):
        return "; ".join(
            f"{p.get('name')}" + (f" ({p['role']})" if p.get("role") else "")
            for p in (m.get("people") or []) if isinstance(p, dict) and p.get("name"))

    extra = [
        ("advisers", advisers_of),
        ("people", people_of),
        ("clarity", lambda m: m.get("clarity")),
        ("published", lambda m: m["published_at"].strftime("%d %b %Y")
                                if m.get("published_at") else None),
        ("date basis", lambda m: m.get("date_basis")),
    ]
    for label, fn in extra:
        cells = cells_of(fn)
        present = [v for _, v in cells if v]
        grid.append({"field": label, "cells": cells, "present": bool(present),
                     "agree": len({str(v).lower() for v in present}) <= 1})

    return members, grid
