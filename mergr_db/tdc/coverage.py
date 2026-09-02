"""
TDC — the firms we watch for leads.

Seeded from the ON advisory_firms roster: name, website and is_active only. Notes
and contact history are relationship data and are deliberately never read.
"""
import difflib
import json
import os
import re
import urllib.parse
import urllib.request

from psycopg2.extras import RealDictCursor

ENTITY_API = os.environ.get("TDC_ENTITY_API", "http://entity:8000")


def _norm(s):
    return re.sub(r"[^a-z0-9 ]", " ", (s or "").lower()).split()


def name_match(firm, slug):
    """Firm name against the resolved slug. The Finder always answers, so this is
    what decides whether the answer is worth believing."""
    a = " ".join(_norm(firm))
    b = " ".join(_norm((slug or "").replace("-", " ")))
    if not a or not b:
        return 0.0
    return round(difflib.SequenceMatcher(None, a, b).ratio(), 2)


def find_linkedin(website):
    q = urllib.parse.quote(website, safe="")
    req = urllib.request.Request(f"{ENTITY_API}/api/linkedin?q={q}")
    with urllib.request.urlopen(req, timeout=240) as r:
        d = json.loads(r.read())
    url = d.get("linkedin_url") or d.get("url") or ""
    return url or None, d.get("employees") or d.get("employee_count")


def upsert(conn, name, website, linkedin_url, employees, origin, resolved_by, match):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO tdc.coverage
              (name, website, linkedin_url, employees, origin, resolved_by,
               name_match, needs_check)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (name) DO UPDATE SET
              website=EXCLUDED.website, linkedin_url=EXCLUDED.linkedin_url,
              employees=EXCLUDED.employees, resolved_by=EXCLUDED.resolved_by,
              name_match=EXCLUDED.name_match, needs_check=EXCLUDED.needs_check,
              updated_at=now()
        """, (name, website, linkedin_url, employees, origin, resolved_by, match,
              (match or 0) < 0.7))
    conn.commit()


def rows(conn, include_inactive=False):
    sql = """SELECT id, name, website, linkedin_url, employees, name_match,
                    needs_check, active, resolved_by
             FROM tdc.coverage {} ORDER BY needs_check DESC, name"""
    where = "" if include_inactive else "WHERE active"
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql.format(where))
        return cur.fetchall()
