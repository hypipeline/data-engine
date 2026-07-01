"""
Entity Lookup Pipeline

Takes a URL, extracts candidate names, searches registries, returns matches.
Designed to be called from Claude Code (free) or wrapped in an API endpoint later.
"""

from __future__ import annotations
import asyncio
import json
from dataclasses import dataclass, field, asdict

from scrapers.website_extractor import extract_from_url, WebsiteExtraction
from scrapers.whois_lookup import whois_lookup, whois_to_jurisdiction_clues, whois_to_candidate_names
from scrapers.base import RegistryResult
from scrapers.sec_edgar import SECEdgarScraper
from scrapers.delaware_dos import DelawareDOSScraper
from scrapers.ontario_obr import OntarioOBRScraper
from scrapers.northdata import NorthDataScraper
from scrapers.registry import get_scraper, JURISDICTION_ALIASES


@dataclass
class LookupResult:
    """Final output for an entity lookup."""
    input_url: str
    whois_data: dict = field(default_factory=dict)
    candidates_found: list[str] = field(default_factory=list)
    jurisdiction_clues: list[str] = field(default_factory=list)
    registry_results: list[dict] = field(default_factory=list)
    best_match: dict | None = None


# Default registries to search when no jurisdiction clue is found
DEFAULT_REGISTRIES = ["US-DE", "US"]


async def create_browserbase_session(api_key: str, project_id: str) -> str:
    """Create a Browserbase session and return the connect URL."""
    import httpx
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.browserbase.com/v1/sessions",
            headers={
                "x-bb-api-key": api_key,
                "Content-Type": "application/json",
            },
            json={"projectId": project_id},
        )
        resp.raise_for_status()
        return resp.json()["connectUrl"]


async def lookup_entity(
    url: str,
    browserbase_api_key: str | None = None,
    browserbase_project_id: str | None = None,
    companies_house_api_key: str | None = None,
    registries: list[str] | None = None,
) -> LookupResult:
    """
    Main pipeline: URL → candidates → registry search → results.

    Args:
        url: Website URL to analyze
        browserbase_api_key: Optional, needed for DE DOS and Ontario OBR
        browserbase_project_id: Optional, needed for Browserbase
        companies_house_api_key: Optional, needed for UK Companies House
        registries: Optional list of jurisdiction codes to search.
                    If None, inferred from website or defaults used.
    """
    # Step 1a: WHOIS lookup (fast, free, often has org name + jurisdiction)
    print(f"[1/4] WHOIS lookup for {url}...")
    whois_result = await whois_lookup(url)
    whois_data = {
        "registrant_name": whois_result.registrant_name,
        "registrant_org": whois_result.registrant_org,
        "registrant_city": whois_result.registrant_city,
        "registrant_state": whois_result.registrant_state,
        "registrant_country": whois_result.registrant_country,
        "registrant_email": whois_result.registrant_email,
        "creation_date": whois_result.creation_date,
    }
    whois_candidates = whois_to_candidate_names(whois_result)
    whois_jurisdictions = whois_to_jurisdiction_clues(whois_result)
    print(f"    WHOIS org: {whois_result.registrant_org}")
    print(f"    WHOIS registrant: {whois_result.registrant_name}")
    print(f"    WHOIS jurisdiction: {whois_jurisdictions}")

    # Step 1b: Extract candidates from website
    print(f"\n[2/4] Extracting candidates from website...")
    extraction = await extract_from_url(url)

    # Merge candidates: WHOIS org first (high value), then website candidates
    candidate_names = []
    seen = set()

    # WHOIS org name is highest confidence (but only if it looks like a company)
    # Corporate suffixes that indicate a comma is part of a company name, not "Last, First"
    _corp_suffixes = {"s.a.", "s.l.", "inc.", "inc", "llc", "ltd", "ltd.", "corp.", "corp",
                      "gmbh", "ag", "b.v.", "n.v.", "plc", "lp", "l.p.", "llp", "sa", "sl"}
    for name in whois_candidates:
        name = name.strip()
        if name.lower() in seen or len(name) < 4:
            continue
        if name.endswith(".com") or name.endswith(".org") or name.endswith(".net"):
            continue
        # If name has a comma, only keep it if it contains a corporate suffix
        if "," in name:
            has_suffix = any(s in name.lower() for s in _corp_suffixes)
            if not has_suffix:
                continue  # likely "Lastname, Firstname"
        seen.add(name.lower())
        candidate_names.append(name)

    # Website candidates (medium confidence first, then low)
    candidates = sorted(extraction.candidates, key=lambda c: c.confidence, reverse=True)
    for c in candidates:
        name = c.name.strip()
        if len(name.split()) > 6:
            continue
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        candidate_names.append(name)

    if not candidate_names:
        domain_name = extraction.domain.split(".")[0].replace("-", " ").title()
        candidate_names = [domain_name]

    # Merge jurisdiction clues from WHOIS + website
    all_jurisdiction_clues = list(dict.fromkeys(whois_jurisdictions + extraction.jurisdiction_clues))

    print(f"    Candidates: {candidate_names[:5]}")
    print(f"    Jurisdiction clues: {all_jurisdiction_clues}")

    # Step 3: Determine which registries to search
    if registries is None:
        registries = _infer_registries(all_jurisdiction_clues)
    print(f"\n[3/4] Searching registries: {registries}")

    # Step 3: Search each registry
    all_results = []
    for jurisdiction in registries:
        for candidate in candidate_names[:3]:  # limit to top 3 candidates
            results = await _search_registry(
                jurisdiction,
                candidate,
                browserbase_api_key=browserbase_api_key,
                browserbase_project_id=browserbase_project_id,
                companies_house_api_key=companies_house_api_key,
            )
            for r in results:
                all_results.append({
                    "search_term": candidate,
                    "registry": jurisdiction,
                    **asdict(r),
                })

    # Step 3b: Find best match
    best = _pick_best_match(candidate_names, all_results)

    result = LookupResult(
        input_url=url,
        whois_data=whois_data,
        candidates_found=candidate_names[:5],
        jurisdiction_clues=all_jurisdiction_clues,
        registry_results=all_results,
        best_match=best,
    )

    print(f"\n[4/4] Done. Found {len(all_results)} registry results.")
    if best:
        print(f"    Best match: {best['entity_name']} ({best['registry']})")
    else:
        print("    No confident match found.")

    return result


def _infer_registries(jurisdiction_clues: list[str]) -> list[str]:
    """Decide which registries to search based on jurisdiction clues."""
    registries = set()

    for clue in jurisdiction_clues:
        clue_lower = clue.lower()
        # Direct match
        if clue.upper() in ["GB", "US", "US-DE", "CA-ON"]:
            registries.add(clue.upper())
            continue
        # Check aliases
        for alias, code in JURISDICTION_ALIASES.items():
            if alias in clue_lower:
                registries.add(code)
                break

    if not registries:
        # Default: search Delaware (most common for US entities) + SEC
        registries = set(DEFAULT_REGISTRIES)

    # For US entities, always include SEC + Delaware (most common incorporation state)
    if any(r.startswith("US") for r in registries):
        registries.add("US")
        registries.add("US-DE")

    return list(registries)


async def _search_registry(
    jurisdiction: str,
    entity_name: str,
    browserbase_api_key: str | None = None,
    browserbase_project_id: str | None = None,
    companies_house_api_key: str | None = None,
) -> list[RegistryResult]:
    """Search a single registry."""
    try:
        if jurisdiction == "US":
            scraper = SECEdgarScraper()
            return await scraper.search(entity_name)

        elif jurisdiction == "US-DE":
            if not browserbase_api_key or not browserbase_project_id:
                print(f"    Skipping {jurisdiction} (no Browserbase credentials)")
                return []
            scraper = DelawareDOSScraper()
            connect_url = await create_browserbase_session(browserbase_api_key, browserbase_project_id)
            return await scraper.search(entity_name, connect_url=connect_url)

        elif jurisdiction == "CA-ON":
            if not browserbase_api_key or not browserbase_project_id:
                print(f"    Skipping {jurisdiction} (no Browserbase credentials)")
                return []
            scraper = OntarioOBRScraper()
            connect_url = await create_browserbase_session(browserbase_api_key, browserbase_project_id)
            return await scraper.search(entity_name, connect_url=connect_url)

        elif jurisdiction == "GB":
            if not companies_house_api_key:
                print(f"    Skipping {jurisdiction} (no Companies House API key)")
                return []
            from scrapers.uk_companies_house import UKCompaniesHouseScraper
            scraper = UKCompaniesHouseScraper(companies_house_api_key)
            return await scraper.search(entity_name)

        elif jurisdiction == "EU":
            scraper = NorthDataScraper()
            return await scraper.search(entity_name)

        else:
            print(f"    No scraper for {jurisdiction}")
            return []

    except Exception as e:
        print(f"    Error searching {jurisdiction} for '{entity_name}': {e}")
        return []


def _normalize_name(name: str) -> str:
    """Strip corporate suffixes, punctuation, and diacritics for comparison."""
    import re
    import unicodedata
    # Remove diacritics (ñ→n, é→e, etc.)
    nfkd = unicodedata.normalize('NFKD', name)
    ascii_name = ''.join(c for c in nfkd if not unicodedata.combining(c))
    suffixes = r'\b(inc\.?|llc|l\.l\.c\.?|ltd\.?|limited|corp\.?|corporation|plc|gmbh|ag|s\.a\.?|s\.l\.?|sa|sl|b\.v\.?|n\.v\.?|lp|l\.p\.?|llp|co\.?|company)\b'
    normalized = re.sub(suffixes, '', ascii_name.lower(), flags=re.IGNORECASE)
    normalized = re.sub(r'[.,;:()\-\'"]+', ' ', normalized)
    return ' '.join(normalized.split()).strip()


def _pick_best_match(candidate_names: list[str], results: list[dict]) -> dict | None:
    """Match registry results to candidates. Requires substantial name overlap, not just a shared word."""
    if not results:
        return None

    best = None
    best_score = 0

    for result in results:
        entity_raw = result.get("entity_name", "")
        entity_norm = _normalize_name(entity_raw)
        entity_words = set(entity_norm.split())

        for candidate in candidate_names:
            candidate_norm = _normalize_name(candidate)
            candidate_words = set(w for w in candidate_norm.split() if len(w) > 2)

            if not candidate_words:
                continue

            # Exact normalized match
            if candidate_norm == entity_norm:
                score = 100
            # Candidate is a substring of entity or vice versa
            elif candidate_norm in entity_norm or entity_norm in candidate_norm:
                score = 95
            # Word overlap: require majority of candidate words to appear
            else:
                overlap = candidate_words & entity_words
                overlap_ratio = len(overlap) / len(candidate_words) if candidate_words else 0
                if overlap_ratio >= 0.75 and len(overlap) >= 2:
                    score = 80
                elif overlap_ratio >= 0.5 and len(overlap) >= 2:
                    score = 60
                else:
                    score = 0

            # Boost for active status
            if result.get("status") and "active" in result.get("status", "").lower():
                score += 5

            if score > best_score:
                best_score = score
                best = result

    return best if best_score >= 60 else None


# CLI entry point
async def main():
    import sys
    if len(sys.argv) < 2:
        print("Usage: python pipeline.py <url> [--browserbase]")
        print("\nExample:")
        print("  python pipeline.py https://etnaindustrialpartners.com/")
        print("  python pipeline.py https://etnaindustrialpartners.com/ --browserbase")
        sys.exit(1)

    url = sys.argv[1]
    use_browserbase = "--browserbase" in sys.argv

    bb_key = None
    bb_project = None
    if use_browserbase:
        import os as _os
        bb_key = _os.environ.get("BROWSERBASE_API_KEY")
        bb_project = _os.environ.get("BROWSERBASE_PROJECT_ID")

    result = await lookup_entity(
        url,
        browserbase_api_key=bb_key,
        browserbase_project_id=bb_project,
    )

    print("\n" + "=" * 60)
    print(json.dumps(asdict(result), indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
