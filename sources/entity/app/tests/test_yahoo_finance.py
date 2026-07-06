"""
Faithful port of ../../tests/test_yahoo_finance.php.

Google Intelligence + Yahoo Finance + LinkedIn integration.

PHP method mapping:
  googleIntelligence   -> google_intelligence(domain)
  yahooFinanceData     -> yahoo_finance_data(ticker)
  fetchLinkedInCompany -> fetch_linkedin_company(url)
  yahooFormatVal       -> _yahoo_format_val(field)   (PHP used reflection on the private)

The value-formatting block (PHP section 4) is PURE (no network) and runs by default.
Everything else hits Bright Data / Yahoo and is gated behind RUN_LIVE=1.
"""
import os

import pytest

from config import load_config
from tools import LookupTools


@pytest.fixture(scope="module")
def tools():
    return LookupTools(load_config())


live = pytest.mark.skipif(not os.environ.get("RUN_LIVE"), reason="live network")


# ══ 1. Google Intelligence (batch SERP) ═════════════════════════════════════

@pytest.mark.live
@live
def test_google_intelligence(tools):
    intel = tools.google_intelligence("franchisebrands.co.uk")
    assert isinstance(intel, dict)                          # Returns array
    assert "google_results" in intel                        # Has google_results key
    assert "yahoo_ticker" in intel                          # Has yahoo_ticker key
    assert "linkedin_url" in intel                          # Has linkedin_url key
    assert len(intel["google_results"]) > 50                # Google results not empty
    assert intel["yahoo_ticker"] == "FRAN.L"                # Yahoo ticker is FRAN.L
    assert intel["linkedin_url"] is not None                # LinkedIn URL found
    assert "/company/" in (intel["linkedin_url"] or "")     # LinkedIn URL contains /company/


# ══ 2. Yahoo Finance data fetch (FRAN.L) ════════════════════════════════════

@pytest.mark.live
@live
def test_yahoo_finance_data(tools):
    data = tools.yahoo_finance_data("FRAN.L")
    assert len(data) > 100                                  # FRAN.L data not empty
    assert "Company Profile" in data                        # Contains company profile
    assert "Income Statement" in data                       # Contains Income Statement
    assert "Revenue" in data                                # Contains Revenue
    assert "finance.yahoo.com/quote/FRAN.L" in data         # Contains source link
    assert "Sector:" in data                                # Contains sector


# ══ 3. LinkedIn company data fetch ══════════════════════════════════════════

@pytest.mark.live
@live
def test_fetch_linkedin_company(tools):
    intel = tools.google_intelligence("franchisebrands.co.uk")
    if not intel["linkedin_url"]:
        pytest.skip("No LinkedIn URL found")
    linkedin = tools.fetch_linkedin_company(intel["linkedin_url"])
    assert len(linkedin) > 50                               # LinkedIn data not empty
    assert "Name:" in linkedin                              # Contains company name
    assert "LinkedIn Company Profile" in linkedin           # Contains header
    assert "Address:" in linkedin                           # Contains address


# ══ 4. Value formatting (PURE — no network) ═════════════════════════════════

def test_yahoo_format_trillions(tools):
    assert tools._yahoo_format_val({"raw": 50684952000000}) == "50.7T"


def test_yahoo_format_billions(tools):
    assert tools._yahoo_format_val({"raw": 1500000000}) == "1.5B"


def test_yahoo_format_millions(tools):
    assert tools._yahoo_format_val({"raw": 89460000}) == "89.5M"


def test_yahoo_format_thousands(tools):
    assert tools._yahoo_format_val({"raw": 5000}) == "5.0K"


def test_yahoo_format_negative(tools):
    assert tools._yahoo_format_val({"raw": -1500000000}) == "-1.5B"


def test_yahoo_format_null(tools):
    assert tools._yahoo_format_val({}) == "—"
