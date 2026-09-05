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
PROMPT_VERSION = "v5"

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
- Quote consideration exactly as written, including the currency, or null.
- The header gives the date this document was PUBLISHED. That is when it was said,
  not when the deal completed. Use it for date_hint only if the document itself
  offers nothing better, and set date_basis to say which you used.
- revenue and ebitda: the TARGET's figures, quoted exactly as written and including
  the currency and the period if stated — "£12m FY25 revenue", "revenues of around
  $40 million", "EBITDA of EUR 3.2m". Null unless the document gives them. Never
  compute one from the other, never derive a multiple, and never convert currency.
  A figure for the acquirer or the enlarged group is not the target's: leave null.

ADVISERS — one entry per advising FIRM, each with:
  firm     the firm's name as written.
  service  what they did. Use one of: corporate finance, legal, tax, financial due
           diligence, commercial due diligence, debt advisory, insurance, broking,
           accountancy, management consulting, other. Pick the closest; use "other"
           rather than inventing a category. This is usually inferable from the wording
           even when the side is not — "legal advice was provided by" means legal.
  side     buy, sell, lender, or null. buy = acting for the acquirer or its backer.
           sell = acting for the seller, the target, or its shareholders. lender = acting
           for a debt provider. **null when the document does not say.** A firm named
           without a side is normal and null is the correct answer — never guess from
           who published the document, and never assume the publisher is sell-side.
  people   individuals at THAT firm named in the document, each {"name", "role"} with
           role being their stated job title or null.

people (top level) is for individuals who are NOT advisers — executives, founders,
shareholders — each {"name", "role", "firm"}.

Reply with JSON only, no prose, matching:
{"is_deal": bool, "headline": str|null, "summary": str|null, "acquirer": str|null,
 "target": str|null, "vendor": str|null, "consideration": str|null,
 "revenue": str|null, "ebitda": str|null, "date_hint": str|null,
 "date_basis": "stated"|"published"|null, "sector": str|null,
 "advisers": [{"firm": str, "service": str, "side": "buy"|"sell"|"lender"|null,
               "people": [{"name": str, "role": str|null}]}],
 "people": [{"name": str, "role": str|null, "firm": str|null}],
 "confidence": number}"""


def _rate(model):
    return RATES.get(model, DEFAULT_RATE)


def _call(api_key, model, text, timeout=90):
    """Returns (response_json, latency_ms, sent_body) — the body included so the
    exact request can be stored, not reconstructed."""
    body = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": text[:24000]}],
        "temperature": 0,
        # One reply hit 1500 and came back cut mid-string. The object is small; a
        # long one means the model is padding, and it should have room to finish
        # rather than fail.
        "max_tokens": 3000,
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
    return j, int((time.time() - t0) * 1000), body


def summarise_item(conn, item, api_key, model=MODEL, force=False, attempts=2):
    """One item, one model. Returns the stored row as a dict.

    Retries once on a provider error or a cut-off reply. Four of thirty-nine calls
    came back with finish_reason 'error' when fired back to back, which is a
    transient condition and not a fact about the document — recording it as a
    failed reading would blame the source for the caller's impatience.
    """
    if not force:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""SELECT * FROM tdc.item_summary
                            WHERE scan_item_id=%s AND model=%s AND prompt_version=%s""",
                        (item["id"], model, PROMPT_VERSION))
            got = cur.fetchone()
        if got:
            return got

    # The publication date is known from the source and was previously withheld from
    # the model, which then returned date_hint null on every item — correctly, since
    # nothing it could see carried a date. Passed as a labelled header rather than
    # mixed into the body, so it stays distinguishable from a date the text states.
    head = []
    if item.get("published_at"):
        head.append(f"Published: {item['published_at']:%Y-%m-%d}")
    if item.get("channel"):
        head.append("Source: " + ("a LinkedIn post by the firm" if item["channel"] == "linkedin"
                                  else "a page on the firm's own website"))
    if item.get("url"):
        head.append("URL: " + item["url"])
    text = "\n\n".join(x for x in (("\n".join(head) if head else None),
                                   item.get("title"),
                                   item.get("full_text") or item.get("body")) if x)
    out, err, usage, cost, cost_source, ms = {}, None, {}, None, None, None
    sent, content, finish, meta = None, None, None, None
    for attempt in range(attempts):
        if attempt:
            time.sleep(1.5 * attempt)          # transient, so back off a little
        err = None
        try:
            j, ms, body = _call(api_key, model, text)
            sent = body["messages"][1]["content"]          # exactly what was sent
            choice = (j.get("choices") or [{}])[0]
            finish = choice.get("finish_reason")
            content = choice.get("message", {}).get("content") or "{}"
            meta = {k: v for k, v in j.items() if k != "choices"}
            meta["finish_reason"] = finish
            out = json.loads(content)
            # json_object is not always honoured: this model returns a bare array
            # often enough to matter, and one such reply killed a whole run.
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
            break
        except Exception as e:
            err = f"{type(e).__name__}: {str(e)[:200]}"
            if finish == "length":
                err += " — reply was cut off (finish_reason=length)"
            elif finish == "error":
                err += " — provider returned finish_reason=error"

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            INSERT INTO tdc.item_summary
              (scan_item_id, model, prompt_version, is_deal, headline, summary,
               acquirer, target, vendor, consideration, revenue, ebitda, date_hint,
               date_basis, sector,
               advisers, people, clarity, raw, prompt_tokens, completion_tokens,
               cost_usd, cost_source, latency_ms, ok, error,
               system_prompt, input_text, output_text, finish_reason, response_meta)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    %s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (scan_item_id, model, prompt_version) DO UPDATE SET
              is_deal=EXCLUDED.is_deal, headline=EXCLUDED.headline, summary=EXCLUDED.summary,
              acquirer=EXCLUDED.acquirer, target=EXCLUDED.target, vendor=EXCLUDED.vendor,
              consideration=EXCLUDED.consideration, revenue=EXCLUDED.revenue,
              ebitda=EXCLUDED.ebitda, date_hint=EXCLUDED.date_hint,
              date_basis=EXCLUDED.date_basis,
              sector=EXCLUDED.sector, advisers=EXCLUDED.advisers, people=EXCLUDED.people,
              clarity=EXCLUDED.clarity, raw=EXCLUDED.raw,
              prompt_tokens=EXCLUDED.prompt_tokens, completion_tokens=EXCLUDED.completion_tokens,
              cost_usd=EXCLUDED.cost_usd, cost_source=EXCLUDED.cost_source,
              latency_ms=EXCLUDED.latency_ms, ok=EXCLUDED.ok, error=EXCLUDED.error,
              system_prompt=EXCLUDED.system_prompt, input_text=EXCLUDED.input_text,
              output_text=EXCLUDED.output_text, finish_reason=EXCLUDED.finish_reason,
              response_meta=EXCLUDED.response_meta, created_at=now()
            RETURNING *""",
            (item["id"], model, PROMPT_VERSION, out.get("is_deal"), out.get("headline"),
             out.get("summary"), out.get("acquirer"), out.get("target"), out.get("vendor"),
             out.get("consideration"), out.get("revenue"), out.get("ebitda"),
             out.get("date_hint"), out.get("date_basis"), out.get("sector"),
             json.dumps(out.get("advisers") or []), json.dumps(out.get("people") or []),
             (out.get("clarity") if out.get("clarity") in ("high", "medium", "low")
              else None),
             json.dumps(out) if out else None,
             usage.get("prompt_tokens"), usage.get("completion_tokens"),
             cost, cost_source, ms, err is None, err,
             SYSTEM, sent if sent is not None else text[:24000], content, finish,
             json.dumps(meta) if meta else None))
        row = cur.fetchone()
    conn.commit()
    return row


def spend(conn):
    """What the reading has cost, by model. Reported and estimated kept apart."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT model || '  ' || prompt_version                 AS model,
                   count(*)                                        AS calls,
                   count(*) FILTER (WHERE is_deal)                 AS deals,
                   count(*) FILTER (WHERE NOT ok)                  AS failed,
                   sum(prompt_tokens)                              AS tok_in,
                   sum(completion_tokens)                          AS tok_out,
                   sum(cost_usd)                                   AS cost,
                   -- the figure worth planning against: a total over 39 items means
                   -- nothing, a rate per thousand is a budget
                   round((sum(cost_usd) / nullif(count(*),0) * 1000)::numeric, 2)
                                                                   AS per_1k,
                   sum(cost_usd) FILTER (WHERE cost_source='estimated') AS cost_estimated,
                   round(avg(latency_ms))                          AS avg_ms
            FROM tdc.item_summary GROUP BY model, prompt_version
             ORDER BY model, prompt_version""")
        return cur.fetchall()


def summaries_for(conn, item_ids, model=MODEL, version=None):
    """Readings for the current prompt version by default. Older versions stay in
    the table — that is the point of keying on them — but the screen shows what the
    prompt in the code today produces."""
    if not item_ids:
        return {}
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""SELECT * FROM tdc.item_summary
                        WHERE scan_item_id = ANY(%s) AND model=%s AND prompt_version=%s""",
                    (list(item_ids), model, version or PROMPT_VERSION))
        return {r["scan_item_id"]: r for r in cur.fetchall()}
