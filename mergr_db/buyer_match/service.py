"""
Buyer Match — read/query service (Data Engine). Pure Postgres/pgvector; exact cosine
(sequential scan, no ANN index) for parity with the standalone NumPy tool.

Query embedding via OpenAI (text-embedding-3-small); ranking via `1 - (embedding <=> q)`,
identical math to the tool's `matrix @ q / (norms·|q|)`.
"""
import hashlib
import os

import httpx
import psycopg2
import psycopg2.extras

EMBED_MODEL = "text-embedding-3-small"
# no_of_employees falls back to the enriched LinkedIn cache when the buyer has none of its own
# (buyer_match.effective_employees). Transparent to callers — the column is still no_of_employees.
BUYER_COLS = ("id, name, description, investment_thesis, sector_keywords, "
              "website, tags, email_count, linkedin_count, is_specialist, specific_matching_criteria, "
              "buyer_match.effective_employees(b.id, b.no_of_employees) AS no_of_employees")
# Mergr attributes from the precomputed buyer_mergr link (fast PK join). All NULL when the
# buyer isn't in Mergr. Keys kept as firm_* for the UI even for company matches (kind tells them apart).
FIRM_COLS = ("bm.kind AS mergr_kind, bm.firm_id AS firm_id, bm.company_id AS company_id, "
             "bm.size_category AS firm_size, bm.aum AS firm_aum, "
             "bm.acquisitions AS firm_acquisitions, bm.largest AS firm_largest, "
             "bm.geographies AS geographies, bm.acquired_countries AS acquired_countries")


def _openai_key():
    k = os.environ.get("OPENAI_API_KEY")
    if not k:
        raise RuntimeError("OPENAI_API_KEY not set")
    return k


def embed(text: str):
    """OpenAI embedding for a query (deterministic — same math as the tool)."""
    r = httpx.post(
        "https://api.openai.com/v1/embeddings",
        headers={"Authorization": f"Bearer {_openai_key()}", "Content-Type": "application/json"},
        json={"model": EMBED_MODEL, "input": text},
        timeout=30,
    )
    r.raise_for_status()
    d = r.json()
    return d["data"][0]["embedding"], d.get("usage", {})


def embed_batch(texts):
    """Batch embeddings (sync re-embed). Returns (list-of-vectors in input order, usage)."""
    r = httpx.post(
        "https://api.openai.com/v1/embeddings",
        headers={"Authorization": f"Bearer {_openai_key()}", "Content-Type": "application/json"},
        json={"model": EMBED_MODEL, "input": texts},
        timeout=120,
    )
    r.raise_for_status()
    d = r.json()
    vecs = [it["embedding"] for it in sorted(d["data"], key=lambda x: x["index"])]
    return vecs, d.get("usage", {})


def _vec(emb) -> str:
    return "[" + ",".join(repr(float(x)) for x in emb) + "]"


def _query_hash(text: str) -> str:
    return hashlib.sha256((EMBED_MODEL + "\n" + (text or "").strip()).encode("utf-8")).hexdigest()


def search(conn, query_text: str, top_n: int = 500):
    """Top-N buyers by exact cosine. Reuses a cached query embedding when the same
    search text was embedded before (skips the OpenAI call)."""
    h = _query_hash(query_text)
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM buyer_match.query_cache WHERE query_hash=%s", (h,))
        cached = cur.fetchone() is not None

    usage = {}
    if cached:
        with conn.cursor() as cur:
            cur.execute("UPDATE buyer_match.query_cache SET hits=hits+1, last_used_at=now() "
                        "WHERE query_hash=%s", (h,))
        conn.commit()
    else:
        qv, usage = embed(query_text)                       # the only external cost
        with conn.cursor() as cur:
            cur.execute("INSERT INTO buyer_match.query_cache (query_hash, query_text, model, embedding) "
                        "VALUES (%s,%s,%s,%s::vector) ON CONFLICT (query_hash) DO NOTHING",
                        (h, (query_text or "")[:4000], EMBED_MODEL, _vec(qv)))
        conn.commit()

    # rank straight off the stored vector (subquery — no round-tripping the embedding)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"""SELECT {BUYER_COLS}, {FIRM_COLS}, 1 - (b.embedding <=> q.e) AS score
                FROM buyer_match.buyers b
                LEFT JOIN buyer_match.buyer_mergr bm ON bm.buyer_id = b.id,
                     (SELECT embedding e FROM buyer_match.query_cache WHERE query_hash=%s) q
                WHERE b.embedding IS NOT NULL
                ORDER BY b.embedding <=> q.e
                LIMIT %s""",
            (h, top_n),
        )
        rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        r["score"] = float(r["score"])
    usage = dict(usage or {})
    usage["cached"] = cached
    return rows, usage


def similar_keywords(conn, keyword: str, top_n: int = 20, min_score: float = 0.7):
    """Keywords most similar to `keyword` by exact cosine (excludes itself)."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT embedding FROM buyer_match.keywords WHERE keyword=%s", (keyword,))
        row = cur.fetchone()
        if not row or row["embedding"] is None:
            return []
        cur.execute(
            """SELECT keyword, buyer_count AS count, 1 - (embedding <=> %s::vector) AS score
               FROM buyer_match.keywords
               WHERE keyword <> %s
               ORDER BY embedding <=> %s::vector
               LIMIT %s""",
            (row["embedding"], keyword, row["embedding"], top_n),
        )
        out = []
        for r in cur.fetchall():
            s = float(r["score"])
            if s < min_score:
                break
            out.append({"keyword": r["keyword"], "score": s, "count": int(r["count"] or 0)})
    return out


def keyword_counts(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT keyword, buyer_count FROM buyer_match.keywords ORDER BY buyer_count DESC")
        return {k: int(c) for k, c in cur.fetchall()}


def keyword_buyers(conn, keywords):
    """Buyers whose sector_keywords contain ANY of the given keywords — UNION, exact
    (comma-split) membership. Mirrors the tool's keyword_index (union, exact match)."""
    kws = [k.strip() for k in keywords if k and k.strip()]
    if not kws:
        return []
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"""SELECT {BUYER_COLS}, {FIRM_COLS} FROM buyer_match.buyers b
                LEFT JOIN buyer_match.buyer_mergr bm ON bm.buyer_id = b.id
                WHERE EXISTS (
                    SELECT 1 FROM unnest(string_to_array(b.sector_keywords, ',')) AS kw
                    WHERE btrim(kw) = ANY(%s))""",
            (kws,))
        return [dict(r) for r in cur.fetchall()]


def buyers_by_ids(conn, ids):
    """Full buyer rows (same cols as search, incl. description + Mergr) for a set of ids —
    used by the tag page to always show fresh data regardless of when a buyer was tagged."""
    if not ids:
        return []
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"""SELECT {BUYER_COLS}, {FIRM_COLS} FROM buyer_match.buyers b
                LEFT JOIN buyer_match.buyer_mergr bm ON bm.buyer_id = b.id
                WHERE b.id = ANY(%s)""",
            (list(ids),))
        return [dict(r) for r in cur.fetchall()]


def list_mandates(conn):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """SELECT id, source_table, code, project_name, company_name, summary, status
               FROM buyer_match.mandates ORDER BY id DESC""")
        out = []
        for r in cur.fetchall():
            co = f" ({r['company_name']})" if r.get("company_name") else ""
            inactive = "[Inactive] " if r.get("status") != 1 else ""
            bs = "[BS] " if r["source_table"] == "bs_opportunities" else ""
            out.append({
                "id": r["id"], "code": r["code"], "table": r["source_table"],
                "label": f"{inactive}{bs}{r['project_name']}{co} — {r['summary']} ({r['code']})",
            })
    return out
