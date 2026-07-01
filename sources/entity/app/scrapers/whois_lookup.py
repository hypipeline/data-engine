"""WHOIS domain lookup. Extracts registrant details — no browser, no API key needed."""

from __future__ import annotations
import asyncio
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse


@dataclass
class WhoisResult:
    """Structured WHOIS data for a domain."""
    domain: str
    registrant_name: str | None = None
    registrant_org: str | None = None
    registrant_street: str | None = None
    registrant_city: str | None = None
    registrant_state: str | None = None
    registrant_country: str | None = None
    registrant_email: str | None = None
    creation_date: str | None = None
    expiry_date: str | None = None
    registrar: str | None = None
    name_servers: list[str] = field(default_factory=list)
    raw_text: str = ""


async def whois_lookup(url_or_domain: str) -> WhoisResult:
    """Run WHOIS on a domain and parse the result."""
    domain = _extract_domain(url_or_domain)
    result = WhoisResult(domain=domain)

    # Step 1: Get registrar WHOIS server from Verisign
    tld = domain.rsplit(".", 1)[-1]
    tld_whois = _tld_whois_server(tld)

    raw = await _run_whois(tld_whois, domain)
    registrar_server = _extract_field(raw, "Registrar WHOIS Server")

    # Step 2: Query registrar for full details
    if registrar_server:
        raw = await _run_whois(registrar_server, domain)

    result.raw_text = raw
    _parse_whois(raw, result)
    return result


def _extract_domain(url_or_domain: str) -> str:
    """Extract bare domain from a URL or domain string."""
    if "://" in url_or_domain:
        parsed = urlparse(url_or_domain)
        domain = parsed.netloc
    else:
        domain = url_or_domain
    return domain.lower().removeprefix("www.")


def _tld_whois_server(tld: str) -> str:
    """Map TLD to its WHOIS server."""
    servers = {
        "com": "whois.verisign-grs.com",
        "net": "whois.verisign-grs.com",
        "org": "whois.pir.org",
        "io": "whois.nic.io",
        "co": "whois.nic.co",
        "us": "whois.nic.us",
        "uk": "whois.nic.uk",
        "de": "whois.denic.de",
        "fr": "whois.nic.fr",
        "ca": "whois.cira.ca",
        "au": "whois.auda.org.au",
    }
    return servers.get(tld, f"whois.nic.{tld}")


async def _run_whois(server: str, domain: str) -> str:
    """Execute whois command against a specific server."""
    proc = await asyncio.create_subprocess_exec(
        "whois", "-h", server, domain,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    except asyncio.TimeoutError:
        proc.kill()
        return ""
    return stdout.decode("utf-8", errors="replace")


def _extract_field(text: str, field_name: str) -> str | None:
    """Extract a single field value from WHOIS text."""
    pattern = re.compile(rf"^\s*{re.escape(field_name)}:\s*(.+)$", re.MULTILINE | re.IGNORECASE)
    match = pattern.search(text)
    return match.group(1).strip() if match else None


def _parse_whois(text: str, result: WhoisResult):
    """Parse WHOIS text into structured fields."""
    field_map = {
        "Registrant Name": "registrant_name",
        "Registrant Organization": "registrant_org",
        "Registrant Street": "registrant_street",
        "Registrant City": "registrant_city",
        "Registrant State/Province": "registrant_state",
        "Registrant Country": "registrant_country",
        "Registrant Email": "registrant_email",
        "Creation Date": "creation_date",
        "Registrar Registration Expiration Date": "expiry_date",
        "Registry Expiry Date": "expiry_date",
        "Registrar": "registrar",
    }

    for whois_field, attr in field_map.items():
        value = _extract_field(text, whois_field)
        if value and value.strip() and not value.strip().startswith("Registrant"):
            setattr(result, attr, value.strip())

    # Name servers
    for match in re.finditer(r"^\s*Name Server:\s*(.+)$", text, re.MULTILINE | re.IGNORECASE):
        ns = match.group(1).strip().lower()
        if ns and ns not in result.name_servers:
            result.name_servers.append(ns)


def whois_to_jurisdiction_clues(result: WhoisResult) -> list[str]:
    """Convert WHOIS data into jurisdiction clues."""
    clues = []
    if result.registrant_state and result.registrant_country == "US":
        clues.append(f"US-{result.registrant_state}")
    if result.registrant_country and not _is_privacy_proxy(result.registrant_country):
        country = result.registrant_country.upper()
        if country in ("US", "GB", "CA", "DE", "FR"):
            clues.append(country)
    if result.registrant_city and not _is_privacy_proxy(result.registrant_city):
        clues.append(result.registrant_city)

    # Infer jurisdiction from corporate suffix in org name
    if result.registrant_org:
        suffix_jurisdiction = _suffix_to_jurisdiction(result.registrant_org)
        if suffix_jurisdiction and suffix_jurisdiction not in clues:
            clues.append(suffix_jurisdiction)

    return clues


# Map corporate suffixes in entity names to likely jurisdictions
_SUFFIX_JURISDICTION_MAP = {
    "S.A.": "EU",  # Spain, France, Luxembourg, etc.
    "SA": "EU",
    "S.L.": "EU",  # Spain
    "SL": "EU",
    "GmbH": "EU",  # Germany, Austria, Switzerland
    "AG": "EU",  # Germany, Austria, Switzerland
    "B.V.": "EU",  # Netherlands
    "BV": "EU",
    "N.V.": "EU",  # Netherlands, Belgium
    "NV": "EU",
    "S.à r.l.": "EU",  # Luxembourg
    "Sarl": "EU",
    "SAS": "EU",  # France
    "S.p.A.": "EU",  # Italy
    "SpA": "EU",
    "ApS": "EU",  # Denmark
    "AB": "EU",  # Sweden
    "AS": "EU",  # Norway
    "Oy": "EU",  # Finland
    "Ltd": "GB",  # UK (or other, but GB is most common)
    "Ltd.": "GB",
    "Limited": "GB",
    "PLC": "GB",
    "Plc": "GB",
    "LLP": "GB",
    "Inc.": "US",
    "Inc": "US",
    "LLC": "US",
    "Corp.": "US",
    "Corp": "US",
    "Corporation": "US",
    "L.P.": "US",
    "LP": "US",
}


def _suffix_to_jurisdiction(org_name: str) -> str | None:
    """Detect jurisdiction from corporate suffix in an org name."""
    # Check longest suffixes first to avoid false matches (e.g. "S.A." before "A")
    for suffix, jurisdiction in sorted(_SUFFIX_JURISDICTION_MAP.items(), key=lambda x: -len(x[0])):
        # Match suffix at end of name, or before parenthetical
        if org_name.rstrip(")").rstrip().endswith(suffix):
            return jurisdiction
        # Also check with trailing punctuation stripped
        clean = org_name.rstrip(".,;:) ")
        if clean.endswith(suffix):
            return jurisdiction
    return None


_PRIVACY_INDICATORS = [
    "privacy", "proxy", "redacted", "withheld", "protected", "private",
    "domains by proxy", "whoisguard", "contact privacy", "identity protect",
    "domain protection", "registration private", "data protected",
    "anonymised", "anonymized",
]


def _is_privacy_proxy(name: str) -> bool:
    """Check if a name is a WHOIS privacy proxy or redacted placeholder."""
    lower = name.lower()
    return any(indicator in lower for indicator in _PRIVACY_INDICATORS)


def whois_to_candidate_names(result: WhoisResult) -> list[str]:
    """Extract candidate entity names from WHOIS data."""
    candidates = []
    if result.registrant_org and not _is_privacy_proxy(result.registrant_org):
        org = result.registrant_org
        candidates.append(org)
        # Extract parenthetical aliases: "Foo Corp (BAR INC)" → also add "BAR INC"
        import re
        paren_match = re.search(r'\(([^)]+)\)', org)
        if paren_match:
            alias = paren_match.group(1).strip()
            if len(alias) > 3 and not _is_privacy_proxy(alias):
                candidates.append(alias)
            # Also add the name without the parenthetical
            base_name = org[:paren_match.start()].strip().rstrip(",")
            if len(base_name) > 3:
                candidates.insert(0, base_name)
    if result.registrant_name and not _is_privacy_proxy(result.registrant_name):
        candidates.append(result.registrant_name)
    # Check email domain — might differ from website domain
    if result.registrant_email and "@" in result.registrant_email:
        email_domain = result.registrant_email.split("@")[1]
        if email_domain != result.domain and not _is_privacy_proxy(email_domain):
            candidates.append(email_domain)
    return candidates
