"""
Faithful port of php/../tests/test_regex.php.

Tests EntityLookup.extract_candidate_names (PHP extractCandidateNames) and
EntityLookup.deduplicate_names (PHP deduplicateNames). Pure-logic, no network.

Each PHP assertion is ported with its identical expected value. Helper semantics:
  assertContains    -> some result item contains `expected` (case-insensitive substring)
  assertNotContains -> no result item contains `expected` (case-insensitive substring)
  assertEmpty       -> result is empty
matching PHP's stripos()-based checks.
"""
import pytest

from config import load_config
from agent import EntityLookup


@pytest.fixture(scope="module")
def lookup():
    return EntityLookup(load_config(), progress_callback=None)


# ── PHP helper equivalents ──────────────────────────────────────────────────

def _contains(result, expected):
    """PHP assertContains: any(stripos($name, $expected) !== false)."""
    el = expected.lower()
    return any(el in str(name).lower() for name in result)


def assert_contains(result, expected):
    assert _contains(result, expected), \
        f"'{expected}' NOT found in {result!r}"


def assert_not_contains(result, expected):
    assert not _contains(result, expected), \
        f"'{expected}' should NOT be present in {result!r}"


def assert_empty(result):
    assert not result, f"expected no candidates, got {result!r}"


# ══ Copyright notices ═══════════════════════════════════════════════════════

def test_copyright_simple_llc(lookup):
    r = lookup.extract_candidate_names("© 2024 Google LLC. All rights reserved.")
    assert_contains(r, 'Google LLC')


def test_copyright_with_ltd(lookup):
    r = lookup.extract_candidate_names("Copyright 2023 Acme Trading Ltd. All rights reserved.")
    assert_contains(r, 'Acme Trading Ltd')


def test_copyright_with_gmbh(lookup):
    # PHP label "Copyright with GmbH" but input is adidas AG
    r = lookup.extract_candidate_names("© 2024 adidas AG. Alle Rechte vorbehalten.")
    assert_contains(r, 'adidas AG')


def test_copyright_with_inc(lookup):
    r = lookup.extract_candidate_names("© 2023-2024 Nike, Inc. All rights reserved.")
    assert_contains(r, 'Nike, Inc')


def test_copyright_long_trailing_text(lookup):
    r = lookup.extract_candidate_names(
        "© 2024 Google LLC — Help Centre, Safety Centre, Transparency Centre, "
        "and other pages accessible from our policies site")
    assert_contains(r, 'Google LLC')
    assert_not_contains(r, 'Help Centre')
    assert_not_contains(r, 'Transparency Centre')


def test_copyright_c_format(lookup):
    r = lookup.extract_candidate_names("(c) 2024 Microsoft Corporation")
    assert_contains(r, 'Microsoft Corporation')


# ══ Sentences ending in common words (false positives) ══════════════════════

def test_sentence_ending_as_1(lookup):
    r = lookup.extract_candidate_names(
        "We collect information about the apps, browsers and devices that you use")
    assert_not_contains(r, 'information')
    assert_not_contains(r, 'devices')


def test_sentence_ending_as_2(lookup):
    r = lookup.extract_candidate_names(
        "We want you to understand the types of information we collect as")
    assert_not_contains(r, 'information we collect as')


def test_sentence_ending_as_3(lookup):
    r = lookup.extract_candidate_names(
        "Some Google services have additional age requirements as")
    assert_not_contains(r, 'age requirements as')


def test_sentence_ending_as_4(lookup):
    r = lookup.extract_candidate_names(
        "Information about things near your device, such as")
    assert_not_contains(r, 'such as')


def test_sentence_company_generic(lookup):
    r = lookup.extract_candidate_names(
        "Our company is committed to providing excellent service")
    assert_not_contains(r, 'Our company')


def test_sentence_ending_se(lookup):
    r = lookup.extract_candidate_names(
        "The permission that we give to you to access and use")
    assert_not_contains(r, 'permission')


# ══ Legitimate entity names in text ═════════════════════════════════════════

def test_entity_at_start_of_line(lookup):
    r = lookup.extract_candidate_names("Amazon Web Services LLC provides cloud computing.")
    assert_contains(r, 'Amazon Web Services LLC')


def test_operated_by_pattern(lookup):
    r = lookup.extract_candidate_names(
        "This site is operated by Acme Holdings Ltd on behalf of its subsidiaries.")
    assert_contains(r, 'Acme Holdings Ltd')


def test_managed_by_pattern(lookup):
    r = lookup.extract_candidate_names(
        "The fund is managed by BlackRock Investment Management (UK) Limited")
    assert_contains(r, 'BlackRock Investment Management (UK) Limited')


def test_german_entity(lookup):
    r = lookup.extract_candidate_names("Betrieben von Siemens AG, München")
    assert_contains(r, 'Siemens AG')


def test_dutch_entity(lookup):
    r = lookup.extract_candidate_names("Shell International B.V. is registered in The Hague")
    assert_contains(r, 'Shell International B.V')


def test_nordic_entity_as(lookup):
    r = lookup.extract_candidate_names("Novo Nordisk A/S is a Danish pharmaceutical company")
    assert_contains(r, 'Novo Nordisk A/S')


def test_french_entity_sas(lookup):
    r = lookup.extract_candidate_names("Dior Couture SAS operates luxury retail stores")
    assert_contains(r, 'Dior Couture SAS')


def test_plc(lookup):
    r = lookup.extract_candidate_names("Barclays PLC announced results today")
    assert_contains(r, 'Barclays PLC')


def test_llp(lookup):
    r = lookup.extract_candidate_names("Clifford Chance LLP is a law firm")
    assert_contains(r, 'Clifford Chance LLP')


def test_norwegian_as(lookup):
    r = lookup.extract_candidate_names("Equinor AS is headquartered in Stavanger")
    assert_contains(r, 'Equinor AS')


# ══ Herculite false positives (sentence preamble before suffix) ═════════════

def test_usa_should_not_match_sa(lookup):
    r = lookup.extract_candidate_names(
        "All of our products are developed, produced, and checked for quality "
        "right here in the U.S.A")
    assert_empty(r)


def test_long_sentence_ending_inc(lookup):
    r = lookup.extract_candidate_names(
        "You have the right at any time to stop Herculite Products Inc")
    assert_contains(r, 'Herculite Products Inc')
    assert_not_contains(r, 'You have the right')


def test_privacy_policy_sentence_ending_inc(lookup):
    r = lookup.extract_candidate_names(
        "This privacy policy will explain how Herculite Products Inc")
    assert_contains(r, 'Herculite Products Inc')
    assert_not_contains(r, 'This privacy policy')


def test_legitimate_herculite_entities(lookup):
    r = lookup.extract_candidate_names(
        "Herculite Products Inc. is a leading manufacturer.\n"
        "Herculite, Inc. was founded in 1955.")
    assert_contains(r, 'Herculite Products Inc')
    assert_contains(r, 'Herculite, Inc')


def test_privacy_policy_preamble(lookup):
    r = lookup.extract_candidate_names(
        "This privacy policy will explain how Herculite Products Inc")
    assert_contains(r, 'Herculite Products Inc')
    assert_not_contains(r, 'policy will explain')


def test_right_to_request_preamble(lookup):
    r = lookup.extract_candidate_names(
        "You have the right to request that Herculite Products Inc")
    assert_contains(r, 'Herculite Products Inc')
    assert_not_contains(r, 'right to request')


def test_any_time_to_stop_preamble(lookup):
    r = lookup.extract_candidate_names(
        "You have the right at any time to stop Herculite Products Inc")
    assert_contains(r, 'Herculite Products Inc')
    assert_not_contains(r, 'any time to stop')


# ══ Google page text (real-world false positives) ═══════════════════════════

def test_google_privacy_multiple_as(lookup):
    r = lookup.extract_candidate_names(
        "We want you to understand the types of information we collect as\n"
        "Some Google services have additional age requirements as\n"
        "Information about things near your device, such as\n"
        "The permission that we give you to access and use\n"
        "© 2024 Google LLC. All rights reserved.")
    assert_contains(r, 'Google LLC')
    assert_not_contains(r, 'information we collect')
    assert_not_contains(r, 'age requirements')
    assert_not_contains(r, 'such as')
    assert_not_contains(r, 'access and use')


def test_help_lp_should_not_match(lookup):
    r = lookup.extract_candidate_names("Help\nLP records available\nHelp Lp")
    assert_not_contains(r, 'Help')


def test_legitimate_lp_entity(lookup):
    r = lookup.extract_candidate_names("Blackstone Capital Partners LP is an investment fund")
    assert_contains(r, 'Blackstone Capital Partners LP')


# ══ Edge cases ══════════════════════════════════════════════════════════════

def test_multiple_entities_one_line(lookup):
    r = lookup.extract_candidate_names(
        "Services provided by Acme Corp. and its subsidiary Acme UK Limited.")
    assert_contains(r, 'Acme Corp')
    assert_contains(r, 'Acme UK Limited')


def test_empty_text(lookup):
    r = lookup.extract_candidate_names("")
    assert_empty(r)


def test_no_entity_names(lookup):
    r = lookup.extract_candidate_names(
        "Welcome to our website. We sell shoes and clothing worldwide.")
    assert_empty(r)


# ══ Name deduplication ══════════════════════════════════════════════════════

def test_dedup_herculite_punctuation_variants(lookup):
    names = ["Herculite Products Inc.", "Herculite, Inc.", "Herculite Products Inc",
             "Herculite, Inc", "Herculite"]
    deduped = lookup.deduplicate_names(names)
    assert_contains(deduped, 'Herculite Products Inc.')
    assert_contains(deduped, 'Herculite, Inc.')
    assert_contains(deduped, 'Herculite')
    assert len(deduped) == 3, f"expected 3 names, got {len(deduped)}: {deduped!r}"


def test_dedup_google(lookup):
    names = ["Google LLC", "Alphabet Inc.", "Alphabet Inc.", "Google"]
    deduped = lookup.deduplicate_names(names)
    assert_contains(deduped, 'Google LLC')
    assert_contains(deduped, 'Alphabet Inc.')
    assert_contains(deduped, 'Google')
    assert len(deduped) == 3, f"expected 3 names, got {len(deduped)}: {deduped!r}"


def test_dedup_suffix_variants(lookup):
    names = ["Acme Inc.", "Acme Incorporated", "Acme Inc"]
    deduped = lookup.deduplicate_names(names)
    assert len(deduped) == 1, f"expected 1 name, got {len(deduped)}: {deduped!r}"


def test_dedup_uk_asda_vs_asda(lookup):
    names = ["UK ASDA Stores Limited", "ASDA Stores Limited"]
    deduped = lookup.deduplicate_names(names)
    assert len(deduped) == 2, f"expected 2 names, got {len(deduped)}: {deduped!r}"


def test_dedup_bv_vs_bv(lookup):
    names = ["Shell International B.V.", "Shell International BV"]
    deduped = lookup.deduplicate_names(names)
    assert len(deduped) == 1, f"expected 1 name, got {len(deduped)}: {deduped!r}"
