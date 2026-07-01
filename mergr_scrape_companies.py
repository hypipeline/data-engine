"""
Mergr.com company scraper using async Playwright.
Usage:
  python3 mergr_scrape_companies.py              # full range 1-351000
  python3 mergr_scrape_companies.py 1 117000     # IDs 1 to 117000
  python3 mergr_scrape_companies.py 117001 234000
  python3 mergr_scrape_companies.py 234001 351000
"""
import asyncio
import concurrent.futures
import json
import os
import re
import sys
import time
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup

# Process pool for CPU-heavy HTML parsing
_parse_pool = concurrent.futures.ProcessPoolExecutor(max_workers=4)

EMAIL = "craig.anderson@hyndlandpartners.com"
PASSWORD = "X9R/N^3RvjtuJ.^"
OUTPUT_DIR = "/Users/craiganderson/Dropbox/dev/on-testing/data-engine/mergr_companies"
PARALLEL = 10  # tabs per process
SKIP_FILE = "/Users/craiganderson/Dropbox/dev/on-testing/data-engine/mergr_skip_ids.txt"
MAX_ID = 351000


def parse_company_page(html, company_id):
    soup = BeautifulSoup(html, "html.parser")
    data = {"company_id": company_id, "url": f"https://mergr.com/company/{company_id}"}

    h1 = soup.find("h1")
    if h1:
        data["name"] = h1.get_text(strip=True).replace("– Company Overview", "").strip()
    else:
        h2 = soup.find("h2")
        if h2:
            data["name"] = h2.get_text(strip=True)

    for h3 in soup.find_all("h3", class_="h5"):
        text = h3.get_text(strip=True)
        if text:
            data["legal_name"] = text
            break

    side_info = soup.find("div", class_="side-info")
    if side_info:
        for p in side_info.find_all("p"):
            text = p.get_text(strip=True)
            ticker_match = re.match(
                r"(NASDAQ|NYSE|LSE|TSX|ASX|HKEX|SGX|BSE|NSE|JSE|XETRA|SIX|Euronext|TSE|KRX|TWSE):\s*(.+)",
                text,
            )
            if ticker_match:
                data["stock_exchange"] = ticker_match.group(1)
                data["ticker"] = ticker_match.group(2).strip()

    breadcrumbs = [li.get_text(strip=True) for li in soup.find_all("li", class_="breadcrumb-item")]
    if len(breadcrumbs) >= 5:
        data["country"] = breadcrumbs[4]
    if len(breadcrumbs) >= 6:
        if breadcrumbs[5] != data.get("name", ""):
            data["state"] = breadcrumbs[5]

    addr_el = soup.find("p", class_="adress-info") or soup.find("p", class_="address-info")
    if addr_el:
        lines = []
        for part in addr_el.children:
            if isinstance(part, str):
                text = part.strip().rstrip(",")
                if text:
                    lines.append(text)
        lines = [l.strip() for l in lines if l.strip()]

        if lines:
            data["street"] = lines[0]
        if len(lines) >= 2:
            city_line = lines[1].strip()
            city_match = re.match(r"(.+?),\s+(.+?)\s+(\d{4,5}(?:-\d{4})?)", city_line)
            if city_match:
                data["city"] = city_match.group(1)
                data["state_full"] = city_match.group(2)
                data["postal_code"] = city_match.group(3)
            else:
                parts = [p.strip() for p in city_line.split(",")]
                if parts:
                    data["city"] = parts[0]

        for line in lines:
            if re.match(r"\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}", line):
                data["phone"] = line
            elif re.match(r"\+[\d\s().-]{7,}", line):
                data["phone"] = line

        a_web = addr_el.find("a", href=lambda h: h and h.startswith("http") and "mergr.com" not in h)
        if a_web:
            data["website"] = a_web["href"]

        a_email = addr_el.find("a", href=lambda h: h and h.startswith("mailto:"))
        if a_email:
            data["email"] = a_email["href"].replace("mailto:", "")

        a_phone = addr_el.find("a", href=lambda h: h and h.startswith("tel:"))
        if a_phone:
            data["phone"] = a_phone.get_text(strip=True)

    logo_img = soup.find("img", src=lambda s: s and "clearbit.com" in s)
    if logo_img:
        data["logo"] = logo_img["src"]

    for a in soup.find_all("a", href=True):
        if "linkedin.com/company" in a["href"] and "mergr" not in a["href"]:
            data["linkedin"] = a["href"]
            break

    summary_map = {
        "SECTOR": "sector",
        "Revenue": "revenue",
        "Employees": "employees",
        "Established": "established",
    }
    for h4 in soup.find_all("h4"):
        label = h4.get_text(strip=True)
        if label in summary_map:
            p = h4.find_next("p")
            if p:
                data[summary_map[label]] = p.get_text(strip=True)

    desc_div = soup.find("div", class_="firm-desc")
    if desc_div:
        p = desc_div.find("p")
        if p:
            data["description"] = p.get_text(strip=True)[:2000]
    else:
        for p in soup.find_all("p"):
            cls = p.get("class", [])
            text = p.get_text(strip=True)
            if (
                "address-info" not in cls
                and "adress-info" not in cls
                and "print-desc" not in cls
                and len(text) > 100
            ):
                data["description"] = text[:2000]
                break

    for a in soup.find_all("a", href=True):
        if "/investments" in a["href"]:
            inv_match = re.search(r"Investors?\((\d+)\)", a.get_text(strip=True))
            if inv_match:
                data["investor_count"] = int(inv_match.group(1))

    tables = soup.find_all("table")
    if tables:
        first_row = tables[0].find("tr")
        if first_row:
            cells = [td.get_text(strip=True) for td in first_row.find_all("td")]
            for c in cells:
                inv_match = re.search(r"(\d+)\s*Investor", c)
                if inv_match:
                    data["investor_count"] = int(inv_match.group(1))
                txn_match = re.search(r"(\d+)\s*M&A", c)
                if txn_match:
                    data["transaction_count"] = int(txn_match.group(1))

    for table in tables:
        for row in table.find_all("tr"):
            cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
            row_text = " ".join(cells)
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

    for table in tables:
        for row in table.find_all("tr"):
            tds = row.find_all("td")
            if len(tds) == 2:
                strong = tds[0].find("strong")
                rev_text = strong.get_text(strip=True) if strong else tds[0].get_text(strip=True)
                year_text = tds[1].get_text(strip=True)
                small = tds[0].find("small")
                growth = small.get_text(strip=True) if small else None
                if re.match(r"[\d,]+$", rev_text) and re.match(r"\d{4}$", year_text):
                    if "revenue_history" not in data:
                        data["revenue_history"] = []
                    entry = {"revenue": rev_text, "year": year_text}
                    if growth:
                        entry["yoy_growth"] = growth
                    data["revenue_history"].append(entry)
            if len(tds) == 1 and "millions" in tds[0].get_text(strip=True).lower():
                data["revenue_currency"] = tds[0].get_text(strip=True)

    for h4 in soup.find_all("h4"):
        text = h4.get_text(strip=True).lower()
        if "pe-backed" in text and "formerly" not in text:
            p = h4.find_next("p")
            if p:
                data["pe_backed"] = p.get_text(strip=True)[:300]
            break

    for h4 in soup.find_all("h4"):
        if "headquartered" in h4.get_text(strip=True).lower():
            p = h4.find_next("p")
            if p:
                data["hq_description"] = p.get_text(strip=True)[:300]
            break

    return data


JS_EXTRACT = """() => {
    const d = {};
    const $ = (sel) => document.querySelector(sel);
    const $$ = (sel) => [...document.querySelectorAll(sel)];

    // Check for WAF or no data
    if (document.body.innerText.includes('awswaf')) return {_status: 'waf'};
    if (!$('.side-info') && !$('.firm-desc')) return {_status: 'nodata'};

    // Name
    const h1 = $('h1');
    if (h1) d.name = h1.innerText.replace('– Company Overview', '').trim();
    else { const h2 = $('h2'); if (h2) d.name = h2.innerText.trim(); }

    // Legal name
    const h3 = $('h3.h5');
    if (h3 && h3.innerText.trim()) d.legal_name = h3.innerText.trim();

    // Ticker
    const sideInfo = $('.side-info');
    if (sideInfo) {
        for (const p of sideInfo.querySelectorAll('p')) {
            const m = p.innerText.trim().match(/^(NASDAQ|NYSE|LSE|TSX|ASX|HKEX|SGX|BSE|NSE|JSE|XETRA|SIX|Euronext|TSE|KRX|TWSE):\\s*(.+)/);
            if (m) { d.stock_exchange = m[1]; d.ticker = m[2].trim(); }
        }
    }

    // Breadcrumbs
    const bcs = $$('li.breadcrumb-item').map(e => e.innerText.trim());
    if (bcs.length >= 5) d.country = bcs[4];
    if (bcs.length >= 6 && bcs[5] !== d.name) d.state = bcs[5];

    // Address
    const addr = $('p.adress-info') || $('p.address-info');
    if (addr) {
        const textNodes = [...addr.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent.trim().replace(/,$/, '')).filter(Boolean);
        if (textNodes.length > 0) d.street = textNodes[0];
        if (textNodes.length >= 2) {
            const cm = textNodes[1].match(/^(.+?),\\s+(.+?)\\s+(\\d{4,5}(?:-\\d{4})?)$/);
            if (cm) { d.city = cm[1]; d.state_full = cm[2]; d.postal_code = cm[3]; }
            else { const parts = textNodes[1].split(','); if (parts.length) d.city = parts[0].trim(); }
        }
        for (const t of textNodes) {
            if (/^\\(?\\d{3}\\)?[\\s.-]?\\d{3}[\\s.-]?\\d{4}/.test(t)) d.phone = t;
            else if (/^\\+[\\d\\s().-]{7,}/.test(t)) d.phone = t;
        }
        const webA = addr.querySelector('a[href^="http"]:not([href*="mergr.com"])');
        if (webA) d.website = webA.href;
        const emailA = addr.querySelector('a[href^="mailto:"]');
        if (emailA) d.email = emailA.href.replace('mailto:', '');
        const phoneA = addr.querySelector('a[href^="tel:"]');
        if (phoneA) d.phone = phoneA.innerText.trim();
    }

    // Logo
    const logo = $('img[src*="clearbit.com"]');
    if (logo) d.logo = logo.src;

    // LinkedIn
    for (const a of $$('a[href*="linkedin.com/company"]')) {
        if (!a.href.includes('mergr')) { d.linkedin = a.href; break; }
    }

    // Summary fields
    const smap = {SECTOR: 'sector', Revenue: 'revenue', Employees: 'employees', Established: 'established'};
    for (const h4 of $$('h4')) {
        const label = h4.innerText.trim();
        if (smap[label]) {
            let el = h4.nextElementSibling;
            while (el && el.tagName !== 'P') el = el.nextElementSibling;
            if (el) d[smap[label]] = el.innerText.trim();
        }
    }

    // Description
    const firmDesc = $('.firm-desc p');
    if (firmDesc) d.description = firmDesc.innerText.trim().slice(0, 2000);
    else {
        for (const p of $$('p')) {
            const cls = [...(p.classList || [])];
            const t = p.innerText.trim();
            if (!cls.includes('address-info') && !cls.includes('adress-info') && !cls.includes('print-desc') && t.length > 100) {
                d.description = t.slice(0, 2000); break;
            }
        }
    }

    // Investor count
    for (const a of $$('a[href*="/investments"]')) {
        const m = a.innerText.match(/Investors?\\((\\d+)\\)/);
        if (m) d.investor_count = parseInt(m[1]);
    }

    // M&A stats from tables
    const tables = $$('table');
    if (tables.length > 0) {
        const firstRow = tables[0].querySelector('tr');
        if (firstRow) {
            for (const td of firstRow.querySelectorAll('td')) {
                const t = td.innerText.trim();
                const im = t.match(/(\\d+)\\s*Investor/);
                if (im) d.investor_count = parseInt(im[1]);
                const tm = t.match(/(\\d+)\\s*M&A/);
                if (tm) d.transaction_count = parseInt(tm[1]);
            }
        }
    }

    for (const table of tables) {
        for (const row of table.querySelectorAll('tr')) {
            const cells = [...row.querySelectorAll('td, th')].map(e => e.innerText.trim());
            const rowText = cells.join(' ');
            const bm = rowText.match(/Buy\\(([\\d.]+)\\/yr\\)/);
            if (bm) { d.buy_rate_per_year = parseFloat(bm[1]); const nums = rowText.match(/\\d+/g); if (nums) d.total_buys = parseInt(nums[nums.length-1]); }
            const sm = rowText.match(/Sell\\(([\\d.]+)\\/yr\\)/);
            if (sm) { d.sell_rate_per_year = parseFloat(sm[1]); const nums = rowText.match(/\\d+/g); if (nums) d.total_sells = parseInt(nums[nums.length-1]); }
        }
    }

    // Revenue history
    for (const table of tables) {
        for (const row of table.querySelectorAll('tr')) {
            const tds = row.querySelectorAll('td');
            if (tds.length === 2) {
                const strong = tds[0].querySelector('strong');
                const revText = strong ? strong.innerText.trim() : tds[0].innerText.trim();
                const yearText = tds[1].innerText.trim();
                const small = tds[0].querySelector('small');
                const growth = small ? small.innerText.trim() : null;
                if (/^[\\d,]+$/.test(revText) && /^\\d{4}$/.test(yearText)) {
                    if (!d.revenue_history) d.revenue_history = [];
                    const entry = {revenue: revText, year: yearText};
                    if (growth) entry.yoy_growth = growth;
                    d.revenue_history.push(entry);
                }
            }
            if (tds.length === 1 && tds[0].innerText.toLowerCase().includes('millions'))
                d.revenue_currency = tds[0].innerText.trim();
        }
    }

    // PE-backed
    for (const h4 of $$('h4')) {
        const t = h4.innerText.trim().toLowerCase();
        if (t.includes('pe-backed') && !t.includes('formerly')) {
            const p = h4.nextElementSibling;
            if (p) d.pe_backed = p.innerText.trim().slice(0, 300);
            break;
        }
    }

    // HQ description
    for (const h4 of $$('h4')) {
        if (h4.innerText.trim().toLowerCase().includes('headquartered')) {
            const p = h4.nextElementSibling;
            if (p) d.hq_description = p.innerText.trim().slice(0, 300);
            break;
        }
    }

    d._status = 'ok';
    return d;
}"""


async def fetch_company(pg, company_id):
    """Fetch and parse a single company page using a reusable tab."""
    try:
        url = f"https://mergr.com/company/{company_id}"
        response = await pg.goto(url, wait_until="domcontentloaded", timeout=30000)

        # Redirected = invalid ID
        if pg.url != url and "/company/" not in pg.url:
            return company_id, None, "redirect"

        html = await pg.content()

        # Check WAF
        if "awswaf" in html:
            await asyncio.sleep(1)
            html = await pg.content()
            if "awswaf" in html:
                return company_id, None, "waf"

        data = parse_company_page(html, company_id)
        if data and data.get("name"):
            return company_id, data, "ok"

        return company_id, None, "nodata"
    except Exception as e:
        return company_id, None, f"error: {e}"


async def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    existing = set()
    for fname in os.listdir(OUTPUT_DIR):
        if fname.endswith(".json"):
            try:
                existing.add(int(fname.replace(".json", "")))
            except ValueError:
                pass

    # Load known-bad IDs
    skip_ids = set()
    if os.path.exists(SKIP_FILE):
        with open(SKIP_FILE) as f:
            for line in f:
                line = line.strip()
                if line.isdigit():
                    skip_ids.add(int(line))

    print(f"Already scraped: {len(existing)}, skip IDs: {len(skip_ids)}")

    import random
    todo = [cid for cid in range(1, MAX_ID + 1) if cid not in existing and cid not in skip_ids]
    random.shuffle(todo)
    print(f"IDs to check: {len(todo)} (randomized)")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        )
        page = await context.new_page()

        # Block images, CSS, fonts, media, analytics
        async def block_resources(route):
            if route.request.resource_type in ("image", "stylesheet", "font", "media"):
                await route.abort()
            elif any(d in route.request.url for d in ["google-analytics", "intercom", "segment", "sentry", "optimizely", "clearbit", "facebook", "twitter"]):
                await route.abort()
            else:
                await route.continue_()
        await context.route("**/*", block_resources)

        # Login
        await page.goto("https://mergr.com/login")
        await page.fill('input[name="username"]', EMAIL)
        await page.fill('input[name="password"]', PASSWORD)
        await page.click('button[type="submit"]')
        await page.wait_for_url("**/dashboard**", timeout=15000)
        print("Logged in OK")
        await page.close()

        await asyncio.sleep(3)

        scraped = 0
        skipped = 0
        waf_hits = 0
        errors = 0
        start = time.time()
        skip_log = open(SKIP_FILE, "a")
        queue = asyncio.Queue()
        for cid in todo:
            queue.put_nowait(cid)

        print(f"Scraping with {PARALLEL} tabs, {queue.qsize()} IDs to check...")

        async def worker():
            nonlocal scraped, skipped, waf_hits, errors
            pg = await context.new_page()
            while not queue.empty():
                company_id = queue.get_nowait()
                cid, data, status = await fetch_company(pg, company_id)

                if status == "ok" and data:
                    with open(os.path.join(OUTPUT_DIR, f"{cid}.json"), "w") as f:
                        json.dump(data, f, indent=2)
                    scraped += 1
                    name = data.get("name", "?")
                    sector = data.get("sector", "")
                    print(f"[{scraped}] ID {cid}: {name} | {sector}", flush=True)
                    errors = 0
                elif status == "redirect":
                    skip_log.write(f"{cid}\n")
                    skipped += 1
                    if scraped == 0 or skipped % 500 == 0:
                        print(f"  [ID {cid}] skipped ({skipped} total, {scraped} scraped)", flush=True)
                elif status == "waf":
                    waf_hits += 1
                    print(f"  [ID {cid}] WAF blocked ({waf_hits} total)", flush=True)
                    if waf_hits >= 10:
                        print("Too many WAF blocks, sleeping 60s...")
                        await asyncio.sleep(60)
                        waf_hits = 0
                elif status == "nodata":
                    # Don't skip — could be transient (WAF, load failure)
                    skipped += 1
                    print(f"  [ID {cid}] nodata (not skipped permanently)", flush=True)
                else:
                    # Errors — don't skip permanently either
                    skipped += 1
                    errors += 1
                    print(f"[ID {cid}] {status}", flush=True)

                skip_log.flush()

                if (scraped + skipped) % 500 == 0 and (scraped + skipped) > 0:
                    elapsed = time.time() - start
                    rate = scraped / elapsed * 3600 if scraped > 0 else 0
                    total_rate = (scraped + skipped) / elapsed * 3600
                    remaining = queue.qsize()
                    eta = remaining / total_rate if total_rate > 0 else 0
                    print(
                        f"  -- {scraped} scraped, {skipped} skipped, "
                        f"{remaining} remaining. ~{rate:.0f} scraped/hr, "
                        f"ETA {eta:.1f}h --",
                        flush=True,
                    )

        workers = [asyncio.create_task(worker()) for _ in range(PARALLEL)]
        await asyncio.gather(*workers)

        skip_log.close()
        elapsed = time.time() - start
        print(f"\nDone. {scraped} companies scraped, {skipped} skipped in {elapsed/3600:.1f}h")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
