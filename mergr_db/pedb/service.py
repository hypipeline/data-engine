"""
PE DB — read queries over the subscriber replica.

Everything here reads Postgres only; the DynamoDB pull lives in pedb.sync.
"""
import os

from psycopg2.extras import RealDictCursor

_SCHEMA = os.path.join(os.path.dirname(__file__), "schema.sql")


def ensure_schema(conn):
    """Apply pedb/schema.sql. Idempotent, and cheap enough to call on every page
    load — the compose file only mounts the root schema at database creation, so
    a sub-app has to bring its own (same approach as buyer_match.email_domains)."""
    with open(_SCHEMA, encoding="utf-8") as fh, conn.cursor() as cur:
        cur.execute(fh.read())
    conn.commit()


def _rows(conn, sql, args=None):
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, args or ())
        return cur.fetchall()


def _one(conn, sql, args=None):
    r = _rows(conn, sql, args)
    return r[0] if r else {}


def counts(conn):
    """Headline numbers. Confirm rate deliberately excludes unsubscribes — it
    measures whether people complete the double opt-in, not whether they stay."""
    r = _one(conn, """
        SELECT
          count(*)                                                  AS total,
          count(*) FILTER (WHERE status = 'confirmed')              AS confirmed,
          count(*) FILTER (WHERE status = 'pending')                AS pending,
          count(*) FILTER (WHERE status = 'unsubscribed')           AS unsubscribed,
          count(*) FILTER (WHERE created_at > now() - interval '7 days')  AS last7,
          count(*) FILTER (WHERE created_at > now() - interval '30 days') AS last30,
          count(*) FILTER (WHERE status = 'confirmed' AND sg_synced IS FALSE) AS unsynced
        FROM pedb.subscribers
    """)
    conf, pend = r.get("confirmed", 0), r.get("pending", 0)
    r["confirm_rate"] = round(100.0 * conf / (conf + pend)) if (conf + pend) else 0
    return r


def by_domain(conn, limit=50):
    """Which firms are on the list — the question a trade publication actually has."""
    return _rows(conn, """
        SELECT domain,
               count(*)                                       AS total,
               count(*) FILTER (WHERE status = 'confirmed')    AS confirmed
        FROM pedb.subscribers
        WHERE domain IS NOT NULL
        GROUP BY domain
        ORDER BY confirmed DESC, total DESC, domain
        LIMIT %s
    """, (limit,))


def by_domain_linked(conn, limit=50):
    """
    Subscriber domains matched against Mergr firms and companies.

    The point of holding subscribers in the same database as 225k companies:
    'Ardwell have four people on the list and appear in twelve transactions.'
    Left join — an unmatched domain is normal, not an error.
    """
    return _rows(conn, """
        WITH d AS (
            SELECT domain, count(*) AS subs,
                   count(*) FILTER (WHERE status = 'confirmed') AS confirmed
            FROM pedb.subscribers
            WHERE domain IS NOT NULL
            GROUP BY domain
        )
        SELECT d.domain, d.subs, d.confirmed,
               f.firm_id, f.name AS firm_name,
               c.company_id, c.name AS company_name
        FROM d
        LEFT JOIN LATERAL (
            SELECT firm_id, name FROM firms
            WHERE website IS NOT NULL AND website ILIKE '%%' || d.domain || '%%'
            LIMIT 1) f ON TRUE
        LEFT JOIN LATERAL (
            SELECT company_id, name FROM companies
            WHERE website IS NOT NULL AND website ILIKE '%%' || d.domain || '%%'
            LIMIT 1) c ON TRUE
        ORDER BY d.confirmed DESC, d.subs DESC, d.domain
        LIMIT %s
    """, (limit,))


def recent(conn, limit=50):
    return _rows(conn, """
        SELECT email, domain, status, cadence, created_at, confirmed_at, unsubscribed_at,
               sg_synced, sg_error
        FROM pedb.subscribers
        ORDER BY created_at DESC NULLS LAST
        LIMIT %s
    """, (limit,))


def signups_by_day(conn, days=30):
    """Zero-filled so a quiet day is a visible gap rather than a missing bar."""
    return _rows(conn, """
        SELECT g.day::date AS day,
               count(s.email)                                    AS signups,
               count(s.email) FILTER (WHERE s.status='confirmed') AS confirmed
        FROM generate_series(
               (now() - (%s || ' days')::interval)::date, now()::date, '1 day') AS g(day)
        LEFT JOIN pedb.subscribers s ON s.created_at::date = g.day::date
        GROUP BY g.day ORDER BY g.day
    """, (days,))


def last_sync(conn, source="dynamodb:dealchronicle-subscribers"):
    return _one(conn, """
        SELECT started_at, finished_at, scanned, inserted, updated, removed, ok, error
        FROM pedb.sync_runs WHERE source = %s
        ORDER BY started_at DESC LIMIT 1
    """, (source,))
