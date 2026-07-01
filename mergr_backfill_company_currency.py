#!/usr/bin/env python3
"""
Backfill the correct revenue CURRENCY for companies. The original company scrape
mis-captured `revenue_currency` (it grabbed description text). Company financials
are in each company's LOCAL reporting currency, labelled on the page as
"(millions of XXX)". This re-fetches each company-with-revenue page and extracts
that currency code.

Output: mergr_company_currency.jsonl — one {"company_id":N,"revenue_currency":"JPY"} per line.
Resumable (skips ids already recorded). Shuffled ids (WAF-safe), worker pool, caffeinate.

  python3 mergr_backfill_company_currency.py                    # all companies-with-revenue
  python3 mergr_backfill_company_currency.py --ids 52246,107637 # TEST specific ids
"""
import asyncio, os, json, sys, re, random, time
from playwright.async_api import async_playwright

EMAIL="craig.anderson@hyndlandpartners.com"; PASSWORD="X9R/N^3RvjtuJ.^"
IDS_FILE="/Users/craiganderson/Dropbox/dev/on-testing/data-engine/mergr_companies_with_revenue.txt"
OUT="/Users/craiganderson/Dropbox/dev/on-testing/data-engine/mergr_company_currency.jsonl"
PARALLEL=10
UNMATCHED="/Users/craiganderson/Dropbox/dev/on-testing/data-engine/mergr_company_currency_unmatched.jsonl"
# capture ANY scale word + currency — do not hardcode the allowed scales.
CUR_RE=re.compile(r'\(([A-Za-z]+) of ([A-Z]{2,3})\)')
# looser probe to grab context when the strict pattern doesn't match (for review)
NEAR_RE=re.compile(r'.{0,40}\([A-Za-z ]+of[^)]{0,20}\).{0,10}')

def load_ids():
    args=sys.argv[1:]
    if "--ids" in args:
        return [int(x) for x in args[args.index("--ids")+1].split(",")], True
    ids=[int(l) for l in open(IDS_FILE) if l.strip().isdigit()]
    return ids, False

async def scrape(ctx,cid):
    for attempt in range(2):
        pg=await ctx.new_page()
        try:
            r=await pg.goto(f"https://mergr.com/company/{cid}",wait_until="domcontentloaded",timeout=45000)
            if r and r.status in (301,302,404): return cid,None
            await asyncio.sleep(0.15)
            h=await pg.content()
            if "awswaf" in h:
                for _ in range(6):
                    await asyncio.sleep(5); h=await pg.content()
                    if "awswaf" not in h: break
            m=CUR_RE.search(h)
            if m:
                return cid, m.group(1).lower(), m.group(2).upper(), None
            # NO match: capture a diagnostic snippet near any "(... of ...)" for later review
            near=NEAR_RE.search(h)
            snippet=(near.group(0).strip() if near else None)
            return cid, None, None, snippet
        except Exception:
            if attempt==0:
                await pg.close(); await asyncio.sleep(0.5); continue
            return cid,None
        finally:
            await pg.close()

async def main():
    ids, test = load_ids()
    if not test:
        have={json.loads(l)["company_id"] for l in open(OUT)} if os.path.exists(OUT) else set()
        ids=[i for i in ids if i not in have]
        random.shuffle(ids)
    print(f"to scrape: {len(ids)}{' (TEST)' if test else ''}",flush=True)
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
        start=time.time(); n=[0]; found=[0]
        out=None if test else open(OUT,"a")
        q=asyncio.Queue()
        for i in ids: q.put_nowait(i)
        scales={}
        unmatched=None if test else open(UNMATCHED,"a")
        async def worker():
            while True:
                try: cid=q.get_nowait()
                except asyncio.QueueEmpty: return
                cid,scale,cur,snippet=await scrape(ctx,cid)
                if test:
                    print(f"  company {cid}: scale={scale} currency={cur}"
                          + (f"  UNMATCHED snippet={snippet!r}" if scale is None else ""),flush=True)
                else:
                    out.write(json.dumps({"company_id":cid,"revenue_scale":scale,"revenue_currency":cur})+"\n"); out.flush()
                    n[0]+=1
                    scales[scale]=scales.get(scale,0)+1
                    if cur: found[0]+=1
                    else:   # store the no-match cases WITH context for later review
                        unmatched.write(json.dumps({"company_id":cid,"snippet":snippet})+"\n"); unmatched.flush()
                    if n[0]%500==0:
                        el=time.time()-start
                        print(f"  {n[0]}/{len(ids)} ({found[0]} matched), scales={scales}, ~{n[0]/el*60:.0f}/min",flush=True)
        await asyncio.gather(*[worker() for _ in range(PARALLEL)])
        if out: out.close()
        if unmatched: unmatched.close()
        print(f"DONE: {n[0]} processed" if not test else "TEST DONE",flush=True)
        await b.close()

if __name__=="__main__":
    asyncio.run(main())
