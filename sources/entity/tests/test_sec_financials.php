<?php
/**
 * Test suite for SEC EDGAR XBRL financial data extraction.
 * Run: php tests/test_sec_financials.php
 */

require_once __DIR__ . '/../php/tools.php';

$config = json_decode(file_get_contents(__DIR__ . '/../php/settings.json'), true);
$tools = new LookupTools($config);

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

function testContains(string $label, string $needle, string $haystack): void
{
    global $passed, $failed;
    if (str_contains($haystack, $needle)) {
        echo "  PASS: {$label}\n";
        $passed++;
    } else {
        echo "  FAIL: {$label} — expected to contain \"{$needle}\"\n    Got: " . substr($haystack, 0, 300) . "\n";
        $failed++;
    }
}

echo "=== SEC EDGAR XBRL Financials ===\n\n";

// 1. Apple — large company, standard US-GAAP tags
echo "1. Apple (CIK 320193):\n";
$result = $tools->secEdgarFinancials('320193');
test('Returns data', true, strlen($result) > 0);
testContains('Has entity name', 'Apple', $result);
testContains('Has Revenue', 'Revenue', $result);
testContains('Has Net Income', 'Net Income', $result);
testContains('Has Total Assets', 'Total Assets', $result);
testContains('Has dollar amounts', '$', $result);
echo "\n{$result}\n\n";

// 2. Goldman Sachs — financial company, uses RevenuesNetOfInterestExpense
echo "2. Goldman Sachs (CIK 886982):\n";
$result2 = $tools->secEdgarFinancials('886982');
test('Returns data', true, strlen($result2) > 0);
testContains('Has entity name', 'Goldman Sachs', $result2);
testContains('Has Revenue', 'Revenue', $result2);
testContains('Has Total Assets', 'Total Assets', $result2);
echo "\n{$result2}\n\n";

// 3. CrowdStrike — uses IncludingAssessedTax variant
echo "3. CrowdStrike (CIK 1535527):\n";
$result3 = $tools->secEdgarFinancials('1535527');
test('Returns data', true, strlen($result3) > 0);
testContains('Has entity name', 'CROWDSTRIKE', $result3);
testContains('Has Revenue', 'Revenue', $result3);
echo "\n{$result3}\n\n";

// 4. Invalid CIK — should return empty
echo "4. Invalid CIK (0):\n";
$result4 = $tools->secEdgarFinancials('0');
test('Returns empty', '', $result4);
echo "\n";

// 5. Private company CIK — should return empty
echo "5. Non-existent CIK (9999999999):\n";
$result5 = $tools->secEdgarFinancials('9999999999');
test('Returns empty', '', $result5);
echo "\n";

echo "=== Results: {$passed} passed, {$failed} failed ===\n";
exit($failed > 0 ? 1 : 0);
