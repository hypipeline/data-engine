"""
Faithful pytest port of two Companies House PHP test suites:

  - php tests/test_ch_brand_search.php          -> class-of-tests "brand search"
  - php tests/test_ch_corporate_appointments.php -> class-of-tests "corporate appointments"

Every assertion here mirrors a `test(label, expected, actual)` call in the PHP
originals, asserting the SAME expected value.

All tests hit the live Companies House API / public web pages (there is no
fixed-input / pure-logic assertion in either PHP file), so the whole module is
marked `live` and skipped unless RUN_LIVE is set.
"""
import os

import pytest

from config import load_config
from tools import LookupTools

# Every test in this module makes real Companies House network calls.
pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.environ.get("RUN_LIVE"), reason="live network"),
]


@pytest.fixture(scope="module")
def tools():
    return LookupTools(load_config())


# ═══════════════════════════════════════════════════════════════════════════
#  test_ch_brand_search.php
# ═══════════════════════════════════════════════════════════════════════════

def test_ch_api_get_company_mornington(tools):
    # 1. CH API - Get Company (Mornington Partners #10258578)
    co = tools.companies_house_get_company("10258578")
    assert (co is not None) is True                                    # Returns data
    assert (co.get("company_name") or "") == "MORNINGTON PARTNERS LIMITED"  # Has company name
    assert bool(co.get("postal_code")) is True                        # Has postcode


def test_ch_api_get_officers_mornington(tools):
    # 2. CH API - Get Officers (Mornington Partners #10258578)
    officers = tools.companies_house_get_officers("10258578")
    assert (len(officers) > 0) is True                                # Has officers


def test_brand_search_inflexion_by_postcode(tools):
    # 3. Brand Search: 'Inflexion' with known postcode W1U 3AY
    matches = tools.companies_house_brand_search("Inflexion", ["W1U 3AY"], [], [])
    assert (len(matches) > 0) is True                                 # Has matches
    match_names = [m["company_name"].upper() for m in matches]
    # Contains INFLEXION LIMITED PARTNERSHIP
    assert ("INFLEXION LIMITED PARTNERSHIP" in match_names) is True


def test_brand_search_inflexion_officer_only(tools):
    # 4. Brand Search: 'Inflexion' with known officer HAZELL-SMITH (postcode won't match)
    matches2 = tools.companies_house_brand_search(
        "Inflexion", ["ZZ99 9ZZ"], ["HAZELL-SMITH"], []
    )
    assert isinstance(matches2, list) is True                         # Returns array


def test_brand_search_inflexion_no_match(tools):
    # 5. Brand Search: 'Inflexion' with unrelated postcode and officer -> empty
    matches3 = tools.companies_house_brand_search(
        "Inflexion", ["ZZ99 9ZZ"], ["ZZZZNONEXISTENT"], []
    )
    assert (len(matches3) == 0) is True                               # No matches


# ═══════════════════════════════════════════════════════════════════════════
#  test_ch_corporate_appointments.php
# ═══════════════════════════════════════════════════════════════════════════

def test_corporate_appointments_mornington(tools):
    # 1. MORNINGTON PARTNERS LIMITED — known corporate appointments
    results = tools.companies_house_corporate_appointments("MORNINGTON PARTNERS LIMITED")
    active_results = [a for a in results if a["status"] == "active"]

    assert (len(results) > 0) is True                                 # Has appointments
    assert (len(active_results) > 0) is True                          # Has active appointments

    names = [a["company_name"].upper() for a in results]
    assert ("GLOBAL HOLDCO LIMITED" in names) is True                 # Contains GLOBAL HOLDCO LIMITED
    assert ("THE BRIARS GROUP LIMITED" in names) is True              # Contains THE BRIARS GROUP LIMITED

    first = results[0] if results else {}
    assert bool(first.get("company_name")) is True                    # Has company_name
    assert bool(first.get("company_number")) is True                  # Has company_number
    assert bool(first.get("role")) is True                            # Has role
    assert (first.get("status") in ["active", "resigned"]) is True    # Has status


def test_corporate_appointments_nonexistent(tools):
    # 2. Nonexistent company -> empty
    results = tools.companies_house_corporate_appointments("ZZZYYYXXX NONEXISTENT CORP LTD")
    assert (len(results) == 0) is True                                # No appointments


def test_corporate_appointments_tesco_returns_array(tools):
    # 3. TESCO PLC — just check it returns an array (may or may not have appointments)
    results = tools.companies_house_corporate_appointments("TESCO PLC")
    assert isinstance(results, list) is True                          # Returns array
