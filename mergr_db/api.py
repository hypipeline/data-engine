"""
Mergr JSON API (FastAPI) — read access to firms, companies, transactions + search.
Single-user bearer-token auth. Every money field returns raw + formatted (+usd).
Docs at /docs (use the Authorize button with the token).
Runs alongside the DB; same DATABASE_URL. Formatting shared with the Streamlit app
via mergr_money.py; domain normalization via domain_utils.py.
"""
import os
import secrets
from fastapi import FastAPI, APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2 import pool as pgpool

from mergr_money import money_obj, mult_obj, money_str
from domain_utils import website_to_domain
import entity_client

DSN = os.environ["DATABASE_URL"]
API_USER = os.environ.get("API_USER", "mergr")
API_PASS = os.environ.get("API_PASS", "mergr")
POOL = pgpool.ThreadedConnectionPool(1, 8, DSN)

# HTTP Basic -> the browser shows its native username/password popup
security = HTTPBasic()


def auth(cred: HTTPBasicCredentials = Depends(security)):
    ok = (secrets.compare_digest(cred.username, API_USER) and
          secrets.compare_digest(cred.password, API_PASS))
    if not ok:
        raise HTTPException(401, "unauthorized", headers={"WWW-Authenticate": "Basic"})


# root_path=/api: the Caddy front door serves this app under /api/* (prefix stripped),
# so Swagger docs (/api/docs) and /api/openapi.json resolve correctly behind the proxy.
app = FastAPI(title="Data Engine API", version="1.0", root_path="/api",
              description="Data Engine — multi-source data API. Mergr endpoints are namespaced under /mergr.")

# Mergr data source — all endpoints under /mergr/*, Basic-auth protected at the router level.
mergr = APIRouter(prefix="/mergr", tags=["mergr"], dependencies=[Depends(auth)])

# Entity-lookup data source — all endpoints under /entity/*, proxied to the PHP sidecar.
entity = APIRouter(prefix="/entity", tags=["entity"], dependencies=[Depends(auth)])


# ---- db helpers ------------------------------------------------------------
def query(sql, params=None, one=False):
    conn = POOL.getconn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params or {})
            rows = cur.fetchall()
        conn.commit()
    finally:
        POOL.putconn(conn)
    rows = [dict(r) for r in rows]
    return (rows[0] if rows else None) if one else rows


def fx_rates():
    return {r["currency"]: (float(r["usd_per_unit"]) if r["usd_per_unit"] is not None else None)
            for r in query("SELECT currency, usd_per_unit FROM fx_rates")}


# ---- serializers -----------------------------------------------------------
def ser_transaction(r, fx):
    return {
        "transaction_id": r["transaction_id"],
        "date": str(r["date"]) if r.get("date") else None,
        "transaction_type": r.get("transaction_type") or None,
        "target": {"name": r.get("target_name"), "company_id": r.get("target_mergr_id"),
                   "sector": r.get("target_sector"), "location": r.get("target_location"),
                   "description": r.get("target_description")},
        "deal_value": money_obj(r.get("deal_value"), r.get("deal_value_currency"), "millions",
                                fx.get(r.get("deal_value_currency"))),
        "revenue": money_obj(r.get("revenue"), r.get("revenue_currency"), "millions",
                             fx.get(r.get("revenue_currency"))),
        "ebitda": money_obj(r.get("ebitda"), r.get("ebitda_currency"), "millions",
                            fx.get(r.get("ebitda_currency"))),
        "ev_revenue": mult_obj(r.get("ev_revenue")),
        "ev_ebitda": mult_obj(r.get("ev_ebitda")),
    }


def ser_company(r, fx):
    raw = r.get("raw") or {}
    rev_ccy, rev_scale = r.get("revenue_currency"), r.get("revenue_scale") or "millions"
    hist = raw.get("revenue_history") or []
    for h in hist:
        h["formatted"] = money_str(h.get("revenue"), rev_ccy, rev_scale)
    return {
        "company_id": r["company_id"], "name": r.get("name"), "legal_name": raw.get("legal_name"),
        "sector": r.get("sector"), "city": r.get("city"), "country": raw.get("country"),
        "website": r.get("website"), "domain": r.get("domain"),
        "ticker": raw.get("ticker"), "stock_exchange": raw.get("stock_exchange"),
        "established": raw.get("established"), "employees": raw.get("employees"),
        "investor_count": r.get("investor_count"),
        "revenue": money_obj(raw.get("revenue"), rev_ccy, rev_scale,
                             fx.get(rev_ccy)),
        "revenue_history": hist,
        "description": r.get("description"),
    }


def ser_firm(r):
    raw = r.get("raw") or {}
    return {
        "firm_id": r["firm_id"], "name": r.get("name"), "legal_name": r.get("legal_name"),
        "investor_type": r.get("investor_type"), "ownership": raw.get("ownership"),
        "size_category": r.get("size_category"), "pe_assets": raw.get("pe_assets"),
        "established": raw.get("established"), "website": r.get("website"),
        "linkedin": raw.get("linkedin"), "domain": r.get("domain"),
        "geographic_preferences": r.get("geographic_preferences"),
        "sectors_of_interest": raw.get("sectors_of_interest"),
        "total_buys": r.get("total_buys"), "total_sells": r.get("total_sells"),
        "investment_criteria": raw.get("investment_criteria_description"),
    }


# ---- endpoints -------------------------------------------------------------
@app.get("/health")
def health():
    return {"ok": True, "counts": query(
        "SELECT (SELECT count(*) FROM firms) firms, (SELECT count(*) FROM companies) companies, "
        "(SELECT count(*) FROM transactions) transactions", one=True)}


@mergr.get("/fx")
def get_fx():
    return query("SELECT currency, usd_per_unit, as_of FROM fx_rates ORDER BY currency")


@mergr.get("/companies/{cid}")
def company(cid: int):
    r = query("""SELECT company_id, name, sector, city, website, domain, investor_count,
                        description, revenue_currency, revenue_scale, raw
                 FROM companies WHERE company_id=%(id)s""", {"id": cid}, one=True)
    if not r:
        raise HTTPException(404, "company not found")
    return ser_company(r, fx_rates())


@mergr.get("/companies")
def companies(search: str = None, sector: str = None, domain: str = None,
              limit: int = Query(50, le=200), offset: int = 0):
    w, p = [], {"lim": limit, "off": offset}
    if search: w.append("name ILIKE %(s)s"); p["s"] = f"%{search}%"
    if sector: w.append("sector=%(sec)s"); p["sec"] = sector
    if domain: w.append("domain=%(d)s"); p["d"] = website_to_domain(domain)
    clause = ("WHERE " + " AND ".join(w)) if w else ""
    total = query(f"SELECT count(*) n FROM companies {clause}", p, one=True)["n"]
    rows = query(f"""SELECT company_id, name, sector, city, website, domain, investor_count
                     FROM companies {clause} ORDER BY name LIMIT %(lim)s OFFSET %(off)s""", p)
    return {"count": total, "limit": limit, "offset": offset, "results": rows}


@mergr.get("/firms/{fid}")
def firm(fid: int):
    r = query("""SELECT firm_id, name, legal_name, investor_type, size_category, website, domain,
                        geographic_preferences, total_buys, total_sells, raw
                 FROM firms WHERE firm_id=%(id)s""", {"id": fid}, one=True)
    if not r:
        raise HTTPException(404, "firm not found")
    return ser_firm(r)


@mergr.get("/firms")
def firms(search: str = None, domain: str = None,
          limit: int = Query(50, le=200), offset: int = 0):
    w, p = [], {"lim": limit, "off": offset}
    if search: w.append("name ILIKE %(s)s"); p["s"] = f"%{search}%"
    if domain: w.append("domain=%(d)s"); p["d"] = website_to_domain(domain)
    clause = ("WHERE " + " AND ".join(w)) if w else ""
    total = query(f"SELECT count(*) n FROM firms {clause}", p, one=True)["n"]
    rows = query(f"""SELECT firm_id, name, investor_type, size_category, website, domain,
                     total_buys, total_sells FROM firms {clause}
                     ORDER BY total_buys DESC NULLS LAST LIMIT %(lim)s OFFSET %(off)s""", p)
    return {"count": total, "limit": limit, "offset": offset, "results": rows}


@mergr.get("/transactions/{tid}")
def transaction(tid: int):
    r = query("""SELECT transaction_id, date, transaction_type, target_mergr_id, target_name,
                        target_sector, target_location, target_description,
                        deal_value, deal_value_currency, revenue, revenue_currency,
                        ebitda, ebitda_currency, ev_revenue, ev_ebitda
                 FROM transactions WHERE transaction_id=%(id)s""", {"id": tid}, one=True)
    if not r:
        raise HTTPException(404, "transaction not found")
    out = ser_transaction(r, fx_rates())
    out["parties"] = query("""SELECT role, entity_type, entity_mergr_id, name, sub_type
                              FROM transaction_parties WHERE transaction_id=%(id)s ORDER BY role""",
                           {"id": tid})
    return out


@mergr.get("/transactions")
def transactions(year: int = None, type: str = None, has_financials: bool = False,
                 limit: int = Query(50, le=200), offset: int = 0):
    w, p = [], {"lim": limit, "off": offset}
    if year: w.append("extract(year FROM date)=%(y)s"); p["y"] = year
    if type: w.append("transaction_type ILIKE %(t)s"); p["t"] = f"%{type}%"
    if has_financials: w.append("deal_value IS NOT NULL")
    clause = ("WHERE " + " AND ".join(w)) if w else ""
    total = query(f"SELECT count(*) n FROM transactions {clause}", p, one=True)["n"]
    fx = fx_rates()
    rows = query(f"""SELECT transaction_id, date, transaction_type, target_mergr_id, target_name,
                            target_sector, target_location, target_description,
                            deal_value, deal_value_currency, revenue, revenue_currency,
                            ebitda, ebitda_currency, ev_revenue, ev_ebitda
                     FROM transactions {clause} ORDER BY date DESC NULLS LAST
                     LIMIT %(lim)s OFFSET %(off)s""", p)
    return {"count": total, "limit": limit, "offset": offset,
            "results": [ser_transaction(r, fx) for r in rows]}


@mergr.get("/domain")
def domain_search(q: str = Query(..., description="A domain or full website URL")):
    d = website_to_domain(q)
    return {"domain": d,
            "firms": query("SELECT firm_id, name, investor_type, website FROM firms WHERE domain=%(d)s",
                           {"d": d}),
            "companies": query("SELECT company_id, name, sector, website FROM companies WHERE domain=%(d)s",
                               {"d": d})}


# ---- entity endpoints (proxied to the Python entity service) ---------------
@entity.get("/health")
def entity_health():
    return {"service_up": entity_client.health(), "base": entity_client.ENTITY_BASE}


@entity.get("/lookup")
def entity_lookup(url: str = Query(..., description="Company website URL to resolve to a legal entity")):
    """
    Resolve a company website URL to its optimal legal contracting entity, with a
    confidence score and a bidirectional evidence chain. Blocks ~1-2 min while the entity
    service gathers register data and Claude reasons over it. Returns {report, input_payload, meta}.
    """
    try:
        payload, status = entity_client.lookup(url)
    except Exception as e:
        raise HTTPException(502, f"entity service error: {e}")
    if status == 400:
        raise HTTPException(400, payload.get("error", "invalid url"))
    return payload


app.include_router(mergr)
app.include_router(entity)
