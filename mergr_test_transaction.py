"""
Test: fetch 2 transaction pages from mergr and print the parsed data.
"""
import asyncio
import json
import re
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup

EMAIL = "craig.anderson@hyndlandpartners.com"
PASSWORD = "X9R/N^3RvjtuJ.^"

# Two known transaction IDs from company 1's page
TEST_IDS = [138527, 171141]


def parse_transaction_page(html, txn_id):
    soup = BeautifulSoup(html, "html.parser")
    data = {"transaction_id": txn_id, "url": f"https://mergr.com/transactions/{txn_id}"}

    # Title / headline
    h1 = soup.find("h1")
    if h1:
        data["title"] = h1.get_text(strip=True)

    h2 = soup.find("h2")
    if h2:
        data["headline"] = h2.get_text(strip=True)

    # Transaction details from h4 -> next p pattern
    detail_fields = {
        "Date": "date",
        "Type": "type",
        "Value": "value",
        "Status": "status",
    }
    for h4 in soup.find_all("h4"):
        label = h4.get_text(strip=True)
        if label in detail_fields:
            p = h4.find_next("p")
            if p:
                data[detail_fields[label]] = p.get_text(strip=True)

    # Buyer and Seller sections — look for links with /company/ or /firms/
    # These are typically in labeled sections
    for h4 in soup.find_all("h4"):
        label = h4.get_text(strip=True).lower()
        if "buyer" in label or "acquirer" in label:
            section = h4.find_parent("div") or h4.parent
            if section:
                buyers = []
                for a in section.find_all("a", href=True):
                    href = a["href"]
                    if "/company/" in href or "/firms/" in href:
                        buyers.append({
                            "name": a.get_text(strip=True),
                            "url": href,
                            "mergr_id": re.search(r'/(?:company|firms)/(\d+)', href).group(1) if re.search(r'/(?:company|firms)/(\d+)', href) else None,
                            "type": "company" if "/company/" in href else "firm",
                        })
                if buyers:
                    data["buyers"] = buyers

        if "seller" in label or "target" in label:
            section = h4.find_parent("div") or h4.parent
            if section:
                sellers = []
                for a in section.find_all("a", href=True):
                    href = a["href"]
                    if "/company/" in href or "/firms/" in href:
                        sellers.append({
                            "name": a.get_text(strip=True),
                            "url": href,
                            "mergr_id": re.search(r'/(?:company|firms)/(\d+)', href).group(1) if re.search(r'/(?:company|firms)/(\d+)', href) else None,
                            "type": "company" if "/company/" in href else "firm",
                        })
                if sellers:
                    data["sellers"] = sellers

    # Advisors
    for h4 in soup.find_all("h4"):
        if "advisor" in h4.get_text(strip=True).lower():
            section = h4.find_parent("div") or h4.parent
            if section:
                advisors = []
                for a in section.find_all("a", href=True):
                    href = a["href"]
                    if "/firms/" in href or "/company/" in href:
                        advisors.append({
                            "name": a.get_text(strip=True),
                            "url": href,
                        })
                if advisors:
                    data["advisors"] = advisors

    # Description
    firm_desc = soup.find("div", class_="firm-desc")
    if firm_desc:
        p = firm_desc.find("p")
        if p:
            data["description"] = p.get_text(strip=True)[:2000]

    # All company/firm links on the page for relationship mapping
    all_links = []
    seen_urls = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = re.search(r'/(company|firms)/(\d+)', href)
        if m and href not in seen_urls:
            seen_urls.add(href)
            all_links.append({
                "name": a.get_text(strip=True),
                "url": href,
                "entity_type": m.group(1),
                "mergr_id": int(m.group(2)),
            })
    if all_links:
        data["related_entities"] = all_links

    return data


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

        for txn_id in TEST_IDS:
            url = f"https://mergr.com/transactions/{txn_id}"
            await page.goto(url, wait_until="networkidle", timeout=30000)

            # WAF wait
            for _ in range(10):
                html = await page.content()
                if "awswaf" not in html and len(html) > 5000:
                    break
                await asyncio.sleep(3)

            html = await page.content()
            if "awswaf" in html or len(html) < 5000:
                print(f"Transaction {txn_id}: WAF blocked")
                continue

            # Save HTML for debugging
            with open(f"/tmp/mergr_txn_{txn_id}.html", "w") as f:
                f.write(html)

            data = parse_transaction_page(html, txn_id)
            print(f"\n{'='*60}")
            print(f"Transaction {txn_id}:")
            print(json.dumps(data, indent=2))

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
