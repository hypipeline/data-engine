"""
TDC — read a scanned item with a model and record what it said, and what it cost.

The output is not a claim. It is one model's reading of one document at one
moment, stored with the model id and prompt version so it can be compared against
another and re-run when either improves.
"""
import json
import os
import time
import urllib.request

from psycopg2.extras import RealDictCursor

OR_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = os.environ.get("TDC_SUMMARY_MODEL", "google/gemini-2.5-flash")
PROMPT_VERSION = "v1"

# Fallback only. OpenRouter reports the actual charge per request when asked, and
# that is used when present — a rate table goes stale silently, and a cost we
# computed ourselves should never be presented as one we were charged.
RATES = {"google/gemini-2.5-flash": {"in": 0.30, "out": 2.50}}
DEFAULT_RATE = {"in": 1.0, "out": 3.0}

SYSTEM = """You read one document from a corporate finance firm's website or LinkedIn
and report what transaction, if any, it describes.

Rules:
- Report only what the document says. Never infer, never complete a partial name from
  knowledge, never estimate a figure.
- Anything the document does not state is null. Absent is not zero and not "undisclosed".
- Many documents are not deals: sector reports, hiring news, promotions, event notices,
  award entries. For those set is_deal false and leave every field null.
- The firm publishing the document is usually an ADVISER, not a party. Do not record it
  as acquirer or target unless the document plainly says it bought or sold something.
- people are individuals named in the document with their stated role and firm.
- Quote consideration exactly as written, including the currency, or null.

Reply with JSON only, no prose, matching:
{"is_deal": bool, "headline": str|null, "summary": str|null, "acquirer": str|null,
 "target": str|null, "vendor": str|null, "consideration": str|null, "date_hint": str|null,
 "sector": str|null, "advisers": [str], "people": [{"name": str, "role": str|null,
 "firm": str|null}], "confidence": number}"""


def _rate(model):
    return RATES.get(model, DEFAULT_RATE)


def _call(api_key, model, text, timeout=90):
    body = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": text[:24000]}],
        "temperature": 0,
        "max_tokens": 1500,
        "response_format": {"type": "json_object"},
        # Ask OpenRouter for the actual charge rather than computing one.
        "usage": {"include": True},
    }
    req = urllib.request.Request(
        OR_URL, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json",
                 "HTTP-Referer": "https://dataengine.hyndlandpartners.com",
                 "X-Title": "TDC summariser"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        j = json.loads(r.read())
    return j, int((time.time() - t0) * 1000)


def summarise_item(conn, item, api_key, model=MODEL, force=False):
    """One item, one model. Returns the stored row as a dict."""
    if not force:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""SELECT * FROM tdc.item_summary
                            WHERE scan_item_id=%s AND model=%s AND prompt_version=%s""",
                        (item["id"], model, PROMPT_VERSION))
            got = cur.fetchone()
        if got:
            return got

    text = "\n\n".join(x for x in (item.get("title"), item.get("full_text")
                                   or item.get("body")) if x)
    out, err, usage, cost, cost_source, ms = {}, None, {}, None, None, None
    # Truncation is the other way a reply arrives unparseable, so leave room for the
    # whole object rather than letting the default cut it mid-string.

    try:
        j, ms = _call(api_key, model, text)
        content = (j.get("choices") or [{}])[0].get("message", {}).get("content") or "{}"
        out = json.loads(content)
        # json_object is not always honoured: this model returns a bare array often
        # enough to matter, and one such reply killed a whole run. Coerce rather
        # than trust the response_format flag.
        if isinstance(out, list):
            out = next((x for x in out if isinstance(x, dict)), {})
        if not isinstance(out, dict):
            raise ValueError(f"model returned {type(out).__name__}, not an object")
        usage = j.get("usage") or {}
        if usage.get("cost") is not None:
            cost, cost_source = float(usage["cost"]), "reported"
        else:
            r = _rate(model)
            cost = (usage.get("prompt_tokens", 0) / 1e6 * r["in"]
                    + usage.get("completion_tokens", 0) / 1e6 * r["out"])
            cost_source = "estimated"
    except Exception as e:
        err = f"{type(e).__name__}: {str(e)[:200]}"

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            INSERT INTO tdc.item_summary
              (scan_item_id, model, prompt_version, is_deal, headline, summary,
               acquirer, target, vendor, consideration, date_hint, sector,
               advisers, people, confidence, raw, prompt_tokens, completion_tokens,
               cost_usd, cost_source, latency_ms, ok, error)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (scan_item_id, model, prompt_version) DO UPDATE SET
              is_deal=EXCLUDED.is_deal, headline=EXCLUDED.headline, summary=EXCLUDED.summary,
              acquirer=EXCLUDED.acquirer, target=EXCLUDED.target, vendor=EXCLUDED.vendor,
              consideration=EXCLUDED.consideration, date_hint=EXCLUDED.date_hint,
              sector=EXCLUDED.sector, advisers=EXCLUDED.advisers, people=EXCLUDED.people,
              confidence=EXCLUDED.confidence, raw=EXCLUDED.raw,
              prompt_tokens=EXCLUDED.prompt_tokens, completion_tokens=EXCLUDED.completion_tokens,
              cost_usd=EXCLUDED.cost_usd, cost_source=EXCLUDED.cost_source,
              latency_ms=EXCLUDED.latency_ms, ok=EXCLUDED.ok, error=EXCLUDED.error,
              created_at=now()
            RETURNING *""",
            (item["id"], model, PROMPT_VERSION, out.get("is_deal"), out.get("headline"),
             out.get("summary"), out.get("acquirer"), out.get("target"), out.get("vendor"),
             out.get("consideration"), out.get("date_hint"), out.get("sector"),
             json.dumps(out.get("advisers") or []), json.dumps(out.get("people") or []),
             out.get("confidence"), json.dumps(out) if out else None,
             usage.get("prompt_tokens"), usage.get("completion_tokens"),
             cost, cost_source, ms, err is None, err))
        row = cur.fetchone()
    conn.commit()
    return row


def spend(conn):
    """What the reading has cost, by model. Reported and estimated kept apart."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT model,
                   count(*)                                        AS calls,
                   count(*) FILTER (WHERE is_deal)                 AS deals,
                   count(*) FILTER (WHERE NOT ok)                  AS failed,
                   sum(prompt_tokens)                              AS tok_in,
                   sum(completion_tokens)                          AS tok_out,
                   sum(cost_usd)                                   AS cost,
                   sum(cost_usd) FILTER (WHERE cost_source='estimated') AS cost_estimated,
                   round(avg(latency_ms))                          AS avg_ms
            FROM tdc.item_summary GROUP BY model ORDER BY model""")
        return cur.fetchall()


def summaries_for(conn, item_ids, model=MODEL):
    if not item_ids:
        return {}
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""SELECT * FROM tdc.item_summary
                        WHERE scan_item_id = ANY(%s) AND model=%s""",
                    (list(item_ids), model))
        return {r["scan_item_id"]: r for r in cur.fetchall()}
