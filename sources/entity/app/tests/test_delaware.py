"""
Faithful port of ../../tests/test_delaware.php.

Delaware Division of Corporations search (Browserbase-backed).

PHP method mapping:
  searchDelaware               -> search_delaware(name)
  lookupDelawareByFileNumber   -> lookup_delaware_by_file_number(file_number)

Every test drives Browserbase against the Delaware ICIS site, so all are gated
behind RUN_LIVE=1.
"""
import os

import pytest

from config import load_config
from tools import LookupTools


@pytest.fixture(scope="module")
def tools():
    return LookupTools(load_config())


live = pytest.mark.skipif(not os.environ.get("RUN_LIVE"), reason="live network")


# ══ Search ══════════════════════════════════════════════════════════════════

@pytest.mark.live
@live
def test_search_leeds_equity(tools):
    r = tools.search_delaware("LEEDS EQUITY ADVISORS")
    assert "No Delaware entities found" not in r        # Found results
    assert "leeds equity" in r.lower()                  # Contains LEEDS EQUITY
    assert "3094669" in r                               # Contains file number 3094669


@pytest.mark.live
@live
def test_search_alphabet(tools):
    r = tools.search_delaware("ALPHABET INC")
    assert "No Delaware entities found" not in r        # Found results
    assert "alphabet" in r.lower()                      # Contains ALPHABET


@pytest.mark.live
@live
def test_search_nonexistent(tools):
    r = tools.search_delaware("ZZZYYYXXX NONEXISTENT CORP")
    assert "No Delaware entities found" in r            # No results found


@pytest.mark.live
@live
def test_search_amazon(tools):
    r = tools.search_delaware("AMAZON")
    assert "No Delaware entities found" not in r        # Found results
    assert "amazon" in r.lower()                        # Contains AMAZON


@pytest.mark.live
@live
def test_search_nike(tools):
    r = tools.search_delaware("NIKE")
    assert "No Delaware entities found" not in r        # Found results
    assert "nike" in r.lower()                          # Contains NIKE


# ══ File Number Lookup (Validation) ═════════════════════════════════════════

@pytest.mark.live
@live
def test_lookup_by_file_number(tools):
    r = tools.lookup_delaware_by_file_number("3094669")
    assert r is not None                                            # Has result
    assert r.get("name") == "LEEDS EQUITY ADVISORS, LLC"           # Name matches
    assert r.get("file_number") == "3094669"                       # File number matches
    assert r.get("status")                                         # Has status


@pytest.mark.live
@live
def test_lookup_nonexistent_file_number(tools):
    r = tools.lookup_delaware_by_file_number("9999999999")
    assert r is None                                               # No result
