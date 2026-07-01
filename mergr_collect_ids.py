"""
Collect all company IDs from mergr.com by paginating the unfiltered search.
Uses 3 parallel tabs. Saves progress after every batch — fully resumable.
"""
import asyncio
import json
import os
import re
import time
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup

EMAIL = "craig.anderson@hyndlandpartners.com"
PASSWORD = "X9R/N^3RvjtuJ.^"
IDS_FILE = "/Users/craiganderson/Dropbox/dev/on-testing/data-engine/mergr_company_ids.json"
PROGRESS_FILE = "/Users/craiganderson/Dropbox/dev/on-testing/data-engine/mergr_collect_progress.json"
PARALLEL = 3


IDS_LOG = "/Users/craiganderson/Dropbox/dev/on-testing/data-engine/mergr_company_ids.txt"


def load_progress():
    ids = set()
    last_page = 0
    # Load from append log
    if os.path.exists(IDS_LOG):
        with open(IDS_LOG) as f:
            for line in f:
                line = line.strip()
                if line.isdigit():
                    ids.add(int(line))
    # Also load any existing JSON
    if os.path.exists(IDS_FILE):
        with open(IDS_FILE) as f:
            ids.update(json.load(f))
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE) as f:
            last_page = json.load(f).get("last_page", 0)
    return ids, last_page


def save_progress(new_ids, page):
    # Append only new IDs to text log (fast)
    if new_ids:
        with open(IDS_LOG, "a") as f:
            for cid in sorted(new_ids):
                f.write(f"{cid}\n")
    # Save page progress
    with open(PROGRESS_FILE, "w") as f:
        json.dump({"last_page": page}, f)


def save_final_json(ids):
    with open(IDS_FILE, "w") as f:
        json.dump(sorted(ids), f)


def extract_ids_from_html(html):
    soup = BeautifulSoup(html, "html.parser")
    ids = set()
    for a in soup.find_all("a", href=True):
        m = re.search(r"/company/(\d+)", a["href"])
        if m:
            ids.add(int(m.group(1)))
    return ids


def get_max_page(html):
    soup = BeautifulSoup(html, "html.parser")
    pages = []
    for a in soup.find_all("a", href=lambda h: h and "page=" in h):
        m = re.search(r"page=(\d+)", a["href"])
        if m:
            pages.append(int(m.group(1)))
    return max(pages) if pages else 1


async def fetch_page(context, page_num):
    """Fetch a single search page, return (page_num, ids, max_page)."""
    pg = await context.new_page()
    try:
        url = f"https://mergr.com/companies/search?page={page_num}"
        await pg.goto(url, wait_until="networkidle", timeout=60000)
        await asyncio.sleep(1)
        html = await pg.content()

        # Handle WAF
        if "awswaf" in html:
            for _ in range(6):
                await asyncio.sleep(5)
                html = await pg.content()
                if "awswaf" not in html:
                    break
            if "awswaf" in html:
                return page_num, set(), 0, "waf"

        ids = extract_ids_from_html(html)
        max_page = get_max_page(html)
        return page_num, ids, max_page, "ok"
    except Exception as e:
        return page_num, set(), 0, f"error: {e}"
    finally:
        await pg.close()


async def main():
    all_ids, last_page = load_progress()
    start_page = last_page + 1
    if last_page > 0:
        print(f"Resuming with {len(all_ids)} IDs, starting from page {start_page}")
    else:
        print("Starting fresh")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        )
        page = await context.new_page()

        await page.goto("https://mergr.com/login")
        await page.fill('input[name="username"]', EMAIL)
        await page.fill('input[name="password"]', PASSWORD)
        await page.click('button[type="submit"]')
        await page.wait_for_url("**/dashboard**", timeout=15000)
        print("Logged in OK")
        await page.close()

        current_page = start_page
        known_max = start_page
        empty_count = 0
        start = time.time()

        while current_page <= known_max + 1:
            # Build batch of page numbers
            batch_pages = []
            for i in range(PARALLEL):
                pg_num = current_page + i
                if pg_num <= known_max + 1:
                    batch_pages.append(pg_num)

            results = await asyncio.gather(*[fetch_page(context, pn) for pn in batch_pages])

            batch_had_ids = False
            batch_new_ids = set()
            max_page_in_batch = current_page
            for page_num, ids, max_page, status in results:
                if status == "waf":
                    print(f"  page {page_num}: WAF blocked, sleeping 30s...")
                    await asyncio.sleep(30)
                    continue
                elif status != "ok":
                    print(f"  page {page_num}: {status}")
                    continue

                if ids:
                    new_ids = ids - all_ids
                    all_ids.update(ids)
                    batch_new_ids.update(new_ids)
                    batch_had_ids = True
                    empty_count = 0
                    print(
                        f"  page {page_num}/{known_max}: "
                        f"+{len(new_ids)} new ({len(all_ids)} total)",
                        flush=True,
                    )

                if max_page > known_max:
                    known_max = max_page

                max_page_in_batch = max(max_page_in_batch, page_num)

            if not batch_had_ids:
                empty_count += 1
                if empty_count >= 3:
                    print(f"  {empty_count} empty batches in a row, done.")
                    break

            save_progress(batch_new_ids, max_page_in_batch)
            current_page = max_page_in_batch + 1

            elapsed = time.time() - start
            rate = len(all_ids) / elapsed * 60 if elapsed > 0 else 0
            if current_page % 30 == 0:
                print(f"  -- {len(all_ids)} IDs, ~{rate:.0f}/min, page {current_page}/{known_max} --", flush=True)

        save_final_json(all_ids)
        elapsed = time.time() - start
        print(f"\nDone. {len(all_ids)} unique company IDs collected in {elapsed/60:.1f} minutes")
        print(f"Saved to {IDS_FILE}")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
