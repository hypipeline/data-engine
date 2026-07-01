#!/usr/bin/env python3
"""
Mergr relationship explorer — Streamlit UI over the Postgres DB.

ONE navigation model: the query param drives everything.
  ?tab=overview|firms|companies|transactions|vector   -> which section
  ?company= / ?firm= / ?transaction=<id>              -> a record detail page
The nav bar links and the Overview count cards point at the SAME ?tab= URLs,
so there are no duplicate/sibling pages. Every id and name is an internal link.
"""
import os
import json
import pandas as pd
import streamlit as st
import psycopg2
from domain_utils import website_to_domain

st.set_page_config(page_title="Mergr Explorer", layout="wide")
DSN = os.environ["DATABASE_URL"]


@st.cache_resource
def conn():
    c = psycopg2.connect(DSN)
    c.autocommit = True
    return c


@st.cache_data(ttl=60)
def q(sql, params=None):
    return pd.read_sql(sql, conn(), params=params)


CUR_SYM = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "CNY": "¥", "INR": "₹",
           "AUD": "A$", "CAD": "C$", "NZD": "NZ$", "HKD": "HK$", "KRW": "₩", "BRL": "R$"}
SCALE_ABBR = {"thousands": "K", "millions": "M", "billions": "B"}


def money(amount, currency=None, scale="millions"):
    """Uniform money formatting: '$2,500M', '£40M', '¥25,769M', '6,500M SEK'."""
    if amount is None:
        return "—"
    try:
        n = float(str(amount).replace(",", ""))
    except (TypeError, ValueError):
        return "—"
    if pd.isna(n):
        return "—"
    ab = SCALE_ABBR.get(scale or "millions", "M")
    amt = f"{n:,.0f}"
    cur = (currency or "").strip()
    sym = CUR_SYM.get(cur)
    if sym:
        return f"{sym}{amt}{ab}"
    return f"{amt}{ab} {cur}".strip()


def mult(x):
    """Uniform multiple formatting: '9.4×'."""
    try:
        if x is None or pd.isna(float(x)):
            return "—"
        return f"{float(x):.1f}×"
    except (TypeError, ValueError):
        return "—"


def fmt_deals(df):
    """Format a deals dataframe's financial columns with money()/mult(); drop the
    currency helper columns. Values not disclosed show as '—'."""
    if df.empty:
        return df
    df = df.copy()
    for col, ccol in (("deal_value", "deal_value_currency"),
                      ("revenue", "revenue_currency"), ("ebitda", "ebitda_currency")):
        if col in df.columns:
            df[col] = df.apply(lambda r, c=col, cc=ccol: money(r[c], r.get(cc)), axis=1)
    if "ev_ebitda" in df.columns:
        df["ev_ebitda"] = df["ev_ebitda"].apply(mult)
    if "ev_revenue" in df.columns:
        df["ev_revenue"] = df["ev_revenue"].apply(mult)
    drop = [c for c in ("deal_value_currency", "revenue_currency", "ebitda_currency") if c in df.columns]
    return df.drop(columns=drop)


@st.cache_data(ttl=60)
def deal_highlights(kind, eid, role):
    """Highlights across deals where the entity holds `role` ('acquirer'|'seller').
    Deal value & EBITDA are PER CURRENCY (largest in each — cross-currency nominal
    comparison is meaningless without FX). EV/EBITDA is a unitless single figure."""
    C = ("transaction_id, deal_value, deal_value_currency, ebitda, ebitda_currency, "
         "ev_ebitda, target_name, target_mergr_id, date")
    CT = ", ".join("t." + c.strip() for c in C.split(","))
    et = "company" if kind == "company" else "firms"
    base = (f"WITH tx AS (SELECT {CT} FROM transactions t "
            "JOIN transaction_parties p USING(transaction_id) "
            "WHERE p.entity_type=%(et)s AND p.entity_mergr_id=%(id)s AND p.role=%(role)s) ")
    p = {"id": eid, "et": et, "role": role}
    # largest deal value per currency, largest EBITDA per currency
    dv = q(base + "SELECT DISTINCT ON (deal_value_currency) deal_value_currency AS ccy, "
           "deal_value, transaction_id, target_mergr_id, target_name FROM tx "
           "WHERE deal_value IS NOT NULL ORDER BY deal_value_currency, deal_value DESC", p)
    eb = q(base + "SELECT DISTINCT ON (ebitda_currency) ebitda_currency AS ccy, "
           "ebitda, transaction_id, target_mergr_id, target_name FROM tx "
           "WHERE ebitda IS NOT NULL ORDER BY ebitda_currency, ebitda DESC", p)
    ev = q(base + "SELECT ev_ebitda, transaction_id, target_mergr_id, target_name FROM tx "
           "WHERE ev_ebitda BETWEEN 0 AND 100 ORDER BY ev_ebitda DESC LIMIT 1", p)
    # USD-normalised single largest (converts every currency via fx_rates)
    udv = q(base + "SELECT tx.transaction_id, tx.target_name, tx.deal_value, tx.deal_value_currency AS ccy, "
            "tx.deal_value*fx.usd_per_unit AS usd FROM tx JOIN fx_rates fx ON fx.currency=tx.deal_value_currency "
            "WHERE tx.deal_value IS NOT NULL AND fx.usd_per_unit IS NOT NULL ORDER BY usd DESC LIMIT 1", p)
    ueb = q(base + "SELECT tx.transaction_id, tx.target_name, tx.ebitda, tx.ebitda_currency AS ccy, "
            "tx.ebitda*fx.usd_per_unit AS usd FROM tx JOIN fx_rates fx ON fx.currency=tx.ebitda_currency "
            "WHERE tx.ebitda IS NOT NULL AND fx.usd_per_unit IS NOT NULL ORDER BY usd DESC LIMIT 1", p)
    return dv, eb, ev, udv, ueb


def render_highlights(kind, eid, role="acquirer", label="as buyer"):
    dv, eb, ev, udv, ueb = deal_highlights(kind, eid, role)
    if dv.empty and eb.empty and ev.empty:
        st.caption(f"Deal highlights ({label}): no deals with disclosed financials.")
        return
    st.write(f"**Deal highlights — {label}**")
    if not ev.empty:
        r = ev.iloc[0]
        st.markdown(f"Highest EV/EBITDA: **{mult(r['ev_ebitda'])}** "
                    f"([{r['target_name']}](?transaction={int(r['transaction_id'])}))")

    def by_ccy(df, valcol, title):
        if df.empty:
            return
        d = df.copy().sort_values(valcol, ascending=False)
        d["Largest"] = d.apply(lambda r: money(r[valcol], r["ccy"]), axis=1)
        d = d.rename(columns={"ccy": "Currency", "target_name": "Deal"})[
            ["Currency", "Largest", "Deal", "transaction_id", "target_mergr_id"]]
        st.caption(title)
        show(d)

    by_ccy(dv, "deal_value", "Largest deal value, by currency")
    by_ccy(eb, "ebitda", "Largest EBITDA, by currency")

    # USD-normalised single largest (across all currencies, via editable FX rates)
    if not udv.empty or not ueb.empty:
        st.caption("USD-normalised — largest overall, converting all currencies "
                   "(rates set in the Settings tab)")
        uc = st.columns(2)
        if not udv.empty:
            r = udv.iloc[0]
            uc[0].metric("Largest deal (≈USD)", money(r["usd"], "USD"),
                         help=f"{r['target_name']} — {money(r['deal_value'], r['ccy'])}")
        if not ueb.empty:
            r = ueb.iloc[0]
            uc[1].metric("Largest EBITDA (≈USD)", money(r["usd"], "USD"),
                         help=f"{r['target_name']} — {money(r['ebitda'], r['ccy'])}")


LINK_PREFIX = {
    "transaction_id":  "?transaction=",
    "company_id":      "?company=",
    "target_mergr_id": "?company=",
    "firm_id":         "?firm=",
}


def show(df, extra_links=None):
    """Interactive table; id columns become internal deep links."""
    if df.empty:
        st.caption("No rows."); return
    df = df.copy()
    cfg = {}
    links = dict(LINK_PREFIX)
    if extra_links:
        links.update(extra_links)
    for col, prefix in links.items():
        if col in df.columns:
            if prefix:
                df[col] = df[col].apply(
                    lambda v, p=prefix: f"{p}{int(v)}" if pd.notna(v) else None)
            cfg[col] = st.column_config.LinkColumn(col, display_text=r"=(\d+)$")
    st.dataframe(df, use_container_width=True, hide_index=True, column_config=cfg)


def show_named(df, name_col, link_col):
    """HTML table where `name_col` is itself a clickable internal link."""
    if df.empty:
        st.caption("No rows."); return
    df = df.copy()
    prefix = LINK_PREFIX.get(link_col, "?company=")
    df[name_col] = df.apply(
        lambda r: f"<a href='{prefix}{int(r[link_col])}' target='_self'>{r[name_col]}</a>"
        if pd.notna(r[name_col]) and pd.notna(r[link_col]) else (r[name_col] or ""), axis=1)
    for col, pfx in LINK_PREFIX.items():
        if col in df.columns:
            df[col] = df[col].apply(
                lambda v, p=pfx: f"<a href='{p}{int(v)}' target='_self'>{int(v)}</a>"
                if pd.notna(v) else "")
    st.markdown(df.to_html(escape=False, index=False, border=0), unsafe_allow_html=True)


def metric_link(col, label, value, href, sub):
    """Clickable metric card — the whole card is one link to a ?tab= section."""
    col.markdown(
        f"""<a href='{href}' target='_self' style='text-decoration:none;color:inherit'>
        <div style='line-height:1.2;padding:8px 10px;border:1px solid #2c2c2c;border-radius:8px'>
          <div style='font-size:0.8rem;color:#888'>{label}</div>
          <div style='font-size:2.1rem;font-weight:700'>{value:,}</div>
          <div style='font-size:0.75rem;color:#4c9a7a'>{sub} →</div>
        </div></a>""",
        unsafe_allow_html=True)


# --------------------------------------------------------------- detail renderers
def render_company(cid):
    rec = q("SELECT raw, revenue_currency, revenue_scale, "
            "(description_embedding IS NOT NULL) AS has_embedding "
            "FROM companies WHERE company_id=%(id)s", {"id": cid})
    if rec.empty:
        st.warning(f"No company with id {cid} in the DB."); return
    row = rec.iloc[0]
    raw = as_dict(row["raw"])
    st.subheader(f"🏢 {raw.get('name','?')}  ·  company {cid}")
    bits = []
    if raw.get("ticker"):         bits.append(f"**{raw['ticker']}**")
    if raw.get("stock_exchange"): bits.append(raw["stock_exchange"])
    if raw.get("website"):        bits.append(f"[Website ↗]({raw['website']})")
    if bits: st.markdown("  ·  ".join(bits))
    if raw.get("description"):
        st.write(raw["description"])

    # financials at a glance — use the CLEAN backfilled currency+scale columns
    cols = st.columns(5)
    cols[0].metric("Revenue", money(raw.get("revenue"), row["revenue_currency"], row["revenue_scale"]))
    cols[1].metric("Employees", raw.get("employees", "—"))
    cols[2].metric("Total buys", raw.get("total_buys", "—"))
    cols[3].metric("Total sells", raw.get("total_sells", "—"))
    cols[4].metric("Investors", raw.get("investor_count", "—"))

    rh = raw.get("revenue_history")
    if rh:
        _ab = SCALE_ABBR.get(row["revenue_scale"], "M")
        _cc = row["revenue_currency"] or ""
        st.write(f"**Revenue history** (in {_ab} {_cc})".rstrip())
        df = pd.DataFrame(rh)
        if "revenue" in df and "year" in df:
            df["revenue_num"] = pd.to_numeric(
                df["revenue"].astype(str).str.replace(",", ""), errors="coerce")
            st.line_chart(df.sort_values("year").set_index("year")["revenue_num"])
        st.dataframe(df, use_container_width=True, hide_index=True)

    st.write("**All scraped fields**")
    show_raw(raw, skip=("revenue_history", "description"))

    render_highlights("company", cid, "acquirer", "as buyer")
    render_highlights("company", cid, "seller", "as vendor")
    st.write("**As transaction target**")
    show(fmt_deals(q("""SELECT transaction_id, date, transaction_type,
                    deal_value, deal_value_currency, revenue, revenue_currency,
                    ebitda, ebitda_currency, ev_ebitda
              FROM transactions WHERE target_mergr_id=%(id)s
              ORDER BY date DESC NULLS LAST""", {"id": cid})))
    st.write("**As acquirer / seller**")
    show_named(fmt_deals(q("""SELECT t.transaction_id, t.date, p.role, t.target_mergr_id, t.target_name,
                    t.deal_value, t.deal_value_currency, t.ebitda, t.ebitda_currency, t.ev_ebitda
              FROM transaction_parties p JOIN transactions t USING (transaction_id)
              WHERE p.entity_type='company' AND p.entity_mergr_id=%(id)s
              ORDER BY t.date DESC NULLS LAST""", {"id": cid})), "target_name", "target_mergr_id")


def render_firm(fid):
    rec = q("SELECT raw FROM firms WHERE firm_id=%(id)s", {"id": fid})
    if rec.empty:
        st.warning(f"No firm with id {fid} in the DB."); return
    raw = as_dict(rec.iloc[0]["raw"])
    st.subheader(f"💼 {raw.get('name','?')}  ·  firm {fid}")
    links = []
    if raw.get("website"):  links.append(f"[Website ↗]({raw['website']})")
    if raw.get("linkedin"): links.append(f"[LinkedIn ↗]({raw['linkedin']})")
    if links: st.markdown("  ·  ".join(links))
    if raw.get("investment_criteria_description"):
        st.write(raw["investment_criteria_description"])
    cols = st.columns(4)
    cols[0].metric("Total buys", raw.get("total_buys", "—"))
    cols[1].metric("Total sells", raw.get("total_sells", "—"))
    cols[2].metric("PE assets", raw.get("pe_assets", "—"))
    cols[3].metric("Type", raw.get("investor_type", "—"))
    st.write("**All scraped fields**")
    show_raw(raw, skip=("investment_criteria_description",))
    render_highlights("firm", fid, "acquirer", "as buyer")
    render_highlights("firm", fid, "seller", "as vendor")
    st.write("**Deals involving this firm**")
    show_named(fmt_deals(q("""SELECT t.transaction_id, t.date, t.transaction_type, p.role,
                     t.target_mergr_id, t.target_name, t.target_sector,
                     t.deal_value, t.deal_value_currency, t.ebitda, t.ebitda_currency, t.ev_ebitda
              FROM transaction_parties p JOIN transactions t USING (transaction_id)
              WHERE p.entity_type='firms' AND p.entity_mergr_id=%(id)s
              ORDER BY t.date DESC NULLS LAST LIMIT 500""", {"id": fid})), "target_name", "target_mergr_id")


def render_transaction(tid):
    rec = q("""SELECT raw, deal_value, deal_value_currency, revenue, revenue_currency,
                      ebitda, ebitda_currency, ev_revenue, ev_ebitda, financials_scraped_at
               FROM transactions WHERE transaction_id=%(id)s""", {"id": tid})
    if rec.empty:
        st.warning(f"No transaction with id {tid} in the DB."); return
    r = rec.iloc[0]
    raw = as_dict(r["raw"])
    tgt = (raw.get("target") or {})
    st.subheader(f"🤝 Transaction {tid} · {raw.get('transaction_type','')} · {raw.get('date','')}")
    meta = []
    if tgt.get("mergr_id") is not None:
        meta.append(f"**Target:** [{tgt.get('name')}](?company={int(tgt['mergr_id'])})")
    if tgt.get("sector"):   meta.append(tgt["sector"])
    if tgt.get("location"): meta.append(tgt["location"])
    if meta: st.markdown("  ·  ".join(meta))
    if tgt.get("description"):
        st.write(tgt["description"])

    # financials (detail-scraped; values in millions of the stated currency)
    st.write("**Financials** (values in millions)")
    fc = st.columns(5)
    fc[0].metric("Deal value", money(r["deal_value"], r["deal_value_currency"]))
    fc[1].metric("Revenue", money(r["revenue"], r["revenue_currency"]))
    fc[2].metric("EBITDA", money(r["ebitda"], r["ebitda_currency"]))
    fc[3].metric("EV / Revenue", mult(r["ev_revenue"]))
    fc[4].metric("EV / EBITDA", mult(r["ev_ebitda"]))
    if pd.isna(r["financials_scraped_at"]):
        st.caption("Financials not yet detail-scraped for this deal (or none disclosed).")

    st.write("**All scraped fields**")
    show_raw(raw, skip=("acquirers", "sellers", "target"))
    st.write("**Parties**")
    show(q("""SELECT role, entity_type, entity_mergr_id, name, sub_type,
                     CASE WHEN entity_type='firms' THEN '?firm='||entity_mergr_id
                          ELSE '?company='||entity_mergr_id END AS link
              FROM transaction_parties WHERE transaction_id=%(id)s ORDER BY role""",
           {"id": tid}), extra_links={"link": None})


def _int(v):
    try: return int(v)
    except (TypeError, ValueError): return None


def as_dict(raw):
    if isinstance(raw, dict): return raw
    if isinstance(raw, str):
        try: return json.loads(raw)
        except Exception: return {}
    return {}


def show_raw(raw, skip=()):
    """Render EVERY field from the raw scraped JSON so nothing is hidden.
    Scalars go in one table; list-of-dict fields (e.g. revenue_history) each
    get their own table."""
    raw = as_dict(raw)
    if not raw:
        return
    scalars = {k: v for k, v in raw.items()
               if k not in skip and not isinstance(v, (list, dict))}
    nested = {k: v for k, v in raw.items()
              if k not in skip and isinstance(v, (list, dict))}
    if scalars:
        st.write("**All fields**")
        st.dataframe(pd.DataFrame(scalars.items(), columns=["field", "value"]),
                     use_container_width=True, hide_index=True)
    for k, v in nested.items():
        st.write(f"**{k}**")
        if isinstance(v, list) and v and isinstance(v[0], dict):
            st.dataframe(pd.DataFrame(v), use_container_width=True, hide_index=True)
        else:
            st.write(v)


PAGE_SIZE = 50


def pager(total, key):
    """Page control; returns SQL OFFSET. Shows 'm–n of total'."""
    pages = max(1, -(-total // PAGE_SIZE))
    c1, c2 = st.columns([1, 3])
    page = c1.number_input(f"Page (of {pages:,})", 1, pages, 1, key=key)
    off = (page - 1) * PAGE_SIZE
    c2.caption(f"Showing {off+1:,}–{min(off+PAGE_SIZE, total):,} of {total:,}")
    return off


# =============================================================================
st.title("Mergr Relationship Explorer")
qp = st.query_params

# ---- 1. record detail pages take precedence -------------------------------
for key, fn in (("company", render_company), ("firm", render_firm),
                ("transaction", render_transaction)):
    if key in qp and _int(qp[key]) is not None:
        st.markdown("[← back to explorer](?tab=overview)")
        fn(_int(qp[key]))
        st.stop()   # a detail page is standalone — no nav/sections below

# ---- 2. single nav, driven entirely by ?tab ------------------------------
SECTIONS = [("overview", "Overview"), ("firms", "Firms"), ("companies", "Companies"),
            ("transactions", "Transactions"), ("domain", "Domain search"),
            ("vector", "Vector search"), ("settings", "Settings")]
section = qp.get("tab", "overview")
if section not in dict(SECTIONS):
    section = "overview"

st.markdown(
    " &nbsp;&nbsp;|&nbsp;&nbsp; ".join(
        f"<a href='?tab={k}' target='_self' style='text-decoration:none;"
        f"font-weight:{800 if k == section else 400};"
        f"border-bottom:{'2px solid #2e8b6f' if k == section else 'none'};padding-bottom:2px;"
        f"color:{'inherit' if k == section else '#2e8b6f'}'>{label}</a>"
        for k, label in SECTIONS),
    unsafe_allow_html=True)
st.divider()

# ---- 3. sections ----------------------------------------------------------
if section == "overview":
    c1, c2, c3, c4 = st.columns(4)
    metric_link(c1, "Firms", int(q("SELECT count(*) n FROM firms").n[0]),
                "?tab=firms", "browse firms")
    metric_link(c2, "Companies", int(q("SELECT count(*) n FROM companies").n[0]),
                "?tab=companies", "browse companies")
    metric_link(c3, "Transactions", int(q("SELECT count(*) n FROM transactions").n[0]),
                "?tab=transactions", "browse transactions")
    metric_link(c4, "Parties", int(q("SELECT count(*) n FROM transaction_parties").n[0]),
                "?tab=transactions", "browse transactions")

    st.subheader("Data completeness (gap analysis)")
    st.dataframe(q("""SELECT entity_type,
                             count(*) FILTER (WHERE have_record)     AS have,
                             count(*) FILTER (WHERE NOT have_record) AS missing
                      FROM v_party_resolution GROUP BY entity_type"""),
                 use_container_width=True, hide_index=True)

    st.subheader("Transactions per year")
    st.bar_chart(q("""SELECT extract(year FROM date)::int AS year, count(*) AS n
                      FROM transactions WHERE date IS NOT NULL
                      GROUP BY 1 ORDER BY 1""").set_index("year"))

elif section == "firms":
    term = st.text_input("Search firms by name (blank = all, most active first)", key="firm_q")
    where = "WHERE name ILIKE %(t)s" if term else ""
    p = {"t": f"%{term}%"} if term else {}
    total = int(q(f"SELECT count(*) n FROM firms {where}", p).n[0])
    off = pager(total, "firm_pg")
    show_named(q(f"""SELECT firm_id, name, investor_type, size_category,
                            geographic_preferences, total_buys, total_sells
                     FROM firms {where}
                     ORDER BY total_buys DESC NULLS LAST
                     LIMIT {PAGE_SIZE} OFFSET {off}""", p or None), "name", "firm_id")
    fid = st.number_input("Inspect firm_id", min_value=0, step=1, value=0)
    if fid:
        render_firm(int(fid))

elif section == "companies":
    term = st.text_input("Search companies by name (blank = all, most investors first)", key="comp_q")
    where = "WHERE name ILIKE %(t)s" if term else ""
    order = "name" if term else "investor_count DESC NULLS LAST"
    p = {"t": f"%{term}%"} if term else {}
    total = int(q(f"SELECT count(*) n FROM companies {where}", p or None).n[0])
    off = pager(total, "comp_pg")
    show_named(q(f"""SELECT company_id, name, sector, city, established, investor_count
                     FROM companies {where} ORDER BY {order}
                     LIMIT {PAGE_SIZE} OFFSET {off}""", p or None), "name", "company_id")
    cid = st.number_input("Inspect company_id", min_value=0, step=1, value=0)
    if cid:
        render_company(int(cid))

elif section == "transactions":
    col1, col2, col3 = st.columns(3)
    ttype = col1.text_input("Type contains", key="tx_type")
    year = col2.text_input("Year (YYYY)", key="tx_year")
    fin_only = col3.checkbox("Only deals with financials", key="tx_fin")
    where, params = [], {}
    if ttype:
        where.append("transaction_type ILIKE %(ty)s"); params["ty"] = f"%{ttype}%"
    if year.isdigit():
        where.append("extract(year FROM date)=%(yr)s"); params["yr"] = int(year)
    if fin_only:
        where.append("deal_value IS NOT NULL")
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    total = int(q(f"SELECT count(*) n FROM transactions {clause}", params or None).n[0])
    off = pager(total, "tx_pg")
    txdf = q(f"""SELECT transaction_id, target_mergr_id, date, transaction_type, target_name,
                        deal_value, deal_value_currency, ebitda, ebitda_currency, ev_ebitda
                 FROM transactions {clause}
                 ORDER BY date DESC NULLS LAST
                 LIMIT {PAGE_SIZE} OFFSET {off}""", params or None)
    if not txdf.empty:
        txdf["deal_value"] = txdf.apply(lambda r: money(r["deal_value"], r["deal_value_currency"]), axis=1)
        txdf["ebitda"] = txdf.apply(lambda r: money(r["ebitda"], r["ebitda_currency"]), axis=1)
        txdf["ev_ebitda"] = txdf["ev_ebitda"].apply(mult)
        txdf = txdf.drop(columns=["deal_value_currency", "ebitda_currency"])
    show_named(txdf, "target_name", "target_mergr_id")
    tid = st.number_input("Inspect transaction_id", min_value=0, step=1, value=0)
    if tid:
        render_transaction(int(tid))

elif section == "domain":
    st.write("Find firms **and** companies by domain. Paste a domain or a full "
             "website URL — both normalize to the same domain key.")
    raw = st.text_input("Domain or website", placeholder="e.g. harvest.fr  or  https://www.harvest.fr/about")
    if raw:
        dom = website_to_domain(raw)
        if not dom:
            st.warning("Couldn't parse a domain from that input.")
        else:
            st.caption(f"Matching on domain: **{dom}**")
            firms = q("""SELECT firm_id, name, investor_type, website
                         FROM firms WHERE domain=%(d)s ORDER BY name""", {"d": dom})
            comps = q("""SELECT company_id, name, sector, city, website
                         FROM companies WHERE domain=%(d)s ORDER BY name""", {"d": dom})
            st.subheader(f"Firms ({len(firms)})")
            show_named(firms, "name", "firm_id")
            st.subheader(f"Companies ({len(comps)})")
            show_named(comps, "name", "company_id")
            if firms.empty and comps.empty:
                st.info(f"No firm or company found with domain {dom}.")

elif section == "settings":
    st.subheader("Currency conversion rates")
    st.caption("USD per 1 unit of each currency — used for the USD-normalised deal "
               "highlights. Set as of today; update periodically as you like.")
    cur_asof = q("SELECT max(as_of) m FROM fx_rates").m[0]
    st.write(f"Rates currently **as of {cur_asof}**.")
    fx = q("SELECT currency, usd_per_unit FROM fx_rates ORDER BY currency")
    edited = st.data_editor(fx, use_container_width=True, hide_index=True,
                            disabled=["currency"], num_rows="fixed", key="fx_editor")
    asof = st.date_input("Set 'as of' date for these rates", value=pd.Timestamp.today().date())
    if st.button("💾 Save rates"):
        cx = conn().cursor()
        for _, r in edited.iterrows():
            val = None if pd.isna(r["usd_per_unit"]) else float(r["usd_per_unit"])
            cx.execute("UPDATE fx_rates SET usd_per_unit=%s, as_of=%s, updated_at=now() "
                       "WHERE currency=%s", (val, asof, r["currency"]))
        conn().commit()
        st.cache_data.clear()
        st.success(f"Saved {len(edited)} rates as of {asof}. USD-normalised highlights updated.")

elif section == "vector":
    st.write("Semantic search over company descriptions / firm criteria.")
    if not os.environ.get("OPENAI_API_KEY"):
        st.info("Set OPENAI_API_KEY and backfill embeddings to enable this tab.")
    else:
        text = st.text_input("Describe what you're looking for")
        target = st.selectbox("Search in", ["companies", "firms"])
        if text:
            from openai import OpenAI
            emb = OpenAI().embeddings.create(
                model="text-embedding-3-small", input=text).data[0].embedding
            vec = "[" + ",".join(map(str, emb)) + "]"
            if target == "companies":
                show_named(q("""SELECT company_id, name, sector, city,
                                  1-(description_embedding<=>%(v)s::vector) AS score
                               FROM companies WHERE description_embedding IS NOT NULL
                               ORDER BY description_embedding<=>%(v)s::vector LIMIT 25""",
                            {"v": vec}), "name", "company_id")
            else:
                show_named(q("""SELECT firm_id, name, investor_type,
                                  1-(criteria_embedding<=>%(v)s::vector) AS score
                               FROM firms WHERE criteria_embedding IS NOT NULL
                               ORDER BY criteria_embedding<=>%(v)s::vector LIMIT 25""",
                            {"v": vec}), "name", "firm_id")
