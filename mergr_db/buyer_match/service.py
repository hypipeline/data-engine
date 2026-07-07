"""
Buyer Match — read/query service (Data Engine). Pure Postgres/pgvector; exact cosine
(sequential scan, no ANN index) for parity with the standalone NumPy tool.

Query embedding via OpenAI (text-embedding-3-small); ranking via `1 - (embedding <=> q)`,
identical math to the tool's `matrix @ q / (norms·|q|)`.
"""
import os

import httpx
import psycopg2
import psycopg2.extras

EMBED_MODEL = "text-embedding-3-small"
BUYER_COLS = ("id, name, description, investment_thesis, sector_keywords, "
              "website, tags, email_count, linkedin_count")


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


def search(conn, query_text: str, top_n: int = 500):
    """Embed the query, return top-N buyers by exact cosine similarity."""
    qv, usage = embed(query_text)
    lit = _vec(qv)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"""SELECT {BUYER_COLS}, 1 - (embedding <=> %s::vector) AS score
                FROM buyer_match.buyers
                WHERE embedding IS NOT NULL
                ORDER BY embedding <=> %s::vector
                LIMIT %s""",
            (lit, lit, top_n),
        )
        rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        r["score"] = float(r["score"])
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
    """Buyers whose sector_keywords contain ALL given keywords (mirrors the tool's AND semantics)."""
    kws = [k.strip() for k in keywords if k and k.strip()]
    if not kws:
        return []
    conds = " AND ".join(["sector_keywords ILIKE %s"] * len(kws))
    params = [f"%{k}%" for k in kws]
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SELECT {BUYER_COLS} FROM buyer_match.buyers WHERE {conds}", params)
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
