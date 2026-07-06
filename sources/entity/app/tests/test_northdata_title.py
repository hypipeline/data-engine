r"""
Faithful Python port of php/tests/test_northdata_title.php.

Tests NorthData page-title → company-name extraction. In the Python port this
logic is inlined in NorthDataMixin.validate_northdata_entity (tools_northdata.py
lines ~791-799):

    title_name = page_title.split(':')[0]
    if registry_id:
        title_name = re.sub(r',?\s*' + re.escape(registry_id) + r'\s*$', '', title_name)
    if country_name:
        title_name = re.sub(r',?\s*' + re.escape(country_name) + r'\s*$', '', title_name, flags=re.I)
    title_name = re.sub(r',\s*[^,]+\s*$', '', title_name)
    return title_name.strip()

Like the PHP test (which re-implements this as extractNameFromTitle), this test
replicates the exact block as a local helper and asserts the same expected names.
Pure/offline.
"""
import re


def extract_name_from_title(page_title, registry_id, country_name):
    """Mirror of the inline title-parsing block in validate_northdata_entity."""
    title_name = page_title.split(":")[0]
    # Strip registry ID suffix (e.g. "PRH 2422742-9", "HRB 6469")
    if registry_id:
        title_name = re.sub(r",?\s*" + re.escape(registry_id) + r"\s*$", "", title_name)
    # Strip country name
    if country_name:
        title_name = re.sub(r",?\s*" + re.escape(country_name) + r"\s*$", "", title_name, flags=re.I)
    # Strip city (last comma-segment)
    title_name = re.sub(r",\s*[^,]+\s*$", "", title_name)
    return title_name.strip()


# 1. Standard Finnish company
def test_finnish_company_scanfil_oyj():
    result = extract_name_from_title(
        "Scanfil Oyj, Sievi, Finland, PRH 2422742-9: Network, Financial information",
        "PRH 2422742-9", "Finland",
    )
    assert result == "Scanfil Oyj"


# 2. German company with HRB registry ID
def test_german_company_siemens_ag():
    result = extract_name_from_title(
        "Siemens AG, München, Germany, HRB 6684: Network, Financial information",
        "HRB 6684", "Germany",
    )
    assert result == "Siemens AG"


# 3. Company name containing a comma (Nike, Inc.)
def test_company_with_comma_in_name_nike_inc():
    result = extract_name_from_title(
        "Nike, Inc., Beaverton, United States: Network, Financial information",
        "", "United States",
    )
    assert result == "Nike, Inc."


# 4. Dutch company with KVK registry
def test_dutch_company_shell():
    result = extract_name_from_title(
        "Shell International B.V., Den Haag, Netherlands, KVK 27155369: Network, Financial information",
        "KVK 27155369", "Netherlands",
    )
    assert result == "Shell International B.V."


# 5. Company with no registry ID in title
def test_no_registry_id_adidas_ag():
    result = extract_name_from_title(
        "adidas AG, Herzogenaurach, Germany: Network, Financial information",
        "", "Germany",
    )
    assert result == "adidas AG"


# 6. Estonian subsidiary (Scanfil OÜ) — unicode
def test_estonian_company_unicode_scanfil_ou():
    result = extract_name_from_title(
        "Scanfil OÜ, Pärnu, Estonia, RK 11348482: Network, Financial information",
        "RK 11348482", "Estonia",
    )
    assert result == "Scanfil OÜ"


# 7. Terminated company (same title format)
def test_terminated_company_scanfil_oy():
    result = extract_name_from_title(
        "Scanfil Oy, Helsinki, Finland, PRH 0830882-6: Network, Financial information",
        "PRH 0830882-6", "Finland",
    )
    assert result == "Scanfil Oy"


# 8. Polish company with Sp. z o.o. suffix
def test_polish_company_scanfil_poland():
    result = extract_name_from_title(
        "Scanfil Poland sp. z o.o., Mysłowice, Poland, KRS 0000071022: Network, Financial information",
        "KRS 0000071022", "Poland",
    )
    assert result == "Scanfil Poland sp. z o.o."


# 9. Italian company with S.r.l. suffix
def test_italian_company_hitech():
    result = extract_name_from_title(
        "Hi-Tech Elettronica S.r.l., Sala Bolognese, Italy, BO 305549: Network, Financial information",
        "BO 305549", "Italy",
    )
    assert result == "Hi-Tech Elettronica S.r.l."


# 10. Company name with multiple commas (edge case)
def test_company_with_multiple_commas():
    result = extract_name_from_title(
        "Smith, Jones & Partners, Ltd., London, United Kingdom, CH 12345678: Network, Financial information",
        "CH 12345678", "United Kingdom",
    )
    assert result == "Smith, Jones & Partners, Ltd."
