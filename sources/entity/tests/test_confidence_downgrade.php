<?php
/**
 * Test suite for confidence auto-downgrade after failed re-validation.
 * Run: php tests/test_confidence_downgrade.php
 *
 * Tests the logic that downgrades confidence to 'low' and adds a
 * validation_warning when Phase 8 re-validation also fails.
 */

$passed = 0;
$failed = 0;

function test(string $label, $expected, $actual): void
{
    global $passed, $failed;
    if ($expected === $actual) {
        echo "  PASS: {$label}\n";
        $passed++;
    } else {
        echo "  FAIL: {$label}\n    Expected: " . json_encode($expected) . "\n    Got:      " . json_encode($actual) . "\n";
        $failed++;
    }
}

/**
 * Simulate the confidence downgrade logic from lookup.php (lines 148-154).
 * $needsReanalysis: whether Phase 8 was triggered
 * $rvStatus: registry_validation status after Phase 8
 * $originalConfidence: the confidence from the LLM report
 */
function applyDowngrade(bool $needsReanalysis, ?string $rvStatus, string $originalConfidence): array
{
    $report = [
        'confidence' => $originalConfidence,
        'registry_validation' => $rvStatus ? ['status' => $rvStatus] : null,
    ];

    // Mirror the exact logic from lookup.php
    $rvStatusCheck = $report['registry_validation']['status'] ?? null;
    if ($needsReanalysis && $rvStatusCheck && $rvStatusCheck !== 'verified') {
        $report['confidence'] = 'low';
        $report['validation_warning'] = 'Registry validation failed after re-analysis — confidence auto-downgraded';
    }

    return $report;
}

echo "=== Confidence Auto-Downgrade Tests ===\n\n";

// 1. Re-analysis triggered, re-validation failed with name_mismatch → downgrade
echo "1. Re-analysis + name_mismatch → downgrade to low:\n";
$r = applyDowngrade(true, 'name_mismatch', 'high');
test('Confidence downgraded to low', 'low', $r['confidence']);
test('Has validation_warning', true, isset($r['validation_warning']));
echo "\n";

// 2. Re-analysis triggered, re-validation failed with name_match_bad_status → downgrade
echo "2. Re-analysis + name_match_bad_status → downgrade to low:\n";
$r = applyDowngrade(true, 'name_match_bad_status', 'medium');
test('Confidence downgraded to low', 'low', $r['confidence']);
test('Has validation_warning', true, isset($r['validation_warning']));
echo "\n";

// 3. Re-analysis triggered, re-validation verified → NO downgrade
echo "3. Re-analysis + verified → no downgrade:\n";
$r = applyDowngrade(true, 'verified', 'high');
test('Confidence stays high', 'high', $r['confidence']);
test('No validation_warning', false, isset($r['validation_warning']));
echo "\n";

// 4. No re-analysis needed, validation failed → NO downgrade (Phase 7 only)
echo "4. No re-analysis + failed validation → no downgrade:\n";
$r = applyDowngrade(false, 'name_mismatch', 'medium');
test('Confidence stays medium', 'medium', $r['confidence']);
test('No validation_warning', false, isset($r['validation_warning']));
echo "\n";

// 5. Re-analysis triggered, no registry validation at all → NO downgrade
echo "5. Re-analysis + no registry validation → no downgrade:\n";
$r = applyDowngrade(true, null, 'high');
test('Confidence stays high', 'high', $r['confidence']);
test('No validation_warning', false, isset($r['validation_warning']));
echo "\n";

// 6. Already low confidence + re-analysis failed → stays low with warning
echo "6. Already low + re-analysis failed → stays low with warning:\n";
$r = applyDowngrade(true, 'name_mismatch', 'low');
test('Confidence is low', 'low', $r['confidence']);
test('Has validation_warning', true, isset($r['validation_warning']));
echo "\n";

// 7. Re-analysis + fictitious_name status → downgrade
echo "7. Re-analysis + fictitious_name → downgrade:\n";
$r = applyDowngrade(true, 'fictitious_name', 'high');
test('Confidence downgraded to low', 'low', $r['confidence']);
test('Has validation_warning', true, isset($r['validation_warning']));
echo "\n";

// 8. Re-analysis + branch_registration → downgrade
echo "8. Re-analysis + branch_registration → downgrade:\n";
$r = applyDowngrade(true, 'branch_registration', 'medium');
test('Confidence downgraded to low', 'low', $r['confidence']);
test('Has validation_warning', true, isset($r['validation_warning']));
echo "\n";

echo "=== RESULTS ===\n";
echo "Passed: {$passed}\n";
echo "Failed: {$failed}\n";
exit($failed > 0 ? 1 : 0);
