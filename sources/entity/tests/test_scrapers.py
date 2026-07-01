"""Quick smoke tests for scrapers that don't need a browser."""

import asyncio
import sys
sys.path.insert(0, "/Users/craiganderson/Dropbox/dev/entity-lookup")

from scrapers.sec_edgar import SECEdgarScraper
from scrapers.registry import get_scraper, list_jurisdictions


async def test_sec_edgar():
    print("=== SEC EDGAR: 'Etna Capital' ===")
    scraper = SECEdgarScraper()
    results = await scraper.search("etna capital")
    print(f"Found {len(results)} results:")
    for r in results[:5]:
        print(f"  {r.entity_name} | ID: {r.registry_id} | Jurisdiction: {r.jurisdiction}")
    print()

    print("=== SEC EDGAR Form D: 'etna capital' ===")
    results = await scraper.search_form_d("etna capital")
    print(f"Found {len(results)} results:")
    for r in results:
        print(f"  {r.entity_name} | ID: {r.registry_id} | {r.raw_data}")
    print()


async def test_registry_map():
    print("=== Registry Map ===")
    print(f"Supported jurisdictions: {list_jurisdictions()}")

    for alias in ["Delaware", "UK", "Ontario", "US", "new york"]:
        cls = get_scraper(alias)
        print(f"  '{alias}' -> {cls.__name__ if cls else None}")
    print()


async def main():
    await test_registry_map()
    await test_sec_edgar()


if __name__ == "__main__":
    asyncio.run(main())
