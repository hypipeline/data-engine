#!/usr/bin/env python3
"""
Buyer Match — sync worker. Refresh the Postgres replica from the SOURCE MySQL
(origryxd_main): upsert buyer/mandate metadata, re-embed ONLY buyers whose embedding-text
hash changed (or new), refresh the keyword set. Cheap when little changed.

Runnable two ways:
  • `python -m buyer_match.sync`            (CLI / cron)
  • run_sync(progress=cb)                    (in-process; the Sync button streams `progress`)

Source connection via BM_SRC_* env (local default: the Docker origryxd copy). Postgres via
DATABASE_URL. OpenAI key via OPENAI_API_KEY.
"""
import os
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
import pymysql

from buyer_match import service as svc
from buyer_match.backfill import (
    load_active_buyers, build_text, text_hash, vec_literal, backfill_mandates,
    EMBED_MODEL, EMBED_VERSION,
)

PG_DSN = os.environ.get("DATABASE_URL", "postgres://mergr:mergr@127.0.0.1:5433/mergr")
EMB_BATCH = 100
KW_BATCH = 500


def _src():
    return dict(
        host=os.environ.get("BM_SRC_HOST", "127.0.0.1"),
        port=int(os.environ.get("BM_SRC_PORT", "3307")),
        user=os.environ.get("BM_SRC_USER", "root"),
        password=os.environ.get("BM_SRC_PASSWORD", "rootpassword"),
        database=os.environ.get("BM_SRC_DB", "origryxd_main"),
        charset="utf8mb4",
    )


def _sync_buyers(pg, buyers, log):
    with pg.cursor() as cur:
        cur.execute("SELECT id, embedding_text_hash, (embedding IS NOT NULL) FROM buyer_match.buyers")
        existing = {r[0]: (r[1], r[2]) for r in cur.fetchall()}

    to_embed = []                                   # (id, text, hash)
    for bid, b in buyers.items():
        txt = build_text(b)
        if not txt:
            continue
        h = text_hash(txt)
        cur_hash, has_emb = existing.get(bid, (None, False))
        if cur_hash != h or not has_emb:
            to_embed.append((bid, txt, h))
    log(f"buyers: {len(buyers):,} active · {len(to_embed):,} to (re)embed")

    emb, new_hash, cost = {}, {}, 0.0
    for i in range(0, len(to_embed), EMB_BATCH):
        chunk = to_embed[i:i + EMB_BATCH]
        vecs, usage = svc.embed_batch([t for _, t, _ in chunk])
        for (bid, _, h), v in zip(chunk, vecs):
            emb[bid] = v
            new_hash[bid] = h
        cost += usage.get("total_tokens", 0) * 0.02 / 1_000_000    # 3-small: $0.02/1M
        log(f"  embedded {min(i + EMB_BATCH, len(to_embed)):,}/{len(to_embed):,}")

    ts = datetime.now(timezone.utc)
    rows = []
    for bid, b in buyers.items():
        v = emb.get(bid)
        rows.append((
            bid, b.get("name"), b.get("description"), b.get("investment_thesis"),
            b.get("sector_keywords"), b.get("website"), b.get("tags"),
            int(b.get("email_count") or 0), int(b.get("linkedin_count") or 0),
            vec_literal(v) if v else None, EMBED_MODEL if v else None, EMBED_VERSION,
            new_hash.get(bid) if v else None, ts if v else None,
        ))
    # metadata always overwritten; embedding/hash only when re-embedded (COALESCE keeps old)
    sql = """INSERT INTO buyer_match.buyers
        (id,name,description,investment_thesis,sector_keywords,website,tags,
         email_count,linkedin_count,embedding,embed_model,embed_version,embedding_text_hash,embedded_at)
        VALUES %s ON CONFLICT (id) DO UPDATE SET
         name=EXCLUDED.name, description=EXCLUDED.description, investment_thesis=EXCLUDED.investment_thesis,
         sector_keywords=EXCLUDED.sector_keywords, website=EXCLUDED.website, tags=EXCLUDED.tags,
         email_count=EXCLUDED.email_count, linkedin_count=EXCLUDED.linkedin_count,
         embedding=COALESCE(EXCLUDED.embedding, buyer_match.buyers.embedding),
         embed_model=COALESCE(EXCLUDED.embed_model, buyer_match.buyers.embed_model),
         embed_version=EXCLUDED.embed_version,
         embedding_text_hash=COALESCE(EXCLUDED.embedding_text_hash, buyer_match.buyers.embedding_text_hash),
         embedded_at=COALESCE(EXCLUDED.embedded_at, buyer_match.buyers.embedded_at),
         synced_at=now()"""
    with pg.cursor() as cur:
        psycopg2.extras.execute_values(cur, sql, rows,
            template="(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::vector,%s,%s,%s,%s)", page_size=500)
        cur.execute("SELECT id FROM buyer_match.buyers")
        gone = [r[0] for r in cur.fetchall() if r[0] not in buyers]
        if gone:
            cur.execute("DELETE FROM buyer_match.buyers WHERE id = ANY(%s)", (gone,))
    pg.commit()
    log(f"buyers: upserted {len(rows):,} · removed {len(gone):,}")
    return len(rows), len(to_embed), len(gone), cost


def _sync_keywords(pg, buyers, log):
    counts = {}
    for b in buyers.values():
        for k in (b.get("sector_keywords") or "").split(","):
            k = k.strip()
            if k:
                counts[k] = counts.get(k, 0) + 1
    with pg.cursor() as cur:
        cur.execute("SELECT keyword, (embedding IS NOT NULL) FROM buyer_match.keywords")
        existing = {r[0]: r[1] for r in cur.fetchall()}
    new_kws = [k for k in counts if k not in existing]
    log(f"keywords: {len(counts):,} unique · {len(new_kws):,} new to embed")

    emb, ts = {}, datetime.now(timezone.utc)
    for i in range(0, len(new_kws), KW_BATCH):
        chunk = new_kws[i:i + KW_BATCH]
        vecs, _ = svc.embed_batch(chunk)
        for k, v in zip(chunk, vecs):
            emb[k] = v
        log(f"  embedded {min(i + KW_BATCH, len(new_kws)):,}/{len(new_kws):,}")

    rows = [(k, vec_literal(emb[k]) if k in emb else None,
             EMBED_MODEL if k in emb else None, c, ts if k in emb else None)
            for k, c in counts.items()]
    sql = """INSERT INTO buyer_match.keywords (keyword,embedding,embed_model,buyer_count,embedded_at)
        VALUES %s ON CONFLICT (keyword) DO UPDATE SET
         embedding=COALESCE(EXCLUDED.embedding, buyer_match.keywords.embedding),
         embed_model=COALESCE(EXCLUDED.embed_model, buyer_match.keywords.embed_model),
         buyer_count=EXCLUDED.buyer_count,
         embedded_at=COALESCE(EXCLUDED.embedded_at, buyer_match.keywords.embedded_at)"""
    with pg.cursor() as cur:
        psycopg2.extras.execute_values(cur, sql, rows,
            template="(%s,%s::vector,%s,%s,%s)", page_size=500)
        gone = [k for k in existing if k not in counts]
        if gone:
            cur.execute("DELETE FROM buyer_match.keywords WHERE keyword = ANY(%s)", (gone,))
    pg.commit()
    log(f"keywords: upserted {len(rows):,} · removed {len(gone):,}")
    return len(new_kws)


def run_sync(progress=None, pg_dsn=None):
    log = progress or (lambda m: None)
    my = pymysql.connect(**_src())
    pg = psycopg2.connect(pg_dsn or PG_DSN)
    try:
        log("Loading buyers from source…")
        buyers = load_active_buyers(my)
        nb, nemb, ndel, cost = _sync_buyers(pg, buyers, log)
        nk = _sync_keywords(pg, buyers, log)
        log("Syncing mandate metadata…")
        nm = backfill_mandates(pg, my)
        with pg.cursor() as cur:
            cur.execute("""INSERT INTO buyer_match.sync_state
                (id,last_sync_at,last_buyers_upserted,last_buyers_embedded,last_keywords_embedded,last_mandates_synced,last_cost_usd)
                VALUES (1,now(),%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO UPDATE SET last_sync_at=now(),
                 last_buyers_upserted=EXCLUDED.last_buyers_upserted,
                 last_buyers_embedded=EXCLUDED.last_buyers_embedded,
                 last_keywords_embedded=EXCLUDED.last_keywords_embedded,
                 last_mandates_synced=EXCLUDED.last_mandates_synced,
                 last_cost_usd=EXCLUDED.last_cost_usd""",
                (nb, nemb, nk, nm, round(cost, 4)))
        pg.commit()
        stats = {"buyers_upserted": nb, "buyers_embedded": nemb, "buyers_removed": ndel,
                 "keywords_embedded": nk, "mandates_synced": nm, "cost_usd": round(cost, 4)}
        log(f"Done. {nemb:,} buyers re-embedded, {nk:,} new keywords, {nm:,} mandates · ${cost:.4f}")
        return stats
    finally:
        my.close()
        pg.close()


def since_counts(last_sync_at):
    """Cheap 'records changed since last sync' nudge (updated_at count in source)."""
    my = pymysql.connect(**_src())
    out = {"buyers": None, "mandates": None}
    try:
        with my.cursor() as cur:
            for key, sql in (
                ("buyers", "SELECT COUNT(*) FROM buyers WHERE deleted_at IS NULL AND updated_at > %s"),
                ("mandates", "SELECT COUNT(*) FROM opportunities WHERE deleted_at IS NULL AND updated_at > %s"),
            ):
                try:
                    cur.execute(sql, (last_sync_at,))
                    out[key] = int(cur.fetchone()[0])
                except Exception:
                    out[key] = None
    finally:
        my.close()
    return out


if __name__ == "__main__":
    run_sync(progress=print)
