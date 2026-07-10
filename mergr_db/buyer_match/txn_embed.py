#!/usr/bin/env python3
"""
Buyer Match · Deal-history mode — one-time / incremental embedding of Mergr transactions.

Embeds each transaction's TARGET (description + sector + deal type) into
`public.transactions.target_embedding` (text-embedding-3-small, 1536-d — same model/space as the
buyer + query embeddings, so a query vector can be compared directly to a deal vector).

Idempotent & resumable: only embeds rows where target_embedding IS NULL and there is text to embed.
Re-run any time (after a Mergr sync) to catch new transactions. Build the ANN index once populated
with `ensure_txn_index()`.

  python -m buyer_match.txn_embed              # embed all pending, then build the HNSW index
  python -m buyer_match.txn_embed --limit 500  # smoke-test on a small batch first

Postgres via DATABASE_URL. OpenAI key via OPENAI_API_KEY.
"""
import os
import sys
import time

import psycopg2
import psycopg2.extras

from buyer_match import service as svc
from buyer_match.backfill import vec_literal, EMBED_MODEL

PG_DSN = os.environ.get("DATABASE_URL", "postgres://mergr:mergr@127.0.0.1:5433/mergr")
EMB_BATCH = 500          # up to 2048 inputs/request; larger batches = fewer round-trips → faster
MAX_RETRIES = 6


def _embed_retry(texts, log):
    """Embed a batch, retrying transient failures (network timeouts / rate limits) with backoff —
    a 220k run spans thousands of calls, so occasional blips must not abort the whole job."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return svc.embed_batch(texts)
        except Exception as e:                                   # noqa: BLE001
            if attempt == MAX_RETRIES:
                raise
            wait = min(2 ** attempt, 30)
            log(f"    batch failed ({type(e).__name__}); retry {attempt}/{MAX_RETRIES - 1} in {wait}s")
            time.sleep(wait)


def build_txn_text(r):
    """Deal 'fingerprint' text: target name + description, plus sector and deal type.
    Matches on the NATURE of the deal (what was bought), not who bought it."""
    name = (r.get("target_name") or "").strip()
    desc = (r.get("target_description") or "").strip()
    sector = (r.get("target_sector") or "").strip()
    ttype = (r.get("transaction_type") or "").strip()
    parts = []
    if name and desc:
        parts.append(f"{name}. {desc}")
    elif desc:
        parts.append(desc)
    elif name:
        parts.append(name)
    if sector:
        parts.append(f"Sector: {sector}")
    if ttype:
        parts.append(f"Deal type: {ttype}")
    return "\n".join(parts).strip()


def _pending(pg, limit=None):
    lim = f" LIMIT {int(limit)}" if limit else ""
    with pg.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT transaction_id, target_name, target_description, target_sector, transaction_type "
            "FROM transactions "
            "WHERE target_embedding IS NULL "
            "  AND COALESCE(target_description,'') <> '' " + lim)
        return cur.fetchall()


def embed_transactions(pg, limit=None, log=print):
    rows = _pending(pg, limit)
    todo = [(r["transaction_id"], build_txn_text(r)) for r in rows]
    todo = [(tid, t) for tid, t in todo if t]
    log(f"transactions to embed: {len(todo):,}")
    done, cost = 0, 0.0
    for i in range(0, len(todo), EMB_BATCH):
        chunk = todo[i:i + EMB_BATCH]
        vecs, usage = _embed_retry([t for _, t in chunk], log)
        payload = [(tid, vec_literal(v)) for (tid, _), v in zip(chunk, vecs)]
        with pg.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                "UPDATE transactions AS t SET target_embedding = v.emb::vector "
                "FROM (VALUES %s) AS v(tid, emb) WHERE t.transaction_id = v.tid",
                payload, template="(%s, %s)", page_size=EMB_BATCH)
        pg.commit()
        cost += usage.get("total_tokens", 0) * 0.02 / 1_000_000     # 3-small: $0.02/1M tokens
        done += len(chunk)
        log(f"  embedded {done:,}/{len(todo):,} · ${cost:.4f}")
    log(f"done · {done:,} embedded · ${cost:.4f}")
    return done, cost


def ensure_txn_index(pg, log=print):
    """HNSW cosine index for fast ANN over deal vectors (no parity constraint here, unlike buyers)."""
    with pg.cursor() as cur:
        cur.execute("SELECT count(target_embedding) FROM transactions")
        n = cur.fetchone()[0]
        if not n:
            log("no embedded transactions yet — skipping index build")
            return
        log(f"building HNSW index over {n:,} deal vectors…")
        cur.execute("CREATE INDEX IF NOT EXISTS transactions_target_embedding_hnsw "
                    "ON transactions USING hnsw (target_embedding vector_cosine_ops)")
    pg.commit()
    log("index ready")


def main():
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])
    pg = psycopg2.connect(PG_DSN)
    try:
        embed_transactions(pg, limit=limit)
        if not limit:
            ensure_txn_index(pg)
    finally:
        pg.close()


if __name__ == "__main__":
    main()
