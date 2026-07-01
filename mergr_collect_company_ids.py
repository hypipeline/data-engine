#!/usr/bin/env python3
"""
Enumerate ALL canonical company IDs from mergr.com/companies/search.

The listing caps at page 400 (=10,000 records), so we partition by sector
(61 values). Any sector that fills to page 400 is >10k and gets subdivided by
country; any sector x country cell that still caps is subdivided by revenue.
IDs are unioned and de-duplicated, then written to mergr_company_ids_full.txt.

Resumable: completed slice labels are recorded in a .state file and skipped.
Obeys single-login rule — run nothing else logged-in while this runs.
"""
import asyncio, re, os, json, time
from playwright.async_api import async_playwright

EMAIL="craig.anderson@hyndlandpartners.com"; PASSWORD="X9R/N^3RvjtuJ.^"
BASE="https://mergr.com/companies/search"
OUT="/Users/craiganderson/Dropbox/dev/on-testing/data-engine/mergr_company_ids_full.txt"
STATE="/Users/craiganderson/Dropbox/dev/on-testing/data-engine/mergr_company_ids.state"
PARALLEL=5
CAP_PAGE=400

# sector + country filter values (authoritative, from the search form: 61 + 102)
SECTORS=[1,2,59,3,4,5,6,7,8,9,10,11,12,13,60,14,57,61,15,16,17,18,19,20,21,22,23,24,
         25,26,27,58,28,29,30,31,32,33,34,35,36,37,38,39,40,41,42,43,44,45,46,47,48,
         49,50,51,52,53,54,55,56]
COUNTRIES=[1,2,3,4,5,6,7,8,9,12,14,15,19,22,23,25,26,27,28,29,30,33,37,39,42,43,45,48,
           51,52,53,55,57,59,61,62,63,69,70,73,74,78,84,86,98,99,101,104,105,107,108,
           110,117,118,122,123,124,129,132,136,138,140,141,144,145,150,151,153,156,160,
           162,164,167,168,170,171,172,173,175,176,177,184,186,188,189,193,113,194,200,
           201,202,204,207,212,213,216,217,218,219,224,225,230]
REVENUE=[1,2,3,4,5]

all_ids=set()
done=set()

def save():
    with open(OUT,"w") as f: f.write("\n".join(map(str,sorted(all_ids)))+"\n")
    with open(STATE,"w") as f: json.dump(sorted(done),f)

async def fetch(ctx,url,n):
    pg=await ctx.new_page()
    try:
        await pg.goto(f"{url}&page={n}",wait_until="domcontentloaded",timeout=45000)
        await asyncio.sleep(0.2)
        h=await pg.content()
        if "awswaf" in h:
            for _ in range(6):
                await asyncio.sleep(5); h=await pg.content()
                if "awswaf" not in h: break
            else:
                return n,set(),0
        ids={int(x) for x in re.findall(r"/company/(\d+)",h)}
        mp=max([int(x) for x in re.findall(r"[?&]page=(\d+)",h)] or [1])
        return n,ids,mp
    except Exception:
        return n,set(),0
    finally:
        await pg.close()

async def is_capped(ctx,url):
    """Cheap pre-probe: if page 400 returns rows, the slice is >10k -> subdivide."""
    _,p400,_=await fetch(ctx,url,CAP_PAGE)
    return len(p400)>0

async def crawl_full(ctx,url):
    """Paginate 1..until empty (ceiling CAP_PAGE). Returns ids."""
    ids=set(); cur=1; known_max=1
    while cur<=min(known_max+1,CAP_PAGE):
        batch=[cur+i for i in range(PARALLEL) if cur+i<=min(known_max+1,CAP_PAGE)]
        res=await asyncio.gather(*[fetch(ctx,url,n) for n in batch])
        got=False
        for n,pids,mp in res:
            if pids: ids|=pids; got=True
            known_max=max(known_max,mp)
        if not got: break
        cur=max(batch)+1
    return ids

async def handle(ctx,url,label,can_subdivide):
    """Returns True if slice is capped and should be subdivided by the caller.
    Otherwise crawls the slice fully, records ids, and returns False."""
    if label in done:
        return False
    if can_subdivide and await is_capped(ctx,url):
        print(f"  {label}: CAPPED -> subdivide",flush=True)
        return True
    ids=await crawl_full(ctx,url)
    all_ids.update(ids)
    done.add(label); save()
    print(f"  {label}: {len(ids)} ids ({len(all_ids)} total)",flush=True)
    return False

async def main():
    if os.path.exists(OUT):
        all_ids.update(int(l) for l in open(OUT) if l.strip().isdigit())
    if os.path.exists(STATE):
        done.update(json.load(open(STATE)))
    print(f"resume: {len(all_ids)} ids, {len(done)} slices done",flush=True)

    async with async_playwright() as p:
        b=await p.chromium.launch(headless=True)
        ctx=await b.new_context(user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
        # Block heavy resources — IDs live in the initial HTML, so skip images/css/fonts/media.
        async def block(route):
            if route.request.resource_type in ("image","stylesheet","font","media"):
                await route.abort()
            else:
                await route.continue_()
        await ctx.route("**/*", block)
        lg=await ctx.new_page()
        await lg.goto("https://mergr.com/login")
        await lg.fill('input[name="username"]',EMAIL); await lg.fill('input[name="password"]',PASSWORD)
        await lg.click('button[type="submit"]'); await lg.wait_for_url("**/dashboard**",timeout=20000)
        await lg.close(); print("logged in",flush=True)
        start=time.time()

        for s in SECTORS:
            surl=f"{BASE}?company[sectors][]={s}"
            slabel=f"sec{s}"
            if slabel in done: continue
            if await handle(ctx,surl,slabel,can_subdivide=True):   # capped sector
                for c in COUNTRIES:
                    curl=f"{surl}&company[countries][]={c}"
                    clabel=f"sec{s}_c{c}"
                    if clabel in done: continue
                    if await handle(ctx,curl,clabel,can_subdivide=True):  # capped sector x country
                        for r in REVENUE:
                            await handle(ctx,f"{curl}&company[revenue][]={r}",f"sec{s}_c{c}_r{r}",can_subdivide=False)
                        done.add(clabel); save()
                done.add(slabel); save()
        save()
        print(f"DONE: {len(all_ids)} company ids in {(time.time()-start)/60:.1f} min -> {OUT}",flush=True)
        await b.close()

if __name__=="__main__":
    asyncio.run(main())
