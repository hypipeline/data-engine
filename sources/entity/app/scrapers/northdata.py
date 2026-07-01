"""North Data scraper. Covers 24 European countries. Plain HTTP, no auth needed."""

from __future__ import annotations
import re
import httpx
from bs4 import BeautifulSoup
from urllib.parse import quote, urljoin
from .base import BaseScraper, RegistryResult


class NorthDataScraper(BaseScraper):
    registry_name = "North Data"
    jurisdiction = "EU"
    needs_browser = False

    BASE_URL = "https://www.northdata.com"
    SEARCH_URL = "https://www.northdata.com/search"
    USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

    # Countries covered by North Data
    COVERED_COUNTRIES = {
        "AT", "BE", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE",
        "GR", "IE", "IL", "LU", "MT", "NL", "NO", "PL", "PT", "RO",
        "ES", "SE", "CH", "GB",
    }

    def _clean_search_query(self, entity_name: str) -> str:
        """Strip corporate suffixes and punctuation that break North Data search."""
        clean = entity_name
        # Remove parenthetical aliases
        clean = re.sub(r'\([^)]*\)', '', clean).strip()
        # Remove common corporate suffixes that interfere with search
        suffix_pattern = r',?\s*\b(S\.A\.?|S\.L\.?|SA|SL|GmbH|AG|B\.V\.?|N\.V\.?|Inc\.?|LLC|Ltd\.?|Limited|Corp\.?|PLC|Plc|LLP|LP|L\.P\.)\s*$'
        clean = re.sub(suffix_pattern, '', clean, flags=re.IGNORECASE).strip()
        # Remove trailing punctuation
        clean = clean.rstrip(".,;:")
        return clean

    async def search(self, entity_name: str) -> list[RegistryResult]:
        headers = {"User-Agent": self.USER_AGENT}
        query = self._clean_search_query(entity_name)

        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
            resp = await client.get(
                self.SEARCH_URL,
                params={"query": query},
                headers=headers,
            )
            resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        results = []
        seen = set()

        for link in soup.select("a[href]"):
            text = link.get_text(strip=True)
            href = link.get("href", "")

            # Skip non-company links
            if not text or len(text) < 5:
                continue
            if not href.startswith("/") or href.startswith("/search"):
                continue

            # Skip non-company text (board members, signatories, concepts, addresses, trademarks)
            text_lower = text.lower()
            skip_prefixes = [
                "chair of", "member of", "authorized", "other concepts",
                "managing director", "secretary", "treasurer", "auditor",
                "avenida", "calle", "rue", "straße", "strasse", "via ",
            ]
            skip_contains = [
                "trademark filing", "wordmark", "figurative mark",
                "modification of articles", "signatory",
                "no longer partner", "former partner", "partner:",
                "shareholder:", "director:", "officer:",
            ]
            if any(text_lower.startswith(p) for p in skip_prefixes):
                continue
            if any(s in text_lower for s in skip_contains):
                continue
            # Skip if text is too long (likely a description, not a company name)
            if len(text) > 120:
                continue

            # Parse "Company Name, City, Country" pattern
            parsed = self._parse_result_text(text)
            if not parsed:
                continue

            name, location = parsed

            # Skip duplicates
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)

            # Extract registry ID from href
            registry_id = self._extract_registry_id(href)

            # Detect country/jurisdiction
            jurisdiction = self._detect_jurisdiction(text, location)

            # Detect if terminated
            status = "Terminated" if "✝" in text else None

            results.append(RegistryResult(
                entity_name=name,
                registry_id=registry_id,
                status=status,
                jurisdiction=jurisdiction,
                address=location,
                raw_data={
                    "northdata_url": urljoin(self.BASE_URL, href),
                    "source": "North Data",
                },
            ))

        return results

    async def get_details(self, northdata_url: str) -> RegistryResult | None:
        """Fetch full company details from a North Data company page."""
        headers = {"User-Agent": self.USER_AGENT}

        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
            resp = await client.get(northdata_url, headers=headers)
            if resp.status_code != 200:
                return None

        soup = BeautifulSoup(resp.text, "html.parser")
        text = soup.get_text(separator="\n", strip=True)

        # Extract key fields from page text
        name = self._extract_page_field(text, "company name") or ""
        reg_id = self._extract_page_field(text, "register")
        status = self._extract_page_field(text, "status")
        address = self._extract_page_field(text, "address")

        # Try to get the company name from the page title
        title = soup.find("title")
        if title:
            title_text = title.get_text(strip=True)
            # Title format: "Company Name, City - North Data"
            if " - North Data" in title_text:
                name = title_text.split(" - North Data")[0].strip()

        return RegistryResult(
            entity_name=name,
            registry_id=reg_id,
            status=status,
            address=address,
            raw_data={"northdata_url": northdata_url, "page_text": text[:3000]},
        )

    def _parse_result_text(self, text: str) -> tuple[str, str] | None:
        """Parse 'Company Name, City, Country' or 'Company Name, City, Country ✝︎'"""
        clean = text.replace("✝︎", "").replace("✝", "").strip()
        # Need at least one comma (name, location)
        if "," not in clean:
            return None
        # Split on last comma group to get name vs location
        # Pattern: "Company Name SL, Madrid, Spain"
        parts = clean.split(",")
        if len(parts) < 2:
            return None

        # The company name may contain commas, so try to find the split point
        # Location is typically "City, Country" at the end
        # Heuristic: last 1-2 parts are location
        country_indicators = [
            "Spain", "Germany", "France", "Italy", "Netherlands", "Belgium",
            "Switzerland", "Luxembourg", "Ireland", "United Kingdom", "UK",
            "Austria", "Denmark", "Sweden", "Norway", "Finland", "Poland",
            "Romania", "Portugal", "Greece", "Czech Republic", "Croatia",
            "Cyprus", "Estonia", "Malta", "Israel", "United States",
        ]

        last_part = parts[-1].strip()
        if last_part in country_indicators or last_part == "US":
            if len(parts) >= 3:
                name = ",".join(parts[:-2]).strip()
                location = f"{parts[-2].strip()}, {last_part}"
            else:
                name = parts[0].strip()
                location = last_part
        else:
            # Might be "Company, City" without country
            name = ",".join(parts[:-1]).strip()
            location = parts[-1].strip()

        if len(name) < 3:
            return None

        return name, location

    def _extract_registry_id(self, href: str) -> str | None:
        """Extract registry ID from North Data URL path."""
        # URL pattern: /Company%20Name,%20City/RegistryType%20Number
        parts = href.strip("/").split("/")
        if len(parts) >= 2:
            from urllib.parse import unquote
            reg_part = unquote(parts[-1])
            # Skip internal IDs like _c1234567890
            if reg_part.startswith("_c"):
                return None
            return reg_part
        return None

    def _detect_jurisdiction(self, text: str, location: str) -> str | None:
        """Detect jurisdiction from result text."""
        country_map = {
            "Spain": "ES", "Germany": "DE", "France": "FR", "Italy": "IT",
            "Netherlands": "NL", "Belgium": "BE", "Switzerland": "CH",
            "Luxembourg": "LU", "Ireland": "IE", "United Kingdom": "GB",
            "UK": "GB", "Austria": "AT", "Denmark": "DK", "Sweden": "SE",
            "Norway": "NO", "Finland": "FI", "Poland": "PL", "Romania": "RO",
            "Portugal": "PT", "Greece": "GR", "Czech Republic": "CZ",
            "Croatia": "HR", "Cyprus": "CY", "Estonia": "EE", "Malta": "MT",
            "Israel": "IL", "United States": "US",
        }
        combined = f"{text} {location}"
        for country, code in country_map.items():
            if country in combined:
                return code
        return None

    def _extract_page_field(self, text: str, field: str) -> str | None:
        """Simple field extraction from page text."""
        lines = text.split("\n")
        for i, line in enumerate(lines):
            if field.lower() in line.lower() and i + 1 < len(lines):
                return lines[i + 1].strip()
        return None
