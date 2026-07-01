"""Registry map: jurisdiction → scraper class."""

from __future__ import annotations
from .base import BaseScraper
from .sec_edgar import SECEdgarScraper
from .delaware_dos import DelawareDOSScraper
from .uk_companies_house import UKCompaniesHouseScraper
from .ontario_obr import OntarioOBRScraper
from .northdata import NorthDataScraper


# Jurisdiction code → scraper class
REGISTRY_MAP: dict[str, type[BaseScraper]] = {
    "US": SECEdgarScraper,
    "US-DE": DelawareDOSScraper,
    "CA-ON": OntarioOBRScraper,
    "GB": UKCompaniesHouseScraper,
    "EU": NorthDataScraper,
}

# North Data covers these jurisdictions
NORTHDATA_JURISDICTIONS = NorthDataScraper.COVERED_COUNTRIES

# Common aliases
JURISDICTION_ALIASES: dict[str, str] = {
    "delaware": "US-DE",
    "de": "US-DE",
    "ontario": "CA-ON",
    "on": "CA-ON",
    "canada": "CA-ON",  # default to Ontario for now
    "uk": "GB",
    "united kingdom": "GB",
    "england": "GB",
    "scotland": "GB",
    "wales": "GB",
    "us": "US",
    "usa": "US",
    "united states": "US",
    "new york": "US",  # TODO: add NY DOS scraper
    "ny": "US",
    # European → North Data
    "germany": "EU", "de": "EU", "france": "EU", "fr": "EU",
    "spain": "EU", "es": "EU", "italy": "EU", "it": "EU",
    "netherlands": "EU", "nl": "EU", "belgium": "EU", "be": "EU",
    "switzerland": "EU", "ch": "EU", "luxembourg": "EU", "lu": "EU",
    "ireland": "EU", "ie": "EU", "austria": "EU", "at": "EU",
    "denmark": "EU", "dk": "EU", "sweden": "EU", "se": "EU",
    "norway": "EU", "no": "EU", "finland": "EU", "fi": "EU",
    "poland": "EU", "pl": "EU", "portugal": "EU", "pt": "EU",
    "romania": "EU", "ro": "EU", "greece": "EU", "gr": "EU",
    "europe": "EU", "eu": "EU",
}


def get_scraper(jurisdiction: str) -> type[BaseScraper] | None:
    """Look up a scraper class by jurisdiction code or alias."""
    key = jurisdiction.strip().upper()
    if key in REGISTRY_MAP:
        return REGISTRY_MAP[key]

    alias_key = jurisdiction.strip().lower()
    if alias_key in JURISDICTION_ALIASES:
        mapped = JURISDICTION_ALIASES[alias_key]
        return REGISTRY_MAP.get(mapped)

    return None


def list_jurisdictions() -> list[str]:
    """Return all supported jurisdiction codes."""
    return list(REGISTRY_MAP.keys())
