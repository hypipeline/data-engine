#!/usr/bin/env python3
"""
Enumerate the authoritative firm (investor) IDs from mergr.com/firms/search by
paginating the directory and extracting /firms/<id> links. Writes the full set
to mergr_firm_ids.txt so we can diff it against what we've actually scraped.

Resumable-ish: re-reads existing mergr_firm_ids.txt and adds to it.
"""
import asyncio, re, os, time
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup

EMAIL = "craig.anderson@hyndlandpartners.com"
PASSWORD = "X9R/N^3RvjtuJ.^"
OUT = "/Users/craiganderson/Dropbox/dev/on-testing/data-engine/mergr_firm_ids.txt"
PARALLEL = 4


def extract(html):
    return {int(x) for x in re.findall(r"/firms/(\d+)", html)}


def max_page(html):
    pages = [int(x) for x in re.findall(r"[?&]page=(\d+)", html)]
    return max(pages) if pages else 1


async def fetch(ctx, n):
    pg = await ctx.new_page()
    try:
        await pg.goto(f"https://mergr.com/firms/search?page={n}",
                      wait_until="networkidle", timeout=60000)
        await asyncio.sleep(0.5)
        html = await pg.content()
        if "awswaf" in html:
            for _ in range(6):
                await asyncio.sleep(5)
                html = await pg.content()
                if "awswaf" not in html:
                    break
            else:
                return n, set(), 0, "waf"
        return n, extract(html), max_page(html), "ok"
    except Exception as e:
        return n, set(), 0, f"err:{e}"
    finally:
        await pg.close()


async def main():
    ids = set()
    if os.path.exists(OUT):
        ids = {int(l) for l in open(OUT) if l.strip().isdigit()}
        print(f"resuming with {len(ids)} ids")
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        ctx = await b.new_context(user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
        lg = await ctx.new_page()
        await lg.goto("https://mergr.com/login")
        await lg.fill('input[name="username"]', EMAIL)
        await lg.fill('input[name="password"]', PASSWORD)
        await lg.click('button[type="submit"]')
        await lg.wait_for_url("**/dashboard**", timeout=20000)
        await lg.close()
        print("logged in", flush=True)

        cur, known_max, empty, start = 1, 1, 0, time.time()
        while cur <= known_max + 1:
            batch = [cur + i for i in range(PARALLEL) if cur + i <= known_max + 1]
            res = await asyncio.gather(*[fetch(ctx, n) for n in batch])
            got = False
            for n, pids, mp, st in res:
                if st != "ok":
                    print(f"  page {n}: {st}", flush=True); continue
                new = pids - ids
                if pids:
                    ids |= pids; got = True
                if new:
                    print(f"  page {n}/{known_max}: +{len(new)} ({len(ids)} total)", flush=True)
                known_max = max(known_max, mp)
            empty = 0 if got else empty + 1
            if empty >= 3:
                print("3 empty batches, done."); break
            with open(OUT, "w") as f:
                f.write("\n".join(str(i) for i in sorted(ids)) + "\n")
            cur = max(batch) + 1
        await b.close()
    print(f"DONE: {len(ids)} firm ids in {(time.time()-start)/60:.1f} min -> {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
