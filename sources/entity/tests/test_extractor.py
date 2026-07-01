"""Test the website extractor against known sites."""

import asyncio
import sys
sys.path.insert(0, "/Users/craiganderson/Dropbox/dev/entity-lookup")

from scrapers.website_extractor import extract_from_url


async def test_site(url: str):
    print(f"=== Extracting from {url} ===\n")
    result = await extract_from_url(url)

    print(f"Domain: {result.domain}")
    print(f"Pages fetched: {len(result.raw_texts)}")
    for path in result.raw_texts:
        print(f"  {path}")

    print(f"\nCandidate entities ({len(result.candidates)}):")
    for c in result.candidates:
        print(f"  [{c.confidence}] {c.name}")

    print(f"\nJurisdiction clues: {result.jurisdiction_clues}")
    print("\n" + "=" * 60 + "\n")


async def main():
    await test_site("https://etnaindustrialpartners.com/")
    await test_site("https://www.blackrock.com/")
    await test_site("https://www.kkr.com/")


if __name__ == "__main__":
    asyncio.run(main())
