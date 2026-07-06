"""
Python port of php/tests/LookupToolsTest.php.

Faithful 1:1 port of the 29 PHP assertions across 8 LookupTools methods. EVERY assertion
in the PHP suite is downstream of a live call (real HTTP to sec.gov / Companies House /
North Data, a real webpage fetch, or a `whois` subprocess) — there are NO pure
parsing/logic assertions in this file. So the whole module is marked `live` and skipped
unless RUN_LIVE is set in the environment.

PHP method  ->  Python method (snake_case):
    fetchWebpage                 -> fetch_webpage        (returns (text, meta) tuple)
    whoisLookup                  -> whois_lookup
    searchCompaniesHouse         -> search_companies_house
    companiesHouseOwnershipChain -> companies_house_ownership_chain
    searchSecCompany             -> search_sec_company
    searchSecFulltext            -> search_sec_fulltext
    fetchSecSubmissions          -> fetch_sec_submissions
    searchNorthdata              -> search_northdata
"""
import json
import os

import pytest

from config import load_config
from tools import LookupTools

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.environ.get("RUN_LIVE"), reason="live network"),
]


@pytest.fixture(scope="module")
def t():
    return LookupTools(load_config(), progress_callback=None)


# ── fetchWebpage ────────────────────────────────────────────────────────────
def test_fetch_webpage(t):
    # PHP: $text = $this->tools->fetchWebpage('https://www.kaincap.com/');
    # Python fetch_webpage returns (text, meta).
    text, _meta = t.fetch_webpage("https://www.kaincap.com/")
    assert len(text) > 200, f"Fetches kaincap.com homepage (got {len(text)} chars)"
    assert "kain" in text.lower(), 'Contains "kain" in text'
    assert not text.startswith("Error"), "No error prefix"


# ── whoisLookup ─────────────────────────────────────────────────────────────
def test_whois_lookup(t):
    result = t.whois_lookup("kaincap.com")
    assert len(result) > 50, f"Returns WHOIS data (got {len(result)} chars)"
    assert ("domain" in result.lower()) or ("registr" in result.lower()), \
        "Contains domain/registrar info"


# ── searchCompaniesHouse ────────────────────────────────────────────────────
def test_search_companies_house(t):
    result = t.search_companies_house("ABCA Systems")
    assert "ABCA" in result, "Finds ABCA in results"
    assert "find-and-update.company-information.service.gov.uk" in result, \
        "Contains Companies House URL"

    result2 = t.search_companies_house("xyznonexistentcompany12345")
    assert "No Companies House results" in result2, "Returns no results for gibberish query"


# ── companiesHouseOwnershipChain ────────────────────────────────────────────
def test_ownership_chain(t):
    # ABCA Systems Limited
    result = t.companies_house_ownership_chain("06294877")
    assert ("ABCA SYSTEMS LIMITED" in result) or ("Abca Systems" in result), \
        "Starts with ABCA Systems"
    assert ("Vulcan1 Topco" in result) or ("VULCAN1 TOPCO" in result), \
        "Reaches Vulcan1 Topco"
    assert ("STOP" in result) or ("TOP OF CHAIN" in result), "Chain terminates"
    assert ("Vulcan1 Jv Llp" not in result) or ("STOP" in result), \
        "Does not follow JV LLP beyond 50% (stops or reports stop)"

    # Greensleeves
    result2 = t.companies_house_ownership_chain("05107549")
    assert ("GREENSLEEVES" in result2) or ("Greensleeves" in result2), \
        "Starts with Greensleeves"
    assert ("Neighbourly" in result2) or ("NEIGHBOURLY" in result2), "Reaches Neighbourly"
    assert "TOP OF CHAIN" in result2, "Chain terminates at top"


# ── searchSecCompany ────────────────────────────────────────────────────────
def test_search_sec_company(t):
    result = t.search_sec_company("Level Equity")
    assert "CIK:" in result, "Finds CIK for Level Equity"
    assert "level" in result.lower(), 'Contains "level" in results'

    result2 = t.search_sec_company("xyznonexistent12345")
    assert "No SEC company results" in result2, "Returns no results for gibberish"


# ── searchSecFulltext ───────────────────────────────────────────────────────
def test_search_sec_fulltext(t):
    result = t.search_sec_fulltext("kkr.com")
    assert "Total hits:" in result, "Returns hit count"
    assert not result.startswith("Error"), "No error"


# ── fetchSecSubmissions ─────────────────────────────────────────────────────
def test_fetch_sec_submissions(t):
    # KKR CIK
    result = t.fetch_sec_submissions("0001404912")
    assert "KKR" in result, "Finds KKR name"
    assert "latest_filings" in result, "Contains filings"

    data = json.loads(result)
    assert isinstance(data, (dict, list)), "Returns valid JSON"  # PHP is_array()
    assert data.get("name"), "Has entity name"


# ── searchNorthdata ─────────────────────────────────────────────────────────
def test_search_northdata(t):
    result = t.search_northdata("Siemens AG")
    assert ("siemens" in result.lower()) or ("No North Data" in result), \
        "Searches for Siemens (may find results or not based on scraping)"
    assert not result.startswith("Error:"), "No hard error"
