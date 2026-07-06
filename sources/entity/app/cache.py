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
        c.commit()


def get_latest(url: str, model: str) -> dict | None:
    """Most-recent cached result for this (url, model), or None."""
    if not enabled():
        return None
    with closing(_conn()) as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT report, meta, progress_log, created_at "
                "FROM entity.lookups WHERE url=%s AND model=%s "
                "ORDER BY created_at DESC LIMIT 1", (url, model))
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
    with closing(_conn()) as c:
        with c.cursor() as cur:
            cur.execute(
                "INSERT INTO entity.lookups "
                "(url, domain, model, entity_name, jurisdiction, registry_id, confidence, "
                " cost_usd, report, meta, progress_log) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (url, domain, model,
                 rec.get("legal_entity_name"), rec.get("jurisdiction"), rec.get("registry_id"),
                 rep.get("confidence"), meta.get("cost_usd"),
                 json.dumps(rep), json.dumps(meta),
                 json.dumps(result.get("progress_log") or [])))
        c.commit()


def history(limit: int = 100) -> list:
    """Recent lookups (one row per run) for a history view."""
    if not enabled():
        return []
    with closing(_conn()) as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT url, domain, entity_name, jurisdiction, confidence, cost_usd, created_at "
                "FROM entity.lookups ORDER BY created_at DESC LIMIT %s", (limit,))
            return [dict(r) for r in cur.fetchall()]
