"""
Entity Lookup — Postgres result cache (append-only history).

Every completed lookup is INSERTed as a row in entity.lookups; the "cache" is simply the
most-recent row for a (url, model). No TTL — repeat lookups return instantly until the
user hits "refresh". Extracted columns (entity_name, confidence, …) double as a queryable
lookup history. If DATABASE_URL is unset, all functions no-op (caching disabled).
"""
import json
import os
from contextlib import closing
from urllib.parse import urlparse

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except Exception:  # pragma: no cover
    psycopg2 = None

DSN = os.environ.get("DATABASE_URL")


def enabled() -> bool:
    return bool(DSN and psycopg2)


def norm_domain(url: str) -> str:
    """Canonical host for a URL — the dedup key. Strips scheme, userinfo, port and leading
    'www.', lower-cased; tolerates schemeless input (e.g. 'www.kkr.com'). So kkr.com,
    www.kkr.com and https://KKR.com/ all collapse to 'kkr.com'."""
    s = (url or "").strip().lower()
    if not s:
        return ""
    if "://" not in s:
        s = "http://" + s
    host = urlparse(s).netloc or urlparse(s).path.split("/")[0]
    host = host.split("@")[-1].split(":")[0]
    while host.startswith("www."):
        host = host[4:]
    return host


def _conn():
    return psycopg2.connect(DSN)


def ensure_schema() -> None:
    if not enabled():
        return
    with closing(_conn()) as c:
        with c.cursor() as cur:
            cur.execute("""
                CREATE SCHEMA IF NOT EXISTS entity;
                CREATE TABLE IF NOT EXISTS entity.lookups (
                    id           bigserial PRIMARY KEY,
                    url          text NOT NULL,
                    domain       text,
                    model        text,
                    entity_name  text,
                    jurisdiction text,
                    registry_id  text,
                    confidence   text,
                    cost_usd     numeric,
                    report       jsonb NOT NULL,
                    meta         jsonb,
                    progress_log jsonb,
                    created_at   timestamptz NOT NULL DEFAULT now()
                );
                CREATE INDEX IF NOT EXISTS lookups_url_model_created
                    ON entity.lookups (url, model, created_at DESC);
            """)
            # One-time, idempotent backfill: recompute the canonical domain for legacy rows so
            # historical www./schemeless entries dedupe with new ones. No-op once converged.
            try:
                cur.execute("SELECT id, url, domain FROM entity.lookups")
                for rid, url, dom in cur.fetchall():
                    nd = norm_domain(url)
                    if nd and nd != dom:
                        cur.execute("UPDATE entity.lookups SET domain=%s WHERE id=%s", (nd, rid))
            except Exception as e:                       # noqa: BLE001
                print(f"[cache] domain backfill skipped: {e}")
        c.commit()


def get_latest(url: str, model: str) -> dict | None:
    """Most-recent cached result for this (url, model), or None."""
    if not enabled():
        return None
    with closing(_conn()) as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            # dedup by canonical domain, not the exact URL string — so www.kkr.com hits
            # kkr.com's cache instead of re-running (and re-charging).
            cur.execute(
                "SELECT report, meta, progress_log, created_at "
                "FROM entity.lookups WHERE domain=%s AND model=%s "
                "ORDER BY created_at DESC LIMIT 1", (norm_domain(url), model))
            row = cur.fetchone()
    if not row:
        return None
    return {
        "report": row["report"],
        "meta": row["meta"] or {},
        "progress_log": row["progress_log"] or [],
        "cached_at": row["created_at"].isoformat() if row["created_at"] else None,
        "from_cache": True,
    }


def save(url: str, domain: str, model: str, result: dict) -> None:
    """Append a completed lookup to the history/cache."""
    if not enabled():
        return
    rep = result.get("report") or {}
    meta = result.get("meta") or {}
    rec = rep.get("recommended_entity") or {}
    dom = norm_domain(url) or domain          # always store the canonical dedup key
    with closing(_conn()) as c:
        with c.cursor() as cur:
            cur.execute(
                "INSERT INTO entity.lookups "
                "(url, domain, model, entity_name, jurisdiction, registry_id, confidence, "
                " cost_usd, report, meta, progress_log) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (url, dom, model,
                 rec.get("legal_entity_name"), rec.get("jurisdiction"), rec.get("registry_id"),
                 rep.get("confidence"), meta.get("cost_usd"),
                 json.dumps(rep), json.dumps(meta),
                 json.dumps(result.get("progress_log") or [])))
        c.commit()


def history(limit: int = 100) -> list:
    """Recent lookups, COMBINED to one row per domain (the latest run), with run_count.
    Old runs stay accessible via runs_for_domain()/get_by_id(). Falls back to the raw url
    as the group key for any legacy row whose domain is blank."""
    if not enabled():
        return []
    with closing(_conn()) as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT id, url, domain, entity_name, jurisdiction, confidence, cost_usd, "
                "       created_at, run_count FROM ("
                "  SELECT DISTINCT ON (COALESCE(NULLIF(domain,''), url)) "
                "         id, url, domain, entity_name, jurisdiction, confidence, cost_usd, created_at, "
                "         count(*) OVER (PARTITION BY COALESCE(NULLIF(domain,''), url)) AS run_count "
                "  FROM entity.lookups "
                "  ORDER BY COALESCE(NULLIF(domain,''), url), created_at DESC"
                ") t ORDER BY created_at DESC LIMIT %s", (limit,))
            return [dict(r) for r in cur.fetchall()]


def get_by_id(row_id: int) -> dict | None:
    """A specific historical run by its row id (for the /entity/<domain>/<id> permalink)."""
    if not enabled():
        return None
    with closing(_conn()) as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT id, url, domain, report, meta, progress_log, created_at "
                "FROM entity.lookups WHERE id=%s", (row_id,))
            row = cur.fetchone()
    if not row:
        return None
    return {
        "id": row["id"], "url": row["url"], "domain": row["domain"],
        "report": row["report"], "meta": row["meta"] or {},
        "progress_log": row["progress_log"] or [],
        "cached_at": row["created_at"].isoformat() if row["created_at"] else None,
        "from_cache": True,
    }


def runs_for_domain(domain: str, limit: int = 50) -> list:
    """All runs for a domain, newest first — powers the historical-run tabs."""
    if not enabled():
        return []
    with closing(_conn()) as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT id, url, entity_name, confidence, cost_usd, created_at "
                "FROM entity.lookups WHERE COALESCE(NULLIF(domain,''), url)=%s "
                "ORDER BY created_at DESC LIMIT %s", (domain, limit))
            return [dict(r) for r in cur.fetchall()]
