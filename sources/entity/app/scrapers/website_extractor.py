"""Extract candidate legal entity names and jurisdiction clues from a website."""

from __future__ import annotations
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse
import httpx
from bs4 import BeautifulSoup


@dataclass
class CandidateEntity:
    """A candidate legal entity name extracted from a website."""
    name: str
    source_page: str  # which URL it was found on
    confidence: str = "low"  # low, medium, high


@dataclass
class WebsiteExtraction:
    """Structured output from website analysis."""
    domain: str
    candidates: list[CandidateEntity] = field(default_factory=list)
    jurisdiction_clues: list[str] = field(default_factory=list)
    raw_texts: dict[str, str] = field(default_factory=dict)


# Corporate suffixes that signal a legal entity name
CORPORATE_SUFFIXES = [
    "Inc.", "Inc", "Incorporated",
    "LLC", "L.L.C.",
    "LP", "L.P.",
    "LLP", "L.L.P.",
    "Ltd", "Ltd.", "Limited",
    "Corp", "Corp.", "Corporation",
    "Co.", "Company",
    "PLC", "Plc", "plc",
    "GmbH", "AG", "S.A.", "S.L.", "B.V.", "N.V.",
    "S.à r.l.", "S.a r.l.", "Sarl",
    "GP", "G.P.",
]

SUFFIX_PATTERN = re.compile(
    r'\b(' + '|'.join(re.escape(s) for s in CORPORATE_SUFFIXES) + r')\b',
    re.IGNORECASE,
)

# Paths to check for legal information
LEGAL_PATHS = [
    "",  # homepage
    "/about",
    "/about-us",
    "/legal",
    "/terms",
    "/terms-of-service",
    "/privacy",
    "/privacy-policy",
    "/contact",
    "/imprint",
]


async def extract_from_url(url: str, max_pages: int = 6) -> WebsiteExtraction:
    """Fetch a website and extract candidate entity names and jurisdiction clues."""
    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    domain = parsed.netloc.replace("www.", "")

    extraction = WebsiteExtraction(domain=domain)
    pages_fetched = 0

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=15,
        headers={"User-Agent": "Mozilla/5.0 (compatible; EntityLookup/1.0)"},
    ) as client:
        for path in LEGAL_PATHS:
            if pages_fetched >= max_pages:
                break

            page_url = base_url + path
            try:
                resp = await client.get(page_url)
                if resp.status_code != 200:
                    continue
            except (httpx.TimeoutException, httpx.ConnectError):
                continue

            pages_fetched += 1
            html = resp.text
            soup = BeautifulSoup(html, "html.parser")

            # Remove script/style tags
            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()

            text = soup.get_text(separator=" ", strip=True)
            # Keep a trimmed version for LLM processing
            extraction.raw_texts[path or "/"] = text[:3000]

            # Extract from copyright notices
            _extract_copyright(text, page_url, extraction)

            # Extract from footer
            footer = soup.find("footer")
            if footer:
                footer_text = footer.get_text(separator=" ", strip=True)
                _extract_copyright(footer_text, page_url, extraction)
                _extract_entities_with_suffix(footer_text, page_url, extraction)

            # Extract any text containing corporate suffixes (only from legal/footer pages, not homepage)
            if path and path != "":
                _extract_entities_with_suffix(text, page_url, extraction)

            # Extract jurisdiction clues (addresses, locations)
            _extract_jurisdiction_clues(text, extraction)

    # Add domain-derived candidate as a fallback
    raw_domain = domain.split(".")[0]
    # Split camelCase or joined words: "etnaindustrialpartners" → "etna industrial partners"
    domain_name = raw_domain.replace("-", " ")
    # Insert spaces before uppercase letters (camelCase)
    domain_name = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', domain_name)
    # Try to split common compound words if still one word
    if " " not in domain_name and len(domain_name) > 8:
        domain_name = _split_compound_domain(domain_name)
    domain_name = domain_name.title()
    if len(domain_name) >= 3:
        extraction.candidates.insert(0, CandidateEntity(
            name=domain_name,
            source_page=base_url,
            confidence="low",
        ))

    # Deduplicate and filter candidates
    seen = set()
    unique = []
    noise_words = {
        "home company", "our company", "the company", "about company",
        "cash management", "investment management", "asset management",
        "portfolio management", "risk management", "wealth management",
    }
    for c in extraction.candidates:
        key = c.name.lower().strip()
        if key in noise_words:
            continue
        if key not in seen and len(key) > 3:
            seen.add(key)
            unique.append(c)
    extraction.candidates = unique

    return extraction


def _split_compound_domain(name: str) -> str:
    """Try to split a compound domain name into words using common business terms."""
    terms = [
        "industrial", "partners", "capital", "management", "holdings",
        "advisors", "advisers", "group", "global", "ventures", "invest",
        "financial", "consulting", "solutions", "technologies", "tech",
        "energy", "health", "bio", "pharma", "media", "digital", "data",
        "systems", "services", "properties", "real", "estate",
    ]
    result = name.lower()
    # Greedy match: find known terms and insert spaces
    for term in sorted(terms, key=len, reverse=True):
        result = result.replace(term, f" {term} ")
    # Clean up multiple spaces
    result = " ".join(result.split())
    return result


def _extract_copyright(text: str, source_url: str, extraction: WebsiteExtraction):
    """Extract entity names from copyright notices."""
    patterns = [
        r'©\s*\d{4}\s+([^.•|©\n]{3,80})',
        r'Copyright\s*©?\s*\d{4}\s+([^.•|©\n]{3,80})',
        r'Copyright\s+([^.•|©\n]{3,80})',
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            name = match.group(1).strip().rstrip(".,;")
            # Remove trailing boilerplate
            name = re.sub(r'\s*(All rights reserved|All Rights Reserved).*', '', name).strip()
            if name and len(name) > 3:
                extraction.candidates.append(CandidateEntity(
                    name=name,
                    source_page=source_url,
                    confidence="medium",
                ))


def _extract_entities_with_suffix(text: str, source_url: str, extraction: WebsiteExtraction):
    """Find text fragments containing corporate suffixes."""
    for match in SUFFIX_PATTERN.finditer(text):
        start = max(0, match.start() - 60)
        end = min(len(text), match.end() + 5)
        fragment = text[start:end].strip()

        # Try to extract the entity name from the fragment
        # Look for a capitalized sequence ending with the suffix
        suffix = match.group(0)
        before = text[start:match.start()]

        # Find the start of the entity name (first capital letter in sequence)
        words = before.split()
        entity_words = []
        for word in reversed(words):
            clean = word.strip("(),;:\"'")
            if clean and (clean[0].isupper() or clean[0].isdigit()):
                entity_words.insert(0, clean)
            else:
                break

        if entity_words:
            name = " ".join(entity_words) + " " + suffix
            name = name.strip()
            if len(name) > 5:
                extraction.candidates.append(CandidateEntity(
                    name=name,
                    source_page=source_url,
                    confidence="low",
                ))


def _extract_jurisdiction_clues(text: str, extraction: WebsiteExtraction):
    """Extract location and jurisdiction clues from text."""
    # US state patterns
    us_states = [
        "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado",
        "Connecticut", "Delaware", "Florida", "Georgia", "Hawaii", "Idaho",
        "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana",
        "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota",
        "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada",
        "New Hampshire", "New Jersey", "New Mexico", "New York",
        "North Carolina", "North Dakota", "Ohio", "Oklahoma", "Oregon",
        "Pennsylvania", "Rhode Island", "South Carolina", "South Dakota",
        "Tennessee", "Texas", "Utah", "Vermont", "Virginia", "Washington",
        "West Virginia", "Wisconsin", "Wyoming",
    ]
    # Canadian provinces
    ca_provinces = [
        "Ontario", "Quebec", "British Columbia", "Alberta", "Manitoba",
        "Saskatchewan", "Nova Scotia", "New Brunswick",
        "Newfoundland", "Prince Edward Island",
    ]
    # UK
    uk_clues = ["London", "England", "United Kingdom", "UK", "Companies House"]
    # European
    eu_clues = ["Germany", "France", "Spain", "Luxembourg", "Netherlands", "Switzerland"]

    text_lower = text.lower()

    for state in us_states:
        if state.lower() in text_lower:
            extraction.jurisdiction_clues.append(f"US-{state}")

    for prov in ca_provinces:
        if prov.lower() in text_lower:
            extraction.jurisdiction_clues.append(f"CA-{prov}")

    for clue in uk_clues:
        if clue.lower() in text_lower:
            extraction.jurisdiction_clues.append("GB")
            break

    for clue in eu_clues:
        if clue.lower() in text_lower:
            extraction.jurisdiction_clues.append(f"EU-{clue}")

    # Deduplicate
    extraction.jurisdiction_clues = list(dict.fromkeys(extraction.jurisdiction_clues))
