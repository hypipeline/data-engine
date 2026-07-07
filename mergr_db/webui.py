"""
Data Engine — unified web UI (FastAPI + Jinja2, server-rendered).

Replaces the Streamlit dashboard. Serves the whole interface (Mergr explorer +
Entity Lookup) as one consistent custom UI on the same origin/port as the JSON API,
behind the shared Caddy front door. Reads the Postgres DB directly (like the old app);
the Entity Lookup page streams from the entity service via /entity-app/* (Caddy).
"""
import os
import json
import html as _html
from urllib.parse import urlencode

import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2 import pool as pgpool
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import entity_client

DSN = os.environ["DATABASE_URL"]
POOL = pgpool.ThreadedConnectionPool(1, 8, DSN)
HERE = os.path.dirname(os.path.abspath(__file__))

app = FastAPI(title="Data Engine UI")
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))
# Cache-buster for the stylesheet: mtime of app.css -> ?v=... so browsers always
# refetch when the CSS changes (otherwise a stale cached app.css hides new styles).
try:
    templates.env.globals["asset_ver"] = str(int(os.path.getmtime(
        os.path.join(HERE, "static", "app.css"))))
except OSError:
    templates.env.globals["asset_ver"] = "0"
PAGE_SIZE = 50


# ---------------------------------------------------------------- db + format
def query(sql, params=None, one=False):
    conn = POOL.getconn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params or {})
            rows = [dict(r) for r in cur.fetchall()] if cur.description else []
        conn.commit()
    except Exception:
        conn.rollback()            # don't return a poisoned (aborted) conn to the pool
        raise
    finally:
        POOL.putconn(conn)
    return (rows[0] if rows else None) if one else rows


def execute(sql, params=None):
    conn = POOL.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params or {})
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        POOL.putconn(conn)


CUR_SYM = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "CNY": "¥", "INR": "₹",
           "AUD": "A$", "CAD": "C$", "NZD": "NZ$", "HKD": "HK$", "KRW": "₩", "BRL": "R$"}
SCALE_ABBR = {"thousands": "K", "millions": "M", "billions": "B"}


def money(amount, currency=None, scale="millions"):
    if amount is None:
        return None
    try:
        n = float(str(amount).replace(",", ""))
    except (TypeError, ValueError):
        return None
    ab = SCALE_ABBR.get(scale or "millions", "M")
    cur = (currency or "").strip()
    sym = CUR_SYM.get(cur)
    return f"{sym}{n:,.0f}{ab}" if sym else f"{n:,.0f}{ab} {cur}".strip()


def mult(x):
    try:
        if x is None:
            return None
        return f"{float(x):.1f}×"
    except (TypeError, ValueError):
        return None


def as_dict(raw):
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return {}
    return {}


def esc(s):
    return _html.escape("" if s is None else str(s))


def link(prefix, id_, text):
    """HTML anchor to a Mergr detail page (namespaced under /mergr); plain text if id missing."""
    if id_ is None or text is None or (isinstance(text, float)):
        return esc(text)
    return f'<a href="/mergr{prefix}{int(id_)}">{esc(text)}</a>'


# Which top-level tool a page belongs to — drives the tool-scoped topbar in base.html.
_MERGR_ACTIVES = {"overview", "firms", "companies", "transactions", "domain", "vector", "settings"}


def _tool_for(active):
    if active in _MERGR_ACTIVES:
        return "mergr"
    if active in {"lookup", "entity"}:
        return "entity"
    if active == "buyer-match":
        return "buyer-match"
    if active == "linkedin":
        return "linkedin"
    return None            # hub / home — no tool chrome


def render(request, name, active, **ctx):
    # no-cache: pages carry inline JS, so a stale cached page hides UI changes
    return templates.TemplateResponse(
        name, {"request": request, "active": active, "tool": _tool_for(active), **ctx},
        headers={"Cache-Control": "no-cache, must-revalidate"})


def fmt_deal_rows(rows):
    """Format financial columns on a list of transaction dict rows for display."""
    for r in rows:
        r["deal_value_f"] = money(r.get("deal_value"), r.get("deal_value_currency"))
        r["revenue_f"] = money(r.get("revenue"), r.get("revenue_currency"))
        r["ebitda_f"] = money(r.get("ebitda"), r.get("ebitda_currency"))
        r["ev_ebitda_f"] = mult(r.get("ev_ebitda"))
        r["ev_revenue_f"] = mult(r.get("ev_revenue"))
    return rows


# ---------------------------------------------------------------- overview
@app.get("/health")
def health():
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    """Data Engine hub — presents the tools (Mergr, Entity Lookup, Buyer Match) as peers."""
    counts = query("SELECT (SELECT count(*) FROM firms) firms, "
                   "(SELECT count(*) FROM companies) companies, "
                   "(SELECT count(*) FROM transactions) transactions", one=True)
    try:                                              # buyer_match schema may not exist yet
        b = query("SELECT count(*) n FROM buyer_match.buyers WHERE embedding IS NOT NULL", one=True)
        counts["buyers"] = b["n"] if b else 0
    except Exception:
        counts["buyers"] = None
    try:                                              # linkedin cache may not exist yet
        li = query("SELECT count(*) n FROM linkedin.companies", one=True)
        counts["linkedin"] = li["n"] if li else 0
    except Exception:
        counts["linkedin"] = None
    return render(request, "home.html", "home",
                  counts=counts, entity_up=entity_client.health())


@app.get("/mergr", response_class=HTMLResponse)
def overview(request: Request):
    counts = query("SELECT (SELECT count(*) FROM firms) firms, "
                   "(SELECT count(*) FROM companies) companies, "
                   "(SELECT count(*) FROM transactions) transactions, "
                   "(SELECT count(*) FROM transaction_parties) parties", one=True)
    gap = query("""SELECT entity_type,
                          count(*) FILTER (WHERE have_record)     AS have,
                          count(*) FILTER (WHERE NOT have_record) AS missing
                   FROM v_party_resolution GROUP BY entity_type ORDER BY entity_type""")
    years = query("""SELECT extract(year FROM date)::int AS year, count(*) AS n
                     FROM transactions WHERE date IS NOT NULL AND extract(year FROM date) >= 1985
                     GROUP BY 1 ORDER BY 1""")
    ymax = max((y["n"] for y in years), default=1)
    for y in years:
        y["pct"] = round(100 * y["n"] / ymax, 1)
    return render(request, "overview.html", "overview",
                  counts=counts, gap=gap, years=years)


# ---------------------------------------------------------------- lists
def _page(request):
    try:
        return max(1, int(request.query_params.get("page", 1)))
    except ValueError:
        return 1


def _pager(total, page, base_qs):
    pages = max(1, -(-total // PAGE_SIZE))
    return {"total": total, "page": page, "pages": pages,
            "start": (page - 1) * PAGE_SIZE + 1,
            "end": min(page * PAGE_SIZE, total),
            "prev_qs": urlencode({**base_qs, "page": page - 1}) if page > 1 else None,
            "next_qs": urlencode({**base_qs, "page": page + 1}) if page < pages else None}


@app.get("/mergr/firms", response_class=HTMLResponse)
def firms(request: Request, q: str = ""):
    page = _page(request)
    where, p = ("WHERE name ILIKE %(t)s", {"t": f"%{q}%"}) if q else ("", {})
    total = query(f"SELECT count(*) n FROM firms {where}", p, one=True)["n"]
    off = (page - 1) * PAGE_SIZE
    rows = query(f"""SELECT firm_id, name, investor_type, size_category,
                            geographic_preferences, total_buys, total_sells
                     FROM firms {where} ORDER BY total_buys DESC NULLS LAST
                     LIMIT {PAGE_SIZE} OFFSET {off}""", p)
    for r in rows:
        r["name_h"] = link("/firm/", r["firm_id"], r["name"])
    return render(request, "firms.html", "firms", rows=rows, q=q,
                  pager=_pager(total, page, {"q": q}))


@app.get("/mergr/companies", response_class=HTMLResponse)
def companies(request: Request, q: str = ""):
    page = _page(request)
    where, p = ("WHERE name ILIKE %(t)s", {"t": f"%{q}%"}) if q else ("", {})
    order = "name" if q else "investor_count DESC NULLS LAST"
    total = query(f"SELECT count(*) n FROM companies {where}", p, one=True)["n"]
    off = (page - 1) * PAGE_SIZE
    rows = query(f"""SELECT company_id, name, sector, city, established, investor_count
                     FROM companies {where} ORDER BY {order}
                     LIMIT {PAGE_SIZE} OFFSET {off}""", p)
    for r in rows:
        r["name_h"] = link("/company/", r["company_id"], r["name"])
    return render(request, "companies.html", "companies", rows=rows, q=q,
                  pager=_pager(total, page, {"q": q}))


@app.get("/mergr/transactions", response_class=HTMLResponse)
def transactions(request: Request, type: str = "", year: str = "", fin: str = ""):
    page = _page(request)
    where, p = [], {}
    if type:
        where.append("transaction_type ILIKE %(ty)s"); p["ty"] = f"%{type}%"
    if year.isdigit():
        where.append("extract(year FROM date)=%(yr)s"); p["yr"] = int(year)
    if fin:
        where.append("deal_value IS NOT NULL")
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    total = query(f"SELECT count(*) n FROM transactions {clause}", p, one=True)["n"]
    off = (page - 1) * PAGE_SIZE
    rows = fmt_deal_rows(query(f"""SELECT transaction_id, target_mergr_id, date, transaction_type,
                            target_name, deal_value, deal_value_currency, ebitda, ebitda_currency, ev_ebitda
                     FROM transactions {clause} ORDER BY date DESC NULLS LAST
                     LIMIT {PAGE_SIZE} OFFSET {off}""", p))
    for r in rows:
        r["target_h"] = link("/company/", r["target_mergr_id"], r["target_name"])
        r["tx_h"] = link("/transaction/", r["transaction_id"], r["transaction_id"])
    return render(request, "transactions.html", "transactions", rows=rows,
                  type=type, year=year, fin=fin,
                  pager=_pager(total, page, {"type": type, "year": year, "fin": fin}))


# ---------------------------------------------------------------- detail: deal highlights
def deal_highlights(kind, eid, role):
    et = "company" if kind == "company" else "firms"
    base = ("WITH tx AS (SELECT t.transaction_id, t.deal_value, t.deal_value_currency, t.ebitda, "
            "t.ebitda_currency, t.ev_ebitda, t.target_name, t.target_mergr_id FROM transactions t "
            "JOIN transaction_parties p USING(transaction_id) "
            "WHERE p.entity_type=%(et)s AND p.entity_mergr_id=%(id)s AND p.role=%(role)s) ")
    p = {"id": eid, "et": et, "role": role}
    dv = query(base + "SELECT DISTINCT ON (deal_value_currency) deal_value_currency ccy, deal_value, "
               "transaction_id, target_mergr_id, target_name FROM tx WHERE deal_value IS NOT NULL "
               "ORDER BY deal_value_currency, deal_value DESC", p)
    eb = query(base + "SELECT DISTINCT ON (ebitda_currency) ebitda_currency ccy, ebitda, "
               "transaction_id, target_mergr_id, target_name FROM tx WHERE ebitda IS NOT NULL "
               "ORDER BY ebitda_currency, ebitda DESC", p)
    ev = query(base + "SELECT ev_ebitda, transaction_id, target_mergr_id, target_name FROM tx "
               "WHERE ev_ebitda BETWEEN 0 AND 100 ORDER BY ev_ebitda DESC LIMIT 1", p)
    udv = query(base + "SELECT tx.transaction_id, tx.target_name, tx.deal_value, tx.deal_value_currency ccy, "
                "tx.deal_value*fx.usd_per_unit usd FROM tx JOIN fx_rates fx ON fx.currency=tx.deal_value_currency "
                "WHERE tx.deal_value IS NOT NULL AND fx.usd_per_unit IS NOT NULL ORDER BY usd DESC LIMIT 1", p, one=True)
    ueb = query(base + "SELECT tx.transaction_id, tx.target_name, tx.ebitda, tx.ebitda_currency ccy, "
                "tx.ebitda*fx.usd_per_unit usd FROM tx JOIN fx_rates fx ON fx.currency=tx.ebitda_currency "
                "WHERE tx.ebitda IS NOT NULL AND fx.usd_per_unit IS NOT NULL ORDER BY usd DESC LIMIT 1", p, one=True)
    for r in dv:
        r["largest"] = money(r["deal_value"], r["ccy"]); r["deal_h"] = link("/company/", r["target_mergr_id"], r["target_name"])
    for r in eb:
        r["largest"] = money(r["ebitda"], r["ccy"]); r["deal_h"] = link("/company/", r["target_mergr_id"], r["target_name"])
    hl = {"dv": dv, "eb": eb, "role": role,
          "ev": ({"mult": mult(ev[0]["ev_ebitda"]), "deal_h": link("/company/", ev[0]["target_mergr_id"], ev[0]["target_name"])} if ev else None),
          "udv": ({"usd": money(udv["usd"], "USD"), "note": f'{udv["target_name"]} — {money(udv["deal_value"], udv["ccy"])}'} if udv else None),
          "ueb": ({"usd": money(ueb["usd"], "USD"), "note": f'{ueb["target_name"]} — {money(ueb["ebitda"], ueb["ccy"])}'} if ueb else None)}
    hl["empty"] = not (dv or eb or ev)
    return hl


def raw_fields(raw, skip=()):
    """Split raw scraped JSON into scalar fields + nested list/dict fields for display."""
    raw = as_dict(raw)
    scalars = [{"field": k, "value": v} for k, v in raw.items()
               if k not in skip and not isinstance(v, (list, dict))]
    nested = {k: v for k, v in raw.items() if k not in skip and isinstance(v, (list, dict))}
    return scalars, nested


# ---------------------------------------------------------------- detail: company
@app.get("/mergr/company/{cid}", response_class=HTMLResponse)
def company(request: Request, cid: int):
    rec = query("SELECT raw, revenue_currency, revenue_scale FROM companies WHERE company_id=%(id)s",
                {"id": cid}, one=True)
    if not rec:
        return render(request, "notfound.html", "companies", what=f"company {cid}")
    raw = as_dict(rec["raw"])
    scale = rec["revenue_scale"]; ccy = rec["revenue_currency"]
    rh = raw.get("revenue_history") or []
    for h in rh:
        h["revenue_f"] = money(h.get("revenue"), ccy, scale)
    target = fmt_deal_rows(query("""SELECT transaction_id, date, transaction_type,
                    deal_value, deal_value_currency, revenue, revenue_currency, ebitda, ebitda_currency, ev_ebitda
              FROM transactions WHERE target_mergr_id=%(id)s ORDER BY date DESC NULLS LAST""", {"id": cid}))
    for r in target:
        r["tx_h"] = link("/transaction/", r["transaction_id"], r["transaction_id"])
    deals = fmt_deal_rows(query("""SELECT t.transaction_id, t.date, p.role, t.target_mergr_id, t.target_name,
                    t.deal_value, t.deal_value_currency, t.ebitda, t.ebitda_currency, t.ev_ebitda
              FROM transaction_parties p JOIN transactions t USING (transaction_id)
              WHERE p.entity_type='company' AND p.entity_mergr_id=%(id)s ORDER BY t.date DESC NULLS LAST""", {"id": cid}))
    for r in deals:
        r["target_h"] = link("/company/", r["target_mergr_id"], r["target_name"])
        r["tx_h"] = link("/transaction/", r["transaction_id"], r["transaction_id"])
    scalars, nested = raw_fields(raw, skip=("revenue_history", "description"))
    return render(request, "company.html", "companies", cid=cid, raw=raw,
                  revenue_f=money(raw.get("revenue"), ccy, scale), scale_ab=SCALE_ABBR.get(scale, "M"), ccy=ccy or "",
                  revenue_history=rh, scalars=scalars, nested=nested,
                  hl_buy=deal_highlights("company", cid, "acquirer"),
                  hl_sell=deal_highlights("company", cid, "seller"),
                  target=target, deals=deals)


# ---------------------------------------------------------------- detail: firm
@app.get("/mergr/firm/{fid}", response_class=HTMLResponse)
def firm(request: Request, fid: int):
    rec = query("SELECT raw FROM firms WHERE firm_id=%(id)s", {"id": fid}, one=True)
    if not rec:
        return render(request, "notfound.html", "firms", what=f"firm {fid}")
    raw = as_dict(rec["raw"])
    deals = fmt_deal_rows(query("""SELECT t.transaction_id, t.date, t.transaction_type, p.role,
                     t.target_mergr_id, t.target_name, t.target_sector,
                     t.deal_value, t.deal_value_currency, t.ebitda, t.ebitda_currency, t.ev_ebitda
              FROM transaction_parties p JOIN transactions t USING (transaction_id)
              WHERE p.entity_type='firms' AND p.entity_mergr_id=%(id)s
              ORDER BY t.date DESC NULLS LAST LIMIT 500""", {"id": fid}))
    for r in deals:
        r["target_h"] = link("/company/", r["target_mergr_id"], r["target_name"])
        r["tx_h"] = link("/transaction/", r["transaction_id"], r["transaction_id"])
    scalars, nested = raw_fields(raw, skip=("investment_criteria_description",))
    return render(request, "firm.html", "firms", fid=fid, raw=raw, scalars=scalars, nested=nested,
                  hl_buy=deal_highlights("firm", fid, "acquirer"),
                  hl_sell=deal_highlights("firm", fid, "seller"), deals=deals)


# ---------------------------------------------------------------- detail: transaction
@app.get("/mergr/transaction/{tid}", response_class=HTMLResponse)
def transaction(request: Request, tid: int):
    r = query("""SELECT raw, deal_value, deal_value_currency, revenue, revenue_currency,
                      ebitda, ebitda_currency, ev_revenue, ev_ebitda, financials_scraped_at
               FROM transactions WHERE transaction_id=%(id)s""", {"id": tid}, one=True)
    if not r:
        return render(request, "notfound.html", "transactions", what=f"transaction {tid}")
    raw = as_dict(r["raw"])
    tgt = raw.get("target") or {}
    fin = {"deal_value": money(r["deal_value"], r["deal_value_currency"]),
           "revenue": money(r["revenue"], r["revenue_currency"]),
           "ebitda": money(r["ebitda"], r["ebitda_currency"]),
           "ev_revenue": mult(r["ev_revenue"]), "ev_ebitda": mult(r["ev_ebitda"]),
           "scraped": r["financials_scraped_at"]}
    parties = query("""SELECT role, entity_type, entity_mergr_id, name, sub_type
              FROM transaction_parties WHERE transaction_id=%(id)s ORDER BY role""", {"id": tid})
    for pr in parties:
        pfx = "/firm/" if pr["entity_type"] == "firms" else "/company/"
        pr["name_h"] = link(pfx, pr["entity_mergr_id"], pr["name"])
    tgt_h = link("/company/", tgt.get("mergr_id"), tgt.get("name")) if tgt.get("mergr_id") is not None else esc(tgt.get("name"))
    scalars, nested = raw_fields(raw, skip=("acquirers", "sellers", "target"))
    return render(request, "transaction.html", "transactions", tid=tid, raw=raw, tgt=tgt, tgt_h=tgt_h,
                  fin=fin, parties=parties, scalars=scalars, nested=nested)


# ---------------------------------------------------------------- domain
@app.get("/mergr/domain", response_class=HTMLResponse)
def domain(request: Request, q: str = ""):
    from domain_utils import website_to_domain
    dom = website_to_domain(q) if q else ""
    firms_ = comps = None
    if dom:
        firms_ = query("SELECT firm_id, name, investor_type, website FROM firms WHERE domain=%(d)s ORDER BY name", {"d": dom})
        comps = query("SELECT company_id, name, sector, city, website FROM companies WHERE domain=%(d)s ORDER BY name", {"d": dom})
        for r in firms_:
            r["name_h"] = link("/firm/", r["firm_id"], r["name"])
        for r in comps:
            r["name_h"] = link("/company/", r["company_id"], r["name"])
    return render(request, "domain.html", "domain", q=q, dom=dom, firms=firms_, comps=comps)


# ---------------------------------------------------------------- settings (FX)
@app.get("/mergr/settings", response_class=HTMLResponse)
def settings(request: Request, saved: str = ""):
    rates = query("SELECT currency, usd_per_unit FROM fx_rates ORDER BY currency")
    asof = query("SELECT max(as_of) m FROM fx_rates", one=True)["m"]
    import datetime
    return render(request, "settings.html", "settings", rates=rates, asof=asof,
                  today=datetime.date.today().isoformat(), saved=saved)


@app.post("/mergr/settings")
async def settings_save(request: Request, as_of: str = Form(...)):
    form = await request.form()
    n = 0
    for key, val in form.items():
        if key.startswith("rate_"):
            ccy = key[5:]
            v = None
            if str(val).strip():
                try:
                    v = float(val)
                except ValueError:
                    v = None
            execute("UPDATE fx_rates SET usd_per_unit=%s, as_of=%s, updated_at=now() WHERE currency=%s",
                    (v, as_of, ccy))
            n += 1
    return RedirectResponse(f"/mergr/settings?saved={n}", status_code=303)


# ---------------------------------------------------------------- vector search
@app.get("/mergr/vector", response_class=HTMLResponse)
def vector(request: Request, q: str = "", target: str = "companies"):
    enabled = bool(os.environ.get("OPENAI_API_KEY"))
    rows = None
    if enabled and q:
        from openai import OpenAI
        emb = OpenAI().embeddings.create(model="text-embedding-3-small", input=q).data[0].embedding
        vec = "[" + ",".join(map(str, emb)) + "]"
        if target == "firms":
            rows = query("""SELECT firm_id, name, investor_type,
                              1-(criteria_embedding<=>%(v)s::vector) score
                           FROM firms WHERE criteria_embedding IS NOT NULL
                           ORDER BY criteria_embedding<=>%(v)s::vector LIMIT 25""", {"v": vec})
            for r in rows:
                r["name_h"] = link("/firm/", r["firm_id"], r["name"])
        else:
            rows = query("""SELECT company_id, name, sector, city,
                              1-(description_embedding<=>%(v)s::vector) score
                           FROM companies WHERE description_embedding IS NOT NULL
                           ORDER BY description_embedding<=>%(v)s::vector LIMIT 25""", {"v": vec})
            for r in rows:
                r["name_h"] = link("/company/", r["company_id"], r["name"])
        for r in rows:
            r["score_f"] = f"{r['score']:.3f}" if r.get("score") is not None else "—"
    return render(request, "vector.html", "vector", enabled=enabled, q=q, target=target, rows=rows)


# ---------------------------------------------------------------- entity lookup
# Native page (no iframe). The streaming UI streams same-origin from the entity
# container via /entity-app/* (Caddy) and drives real /entity/<domain> URLs.
@app.get("/entity", response_class=HTMLResponse)
def entity(request: Request):
    return render(request, "entity.html", "entity", initial_url="")


# Sidebar tools (must precede /entity/{domain} so they aren't swallowed by it).
# Each is a thin UI over the entity app's /entity-app/api/* endpoints.
@app.get("/entity/tools/company", response_class=HTMLResponse)
def tool_company(request: Request):
    return render(request, "tool_company.html", "entity")


@app.get("/entity/tools/trademark", response_class=HTMLResponse)
def tool_trademark(request: Request):
    return render(request, "tool_trademark.html", "entity")


@app.get("/entity/tools/validate", response_class=HTMLResponse)
def tool_validate(request: Request):
    return render(request, "tool_validate.html", "entity")


@app.get("/entity/{domain:path}", response_class=HTMLResponse)
def entity_lookup(request: Request, domain: str):
    """Deep-link to a past lookup: resolve <domain> -> the exact URL that was looked
    up (from the shared entity.lookups cache), so it replays instantly on load."""
    from domain_utils import website_to_domain
    dom = website_to_domain(domain) or (domain or "").strip().lower().strip("/")
    initial = ("https://" + dom) if dom else ""      # cold deep-link: run it fresh
    if dom:
        try:
            r = query("SELECT url FROM entity.lookups WHERE domain=%(d)s "
                      "ORDER BY created_at DESC LIMIT 1", {"d": dom}, one=True)
            if r and r.get("url"):
                initial = r["url"]                    # exact URL -> cache hit / replay
        except Exception:
            pass                                      # cache table absent -> fall back
    return render(request, "entity.html", "entity", initial_url=initial)


# ─────────────────────────────────────────────────────────────────────────────
# Buyer Match — semantic buyer↔mandate matching (pgvector). UI at /buyer-match;
# JSON endpoints under /buyer-match/*. See buyer_match/ + BUYER_MATCH_SPEC.md.
# ─────────────────────────────────────────────────────────────────────────────
from fastapi.responses import JSONResponse            # noqa: E402
from pydantic import BaseModel                          # noqa: E402
from buyer_match import service as bm_svc, mandate as bm_mand  # noqa: E402


def _bm(fn, *args):
    """Run a read-only Buyer Match query on a pooled connection."""
    conn = POOL.getconn()
    try:
        return fn(conn, *args)
    finally:
        conn.rollback()          # read-only; reset the transaction
        POOL.putconn(conn)


class _SearchReq(BaseModel):
    query: str = ""

class _MandateReq(BaseModel):
    identifier: str = ""

class _KeywordsReq(BaseModel):
    keywords: list[str] = []

class _KeywordReq(BaseModel):
    keyword: str = ""


@app.get("/buyer-match", response_class=HTMLResponse)
def buyer_match_page(request: Request):
    return render(request, "buyer_match.html", "buyer-match")


@app.get("/linkedin", response_class=HTMLResponse)
def linkedin_page(request: Request):
    """LinkedIn Finder — company domain/name → LinkedIn page + employee count.
    Backend lives in the entity app (Bright Data); page calls /entity-app/api/linkedin."""
    return render(request, "linkedin.html", "linkedin")


@app.get("/buyer-match/mandates")
def bm_mandates():
    return JSONResponse(_bm(bm_svc.list_mandates))


@app.post("/buyer-match/search")
def bm_search(req: _SearchReq):
    q = (req.query or "").strip()
    if not q:
        return JSONResponse({"results": [], "count": 0})
    rows, usage = _bm(bm_svc.search, q, 500)
    return JSONResponse({"results": rows, "count": len(rows), "usage": usage})


@app.post("/buyer-match/load-mandate")
def bm_load_mandate(req: _MandateReq):
    ident = (req.identifier or "").strip()
    if not ident:
        return JSONResponse({"error": "empty identifier"}, status_code=400)
    return JSONResponse(_bm(bm_mand.load_mandate, ident))


@app.get("/buyer-match/keyword-counts")
def bm_keyword_counts():
    return JSONResponse(_bm(bm_svc.keyword_counts))


@app.post("/buyer-match/keyword-buyers")
def bm_keyword_buyers(req: _KeywordsReq):
    buyers = _bm(bm_svc.keyword_buyers, req.keywords)
    return JSONResponse({"keywords": req.keywords, "count": len(buyers), "buyers": buyers})


@app.post("/buyer-match/similar-keywords")
def bm_similar_keywords(req: _KeywordReq):
    kw = (req.keyword or "").strip()
    return JSONResponse({"keyword": kw, "similar": _bm(bm_svc.similar_keywords, kw)})


import queue as _queue                               # noqa: E402
import threading as _threading                       # noqa: E402
from fastapi.responses import StreamingResponse      # noqa: E402
from buyer_match import sync as bm_sync              # noqa: E402


@app.get("/buyer-match/sync-status")
def bm_sync_status():
    """Last-sync info + a cheap 'records changed since' nudge (from the source DB)."""
    st = None
    try:
        st = query("SELECT last_sync_at, last_buyers_embedded, last_keywords_embedded, "
                   "last_mandates_synced, last_cost_usd FROM buyer_match.sync_state WHERE id=1", one=True)
    except Exception:
        pass
    last = st["last_sync_at"] if st else None
    since = {}
    if last:
        try:
            since = bm_sync.since_counts(last)
        except Exception:
            since = {}
    stats = {}
    if st:
        stats = {"buyers_embedded": st["last_buyers_embedded"],
                 "keywords_embedded": st["last_keywords_embedded"],
                 "mandates_synced": st["last_mandates_synced"],
                 "cost_usd": float(st["last_cost_usd"]) if st["last_cost_usd"] is not None else 0}
    return JSONResponse({"last_sync_at": last.isoformat() if last else None,
                         "stats": stats, "since": since})


@app.get("/buyer-match/sync")
def bm_run_sync():
    """Run the sync worker, streaming progress as SSE (GET so EventSource can consume it)."""
    q = _queue.Queue()

    def worker():
        try:
            stats = bm_sync.run_sync(progress=lambda m: q.put(("log", m)), pg_dsn=DSN)
            q.put(("result", stats))
        except Exception as e:                        # noqa: BLE001
            q.put(("error", str(e)))
        finally:
            q.put(("__done__", None))

    _threading.Thread(target=worker, daemon=True).start()

    def gen():
        while True:
            kind, data = q.get()
            if kind == "__done__":
                yield "event: done\ndata: {}\n\n"
                break
            yield f"event: {kind}\ndata: {json.dumps(data)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")
