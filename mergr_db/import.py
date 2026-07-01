#!/usr/bin/env python3
"""
Import scraped Mergr JSON (firms, companies, transactions) into Postgres.

Idempotent: re-running upserts rows. Reads the JSON directories that live in the
parent project folder (mergr_investors/, mergr_companies/, mergr_transactions/).

Env:
  DATABASE_URL   postgres connection string
  DATA_DIR       parent dir holding the mergr_* folders (default: ..)
  LIMIT          optional int, cap files per type (for quick test runs)
"""
import os, sys, json, glob, re
import psycopg2
from psycopg2.extras import execute_values, Json

DATA_DIR = os.environ.get("DATA_DIR", "..")
DSN      = os.environ["DATABASE_URL"]
LIMIT    = int(os.environ["LIMIT"]) if os.environ.get("LIMIT") else None
BATCH    = 1000


def num(v):
    """Best-effort numeric parse; returns None on junk."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return v
    s = str(v).strip().replace(",", "")
    if s in ("", "-", "N/A", "n/a", "null"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def to_int(v):
    n = num(v)
    return int(n) if n is not None else None


def date_or_none(v):
    if not v or not isinstance(v, str):
        return None
    v = v.strip()
    if not v:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        return v
    if re.fullmatch(r"\d{4}-\d{2}", v):          # year-month -> 1st
        return v + "-01"
    if re.fullmatch(r"\d{4}", v):                # year-only -> Jan 1 (old deals)
        return v + "-01-01"
    return None                                   # unparseable -> null


def records(folder):
    pattern = os.path.join(DATA_DIR, folder, "*.json")
    files = glob.glob(pattern)
    if LIMIT:
        files = files[:LIMIT]
    print(f"  {folder}: {len(files)} files", flush=True)
    for fp in files:
        try:
            d = json.load(open(fp))
        except Exception as e:
            print(f"    skip {fp}: {e}", file=sys.stderr)
            continue
        # some files are single dicts, a few are [dict]
        if isinstance(d, list):
            d = d[0] if d else None
        if isinstance(d, dict):
            yield d


def flush(cur, sql, rows):
    if rows:
        execute_values(cur, sql, rows, page_size=BATCH)
        rows.clear()


# --------------------------------------------------------------------------- firms
FIRM_COLS = [
    "firm_id","name","legal_name","address_raw","website","email","phone","linkedin",
    "investor_type","ownership","size_category","pe_assets","established","specialist_generalist",
    "investment_criteria_description","sectors_of_interest","target_transaction_types",
    "geographic_preferences","target_revenue_min","target_revenue_max","target_ebitda_min",
    "target_ebitda_max","investment_size_min","investment_size_max","enterprise_value_min",
    "enterprise_value_max","criteria_currency","buy_rate_per_year","total_buys","sell_rate_per_year",
    "total_sells","total_buy_volume","largest_buy","total_sell_volume","largest_sell","ma_by_sector","raw",
]
FIRM_SQL = f"""
INSERT INTO firms ({",".join(FIRM_COLS)}) VALUES %s
ON CONFLICT (firm_id) DO UPDATE SET
""" + ", ".join(f"{c}=EXCLUDED.{c}" for c in FIRM_COLS if c != "firm_id")


def firm_row(d):
    fid = to_int(d.get("firm_id"))
    if fid is None:
        return None
    return (
        fid, d.get("name"), d.get("legal_name"), d.get("address_raw"), d.get("website"),
        d.get("email"), d.get("phone"), d.get("linkedin"), d.get("investor_type"),
        d.get("ownership"), d.get("size_category"), d.get("pe_assets"), d.get("established"),
        d.get("specialist_generalist"), d.get("investment_criteria_description"),
        d.get("sectors_of_interest"), d.get("target_transaction_types"),
        d.get("geographic_preferences"), num(d.get("target_revenue_min")),
        num(d.get("target_revenue_max")), num(d.get("target_ebitda_min")),
        num(d.get("target_ebitda_max")), num(d.get("investment_size_min")),
        num(d.get("investment_size_max")), num(d.get("enterprise_value_min")),
        num(d.get("enterprise_value_max")), d.get("criteria_currency"),
        num(d.get("buy_rate_per_year")), to_int(d.get("total_buys")),
        num(d.get("sell_rate_per_year")), to_int(d.get("total_sells")),
        d.get("total_buy_volume"), d.get("largest_buy"), d.get("total_sell_volume"),
        d.get("largest_sell"),
        Json(d.get("ma_by_sector")) if d.get("ma_by_sector") is not None else None,
        Json(d),
    )


# ----------------------------------------------------------------------- companies
COMP_COLS = [
    "company_id","name","legal_name","street","city","state_full","postal_code","phone",
    "website","sector","established","description","investor_count","raw",
]
COMP_SQL = f"""
INSERT INTO companies ({",".join(COMP_COLS)}) VALUES %s
ON CONFLICT (company_id) DO UPDATE SET
""" + ", ".join(f"{c}=EXCLUDED.{c}" for c in COMP_COLS if c != "company_id")


def company_row(d):
    cid = to_int(d.get("company_id"))
    if cid is None:
        return None
    return (
        cid, d.get("name"), d.get("legal_name"), d.get("street"), d.get("city"),
        d.get("state_full"), d.get("postal_code"), d.get("phone"), d.get("website"),
        d.get("sector"), d.get("established"), d.get("description"),
        to_int(d.get("investor_count")), Json(d),
    )


# -------------------------------------------------------------------- transactions
TX_COLS = [
    "transaction_id","date","transaction_url","transaction_type","target_mergr_id",
    "target_name","target_sector","target_location","target_description","raw",
]
TX_SQL = f"""
INSERT INTO transactions ({",".join(TX_COLS)}) VALUES %s
ON CONFLICT (transaction_id) DO UPDATE SET
""" + ", ".join(f"{c}=EXCLUDED.{c}" for c in TX_COLS if c != "transaction_id")

PARTY_SQL = """
INSERT INTO transaction_parties
    (transaction_id, role, entity_type, entity_mergr_id, name, label, sub_type)
VALUES %s
ON CONFLICT (transaction_id, role, entity_type, entity_mergr_id) DO NOTHING
"""


def tx_rows(d):
    tid = to_int(d.get("transaction_id"))
    if tid is None:
        return None, []
    tgt = d.get("target") or {}
    tx = (
        tid, date_or_none(d.get("date")), d.get("transaction_url"), d.get("transaction_type"),
        to_int(tgt.get("mergr_id")), tgt.get("name"), tgt.get("sector"),
        tgt.get("location"), tgt.get("description"), Json(d),
    )
    parties = []
    for role, key in (("acquirer", "acquirers"), ("seller", "sellers")):
        for e in d.get(key) or []:
            emid = to_int(e.get("mergr_id"))
            et = e.get("entity_type")
            if emid is None or et not in ("firms", "company"):
                continue
            parties.append((tid, role, et, emid, e.get("name"), e.get("label"), e.get("sub_type")))
    return tx, parties


def load_simple(cur, folder, sql, rowfn):
    rows, n = [], 0
    for d in records(folder):
        r = rowfn(d)
        if r:
            rows.append(r); n += 1
        if len(rows) >= BATCH:
            flush(cur, sql, rows)
    flush(cur, sql, rows)
    return n


def load_transactions(cur):
    tx_buf, party_buf, n = [], [], 0
    for d in records("mergr_transactions"):
        tx, parties = tx_rows(d)
        if tx:
            tx_buf.append(tx); n += 1
        party_buf.extend(parties)
        # Parties FK->transactions, so the parent rows must be inserted first.
        # Whenever either buffer is full, flush transactions before parties.
        if len(tx_buf) >= BATCH or len(party_buf) >= BATCH:
            flush(cur, TX_SQL, tx_buf)
            if len(party_buf) >= BATCH:
                flush(cur, PARTY_SQL, party_buf)
    flush(cur, TX_SQL, tx_buf)        # parents first
    flush(cur, PARTY_SQL, party_buf)  # then remaining parties
    return n


def main():
    print(f"Connecting… DATA_DIR={DATA_DIR}", flush=True)
    conn = psycopg2.connect(DSN)
    conn.autocommit = False
    with conn, conn.cursor() as cur:
        print("Loading firms…", flush=True)
        nf = load_simple(cur, "mergr_investors", FIRM_SQL, firm_row); conn.commit()
        print(f"  -> {nf} firms", flush=True)

        print("Loading companies…", flush=True)
        nc = load_simple(cur, "mergr_companies", COMP_SQL, company_row); conn.commit()
        print(f"  -> {nc} companies", flush=True)

        print("Loading transactions + parties…", flush=True)
        nt = load_transactions(cur); conn.commit()
        print(f"  -> {nt} transactions", flush=True)
    conn.close()
    print("Done.", flush=True)


if __name__ == "__main__":
    main()
