<?php
/**
 * Test suite for Companies House corporate appointments lookup.
 * Run: php tests/test_ch_corporate_appointments.php
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

echo "=== Companies House Corporate Appointments ===\n\n";

// 1. Mornington Partners Limited — known to have 9 corporate appointments
echo "1. MORNINGTON PARTNERS LIMITED:\n";
$results = $tools->companiesHouseCorporateAppointments('MORNINGTON PARTNERS LIMITED');
echo "   Found " . count($results) . " appointment(s)\n";
$activeResults = array_filter($results, fn($a) => $a['status'] === 'active');
echo "   Active: " . count($activeResults) . "\n";
test('Has appointments', true, count($results) > 0);
test('Has active appointments', true, count($activeResults) > 0);

// Check for known portfolio companies
$names = array_map(fn($a) => strtoupper($a['company_name']), $results);
test('Contains GLOBAL HOLDCO LIMITED', true, in_array('GLOBAL HOLDCO LIMITED', $names));
test('Contains THE BRIARS GROUP LIMITED', true, in_array('THE BRIARS GROUP LIMITED', $names));

// Check structure
$first = $results[0] ?? [];
test('Has company_name', true, !empty($first['company_name']));
test('Has company_number', true, !empty($first['company_number']));
test('Has role', true, !empty($first['role']));
test('Has status', true, in_array($first['status'], ['active', 'resigned']));
echo "\n";

// 2. Nonexistent company — should return empty
echo "2. ZZZYYYXXX NONEXISTENT CORP LTD:\n";
$results = $tools->companiesHouseCorporateAppointments('ZZZYYYXXX NONEXISTENT CORP LTD');
test('No appointments', true, count($results) === 0);
echo "\n";

// 3. A well-known company that probably isn't a corporate director of others
echo "3. TESCO PLC (unlikely to be corporate director):\n";
$results = $tools->companiesHouseCorporateAppointments('TESCO PLC');
echo "   Found " . count($results) . " appointment(s)\n";
// Just check it doesn't error — Tesco may or may not have corporate appointments
test('Returns array', true, is_array($results));
echo "\n";

echo "=== RESULTS ===\n";
echo "Passed: {$passed}\n";
echo "Failed: {$failed}\n";
exit($failed > 0 ? 1 : 0);
