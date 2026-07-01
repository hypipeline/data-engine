<?php
/**
 * Test suite for NorthData authentication and financial data extraction.
 * Run: php tests/test_northdata.php
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

function testNotContains(string $label, string $needle, string $haystack): void
{
    global $passed, $failed;
    if (!str_contains($haystack, $needle)) {
        echo "  PASS: {$label}\n";
        $passed++;
    } else {
        echo "  FAIL: {$label} — should NOT contain \"{$needle}\"\n";
        $failed++;
    }
}

echo "=== NorthData Tests ===\n\n";

// ── 1. Authentication ──────────────────────────────────────────────────────

echo "1. NorthData Login:\n";
// Use reflection to test private method
$ref = new ReflectionClass($tools);
$method = $ref->getMethod('getNorthdataAuthCookie');
$method->setAccessible(true);
$cookie = $method->invoke($tools);
test('Returns auth cookie', true, $cookie !== null);
test('Cookie is JWT', true, $cookie !== null && substr_count($cookie, '.') === 2);
echo "   Cookie: " . substr($cookie ?? '', 0, 50) . "...\n\n";

// ── 2. Financial extraction from authenticated HTML (ABCA Group) ────────

echo "2. Parse financials — ABCA Systems Group Ltd (auth HTML):\n";
$authHtml = file_get_contents(__DIR__ . '/fixtures/abca_group_auth.html');
test('Auth fixture loaded', true, strlen($authHtml) > 50000);

$parseMethod = $ref->getMethod('parseNorthdataHtml');
$parseMethod->setAccessible(true);
$result = $parseMethod->invoke($tools, $authHtml);

testContains('Has company name', 'Abca Systems Group Ltd', $result);
testContains('Has registry ID', 'Companies House 12500353', $result);
testContains('Has Financials section', '### Financials', $result);
testContains('Has Revenue', 'Revenue', $result);
testContains('Has Revenue value £11M', '£11M', $result);
testContains('Has Earnings', 'Earnings', $result);
testContains('Has Total assets', 'Total assets', $result);
echo "\n   Full result:\n";
echo $result . "\n\n";

// ── 3. Financial extraction from unauthenticated HTML (ABCA Group) ──────

echo "3. Parse financials — ABCA Systems Group Ltd (NO auth HTML):\n";
$noAuthHtml = file_get_contents(__DIR__ . '/fixtures/abca_group_noauth.html');
test('NoAuth fixture loaded', true, strlen($noAuthHtml) > 50000);

$noAuthResult = $parseMethod->invoke($tools, $noAuthHtml);

testContains('Has company name', 'Abca Systems Group Ltd', $noAuthResult);
// Without auth, we should NOT have financials — verify
// (This documents expected behaviour: no auth = no premium data)
echo "   No-auth result:\n";
echo $noAuthResult . "\n\n";

// ── 4. Financial extraction from authenticated HTML (ABCA Ltd) ──────────

echo "4. Parse financials — Abca Systems Ltd (auth HTML):\n";
$ltdHtml = file_get_contents(__DIR__ . '/fixtures/abca_ltd_auth.html');
test('Ltd fixture loaded', true, strlen($ltdHtml) > 50000);

$ltdResult = $parseMethod->invoke($tools, $ltdHtml);

testContains('Has company name', 'Abca Systems Ltd', $ltdResult);
testContains('Has Financials section', '### Financials', $ltdResult);
testContains('Has Revenue', 'Revenue', $ltdResult);
testContains('Has Earnings', 'Earnings', $ltdResult);
testContains('Has Total assets', 'Total assets', $ltdResult);
echo "\n   Full result:\n";
echo $ltdResult . "\n\n";

// ── 5. extractNorthdataFinancials directly ──────────────────────────────

echo "5. extractNorthdataFinancials — isolated test on auth HTML:\n";
$finMethod = $ref->getMethod('extractNorthdataFinancials');
$finMethod->setAccessible(true);

$authFin = $finMethod->invoke($tools, $authHtml);
echo "   Auth financials result:\n{$authFin}\n";
test('Auth financials not empty', true, strlen($authFin) > 0);
if (strlen($authFin) > 0) {
    testContains('Has Revenue row', 'Revenue', $authFin);
    testContains('Has Earnings row', 'Earnings', $authFin);
}

$noAuthFin = $finMethod->invoke($tools, $noAuthHtml);
echo "\n   No-auth financials result:\n{$noAuthFin}\n";
echo "   (Empty is expected — premium data gated)\n\n";

// ── 6. Live search with auth ────────────────────────────────────────────

echo "6. Live searchNorthdata — ABCA Systems Group Ltd:\n";
$liveResult = $tools->searchNorthdata('ABCA Systems Group Ltd');
testContains('Has company name', 'Abca Systems Group Ltd', $liveResult);
testContains('Has Financials section', '### Financials', $liveResult);
testContains('Has Revenue', 'Revenue', $liveResult);
echo "\n   Live result:\n";
echo $liveResult . "\n\n";

// ── Summary ─────────────────────────────────────────────────────────────

echo "=== Results: {$passed} passed, {$failed} failed ===\n";
exit($failed > 0 ? 1 : 0);
