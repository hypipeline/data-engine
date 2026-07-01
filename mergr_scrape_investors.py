"""
Mergr.com investor scraper.
Iterates /firms/{id} pages (1–10000), extracts structured data,
and saves to JSON. Respectful speed: 5s between requests.
Invalid IDs return 302 and are skipped instantly.
"""
import json
import os
import re
import time
import requests
from bs4 import BeautifulSoup

EMAIL = "craig.anderson@hyndlandpartners.com"
PASSWORD = "X9R/N^3RvjtuJ.^"
OUTPUT_DIR = "/Users/craiganderson/Dropbox/dev/on-testing/data-engine/mergr_investors"
MAX_ID = 10000
DELAY = 5  # seconds between full page fetches


def create_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.9",
    })
    return s


def login(session):
    session.get("https://mergr.com/login")
    time.sleep(2)
    r = session.post("https://mergr.com/login?redirectTo=", data={
        "username": EMAIL,
        "password": PASSWORD,
    }, allow_redirects=True)
    if "/dashboard" in r.url or "/login" not in r.url:
        print("Logged in OK")
        return True
    print("LOGIN FAILED")
    return False


def parse_firms_page(html, firm_id):
    soup = BeautifulSoup(html, "html.parser")
    data = {"firm_id": firm_id, "url": f"https://mergr.com/firms/{firm_id}"}

    # Name from H2 (firms page uses h2, not h1)
    h2 = soup.find("h2")
    if h2:
        data["name"] = h2.get_text(strip=True)

    # Legal name (h3 with class h5)
    for h3 in soup.find_all("h3", class_="h5"):
        text = h3.get_text(strip=True)
        if text:
            data["legal_name"] = text
            break

    # Breadcrumb — extract country
    breadcrumbs = [li.get_text(strip=True) for li in soup.find_all("li", class_="breadcrumb-item")]
    if len(breadcrumbs) >= 4:
        data["country"] = breadcrumbs[-2]

    # Address, phone, website, email
    addr_el = soup.find("p", class_="adress-info")
    if addr_el:
        addr_text = addr_el.get_text(separator="|", strip=True)
        data["address_raw"] = addr_text
        # Website (skip mergr.com links)
        a_web = addr_el.find("a", href=lambda h: h and h.startswith("http") and "mergr.com" not in h)
        if a_web:
            data["website"] = a_web["href"]
        else:
            www_match = re.search(r"(www\.\S+)", addr_text)
            if www_match:
                data["website"] = "https://" + www_match.group(1)
        # Email
        a_email = addr_el.find("a", href=lambda h: h and h.startswith("mailto:"))
        if a_email:
            data["email"] = a_email["href"].replace("mailto:", "")
        # Phone
        a_phone = addr_el.find("a", href=lambda h: h and h.startswith("tel:"))
        if a_phone:
            data["phone"] = a_phone.get_text(strip=True)
        else:
            phone_match = re.search(r"(\+[\d\s().-]{7,})", addr_text)
            if phone_match:
                data["phone"] = phone_match.group(1).strip()

    # LinkedIn
    for a in soup.find_all("a", href=True):
        if "linkedin.com/company" in a["href"]:
            data["linkedin"] = a["href"]
            break

    # Investor Summary — h4 label -> next p value
    summary_map = {
        "Investor Type": "investor_type",
        "Ownership": "ownership",
        "Size": "size_category",
        "PE Assets": "pe_assets",
        "Established": "established",
        "Specialist/Generalist": "specialist_generalist",
    }
    for h4 in soup.find_all("h4"):
        label = h4.get_text(strip=True)
        if label in summary_map:
            p = h4.find_next("p")
            if p:
                data[summary_map[label]] = p.get_text(strip=True)

    # Firm description (in .firm-desc div, before Investment Criteria heading)
    firm_desc = soup.find("div", class_="firm-desc")
    if firm_desc:
        p = firm_desc.find("p")
        if p:
            data["investment_criteria_description"] = p.get_text(strip=True)[:2000]

    # Investment Criteria — structured fields (sectors, transaction types, geo, table)
    for h3 in soup.find_all("h3"):
        if "investment criteria" not in h3.get_text(strip=True).lower():
            continue
        # The criteria content is in the next sibling div
        criteria_div = h3.find_next_sibling("div")
        if not criteria_div:
            break

        for p in criteria_div.find_all("p"):
            pt = p.get_text(strip=True)
            if pt.startswith("Sectors of Interest:"):
                data["sectors_of_interest"] = pt.replace("Sectors of Interest:", "").strip()
            elif pt.startswith("Target Transaction Types:"):
                data["target_transaction_types"] = pt.replace("Target Transaction Types:", "").strip()
            elif pt.startswith("Geographic Preferences:"):
                data["geographic_preferences"] = pt.replace("Geographic Preferences:", "").strip()

        # Transaction criteria table (min/max)
        for table in criteria_div.find_all("table"):
            for row in table.find_all("tr"):
                cells = [td.get_text(strip=True) for td in row.find_all("td")]
                if len(cells) >= 3:
                    metric, min_val, max_val = cells[0], cells[1], cells[2]
                    min_val = None if min_val == "-" else min_val
                    max_val = None if max_val == "-" else max_val
                    key_map = {
                        "Target Revenue": ("target_revenue_min", "target_revenue_max"),
                        "Target EBITDA": ("target_ebitda_min", "target_ebitda_max"),
                        "Investment Size": ("investment_size_min", "investment_size_max"),
                        "Enterprise Value": ("enterprise_value_min", "enterprise_value_max"),
                    }
                    if metric in key_map:
                        data[key_map[metric][0]] = min_val
                        data[key_map[metric][1]] = max_val
                if len(cells) == 1 and "millions" in cells[0].lower():
                    data["criteria_currency"] = cells[0]
        break

    # M&A Summary — Buy vs Sell yearly table
    ma_summary = soup.find("h3", string=lambda s: s and "M&A Summary" in s if s else False)
    if ma_summary:
        section = ma_summary.find_parent()
        if section:
            for table in section.find_all("table"):
                for row in table.find_all("tr"):
                    cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
                    row_text = " ".join(cells)

                    # Buy/Sell rate and totals
                    buy_match = re.search(r"Buy\(([\d.]+)/yr\)", row_text)
                    if buy_match:
                        data["buy_rate_per_year"] = float(buy_match.group(1))
                        nums = re.findall(r"\d+", row_text)
                        if nums:
                            data["total_buys"] = int(nums[-1])
                    sell_match = re.search(r"Sell\(([\d.]+)/yr\)", row_text)
                    if sell_match:
                        data["sell_rate_per_year"] = float(sell_match.group(1))
                        nums = re.findall(r"\d+", row_text)
                        if nums:
                            data["total_sells"] = int(nums[-1])

                    # Buy/sell volume rows
                    if row_text.startswith("vol") and "Buy" not in row_text and "Sell" not in row_text:
                        vol_match = re.findall(r"\$([\d.,]+[BMK]?)", row_text)
                        # Handled below in buy/sell specific tables

                    # Largest buy/sell
                    if "Largest" in row_text:
                        largest_match = re.search(r"Largest\s+(.+?)(\$[\d.,]+[BMK]?)\((\d{4}-\d{2}-\d{2})\)", row_text)
                        if largest_match:
                            deal_name = largest_match.group(1).strip()
                            deal_value = largest_match.group(2)
                            deal_date = largest_match.group(3)
                            # Determine if buy or sell by context
                            # Check preceding rows in same table
                            prev_text = ""
                            for prev_row in table.find_all("tr"):
                                prev_cells = [td.get_text(strip=True) for td in prev_row.find_all(["td", "th"])]
                                if "Largest" in " ".join(prev_cells):
                                    break
                                prev_text = " ".join(prev_cells)
                            if "buy" in prev_text.lower() or "buy" in cells[0].lower() if cells else False:
                                data["largest_buy"] = f"{deal_name} {deal_value} ({deal_date})"
                            elif "sell" in prev_text.lower() or "sell" in cells[0].lower() if cells else False:
                                data["largest_sell"] = f"{deal_name} {deal_value} ({deal_date})"
                            else:
                                # Use table header to determine
                                header = table.find("th") or table.find("td")
                                if header:
                                    ht = header.get_text(strip=True).lower()
                                    if "buy" in ht:
                                        data["largest_buy"] = f"{deal_name} {deal_value} ({deal_date})"
                                    else:
                                        data["largest_sell"] = f"{deal_name} {deal_value} ({deal_date})"

                    # Total volume
                    if "TOTAL" in row_text:
                        total_match = re.search(r"\$([\d.,]+[BMK]?)", row_text)
                        if total_match:
                            header = table.find("th") or table.find("td")
                            if header:
                                ht = header.get_text(strip=True).lower()
                                if "buy" in ht:
                                    data["total_buy_volume"] = "$" + total_match.group(1)
                                elif "sell" in ht:
                                    data["total_sell_volume"] = "$" + total_match.group(1)

    # Most Recent M&A
    recent_deals = []
    recent_header = soup.find("h4", string=lambda s: s and "Most Recent M&A" in s if s else False)
    if recent_header:
        table = recent_header.find_next("table")
        if table:
            for row in table.find_all("tr"):
                cells = row.find_all("td")
                if len(cells) >= 3:
                    company_cell = cells[0]
                    h5 = company_cell.find("h5")
                    if h5:
                        a = company_cell.find("a", href=lambda h: h and "/company/" in h)
                        desc_p = company_cell.find("p", class_="font-alt")
                        recent_deals.append({
                            "label": h5.get_text(strip=True),
                            "company": a.get_text(strip=True) if a else "",
                            "company_url": a["href"] if a else "",
                            "date": cells[1].get_text(strip=True) if len(cells) > 1 else "",
                            "value": cells[2].get_text(strip=True) if len(cells) > 2 else "",
                            "type": cells[3].get_text(strip=True) if len(cells) > 3 else "",
                            "description": desc_p.get_text(strip=True)[:300] if desc_p else "",
                        })
    if recent_deals:
        data["recent_deals"] = recent_deals

    # M&A by Sector
    sector_header = soup.find("h4", string=lambda s: s and "M&A by Sector" in s if s else False)
    if sector_header:
        table = sector_header.find_next("table")
        if table:
            sectors = []
            for row in table.find_all("tr"):
                cells = [td.get_text(strip=True) for td in row.find_all("td")]
                if len(cells) >= 2 and cells[0] and cells[0] != "Total":
                    sectors.append({"sector": cells[0], "count": cells[1]})
            if sectors:
                data["ma_by_sector"] = sectors

    # M&A by Geography
    geo_header = None
    for h4 in soup.find_all("h4"):
        if "geography" in h4.get_text(strip=True).lower() or "country" in h4.get_text(strip=True).lower():
            geo_header = h4
            break
    if not geo_header:
        # Look in table headers
        for th in soup.find_all("th"):
            if "State/Country" in th.get_text(strip=True):
                geo_header = th
                break
    if geo_header:
        table = geo_header.find_parent("table") if geo_header.name == "th" else geo_header.find_next("table")
        if table:
            geos = []
            for row in table.find_all("tr"):
                cells = [td.get_text(strip=True) for td in row.find_all("td")]
                if len(cells) >= 2 and cells[0] and cells[0] not in ("Total", "Domestic", "Cross-border"):
                    geos.append({"country": cells[0], "current": cells[1], "alltime": cells[3] if len(cells) > 3 else ""})
            if geos:
                data["ma_by_geography"] = geos

    return data


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Load existing progress — check which IDs already have files
    existing = set()
    for fname in os.listdir(OUTPUT_DIR):
        if fname.endswith(".json"):
            try:
                existing.add(int(fname.replace(".json", "")))
            except ValueError:
                pass
    print(f"Already scraped: {len(existing)} investors")

    session = create_session()
    if not login(session):
        return

    time.sleep(3)
    errors = 0
    scraped = 0
    skipped = 0
    start = time.time()

    for firm_id in range(2, MAX_ID + 1):
        if firm_id in existing:
            continue

        try:
            # Use allow_redirects=False to quickly skip invalid IDs (302)
            r = session.get(f"https://mergr.com/firms/{firm_id}", allow_redirects=False, timeout=30)

            if r.status_code == 302:
                skipped += 1
                if skipped % 100 == 0:
                    print(f"  [ID {firm_id}] {skipped} skipped so far, {scraped} scraped")
                time.sleep(0.5)  # minimal delay for redirects
                continue

            if r.status_code == 200:
                # Full page — need to follow through and parse
                time.sleep(DELAY)
                # Re-fetch with redirects enabled to get final page
                r = session.get(f"https://mergr.com/firms/{firm_id}", timeout=30)
                data = parse_firms_page(r.text, firm_id)
                # Save individual JSON file
                with open(os.path.join(OUTPUT_DIR, f"{firm_id}.json"), "w") as f:
                    json.dump(data, f, indent=2)
                existing.add(firm_id)
                scraped += 1
                name = data.get("name", "?")
                pe = data.get("pe_assets", "")
                print(f"[{scraped}] ID {firm_id}: {name} | PE: {pe}")
                errors = 0

            elif r.status_code == 429:
                print(f"[ID {firm_id}] RATE LIMITED — sleeping 60s")
                time.sleep(60)
                login(session)
                time.sleep(3)
                continue
            else:
                skipped += 1
                time.sleep(0.5)

            if scraped > 0 and scraped % 50 == 0:
                elapsed = time.time() - start
                rate = scraped / elapsed * 3600
                print(f"  -- {scraped} scraped, {skipped} skipped, ID {firm_id}/{MAX_ID}. ~{rate:.0f}/hr --")

        except Exception as e:
            errors += 1
            print(f"[ID {firm_id}] ERROR: {e}")
            if errors >= 5:
                print("Too many consecutive errors, stopping.")
                break
            time.sleep(10)

    elapsed = time.time() - start
    print(f"\nDone. {scraped} investors scraped, {skipped} skipped in {elapsed/3600:.1f}h")


if __name__ == "__main__":
    main()
