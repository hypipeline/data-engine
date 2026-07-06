"""
Test: scrape a few transactions from the mergr listing page to validate parsing.
Filters by month+sector, parses table rows, prints JSON for review.
"""
import asyncio
import json
import re
from urllib.parse import urlencode
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup

EMAIL = "craig.anderson@hyndlandpartners.com"
PASSWORD = "X9R/N^3RvjtuJ.^"

BASE_URL = "https://mergr.com/transactions/search"


def build_url(year, month, sector_id, page=1):
    """Build filtered transaction search URL."""
    params = {
        "transaction[startMonth]": month,
        "transaction[startYear]": year,
        "transaction[endMonth]": month,
        "transaction[endYear]": year,
        "transaction[sectors][]": sector_id,
        "page": page,
    }
    return BASE_URL + "?" + urlencode(params)


def parse_entity(td):
    """Parse an acquirer or seller <td> cell, extracting linked entities."""
    entities = []
    for p in td.find_all("p", class_="adv"):
        a = p.find("a", href=True)
        if not a:
            continue
        href = a["href"]
        m = re.search(r"/(company|firms)/(\d+)", href)
        if not m:
            continue
        entity = {
            "name": a.get_text(strip=True),
            "url": href,
            "entity_type": m.group(1),  # "company" or "firms"
            "mergr_id": int(m.group(2)),
        }
        # Entity label (Investor / Company)
        label_span = p.find("span", class_=re.compile(r"label-sm"))
        if label_span:
            entity["label"] = label_span.get_text(strip=True)
        # Sub-type (e.g. "Private Equity Firm", "Software")
        type_span = p.find("span", class_="text-gray")
        if type_span:
            entity["sub_type"] = type_span.get_text(strip=True)
        entities.append(entity)
    return entities if entities else None


def parse_transaction_rows(html):
    """Parse all transaction rows from a listing page."""
    soup = BeautifulSoup(html, "html.parser")
    transactions = []

    for tr in soup.find_all("tr", class_="transaction-row-main"):
        txn = {}

        # Transaction ID from checkbox value
        checkbox = tr.find("input", class_="bulk-row-checkbox")
        if checkbox:
            txn["transaction_id"] = int(checkbox["value"])

        # All <td> cells
        tds = tr.find_all("td")
        # tds: [0]=bulk checkbox, [1]=date, [2]=target, [3]=type/value, [4]=acquirer, [5]=seller

        # Date
        if len(tds) > 1:
            date_a = tds[1].find("a", href=True)
            if date_a:
                txn["date"] = date_a.get_text(strip=True)
                txn["transaction_url"] = date_a["href"]

        # Target
        if len(tds) > 2:
            target_td = tds[2]
            target_a = target_td.find("a", href=True)
            if target_a:
                href = target_a["href"]
                m = re.search(r"/company/(\d+)", href)
                txn["target"] = {
                    "name": target_a.get_text(strip=True),
                    "url": href,
                    "mergr_id": int(m.group(1)) if m else None,
                }
            # Sector and location from <small> tag icons
            small = target_td.find("small")
            if small:
                for icon in small.find_all("i", class_=True):
                    classes = icon.get("class", [])
                    # Collect text from next siblings until next <br> or <i>
                    texts = []
                    for sib in icon.next_siblings:
                        if getattr(sib, "name", None) in ("br", "i"):
                            break
                        if getattr(sib, "name", None) == "a" and "fa-external-link" not in " ".join(classes):
                            texts.append(sib.get_text(strip=True))
                        elif isinstance(sib, str):
                            texts.append(sib.strip().strip(","))
                    val = " ".join(t for t in texts if t)
                    val = re.sub(r"\s+", " ", val).strip(", ")
                    if "fa-industry" in classes and val:
                        txn["target"]["sector"] = val
                    elif "fa-map-marker" in classes and val:
                        txn["target"]["location"] = val

            # Description
            desc_p = target_td.find("p", class_="font-alt")
            if desc_p:
                desc = desc_p.get_text(strip=True)
                if desc:
                    txn["target"]["description"] = desc[:1000]

        # Transaction type & value
        if len(tds) > 3:
            type_span = tds[3].find("span", class_="text-primary")
            if type_span:
                txn["transaction_type"] = type_span.get_text(strip=True)
            # Value — look for text after the span
            value_text = tds[3].get_text(strip=True)
            # Remove the type text to isolate value
            if txn.get("transaction_type"):
                value_text = value_text.replace(txn["transaction_type"], "").strip()
            if value_text and value_text != "-":
                txn["value"] = value_text

        # Acquirer
        if len(tds) > 4:
            acquirers = parse_entity(tds[4])
            if acquirers:
                txn["acquirers"] = acquirers
            elif tds[4].get_text(strip=True) not in ("", "-"):
                txn["acquirer_text"] = tds[4].get_text(strip=True)[:200]

        # Seller
        if len(tds) > 5:
            sellers = parse_entity(tds[5])
            if sellers:
                txn["sellers"] = sellers
            elif tds[5].get_text(strip=True) not in ("", "-"):
                txn["seller_text"] = tds[5].get_text(strip=True)[:200]

        if txn.get("transaction_id"):
            transactions.append(txn)

    # Pagination — check if there's a next page
    has_next = False
    pagination = soup.find("ul", class_="pagination")
    if pagination:
        active = pagination.find("a", class_="active")
        if active:
            current_li = active.find_parent("li")
            if current_li and current_li.find_next_sibling("li"):
                next_a = current_li.find_next_sibling("li").find("a")
                if next_a and next_a.get_text(strip=True).isdigit():
                    has_next = True

    return transactions, has_next


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        )
        page = await context.new_page()

        # Login
        await page.goto("https://mergr.com/login", wait_until="networkidle")
        await page.fill('input[name="username"]', EMAIL)
        await page.fill('input[name="password"]', PASSWORD)
        await page.click('button[type="submit"]')
        await page.wait_for_url("**/dashboard**", timeout=30000)
        print("Logged in OK")
        await asyncio.sleep(5)

        # Test: fetch Jan 2025, Business Services (sector 7), page 1
        url = build_url(2025, 1, 7, page=1)
        print(f"\nFetching: {url}")
        await page.goto(url, wait_until="networkidle", timeout=30000)

        # WAF wait
        for _ in range(10):
            html = await page.content()
            if "awswaf" not in html and len(html) > 5000:
                break
            await asyncio.sleep(3)

        html = await page.content()
        if "awswaf" in html or len(html) < 5000:
            print("WAF blocked!")
            await browser.close()
            return

        transactions, has_next = parse_transaction_rows(html)
        print(f"\nParsed {len(transactions)} transactions, has_next={has_next}")

        # Show first 2 transactions
        for txn in transactions[:2]:
            print(f"\n{'='*60}")
            print(json.dumps(txn, indent=2))

        # Save full page for debugging
        with open("/tmp/mergr_txn_filtered.html", "w") as f:
            f.write(html)
        print(f"\nSaved HTML ({len(html)} bytes) to /tmp/mergr_txn_filtered.html")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
