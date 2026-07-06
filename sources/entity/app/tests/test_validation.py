"""
Faithful pytest port of php tests/test_validation.php.

Exercises the three registry validation lookups directly on LookupTools, exactly
like the PHP original:

  lookupBizapediaByFileNumber  -> lookup_bizapedia_by_file_number
  lookupCompaniesHouseByNumber -> lookup_companies_house_by_number
  validateNorthdataEntity      -> validate_northdata_entity
  searchNorthdata              -> search_northdata

The PHP file contains no fixed-input routing / status-derivation logic — it calls
each registry tool against a real, known entity and asserts on the live response.
Every assertion therefore requires a real network call (Bizapedia REST, Companies
House public web, NorthData / Browserbase), so the whole module is marked `live`
and skipped unless RUN_LIVE is set.
"""
import os

import pytest

from config import load_config
from tools import LookupTools

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.environ.get("RUN_LIVE"), reason="live network"),
]


@pytest.fixture(scope="module")
def tools():
    return LookupTools(load_config())


# ═══════════════════════════════════════════════════════════════════════════
#  1. Bizapedia — lookup_bizapedia_by_file_number
# ═══════════════════════════════════════════════════════════════════════════

def test_bizapedia_apple_by_file_number(tools):
    # Known entity: Apple Inc, California, file number 806592
    biz = tools.lookup_bizapedia_by_file_number("806592", "CA")
    assert (biz is not None) is True                                  # Returns result
    assert bool(biz.get("EntityName")) is True                        # Has EntityName
    assert ("APPLE" in (biz.get("EntityName") or "").upper()) is True # Name contains Apple
    assert bool(biz.get("FilingStatus")) is True                      # Has FilingStatus
    assert ((biz.get("FilingStatus") or "").lower() == "active") is True  # Status is Active


def test_bizapedia_bad_file_number(tools):
    # Bizapedia — non-existent file number
    biz_bad = tools.lookup_bizapedia_by_file_number("XXXXXXXXX", "CA")
    assert (biz_bad is None) is True                                  # Returns null for bad file number


# ═══════════════════════════════════════════════════════════════════════════
#  2. Companies House — lookup_companies_house_by_number
# ═══════════════════════════════════════════════════════════════════════════

def test_ch_vodafone_by_number(tools):
    # Known entity: Vodafone Group Plc, company number 01833679
    ch = tools.lookup_companies_house_by_number("01833679")
    assert (ch is not None) is True                                   # Returns result
    assert bool(ch.get("company_name")) is True                       # Has company_name
    assert ("VODAFONE" in (ch.get("company_name") or "").upper()) is True  # Name contains VODAFONE
    assert bool(ch.get("company_status")) is True                     # Has company_status
    assert ((ch.get("company_status") or "").lower() == "active") is True  # Status is active


def test_ch_bad_number(tools):
    # Companies House — non-existent number
    ch_bad = tools.lookup_companies_house_by_number("99999999")
    assert (ch_bad is None) is True                                   # Returns null for bad company number


# ═══════════════════════════════════════════════════════════════════════════
#  3. NorthData — validate_northdata_entity
# ═══════════════════════════════════════════════════════════════════════════

def test_northdata_siemens(tools):
    # 3a: German entity — Siemens AG, HRB 6684
    nd = tools.validate_northdata_entity("Siemens AG", "HRB 6684", "DE")
    assert (nd is not None) is True                                   # Returns result
    assert ("siemens" in (nd.get("name") or "").lower()) is True      # Name contains Siemens
    assert (nd.get("country_match") is True)                          # Country match
    assert (nd.get("registry_id_match") is True)                      # Registry ID found on page
    assert ((nd.get("status") or "").lower() == "active") is True     # Status is active


def test_northdata_iberveg(tools):
    # 3b: Spanish entity — Iberveg Spain SL, B63437917
    nd2 = tools.validate_northdata_entity("Iberveg Spain SL", "B63437917", "ES")
    assert (nd2 is not None) is True                                  # Returns result
    assert ("iberveg" in (nd2.get("name") or "").lower()) is True     # Name contains Iberveg
    assert (nd2.get("country_match") is True)                         # Country match
    assert (nd2.get("registry_id_match") is True)                     # Registry ID found on page


def test_northdata_siemens_wrong_country(tools):
    # 3c: Valid name, wrong country — registry ID should not match
    nd_wrong = tools.validate_northdata_entity("Siemens AG", "HRB 6684", "ES")
    assert (nd_wrong is not None) is True                             # Returns result
    assert (nd_wrong.get("registry_id_match") is False)              # Registry ID match is false


def test_northdata_nonexistent(tools):
    # 3d: Non-existent entity
    nd_bad = tools.validate_northdata_entity("Xyzzy Totally Fake Corp", "XYZ 000000", "DE")
    if nd_bad is None:
        assert True                                                  # Returns null for unknown entity
    else:
        # Registry ID match is false for fake entity
        assert (nd_bad.get("registry_id_match") is False)


# ═══════════════════════════════════════════════════════════════════════════
#  4. Bizapedia — Branch (Foreign) Detection
# ═══════════════════════════════════════════════════════════════════════════

def test_bizapedia_radian_foreign_branch(tools):
    # RADIAN CAPITAL LLC — Foreign LLC in NY, domestic jurisdiction Delaware
    biz_branch = tools.lookup_bizapedia_by_file_number("6001112", "NY")
    assert (biz_branch is not None) is True                          # Returns result
    assert bool(biz_branch.get("EntityName")) is True                # Has EntityName
    assert ("RADIAN" in (biz_branch.get("EntityName") or "").upper()) is True  # Name contains RADIAN
    branch_type = (biz_branch.get("EntityType") or "").upper()
    assert ("FOREIGN" in branch_type) is True                        # EntityType contains FOREIGN
    # Domestic jurisdiction is DE
    assert ((biz_branch.get("DomesticJurisdictionPostalAbbreviation") or "") == "DE") is True


# ═══════════════════════════════════════════════════════════════════════════
#  5. NorthData — search_northdata
# ═══════════════════════════════════════════════════════════════════════════

def test_northdata_search_siemens(tools):
    # 5a: German entity search
    nd_search1 = tools.search_northdata("Siemens AG")
    assert ("No North Data results" not in nd_search1) is True        # Returns results
    assert ("siemens" in nd_search1.lower()) is True                  # Contains Siemens


def test_northdata_search_scanfil(tools):
    # 5b: Finnish entity search
    nd_search2 = tools.search_northdata("Scanfil Oyj")
    assert ("No North Data results" not in nd_search2) is True        # Returns results
    assert ("scanfil" in nd_search2.lower()) is True                  # Contains Scanfil


def test_northdata_validate_scanfil(tools):
    # 5c: Finnish entity validation — Scanfil Oyj (parent, active)
    nd_fi = tools.validate_northdata_entity("Scanfil Oyj", "2422742-9", "FI")
    assert (nd_fi is not None) is True                                # Returns result
    assert ("scanfil" in (nd_fi.get("name") or "").lower()) is True   # Name contains Scanfil
    assert ("oyj" in (nd_fi.get("name") or "").lower()) is True       # Name contains Oyj
    assert (nd_fi.get("country_match") is True)                       # Country match
    assert (nd_fi.get("registry_id_match") is True)                   # Registry ID match
    assert ((nd_fi.get("status") or "").lower() == "active") is True  # Status is active
