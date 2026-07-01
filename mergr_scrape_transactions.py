"""
Scrape mergr.com transaction listing pages.
Iterates month × sector, paginates within each combo (up to 15 pages).
Output: mergr_transactions/<transaction_id>.json
"""
import asyncio
import json
import os
import re
import random
import time
from urllib.parse import urlencode
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup

EMAIL = "craig.anderson@hyndlandpartners.com"
PASSWORD = "X9R/N^3RvjtuJ.^"

BASE_URL = "https://mergr.com/transactions/search"
OUT_DIR = "mergr_transactions"
NUM_TABS = 5
MAX_PAGES = 15

# All sector IDs from the mergr dropdown
SECTOR_IDS = [
    1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20,
    21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38,
    39, 40, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 52, 53, 54, 55, 56,
    57, 58, 59, 60, 61,
]

# Year range
START_YEAR = 1995
END_YEAR = 2026


def build_url(year, month, sector_id, page=1):
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
    """Parse an acquirer or seller <td> cell."""
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
            "entity_type": m.group(1),
            "mergr_id": int(m.group(2)),
        }
        label_span = p.find("span", class_=re.compile(r"label-sm"))
        if label_span:
            entity["label"] = label_span.get_text(strip=True)
        type_span = p.find("span", class_="text-gray")
        if type_span:
            entity["sub_type"] = type_span.get_text(strip=True)
        entities.append(entity)
    return entities if entities else None


def extract_icon_text(small_tag, icon_class):
    """Extract text following a specific Font Awesome icon in a <small> tag."""
    icon = small_tag.find("i", class_=icon_class)
    if not icon:
        return None
    texts = []
    for sib in icon.next_siblings:
        if getattr(sib, "name", None) in ("br", "i"):
            break
        if getattr(sib, "name", None) == "a" and icon_class != "fa-external-link":
            texts.append(sib.get_text(strip=True))
        elif isinstance(sib, str):
            texts.append(sib.strip().strip(","))
    val = " ".join(t for t in texts if t)
    val = re.sub(r"\s+", " ", val).strip(", ")
    return val if val else None


def parse_transaction_rows(html):
    """Parse all transaction rows from a listing page."""
    soup = BeautifulSoup(html, "html.parser")
    transactions = []

    for tr in soup.find_all("tr", class_="transaction-row-main"):
        txn = {}

        checkbox = tr.find("input", class_="bulk-row-checkbox")
        if checkbox:
            txn["transaction_id"] = int(checkbox["value"])

        tds = tr.find_all("td")

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
            small = target_td.find("small")
            if small and "target" in txn:
                sector = extract_icon_text(small, "fa-industry")
                if sector:
                    txn["target"]["sector"] = sector
                location = extract_icon_text(small, "fa-map-marker")
                if location:
                    txn["target"]["location"] = location

            desc_p = target_td.find("p", class_="font-alt")
            if desc_p and "target" in txn:
                desc = desc_p.get_text(strip=True)
                if desc:
                    txn["target"]["description"] = desc[:1000]

        # Transaction type & value
        if len(tds) > 3:
            type_span = tds[3].find("span", class_="text-primary")
            if type_span:
                txn["transaction_type"] = type_span.get_text(strip=True)
            value_text = tds[3].get_text(strip=True)
            if txn.get("transaction_type"):
                value_text = value_text.replace(txn["transaction_type"], "").strip()
            if value_text and value_text != "-":
                txn["value"] = value_text

        # Acquirer
        if len(tds) > 4:
            acquirers = parse_entity(tds[4])
            if acquirers:
                txn["acquirers"] = acquirers

        # Seller
        if len(tds) > 5:
            sellers = parse_entity(tds[5])
            if sellers:
                txn["sellers"] = sellers

        if txn.get("transaction_id"):
            transactions.append(txn)

    # Pagination
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


def load_existing_ids():
    """Load already-scraped transaction IDs."""
    existing = set()
    if os.path.isdir(OUT_DIR):
        for fn in os.listdir(OUT_DIR):
            if fn.endswith(".json"):
                try:
                    existing.add(int(fn[:-5]))
                except ValueError:
                    pass
    return existing


def save_transaction(txn):
    """Save a single transaction to JSON file."""
    tid = txn["transaction_id"]
    path = os.path.join(OUT_DIR, f"{tid}.json")
    with open(path, "w") as f:
        json.dump(txn, f, indent=2)


async def worker(worker_id, pg, queue, existing, stats, lock):
    """Process (year, month, sector_id) combos from queue."""
    while True:
        try:
            year, month, sector_id = queue.get_nowait()
        except asyncio.QueueEmpty:
            return

        page_num = 1
        combo_saved = 0
        combo_dupes = 0

        while page_num <= MAX_PAGES:
            url = build_url(year, month, sector_id, page_num)
            try:
                await pg.goto(url, wait_until="domcontentloaded", timeout=30000)

                # WAF wait
                for _ in range(10):
                    html = await pg.content()
                    if "awswaf" not in html and len(html) > 3000:
                        break
                    await asyncio.sleep(3)

                html = await pg.content()
                if "awswaf" in html or len(html) < 3000:
                    stats["waf"] += 1
                    print(f"  [W{worker_id}] WAF blocked: {year}-{month:02d} sector={sector_id} p={page_num}", flush=True)
                    break

                transactions, has_next = parse_transaction_rows(html)

                if not transactions:
                    break

                for txn in transactions:
                    tid = txn["transaction_id"]
                    async with lock:
                        if tid in existing:
                            combo_dupes += 1
                            continue
                        existing.add(tid)
                    save_transaction(txn)
                    combo_saved += 1
                    stats["saved"] += 1

                if not has_next:
                    break
                page_num += 1

            except Exception as e:
                stats["errors"] += 1
                print(f"  [W{worker_id}] Error {year}-{month:02d} sector={sector_id} p={page_num}: {e}", flush=True)
                break

        stats["combos_done"] += 1
        if combo_saved > 0 or combo_dupes > 0:
            print(
                f"  [W{worker_id}] {year}-{month:02d} sector={sector_id}: "
                f"{combo_saved} saved, {combo_dupes} dupes, {page_num} pages",
                flush=True,
            )

        # Progress report every 50 combos
        if stats["combos_done"] % 50 == 0:
            elapsed = time.time() - stats["start_time"]
            rate = stats["saved"] / (elapsed / 3600) if elapsed > 0 else 0
            print(
                f"\n-- {stats['combos_done']}/{stats['total_combos']} combos, "
                f"{stats['saved']} saved, {stats['waf']} waf, {stats['errors']} errors, "
                f"~{rate:.0f} txns/hr --\n",
                flush=True,
            )

        queue.task_done()


async def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    existing = load_existing_ids()
    print(f"Existing transactions: {len(existing)}")

    # Build all (year, month, sector) combos
    combos = []
    for year in range(START_YEAR, END_YEAR + 1):
        for month in range(1, 13):
            for sector_id in SECTOR_IDS:
                combos.append((year, month, sector_id))

    random.shuffle(combos)
    print(f"Total combos to check: {len(combos)}")

    queue = asyncio.Queue()
    for c in combos:
        queue.put_nowait(c)

    lock = asyncio.Lock()
    stats = {
        "saved": 0,
        "waf": 0,
        "errors": 0,
        "combos_done": 0,
        "total_combos": len(combos),
        "start_time": time.time(),
    }

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        )
        # Block unnecessary resources
        await context.route(
            re.compile(r"\.(png|jpg|jpeg|gif|svg|woff|woff2|ttf|eot|css)(\?|$)", re.IGNORECASE),
            lambda route: route.abort(),
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

        # Create worker tabs
        pages = [page]
        for _ in range(NUM_TABS - 1):
            pages.append(await context.new_page())

        print(f"Starting {NUM_TABS} workers...")
        workers = [
            worker(i, pages[i], queue, existing, stats, lock)
            for i in range(NUM_TABS)
        ]
        await asyncio.gather(*workers)

        elapsed = time.time() - stats["start_time"]
        rate = stats["saved"] / (elapsed / 3600) if elapsed > 0 else 0
        print(f"\nDone! {stats['saved']} saved, {stats['waf']} waf blocks, "
              f"{stats['errors']} errors in {elapsed/60:.1f} min (~{rate:.0f} txns/hr)")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
