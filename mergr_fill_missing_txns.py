#!/usr/bin/env python3
"""
Fill the ~49k MISSING transactions. Root cause found: the original scraper only
captured PAGE 1 (25 rows) of each year×month×sector combo — busy combos' pages
2..15 were dropped. This re-enumerates year×sector combos with CORRECT pagination
(dedup-terminate; sub-split to months if a combo exceeds the ~15-page/375-row cap),
parses full row records (reusing parse_transaction_rows), and saves NEW deals to
mergr_transactions/ in the same format as the existing 173k.

Resumable (done combos in a .state file), shuffled combo order (WAF-safe),
worker pool, caffeinate-friendly.
"""
import asyncio, os, json, sys, time, random, re
from urllib.parse import urlencode
from playwright.async_api import async_playwright
from mergr_scrape_transactions import parse_transaction_rows, save_transaction, load_existing_ids

EMAIL="craig.anderson@hyndlandpartners.com"; PASSWORD="X9R/N^3RvjtuJ.^"
BASE="https://mergr.com/transactions/search"
SECTORS=list(range(1,62))                      # 61 sectors (all have coverage)
YEARS=list(range(1985,2027))                   # our data min is 1922 but pre-1985 is negligible; widen if needed
STATE="/Users/craiganderson/Dropbox/dev/on-testing/data-engine/mergr_fill.state"
MAX_PAGES=15; PARALLEL=8
existing=set(); done=set(); saved=[0]; new_ids=[0]

def url(year,sector,page,month=None):
    p={"transaction[startYear]":year,"transaction[endYear]":year,
       "transaction[sectors][]":sector,"page":page}
    if month:
        p["transaction[startMonth]"]=month; p["transaction[endMonth]"]=month
    return BASE+"?"+urlencode(p)

def save_state():
    with open(STATE,"w") as f: json.dump(sorted(done),f)

async def fetch(ctx,u):
    pg=await ctx.new_page()
    try:
        await pg.goto(u,wait_until="domcontentloaded",timeout=45000)
        await asyncio.sleep(0.2)
        h=await pg.content()
        if "awswaf" in h:
            for _ in range(6):
                await asyncio.sleep(5); h=await pg.content()
                if "awswaf" not in h: break
            else: return None
        return h
    except Exception:
        return None
    finally:
        await pg.close()

async def crawl(ctx,year,sector,month=None):
    """Paginate one combo with dedup-terminate. Returns (capped, n_new)."""
    seen=set(); page=1; n_new=0
    while page<=MAX_PAGES:
        h=await fetch(ctx,url(year,sector,page,month))
        if h is None: break
        rows=parse_transaction_rows(h)
        if isinstance(rows,tuple): rows=rows[0]   # parser returns (transactions, page_info)
        fresh=0
        for txn in rows:
            tid=txn.get("transaction_id")
            if tid is None or tid in seen: continue
            seen.add(tid); fresh+=1
            if tid not in existing:
                save_transaction(txn); existing.add(tid)
                saved[0]+=1; new_ids[0]+=1
        if fresh==0: break
        page+=1
    capped = page>MAX_PAGES
    return capped, len(seen)

async def handle(ctx,year,sector):
    label=f"{year}_{sector}"
    if label in done: return
    capped,n=await crawl(ctx,year,sector)
    if capped:                                  # >375 in a year×sector -> split by month
        for m in range(1,13):
            await crawl(ctx,year,sector,m)
    done.add(label); save_state()

async def main():
    existing.update(load_existing_ids())
    if os.path.exists(STATE): done.update(json.load(open(STATE)))
    combos=[(y,s) for y in YEARS for s in SECTORS if f"{y}_{s}" not in done]
    random.shuffle(combos)
    print(f"start: {len(existing)} existing, {len(combos)} combos to crawl",flush=True)
    if not combos:
        print("ALL COMPLETE",flush=True); return
    async with async_playwright() as p:
        b=await p.chromium.launch(headless=True)
        ctx=await b.new_context(user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
        async def blk(r):
            if r.request.resource_type in ("image","stylesheet","font","media"): await r.abort()
            else: await r.continue_()
        await ctx.route("**/*",blk)
        lg=await ctx.new_page()
        await lg.goto("https://mergr.com/login"); await lg.fill('input[name="username"]',EMAIL); await lg.fill('input[name="password"]',PASSWORD)
        await lg.click('button[type="submit"]'); await lg.wait_for_url("**/dashboard**",timeout=20000); await lg.close()
        print("logged in",flush=True)
        start=time.time()
        q=asyncio.Queue()
        for c in combos: q.put_nowait(c)
        n_done=[0]
        async def worker():
            while True:
                try: year,sector=q.get_nowait()
                except asyncio.QueueEmpty: return
                await handle(ctx,year,sector)
                n_done[0]+=1
                if n_done[0]%200==0:
                    el=time.time()-start
                    print(f"  {n_done[0]}/{len(combos)} combos, {new_ids[0]} new deals, {el/60:.0f}min",flush=True)
        await asyncio.gather(*[worker() for _ in range(PARALLEL)])
        print(f"DONE: {new_ids[0]} new transactions saved, {(time.time()-start)/60:.1f} min",flush=True)
        await b.close()

if __name__=="__main__":
    asyncio.run(main())
