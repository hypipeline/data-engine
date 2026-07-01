#!/usr/bin/env python3
"""
Detail-scrape transactions for the financials block (deal value, revenue, EBITDA,
EV/Revenue, EV/EBITDA) that is NOT available in the search rows.

Input list of ids (default: mergr_txn_valued_ids.txt — deals whose search row
showed a value, i.e. the ones that actually have a tr-footer financials block).
Output: one JSON per id in mergr_txn_details/.  Resumable (skips existing files).

Usage:
  python3 mergr_scrape_txn_details.py                       # all valued ids
  python3 mergr_scrape_txn_details.py --ids 240586,240307   # specific ids (TEST)
  python3 mergr_scrape_txn_details.py --limit 200           # first 200 (TEST)
"""
import asyncio, os, json, sys, time, random
from playwright.async_api import async_playwright
from mergr_parse_txn_detail import parse_txn_detail

EMAIL="craig.anderson@hyndlandpartners.com"; PASSWORD="X9R/N^3RvjtuJ.^"
OUTDIR="/Users/craiganderson/Dropbox/dev/on-testing/data-engine/mergr_txn_details"
SRCDIR="/Users/craiganderson/Dropbox/dev/on-testing/data-engine/mergr_transactions"  # the 173k we already have
PARALLEL=10

def load_ids():
    args=sys.argv[1:]
    if "--ids" in args:
        return [int(x) for x in args[args.index("--ids")+1].split(",")]
    # default: every transaction id we already hold -> detail-scrape for financials
    ids=sorted(int(f[:-5]) for f in os.listdir(SRCDIR) if f.endswith(".json"))
    if "--limit" in args:
        ids=ids[:int(args[args.index("--limit")+1])]
    return ids

async def scrape(ctx,tid):
    for attempt in range(2):
        pg=await ctx.new_page()
        try:
            r=await pg.goto(f"https://mergr.com/transactions/{tid}",wait_until="domcontentloaded",timeout=45000)
            if r and r.status in (301,302,404):
                return tid,None,"gone"
            await asyncio.sleep(0.15)
            h=await pg.content()
            if "awswaf" in h:
                for _ in range(6):
                    await asyncio.sleep(5); h=await pg.content()
                    if "awswaf" not in h: break
                else:
                    return tid,None,"waf"
            return tid,parse_txn_detail(h,tid),"ok"
        except Exception as e:
            if attempt==0:
                await pg.close(); await asyncio.sleep(0.5); continue   # retry once (nav race)
            return tid,None,f"err:{e}"
        finally:
            await pg.close()

async def main():
    os.makedirs(OUTDIR,exist_ok=True)
    existing={int(f[:-5]) for f in os.listdir(OUTDIR) if f.endswith(".json")}
    todo=[i for i in load_ids() if i not in existing]
    random.shuffle(todo)   # shuffle ID order -> avoids WAF sequential rate-limiting (~10x faster)
    print(f"to scrape: {len(todo)} (already have {len(existing)})",flush=True)
    saved=fin=0
    async with async_playwright() as p:
        b=await p.chromium.launch(headless=True)
        ctx=await b.new_context(user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
        async def block(r):
            if r.request.resource_type in ("image","stylesheet","font","media"): await r.abort()
            else: await r.continue_()
        await ctx.route("**/*",block)
        lg=await ctx.new_page()
        await lg.goto("https://mergr.com/login"); await lg.fill('input[name="username"]',EMAIL); await lg.fill('input[name="password"]',PASSWORD)
        await lg.click('button[type="submit"]'); await lg.wait_for_url("**/dashboard**",timeout=20000); await lg.close()
        print("logged in",flush=True)
        start=time.time()
        q=asyncio.Queue()
        for t in todo: q.put_nowait(t)
        done_n=[0]
        async def worker():
            nonlocal saved,fin
            while True:
                try: tid=q.get_nowait()
                except asyncio.QueueEmpty: return
                _,d,st=await scrape(ctx,tid)
                if d is not None:
                    json.dump(d,open(os.path.join(OUTDIR,f"{tid}.json"),"w"),indent=1)
                    saved+=1
                    if any(d.get(k) is not None for k in ("revenue","ebitda","ev_revenue","ev_ebitda")): fin+=1
                done_n[0]+=1
                if done_n[0]%500==0:
                    el=time.time()-start; rate=done_n[0]/el*60 if el else 0
                    print(f"  {done_n[0]}/{len(todo)} ({saved} saved, {fin} w/financials), ~{rate:.0f}/min",flush=True)
        await asyncio.gather(*[worker() for _ in range(PARALLEL)])
        print(f"DONE: {saved} saved, {fin} with financials, {(time.time()-start)/60:.1f} min",flush=True)
        await b.close()

if __name__=="__main__":
    asyncio.run(main())
