"""
Faithful Python port of php/tests/test_confidence_downgrade.php.

Tests the confidence auto-downgrade logic that runs after a failed Phase 8
re-validation. In the Python port this lives inline in EntityLookup.run()
(agent.py lines ~181-185):

    rv_status = (report.get('registry_validation') or {}).get('status')
    if needs_reanalysis and rv_status and rv_status != 'verified':
        report['confidence'] = 'low'
        report['validation_warning'] = 'Registry validation failed after re-analysis — confidence auto-downgraded'

Like the PHP test (which re-implements the block as applyDowngrade), this test
replicates that exact block as a local helper and drives the same 8 cases. Pure/offline.
"""

WARNING_MSG = "Registry validation failed after re-analysis — confidence auto-downgraded"


def apply_downgrade(needs_reanalysis, rv_status, original_confidence):
    """Mirror of the inline downgrade block in EntityLookup.run()."""
    report = {
        "confidence": original_confidence,
        "registry_validation": {"status": rv_status} if rv_status else None,
    }

    rv_status_check = (report.get("registry_validation") or {}).get("status")
    if needs_reanalysis and rv_status_check and rv_status_check != "verified":
        report["confidence"] = "low"
        report["validation_warning"] = WARNING_MSG

    return report


# 1. Re-analysis + name_mismatch → downgrade to low
def test_reanalysis_name_mismatch_downgrades():
    r = apply_downgrade(True, "name_mismatch", "high")
    assert r["confidence"] == "low"
    assert "validation_warning" in r


# 2. Re-analysis + name_match_bad_status → downgrade to low
def test_reanalysis_name_match_bad_status_downgrades():
    r = apply_downgrade(True, "name_match_bad_status", "medium")
    assert r["confidence"] == "low"
    assert "validation_warning" in r


# 3. Re-analysis + verified → NO downgrade
def test_reanalysis_verified_no_downgrade():
    r = apply_downgrade(True, "verified", "high")
    assert r["confidence"] == "high"
    assert "validation_warning" not in r


# 4. No re-analysis + failed validation → NO downgrade
def test_no_reanalysis_no_downgrade():
    r = apply_downgrade(False, "name_mismatch", "medium")
    assert r["confidence"] == "medium"
    assert "validation_warning" not in r


# 5. Re-analysis + no registry validation → NO downgrade
def test_reanalysis_no_registry_validation_no_downgrade():
    r = apply_downgrade(True, None, "high")
    assert r["confidence"] == "high"
    assert "validation_warning" not in r


# 6. Already low + re-analysis failed → stays low with warning
def test_already_low_reanalysis_failed_stays_low_with_warning():
    r = apply_downgrade(True, "name_mismatch", "low")
    assert r["confidence"] == "low"
    assert "validation_warning" in r


# 7. Re-analysis + fictitious_name → downgrade
def test_reanalysis_fictitious_name_downgrades():
    r = apply_downgrade(True, "fictitious_name", "high")
    assert r["confidence"] == "low"
    assert "validation_warning" in r


# 8. Re-analysis + branch_registration → downgrade
def test_reanalysis_branch_registration_downgrades():
    r = apply_downgrade(True, "branch_registration", "medium")
    assert r["confidence"] == "low"
    assert "validation_warning" in r
