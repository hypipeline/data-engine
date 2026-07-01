#!/usr/bin/env python3
"""
Parse a Mergr transaction DETAIL page for the deal facts + financials block
(<div class="tr-footer">). Captures value, revenue, EBITDA, and the EV/Revenue
and EV/EBITDA multiples — the financial data that is NOT in the search rows.
(We deliberately skip the announcement/news link per requirements.)
"""
import re
from bs4 import BeautifulSoup

_MONEY = ("USD", "EUR", "GBP", "CAD", "AUD", "JPY", "CHF", "SEK", "NOK", "DKK")
_METRIC_KEY = {
    "Revenue": "revenue", "EBITDA": "ebitda",
    "EV/Revenue": "ev_revenue", "EV/EBITDA": "ev_ebitda",
    "Enterprise Value": "enterprise_value",
}


def _num(s):
    """'1,200' -> 1200.0 ; '3.7' -> 3.7 ; junk -> None."""
    if not s:
        return None
    s = s.replace(",", "").strip()
    m = re.search(r"-?\d+(\.\d+)?", s)
    return float(m.group(0)) if m else None


def parse_txn_detail(html, tid):
    soup = BeautifulSoup(html, "html.parser")
    out = {"transaction_id": tid}
    foot = soup.find("div", class_="tr-footer")
    if not foot:
        return out

    # h4 -> p pairs: Sector, Transaction Type, Value
    for h4 in foot.find_all("h4"):
        label = h4.get_text(strip=True)
        p = h4.find_next_sibling("p")
        if not p:
            continue
        if label == "Value":
            amt = p.find("span")
            cur = p.find("small")
            out["deal_value"] = _num(amt.get_text(strip=True)) if amt else None
            out["deal_value_currency"] = cur.get_text(strip=True) if cur else None
        elif label == "Sector":
            out["sector"] = p.get_text(strip=True)
        elif label == "Transaction Type":
            out["transaction_type"] = p.get_text(strip=True)

    # ul.clearfix li: <small>label</small> <strong>amount <small>unit</small></strong>
    for li in foot.select("ul li"):
        small = li.find("small")
        strong = li.find("strong")
        if not (small and strong):
            continue
        label = small.get_text(strip=True)
        key = _METRIC_KEY.get(label)
        if not key:
            continue
        unit_el = strong.find("small")
        unit = unit_el.get_text(strip=True) if unit_el else ""
        amount_txt = strong.get_text(" ", strip=True)
        if unit:
            amount_txt = amount_txt.replace(unit, "")
        out[key] = _num(amount_txt)
        if unit in _MONEY:
            out[key + "_currency"] = unit
        # multiples carry unit 'x' -> value already captured as a float
    return out


if __name__ == "__main__":
    import sys, json, glob
    files = sys.argv[1:] or glob.glob(
        "/private/tmp/claude-501/-Users-craiganderson-Dropbox-dev-on-testing/"
        "b27477e6-ee7a-4d71-aeab-f3055a1e0615/scratchpad/txnfin_*.html")
    for f in files:
        tid = int(re.search(r"(\d+)\.html$", f).group(1))
        print(f"\n{f}")
        print(json.dumps(parse_txn_detail(open(f).read(), tid), indent=2))


def parse_txn_parties(html):
    """Extract acquirers/sellers from a transaction DETAIL page's Investor(s)/
    Seller(s) sections (each party is an <h4><a href=/firms|company/id>Name</a>)."""
    soup = BeautifulSoup(html, "html.parser")
    out = {"acquirers": [], "sellers": []}
    for sec in soup.find_all("div", class_="tra-section"):
        h3 = sec.find("h3")
        if not h3:
            continue
        head = h3.get_text(" ", strip=True).lower()
        if any(k in head for k in ("investor", "buyer", "acquirer")):
            role = "acquirers"
        elif "seller" in head:
            role = "sellers"
        else:
            continue
        for h4 in sec.find_all("h4"):
            a = h4.find("a", href=re.compile(r'/(firms|company)/(\d+)'))
            if a:
                m = re.search(r'/(firms|company)/(\d+)', a["href"])
                out[role].append({"entity_type": m.group(1),
                                   "mergr_id": int(m.group(2)),
                                   "name": a.get_text(strip=True)})
    return out
