#!/usr/bin/env python3
"""
TDC — pull the subscriber replica from DynamoDB into Postgres.

DynamoDB is the upstream source of truth: the public sign-up Lambda writes there,
and must keep working when Data Engine is down. Postgres holds the queryable copy
(same arrangement as buyer_match's replica of origryxd_main).

Credentials: none. The EC2 instance carries an IAM role scoped to Scan/Query on
the one table, so boto3 resolves it from instance metadata. Nothing on disk.

Runnable two ways:
  * python -m tdc.sync            (CLI / cron)
  * run_sync(progress=cb)          (in-process; the Sync button streams progress)
"""
import os
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

from tdc import service as svc

PG_DSN = os.environ.get("DATABASE_URL", "postgres://mergr:mergr@127.0.0.1:5433/mergr")
TABLE = os.environ.get("TDC_SUBSCRIBERS_TABLE", "dealchronicle-subscribers")
REGION = os.environ.get("TDC_DDB_REGION", "us-east-1")
SOURCE = f"dynamodb:{TABLE}"


def _s(item, key):
    return (item.get(key) or {}).get("S")


def _ts(item, key):
    """DynamoDB stores these as ISO strings; tolerate absence and junk."""
    v = _s(item, key)
    if not v:
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None


def _ss(item, key):
    # '__none__' is the placeholder the Lambda writes for an empty string set,
    # because DynamoDB rejects empty ones.
    vals = (item.get(key) or {}).get("SS") or []
    return [v for v in vals if v != "__none__"]


def scan_subscribers():
    """Only sub# rows. The table also holds rate-limit counters (ip#, em#, global)."""
    # Imported here, not at module scope: webui imports this module at startup, and a
    # missing boto3 should break the Sync button, not the whole dashboard.
    import boto3
    ddb = boto3.client("dynamodb", region_name=REGION)
    out, kwargs = [], {"TableName": TABLE}
    while True:
        page = ddb.scan(**kwargs)
        for it in page.get("Items", []):
            pk = _s(it, "pk") or ""
            if pk.startswith("sub#"):
                out.append(it)
        if "LastEvaluatedKey" not in page:
            return out
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def run_sync(conn=None, progress=None):
    """Upsert every subscriber. Returns the sync_runs row as a dict."""
    def say(msg):
        if progress:
            progress(msg)

    own = conn is None
    conn = conn or psycopg2.connect(PG_DSN)
    svc.ensure_schema(conn)
    stats = {"scanned": 0, "inserted": 0, "updated": 0, "removed": 0}
    run_id = None
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO tdc.sync_runs (source) VALUES (%s) RETURNING id", (SOURCE,))
            run_id = cur.fetchone()[0]
        conn.commit()

        say(f"scanning {TABLE}…")
        items = scan_subscribers()
        stats["scanned"] = len(items)
        say(f"{len(items)} subscriber rows")

        seen = []
        with conn.cursor() as cur:
            for it in items:
                email = (_s(it, "email") or "").lower()
                if not email:
                    continue
                seen.append(email)
                cur.execute("""
                    INSERT INTO tdc.subscribers
                      (email, domain, status, cadence, regions, sectors,
                       created_at, confirmed_at, unsubscribed_at,
                       delivery, bounce_type, bounce_reason, sandboxed_at, complained_at,
                       unsandboxed_at, unsandboxed_by, synced_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
                    ON CONFLICT (email) DO UPDATE SET
                      domain=EXCLUDED.domain, status=EXCLUDED.status, cadence=EXCLUDED.cadence,
                      regions=EXCLUDED.regions, sectors=EXCLUDED.sectors,
                      created_at=EXCLUDED.created_at, confirmed_at=EXCLUDED.confirmed_at,
                      unsubscribed_at=EXCLUDED.unsubscribed_at,
                      delivery=EXCLUDED.delivery, bounce_type=EXCLUDED.bounce_type,
                      bounce_reason=EXCLUDED.bounce_reason, sandboxed_at=EXCLUDED.sandboxed_at,
                      complained_at=EXCLUDED.complained_at,
                      unsandboxed_at=EXCLUDED.unsandboxed_at,
                      unsandboxed_by=EXCLUDED.unsandboxed_by, synced_at=now()
                    RETURNING (xmax = 0) AS inserted
                """, (
                    email, email.split("@")[-1] or None,
                    _s(it, "status"), _s(it, "cadence"),
                    _ss(it, "regions"), _ss(it, "sectors"),
                    _ts(it, "createdAt"), _ts(it, "confirmedAt"), _ts(it, "unsubscribedAt"),
                    _s(it, "delivery") or "ok", _s(it, "bounceType") or None,
                    _s(it, "bounceReason") or None,
                    _ts(it, "sandboxedAt"), _ts(it, "complainedAt"),
                    _ts(it, "unsandboxedAt"), _s(it, "unsandboxedBy") or None,
                ))
                stats["inserted" if cur.fetchone()[0] else "updated"] += 1

            # Rows that vanished upstream. Deleting is right: a subscriber erased
            # from DynamoDB (a GDPR erasure request) must not survive in the replica.
            if seen:
                cur.execute("DELETE FROM tdc.subscribers WHERE NOT (email = ANY(%s))", (seen,))
                stats["removed"] = cur.rowcount

        with conn.cursor() as cur:
            cur.execute("""UPDATE tdc.sync_runs
                           SET finished_at=now(), scanned=%s, inserted=%s, updated=%s,
                               removed=%s, ok=TRUE
                           WHERE id=%s""",
                        (stats["scanned"], stats["inserted"], stats["updated"],
                         stats["removed"], run_id))
        conn.commit()
        say(f"done — {stats['inserted']} new, {stats['updated']} updated, {stats['removed']} removed")
        return {"ok": True, **stats}

    except Exception as e:
        conn.rollback()
        if run_id:
            with conn.cursor() as cur:
                cur.execute("UPDATE tdc.sync_runs SET finished_at=now(), ok=FALSE, error=%s WHERE id=%s",
                            (str(e)[:500], run_id))
            conn.commit()
        say(f"failed — {e}")
        raise
    finally:
        if own:
            conn.close()


if __name__ == "__main__":
    print(run_sync(progress=lambda m: print(" ", m)))
