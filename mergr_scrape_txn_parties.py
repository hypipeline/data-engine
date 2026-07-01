#!/usr/bin/env python3
"""
Scrape acquirer/seller parties from transaction DETAIL pages for the financial
deals that currently have NO parties (mergr_txn_needs_parties.txt). Writes one
JSON line per deal to mergr_txn_parties.jsonl. Shuffled (WAF-safe), resumable,
worker pool. Single Mergr login.
  python3 mergr_scrape_txn_parties.py --ids 214363,234686   # TEST
"""
import asyncio, os, json, sys, random, time
from playwright.async_api import async_playwright
from mergr_parse_txn_detail import parse_txn_parties

EMAIL="craig.anderson@hyndlandpartners.com"; PASSWORD="X9R/N^3RvjtuJ.^"
IN="/Users/craiganderson/Dropbox/dev/on-testing/data-engine/mergr_txn_needs_parties.txt"
OUT="/Users/craiganderson/Dropbox/dev/on-testing/data-engine/mergr_txn_parties.jsonl"
PARALLEL=10

def load_ids():
    args=sys.argv[1:]
    if "--ids" in args:
        return [int(x) for x in args[args.index("--ids")+1].split(",")], True
    return [int(l) for l in open(IN) if l.strip().isdigit()], False

async def scrape(ctx,tid):
    for attempt in range(2):
        pg=await ctx.new_page()
        try:
            r=await pg.goto(f"https://mergr.com/transactions/{tid}",wait_until="domcontentloaded",timeout=45000)
            if r and r.status in (301,302,404): return tid,None
            await asyncio.sleep(0.2); h=await pg.content()
            if "awswaf" in h:
                for _ in range(6):
                    await asyncio.sleep(5); h=await pg.content()
                    if "awswaf" not in h: break
                else: return tid,None
            return tid, parse_txn_parties(h)
        except Exception:
            if attempt==0:
                await pg.close(); await asyncio.sleep(0.5); continue
            return tid,None
        finally:
            await pg.close()

async def main():
    ids, test = load_ids()
    if not test and os.path.exists(OUT):
        have={json.loads(l)["transaction_id"] for l in open(OUT)}
        ids=[i for i in ids if i not in have]
    if not test: random.shuffle(ids)
    print(f"to scrape: {len(ids)}{' TEST' if test else ''}",flush=True)
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
        out=None if test else open(OUT,"a")
        n=[0]; found=[0]; start=time.time()
        q=asyncio.Queue()
        for i in ids: q.put_nowait(i)
        async def w():
            while True:
                try: tid=q.get_nowait()
                except asyncio.QueueEmpty: return
                tid,parties=await scrape(ctx,tid)
                rec={"transaction_id":tid,"acquirers":(parties or {}).get("acquirers",[]),"sellers":(parties or {}).get("sellers",[])}
                if test:
                    print(" ",json.dumps(rec),flush=True)
                else:
                    out.write(json.dumps(rec)+"\n"); out.flush(); n[0]+=1
                    if rec["acquirers"] or rec["sellers"]: found[0]+=1
                    if n[0]%500==0:
                        print(f"  {n[0]}/{len(ids)} ({found[0]} with parties), ~{n[0]/(time.time()-start)*60:.0f}/min",flush=True)
        await asyncio.gather(*[w() for _ in range(PARALLEL)])
        if out: out.close()
        print(f"DONE: {n[0] if not test else len(ids)} processed, {found[0]} with parties",flush=True)
        await b.close()

if __name__=="__main__":
    asyncio.run(main())
