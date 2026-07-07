"""
LinkedIn Finder — Postgres result cache (append-only history).

Every completed LinkedIn lookup is INSERTed as a row in linkedin.companies; the "cache" is
the most-recent row for a normalized query. No TTL — repeat lookups return instantly until
the user hits "refresh" (Bright Data SERP + Web Unlocker calls cost per request, so caching
matters). `employees` is stored as its own column — the headline figure the tool is after —
alongside the full structured payload in `data` (jsonb). If DATABASE_URL is unset, no-op.
"""
import json
import os
from contextlib import closing

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except Exception:  # pragma: no cover
    psycopg2 = None

DSN = os.environ.get("DATABASE_URL")


def enabled() -> bool:
    return bool(DSN and psycopg2)


def _conn():
    return psycopg2.connect(DSN)


def ensure_schema() -> None:
    if not enabled():
        return
    with closing(_conn()) as c:
        with c.cursor() as cur:
            cur.execute("""
                CREATE SCHEMA IF NOT EXISTS linkedin;
                CREATE TABLE IF NOT EXISTS linkedin.companies (
                    id           bigserial PRIMARY KEY,
                    query        text NOT NULL,       -- normalized user query (domain or name)
                    linkedin_url text,
                    name         text,
                    employees    int,                 -- LD+JSON numberOfEmployees.value
                    website      text,
                    address      text,
                    yahoo_ticker text,
                    data         jsonb NOT NULL,       -- full structured payload (all we could get)
                    created_at   timestamptz NOT NULL DEFAULT now()
                );
                CREATE INDEX IF NOT EXISTS companies_query_created
                    ON linkedin.companies (query, created_at DESC);
            """)
        c.commit()


def get_latest(query: str) -> dict | None:
    """Most-recent cached result for this normalized query, or None."""
    if not enabled():
        return None
    with closing(_conn()) as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT data, created_at FROM linkedin.companies "
                "WHERE query=%s ORDER BY created_at DESC LIMIT 1", (query,))
            row = cur.fetchone()
    if not row:
        return None
    data = dict(row["data"] or {})
    data["from_cache"] = True
    data["cached_at"] = row["created_at"].isoformat() if row["created_at"] else None
    return data


def save(query: str, data: dict) -> None:
    """Append a completed lookup to the history/cache."""
    if not enabled():
        return
    with closing(_conn()) as c:
        with c.cursor() as cur:
            cur.execute(
                "INSERT INTO linkedin.companies "
                "(query, linkedin_url, name, employees, website, address, yahoo_ticker, data) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (query, data.get("linkedin_url"), data.get("name"), data.get("employees"),
                 data.get("website"), data.get("address"), data.get("yahoo_ticker"),
                 json.dumps(data)))
        c.commit()


def history(limit: int = 100) -> list:
    """Recent lookups (one row per run) for a history view."""
    if not enabled():
        return []
    with closing(_conn()) as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT query, name, employees, linkedin_url, website, created_at "
                "FROM linkedin.companies ORDER BY created_at DESC LIMIT %s", (limit,))
            out = []
            for r in cur.fetchall():
                r = dict(r)
                if r.get("created_at"):
                    r["created_at"] = r["created_at"].isoformat()
                out.append(r)
            return out
