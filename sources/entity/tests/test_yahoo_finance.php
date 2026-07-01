<?php
/**
 * Test Google Intelligence + Yahoo Finance + LinkedIn integration.
 * Run: php tests/test_yahoo_finance.php
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

require_once __DIR__ . '/../php/tools.php';

$config = require __DIR__ . '/../php/config.php';
$tools = new LookupTools($config);

echo "=== Google Intelligence + Yahoo Finance ===\n\n";

// 1. Test Google Intelligence batch search
echo "1. Google Intelligence (3 searches in 1 API call):\n";
$intel = $tools->googleIntelligence('franchisebrands.co.uk');
test('Returns array', true, is_array($intel));
test('Has google_results key', true, isset($intel['google_results']));
test('Has yahoo_ticker key', true, array_key_exists('yahoo_ticker', $intel));
test('Has linkedin_url key', true, array_key_exists('linkedin_url', $intel));
test('Google results not empty', true, strlen($intel['google_results']) > 50);
test('Yahoo ticker is FRAN.L', 'FRAN.L', $intel['yahoo_ticker']);
test('LinkedIn URL found', true, $intel['linkedin_url'] !== null);
test('LinkedIn URL contains /company/', true, str_contains($intel['linkedin_url'] ?? '', '/company/'));

// 2. Test Yahoo Finance data fetch
echo "\n2. Yahoo Finance data fetch (FRAN.L):\n";
$data = $tools->yahooFinanceData('FRAN.L');
test('FRAN.L data not empty', true, strlen($data) > 100);
test('Contains company profile', true, str_contains($data, 'Company Profile'));
test('Contains Income Statement', true, str_contains($data, 'Income Statement'));
test('Contains Revenue', true, str_contains($data, 'Revenue'));
test('Contains source link', true, str_contains($data, 'finance.yahoo.com/quote/FRAN.L'));
test('Contains sector', true, str_contains($data, 'Sector:'));

// 3. Test LinkedIn company data fetch
echo "\n3. LinkedIn company data fetch:\n";
if ($intel['linkedin_url']) {
    $linkedinData = $tools->fetchLinkedInCompany($intel['linkedin_url']);
    test('LinkedIn data not empty', true, strlen($linkedinData) > 50);
    test('Contains company name', true, str_contains($linkedinData, 'Name:'));
    test('Contains LinkedIn Profile header', true, str_contains($linkedinData, 'LinkedIn Company Profile'));
    test('Contains address', true, str_contains($linkedinData, 'Address:'));
} else {
    echo "  SKIP: No LinkedIn URL found\n";
}

// 4. Test value formatting
echo "\n4. Value formatting:\n";
$ref = new ReflectionClass($tools);
$method = $ref->getMethod('yahooFormatVal');
test('Trillions', '50.7T', $method->invoke($tools, ['raw' => 50684952000000]));
test('Billions', '1.5B', $method->invoke($tools, ['raw' => 1500000000]));
test('Millions', '89.5M', $method->invoke($tools, ['raw' => 89460000]));
test('Thousands', '5.0K', $method->invoke($tools, ['raw' => 5000]));
test('Negative', '-1.5B', $method->invoke($tools, ['raw' => -1500000000]));
test('Null', '—', $method->invoke($tools, []));

echo "\n=== Results: {$passed} passed, {$failed} failed ===\n";
exit($failed > 0 ? 1 : 0);
