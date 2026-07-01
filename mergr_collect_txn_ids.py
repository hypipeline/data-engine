#!/usr/bin/env python3
"""
Enumerate ALL transaction IDs from mergr.com/transactions/search, partitioned by
YEAR (sub-split by MONTH for any year that hits the 10,000-row pagination cap).

Per row we capture the transaction id and the deal value shown in the table.
Outputs:
  mergr_txn_all_ids.txt      every transaction id found  (-> diff vs scraped = missing)
  mergr_txn_valued_ids.txt   ids whose row shows a deal value (-> these have financials
                             worth a detail scrape; valueless deals have no tr-footer)
Resumable: completed year/month partitions recorded in a .state file.

Usage:
  python3 mergr_collect_txn_ids.py            # full 1922..2026
  python3 mergr_collect_txn_ids.py 2023       # single year (TEST)
"""
import asyncio, re, os, json, sys, time
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup

EMAIL="craig.anderson@hyndlandpartners.com"; PASSWORD="X9R/N^3RvjtuJ.^"
BASE="https://mergr.com/transactions/search"
ALL_OUT="/Users/craiganderson/Dropbox/dev/on-testing/data-engine/mergr_txn_all_ids.txt"
VAL_OUT="/Users/craiganderson/Dropbox/dev/on-testing/data-engine/mergr_txn_valued_ids.txt"
STATE="/Users/craiganderson/Dropbox/dev/on-testing/data-engine/mergr_txn_ids.state"
PARALLEL=6; CAP_PAGE=400
MONEY_RE=re.compile(r'^[$€£¥]?[\d,]+$')

all_ids=set(); valued_ids=set(); done=set()

def save():
    with open(ALL_OUT,"w") as f: f.write("\n".join(map(str,sorted(all_ids)))+"\n")
    with open(VAL_OUT,"w") as f: f.write("\n".join(map(str,sorted(valued_ids)))+"\n")
    with open(STATE,"w") as f: json.dump(sorted(done),f)

def parse_rows(html):
    """Return list of (txn_id, has_value:bool)."""
    soup=BeautifulSoup(html,"html.parser")
    out=[]
    tbl=None
    for t in soup.find_all("table"):
        if t.find("th") and "Target" in t.get_text(): tbl=t; break
    if not tbl: return out
    for tr in tbl.find_all("tr"):
        m=re.search(r'/transactions/(\d+)', str(tr))
        if not m: continue
        tid=int(m.group(1))
        has_val=False
        for td in tr.find_all("td"):
            t=td.get_text(strip=True)
            if t and t!="-" and MONEY_RE.match(t) and not re.match(r'^\d{4}-\d{2}-\d{2}$',t):
                has_val=True; break
        out.append((tid,has_val))
    return out

def maxpage(html):
    return max([int(x) for x in re.findall(r'[?&]page=(\d+)', html)] or [1])

async def fetch(ctx,url,n):
    pg=await ctx.new_page()
    try:
        sep="&" if "?" in url else "?"
        await pg.goto(f"{url}{sep}page={n}",wait_until="domcontentloaded",timeout=45000)
        await asyncio.sleep(0.25)
        h=await pg.content()
        if "awswaf" in h:
            for _ in range(6):
                await asyncio.sleep(5); h=await pg.content()
                if "awswaf" not in h: break
            else: return n,[],0
        return n,parse_rows(h),maxpage(h)
    except Exception:
        return n,[],0
    finally:
        await pg.close()

async def crawl(ctx,url):
    """Paginate a partition until a batch yields NO NEW ids. Out-of-range pages on
    this search clamp/repeat the last page rather than returning empty, so we
    terminate on dedup (no new ids), not on emptiness. Returns (rows, capped)."""
    rows=[]; seen=set(); cur=1; hit_cap=False
    while cur<=CAP_PAGE:
        batch=[cur+i for i in range(PARALLEL) if cur+i<=CAP_PAGE]
        res=await asyncio.gather(*[fetch(ctx,url,n) for n in batch])
        new=0
        for n,r,mp in res:
            for tid,hv in r:
                if tid not in seen:
                    seen.add(tid); rows.append((tid,hv)); new+=1
        if new==0:
            break
        cur=max(batch)+1
    else:
        hit_cap=True            # filled to CAP_PAGE still finding new ids => genuinely >10k
    return rows,hit_cap

def absorb(rows):
    for tid,hv in rows:
        all_ids.add(tid)
        if hv: valued_ids.add(tid)

async def do_partition(ctx,label,url):
    if label in done: return
    rows,capped=await crawl(ctx,url)
    if capped:
        # sub-split by month
        yr=label.replace("y","")
        for mo in range(1,13):
            ml=f"{label}m{mo}"
            if ml in done: continue
            murl=f"{url}&transaction[startMonth]={mo}&transaction[endMonth]={mo}"
            mrows,_=await crawl(ctx,murl)
            absorb(mrows); done.add(ml)
            print(f"   {ml}: +{len(mrows)} rows ({len(all_ids)} ids)",flush=True)
    else:
        absorb(rows)
    done.add(label); save()
    print(f"  {label}: {len(rows)} rows{' [MONTH-SPLIT]' if capped else ''} -> {len(all_ids)} ids, {len(valued_ids)} valued",flush=True)

async def main():
    years=[int(sys.argv[1])] if len(sys.argv)>1 else list(range(2026,1921,-1))
    if os.path.exists(ALL_OUT): all_ids.update(int(l) for l in open(ALL_OUT) if l.strip().isdigit())
    if os.path.exists(VAL_OUT): valued_ids.update(int(l) for l in open(VAL_OUT) if l.strip().isdigit())
    if os.path.exists(STATE): done.update(json.load(open(STATE)))
    print(f"resume: {len(all_ids)} ids, {len(valued_ids)} valued, {len(done)} partitions",flush=True)
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
        for y in years:
            url=f"{BASE}?transaction[startYear]={y}&transaction[endYear]={y}"
            await do_partition(ctx,f"y{y}",url)
        save()
        print(f"DONE: {len(all_ids)} ids, {len(valued_ids)} valued in {(time.time()-start)/60:.1f} min",flush=True)
        await b.close()

if __name__=="__main__":
    asyncio.run(main())
