"""SEC EDGAR full-text search scraper. Plain HTTP, no browser needed."""

from __future__ import annotations
import httpx
from .base import BaseScraper, RegistryResult


class SECEdgarScraper(BaseScraper):
    registry_name = "SEC EDGAR"
    jurisdiction = "US"
    needs_browser = False

    SEARCH_INDEX_URL = "https://efts.sec.gov/LATEST/search-index"
    COMPANY_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
    COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
    USER_AGENT = "EntityLookup research@entitylookup.dev"

    async def search(self, entity_name: str, forms: str | None = None) -> list[RegistryResult]:
        headers = {"User-Agent": self.USER_AGENT}
        results = []
        seen = set()

        # Strategy 1: Try search-index API (works for multi-word, specific queries)
        try:
            params = {"q": f'"{entity_name}"'}
            if forms:
                params["forms"] = forms

            async with httpx.AsyncClient() as client:
                resp = await client.get(self.SEARCH_INDEX_URL, params=params, headers=headers, timeout=30)
                resp.raise_for_status()
                data = resp.json()

            # Extract unique entities from aggregation buckets
            for bucket in data.get("aggregations", {}).get("entity_filter", {}).get("buckets", []):
                raw_name = bucket["key"]
                name = raw_name.split("(CIK")[0].strip() if "(CIK" in raw_name else raw_name
                cik = raw_name.split("CIK ")[1].rstrip(")").strip() if "CIK " in raw_name else None

                if name.lower() in seen:
                    continue
                seen.add(name.lower())

                results.append(RegistryResult(
                    entity_name=name,
                    registry_id=cik,
                    jurisdiction="US",
                    raw_data={"doc_count": bucket["doc_count"], "display_name": raw_name},
                ))

            # Also check individual hits for more detail
            for hit in data.get("hits", {}).get("hits", [])[:20]:
                src = hit["_source"]
                names = src.get("display_names", [])
                for raw_name in names:
                    name = raw_name.split("(CIK")[0].strip() if "(CIK" in raw_name else raw_name
                    if name.lower() in seen:
                        continue
                    seen.add(name.lower())

                    cik = raw_name.split("CIK ")[1].rstrip(")").strip() if "CIK " in raw_name else None
                    locations = src.get("biz_locations", [])
                    inc_states = src.get("inc_states", [])

                    results.append(RegistryResult(
                        entity_name=name,
                        registry_id=cik,
                        jurisdiction=f"US-{inc_states[0]}" if inc_states else "US",
                        address=locations[0] if locations else None,
                        raw_data={
                            "form": src.get("form"),
                            "file_date": src.get("file_date"),
                            "adsh": src.get("adsh"),
                        },
                    ))
        except httpx.HTTPStatusError:
            pass  # search-index API fails on short/common terms, fall through

        # Strategy 2: If no results, try company name search via EDGAR company search
        if not results:
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(
                        "https://efts.sec.gov/LATEST/search-index",
                        params={"q": entity_name, "dateRange": "custom", "startdt": "2020-01-01"},
                        headers=headers,
                        timeout=30,
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        for bucket in data.get("aggregations", {}).get("entity_filter", {}).get("buckets", []):
                            raw_name = bucket["key"]
                            name = raw_name.split("(CIK")[0].strip() if "(CIK" in raw_name else raw_name
                            cik = raw_name.split("CIK ")[1].rstrip(")").strip() if "CIK " in raw_name else None

                            if name.lower() in seen:
                                continue
                            seen.add(name.lower())
                            results.append(RegistryResult(
                                entity_name=name,
                                registry_id=cik,
                                jurisdiction="US",
                                raw_data={"doc_count": bucket["doc_count"], "display_name": raw_name},
                            ))
            except httpx.HTTPStatusError:
                pass

        return results

    async def search_form_d(self, entity_name: str) -> list[RegistryResult]:
        """Search specifically in Form D filings (private fund offerings)."""
        return await self.search(entity_name, forms="D")

    async def search_adviser(self, entity_name: str) -> list[RegistryResult]:
        """Search specifically in adviser-related filings."""
        return await self.search(entity_name, forms="ADV,ADV-W,ADV-H")
