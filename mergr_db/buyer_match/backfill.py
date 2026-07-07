#!/usr/bin/env python3
"""
Buyer Match — one-time backfill of the Postgres replica from existing artefacts.

Loads buyer/mandate metadata from the SOURCE MySQL (origryxd_main) and pairs each buyer
with its EXISTING on-disk embedding (embeddings/buyers/{id}.json) — exact-parity vectors,
no OpenAI cost. Keywords come from the on-disk keyword_embeddings.json. Idempotent (upsert).

Env (local defaults):
  DATABASE_URL              postgres://mergr:mergr@127.0.0.1:5433/mergr   (Data Engine PG)
  BM_SRC_HOST/PORT/USER/PASSWORD/DB   127.0.0.1 / 3307 / root / rootpassword / origryxd_main
  BM_EMB_DIR   /Users/craiganderson/Dropbox/dev/on-testing/embeddings/buyers
  BM_KW_FILE   /Users/craiganderson/Dropbox/dev/on-testing/embeddings/keyword_embeddings.json
"""
import hashlib
import json
import os
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
import pymysql

EMBED_MODEL = "text-embedding-3-small"
EMBED_VERSION = 1
PG_DSN = os.environ.get("DATABASE_URL", "postgres://mergr:mergr@127.0.0.1:5433/mergr")
SRC = dict(
    host=os.environ.get("BM_SRC_HOST", "127.0.0.1"),
    port=int(os.environ.get("BM_SRC_PORT", "3307")),
    user=os.environ.get("BM_SRC_USER", "root"),
    password=os.environ.get("BM_SRC_PASSWORD", "rootpassword"),
    database=os.environ.get("BM_SRC_DB", "origryxd_main"),
    charset="utf8mb4",
)
EMB_DIR = os.environ.get("BM_EMB_DIR", "/Users/craiganderson/Dropbox/dev/on-testing/embeddings/buyers")
KW_FILE = os.environ.get("BM_KW_FILE", "/Users/craiganderson/Dropbox/dev/on-testing/embeddings/keyword_embeddings.json")


def build_text(b):
    """EXACT parity with backfill_embeddings.build_text: description \\n\\n thesis \\n\\n keywords."""
    parts = [b.get("description"), b.get("investment_thesis"), b.get("sector_keywords")]
    return "\n\n".join(p for p in parts if p)


def text_hash(txt):
    return hashlib.sha256(f"v{EMBED_VERSION}:{txt}".encode("utf-8")).hexdigest()


def vec_literal(emb):
    return "[" + ",".join(repr(float(x)) for x in emb) + "]"


def load_active_buyers(my):
    """Buyer metadata slice, mirroring match_server.py's startup load."""
    with my.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute("SELECT id, name, description, investment_thesis, sector_keywords, website, "
                    "no_of_employees FROM buyers WHERE deleted_at IS NULL")
        buyers = {r["id"]: r for r in cur.fetchall()}
        cur.execute("SELECT id, name FROM tags")
        tag_names = {r["id"]: r["name"] for r in cur.fetchall()}
        cur.execute("SELECT buyer_id, tag_id FROM buyer_tag")
        tags = {}
        for r in cur.fetchall():
            if r["buyer_id"] in buyers and r["tag_id"] in tag_names:
                tags.setdefault(r["buyer_id"], []).append(tag_names[r["tag_id"]])
        for bid, tl in tags.items():
            buyers[bid]["tags"] = ", ".join(tl)
        cur.execute("""SELECT buyer_id,
              SUM(CASE WHEN email IS NOT NULL AND email!='' THEN 1 ELSE 0 END) AS ec,
              SUM(CASE WHEN linkedin_url IS NOT NULL AND linkedin_url!='' THEN 1 ELSE 0 END) AS lc
            FROM buyer_contacts WHERE deleted_at IS NULL AND sandboxed != 1 GROUP BY buyer_id""")
        for r in cur.fetchall():
            if r["buyer_id"] in buyers:
                buyers[r["buyer_id"]]["email_count"] = int(r["ec"] or 0)
                buyers[r["buyer_id"]]["linkedin_count"] = int(r["lc"] or 0)
    return buyers


def backfill_buyers(pg, buyers):
    ts = datetime.now(timezone.utc)
    rows, with_emb = [], 0
    for bid, b in buyers.items():
        emb = None
        fpath = os.path.join(EMB_DIR, f"{bid}.json")
        if os.path.exists(fpath):
            try:
                with open(fpath) as f:
                    emb = json.load(f).get("embedding")
            except Exception:
                emb = None
        txt = build_text(b)
        rows.append((
            bid, b.get("name"), b.get("description"), b.get("investment_thesis"),
            b.get("sector_keywords"), b.get("website"), b.get("tags"),
            int(b.get("email_count") or 0), int(b.get("linkedin_count") or 0),
            b.get("no_of_employees"),
            vec_literal(emb) if emb else None,
            EMBED_MODEL if emb else None, EMBED_VERSION,
            text_hash(txt) if emb else None,
            ts if emb else None,
        ))
        if emb:
            with_emb += 1
    sql = """INSERT INTO buyer_match.buyers
        (id,name,description,investment_thesis,sector_keywords,website,tags,
         email_count,linkedin_count,no_of_employees,embedding,embed_model,embed_version,embedding_text_hash,embedded_at)
        VALUES %s
        ON CONFLICT (id) DO UPDATE SET
         name=EXCLUDED.name, description=EXCLUDED.description, investment_thesis=EXCLUDED.investment_thesis,
         sector_keywords=EXCLUDED.sector_keywords, website=EXCLUDED.website, tags=EXCLUDED.tags,
         email_count=EXCLUDED.email_count, linkedin_count=EXCLUDED.linkedin_count,
         no_of_employees=EXCLUDED.no_of_employees,
         embedding=EXCLUDED.embedding, embed_model=EXCLUDED.embed_model, embed_version=EXCLUDED.embed_version,
         embedding_text_hash=EXCLUDED.embedding_text_hash, embedded_at=EXCLUDED.embedded_at,
         synced_at=now()"""
    with pg.cursor() as cur:
        psycopg2.extras.execute_values(cur, sql, rows,
            template="(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::vector,%s,%s,%s,%s)", page_size=500)
    pg.commit()
    return len(rows), with_emb


def backfill_keywords(pg, buyers):
    counts = {}
    for b in buyers.values():
        for k in (b.get("sector_keywords") or "").split(","):
            k = k.strip()
            if k:
                counts[k] = counts.get(k, 0) + 1
    with open(KW_FILE) as f:
        kw_emb = json.load(f)
    rows = [(k, vec_literal(e), EMBED_MODEL, counts.get(k, 0)) for k, e in kw_emb.items()]
    sql = """INSERT INTO buyer_match.keywords (keyword,embedding,embed_model,buyer_count,embedded_at)
        VALUES %s ON CONFLICT (keyword) DO UPDATE SET
         embedding=EXCLUDED.embedding, embed_model=EXCLUDED.embed_model,
         buyer_count=EXCLUDED.buyer_count, embedded_at=now()"""
    with pg.cursor() as cur:
        psycopg2.extras.execute_values(cur, sql, rows,
            template="(%s,%s::vector,%s,%s,now())", page_size=500)
    pg.commit()
    return len(rows)


def _json_or_none(v):
    if v is None:
        return None
    try:
        return json.dumps(json.loads(v) if isinstance(v, str) else v)
    except Exception:
        return None


def backfill_mandates(pg, my):
    total = 0
    for table, active_only in (("opportunities", True), ("bs_opportunities", False)):
        with my.cursor(pymysql.cursors.DictCursor) as cur:
            where = "WHERE deleted_at IS NULL" if active_only else ""
            cur.execute(f"SELECT * FROM {table} {where}")
            src = cur.fetchall()
        rows = [(
            r.get("id"), table, r.get("code"), r.get("project_name"), r.get("company_name"),
            r.get("summary"), _json_or_none(r.get("points")), r.get("points_paragraph_top"),
            _json_or_none(r.get("documents")), r.get("status"),
        ) for r in src]
        sql = """INSERT INTO buyer_match.mandates
            (id,source_table,code,project_name,company_name,summary,points,points_paragraph_top,documents,status)
            VALUES %s ON CONFLICT (source_table,id) DO UPDATE SET
             code=EXCLUDED.code, project_name=EXCLUDED.project_name, company_name=EXCLUDED.company_name,
             summary=EXCLUDED.summary, points=EXCLUDED.points, points_paragraph_top=EXCLUDED.points_paragraph_top,
             documents=EXCLUDED.documents, status=EXCLUDED.status, synced_at=now()"""
        with pg.cursor() as cur:
            psycopg2.extras.execute_values(cur, sql, rows,
                template="(%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s)", page_size=500)
        pg.commit()
        total += len(rows)
    return total


def main():
    print("connecting…")
    my = pymysql.connect(**SRC)
    pg = psycopg2.connect(PG_DSN)
    try:
        buyers = load_active_buyers(my)
        print(f"active buyers in source: {len(buyers)}")
        n, ne = backfill_buyers(pg, buyers)
        print(f"buyers upserted: {n} ({ne} with embedding)")
        nk = backfill_keywords(pg, buyers)
        print(f"keywords upserted: {nk}")
        nm = backfill_mandates(pg, my)
        print(f"mandates upserted: {nm}")
        with pg.cursor() as cur:
            cur.execute("""INSERT INTO buyer_match.sync_state
                (id,last_sync_at,last_buyers_upserted,last_buyers_embedded,last_keywords_embedded,last_mandates_synced,last_cost_usd)
                VALUES (1,now(),%s,%s,%s,%s,0)
                ON CONFLICT (id) DO UPDATE SET last_sync_at=now(),
                 last_buyers_upserted=EXCLUDED.last_buyers_upserted,
                 last_buyers_embedded=EXCLUDED.last_buyers_embedded,
                 last_keywords_embedded=EXCLUDED.last_keywords_embedded,
                 last_mandates_synced=EXCLUDED.last_mandates_synced""", (n, ne, nk, nm))
        pg.commit()
        print("done.")
    finally:
        my.close()
        pg.close()


if __name__ == "__main__":
    main()
