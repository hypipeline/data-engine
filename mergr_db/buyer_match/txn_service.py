"""
Buyer Match · Deal-history mode — query layer.

Ranks buyers by REVEALED preference: find the historical transactions whose target most resembles
the query (exact deal-fingerprint cosine over `transactions.target_embedding`, ANN index), then roll
those deals up to the acquirers behind them:

  • ON tier        — acquirers that map to one of your ON buyers (via buyer_match.buyer_mergr).
  • Discovery tier — strong acquirers NOT in your buyer set ("buyers you don't hold yet").

Each buyer/acquirer carries its comparable deals as evidence. Default sort = breadth (# comparable
deals) then best single-deal similarity. Query embedding reuses the shared buyer_match.query_cache.
"""
import psycopg2.extras

from buyer_match import service as svc

TXN_TOP = 500          # similar transactions pulled for the roll-up
MAX_DEALS = 6          # precedent deals kept per buyer/acquirer (evidence)


def _ensure_query_embedding(conn, query_text):
    """Embed the query (or reuse the cached vector); returns (query_hash, usage). Same cache and
    math as buyer thesis search, so a query maps into the same 1536-d space as the deal vectors."""
    h = svc._query_hash(query_text)
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
        qv, usage = svc.embed(query_text)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO buyer_match.query_cache (query_hash, query_text, model, embedding) "
                        "VALUES (%s,%s,%s,%s::vector) ON CONFLICT (query_hash) DO NOTHING",
                        (h, (query_text or "")[:4000], svc.EMBED_MODEL, svc._vec(qv)))
        conn.commit()
    usage = dict(usage or {})
    usage["cached"] = cached
    return h, usage


def _deal(r):
    return {
        "transaction_id": r["transaction_id"],
        "date": str(r["date"]) if r.get("date") else None,
        "type": r.get("transaction_type"),
        "target": r.get("target_name"),
        "sector": r.get("target_sector"),
        "value": float(r["deal_value"]) if r.get("deal_value") is not None else None,
        "value_ccy": r.get("deal_value_currency"),
        "url": r.get("transaction_url"),
        "score": float(r["score"]),
    }


def search_transactions(conn, query_text, top_n=TXN_TOP):
    """Top-N most similar historical deals to the query (for a raw precedent-deals view)."""
    h, usage = _ensure_query_embedding(conn, query_text)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """SELECT t.transaction_id, t.date, t.transaction_type, t.target_name, t.target_sector,
                      t.target_location, t.deal_value, t.deal_value_currency, t.transaction_url,
                      1 - (t.target_embedding <=> q.e) AS score
               FROM transactions t,
                    (SELECT embedding e FROM buyer_match.query_cache WHERE query_hash=%s) q
               WHERE t.target_embedding IS NOT NULL
               ORDER BY t.target_embedding <=> q.e
               LIMIT %s""",
            (h, top_n))
        rows = [_deal(dict(r)) for r in cur.fetchall()]
    return rows, usage


def match_buyers_by_deals(conn, query_text, top_n_txns=TXN_TOP, max_deals=MAX_DEALS):
    """Rank buyers by comparable deals done. Returns {on, discovery, txn_count, usage}."""
    h, usage = _ensure_query_embedding(conn, query_text)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """WITH top AS (
                 SELECT t.transaction_id, t.date, t.transaction_type, t.target_name, t.target_sector,
                        t.deal_value, t.deal_value_currency, t.transaction_url,
                        1 - (t.target_embedding <=> q.e) AS score
                 FROM transactions t,
                      (SELECT embedding e FROM buyer_match.query_cache WHERE query_hash=%s) q
                 WHERE t.target_embedding IS NOT NULL
                 ORDER BY t.target_embedding <=> q.e
                 LIMIT %s)
               SELECT top.*, tp.entity_type, tp.entity_mergr_id, tp.name AS acquirer_name,
                      bm.buyer_id,
                      CASE WHEN tp.entity_type='firms' THEN f.name    ELSE c.name    END AS acq_fullname,
                      CASE WHEN tp.entity_type='firms' THEN f.website ELSE c.website END AS acq_website
               FROM top
               JOIN transaction_parties tp
                    ON tp.transaction_id = top.transaction_id AND tp.role='acquirer'
               LEFT JOIN buyer_match.buyer_mergr bm
                    ON (tp.entity_type='firms'   AND bm.firm_id    = tp.entity_mergr_id)
                    OR (tp.entity_type='company' AND bm.company_id = tp.entity_mergr_id)
               LEFT JOIN firms f      ON tp.entity_type='firms'   AND f.firm_id    = tp.entity_mergr_id
               LEFT JOIN companies c  ON tp.entity_type='company' AND c.company_id = tp.entity_mergr_id""",
            (h, top_n_txns))
        rows = [dict(r) for r in cur.fetchall()]

    on_acc, disc_acc = {}, {}
    for r in rows:
        d = _deal(r)
        if r.get("buyer_id") is not None:
            a = on_acc.setdefault(r["buyer_id"], {"deals": [], "best": 0.0})
        else:
            if r.get("entity_mergr_id") is None:
                continue
            key = (r["entity_type"], r["entity_mergr_id"])
            a = disc_acc.setdefault(key, {"deals": [], "best": 0.0,
                "name": r.get("acq_fullname") or r.get("acquirer_name"),
                "website": r.get("acq_website"), "entity_type": r["entity_type"],
                "entity_mergr_id": r["entity_mergr_id"]})
        a["deals"].append(d)
        if d["score"] > a["best"]:
            a["best"] = d["score"]

    # Enrich ON buyers with their full card row (same fields as thesis search — Mergr, geo, links…).
    buyers = {b["id"]: b for b in svc.buyers_by_ids(conn, list(on_acc.keys()))} if on_acc else {}
    on_out = []
    for bid, a in on_acc.items():
        b = dict(buyers.get(bid) or {"id": bid})
        deals = sorted(a["deals"], key=lambda x: -x["score"])
        b["deal_count"] = len(deals)
        b["best_score"] = a["best"]
        b["deals"] = deals[:max_deals]
        on_out.append(b)

    disc_out = []
    for a in disc_acc.values():
        deals = sorted(a["deals"], key=lambda x: -x["score"])
        disc_out.append({
            "mergr_kind": "firm" if a["entity_type"] == "firms" else "company",
            "entity_mergr_id": a["entity_mergr_id"], "name": a["name"], "website": a["website"],
            "deal_count": len(deals), "best_score": a["best"], "deals": deals[:max_deals],
        })

    keyf = lambda x: (-x["deal_count"], -x["best_score"])       # breadth first, then best single deal
    on_out.sort(key=keyf)
    disc_out.sort(key=keyf)
    return {"on": on_out, "discovery": disc_out, "txn_count": len(rows), "usage": usage}
