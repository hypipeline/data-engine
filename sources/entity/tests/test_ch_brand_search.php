<?php
/**
 * Test suite for Companies House brand search with cross-referencing.
 * Run: php tests/test_ch_brand_search.php
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

echo "=== Companies House Brand Search ===\n\n";

// 1. Test CH API helpers
echo "1. CH API - Get Company (Mornington Partners #10258578):\n";
$co = $tools->companiesHouseGetCompany('10258578');
test('Returns data', true, $co !== null);
test('Has company name', 'MORNINGTON PARTNERS LIMITED', $co['company_name'] ?? '');
test('Has postcode', true, !empty($co['postal_code']));
echo "   Address: {$co['address']}, Postcode: {$co['postal_code']}\n\n";

echo "2. CH API - Get Officers (Mornington Partners #10258578):\n";
$officers = $tools->companiesHouseGetOfficers('10258578');
test('Has officers', true, count($officers) > 0);
echo "   Officers: " . implode(', ', $officers) . "\n\n";

// 2. Brand search: Inflexion, using known Inflexion postcode W1U 3AY
echo "3. Brand Search: 'Inflexion' with known postcode W1U 3AY:\n";
$matches = $tools->companiesHouseBrandSearch('Inflexion', ['W1U 3AY'], [], []);
echo "   Found " . count($matches) . " matching companies\n";
test('Has matches', true, count($matches) > 0);

// Check for known Inflexion entities at Mandeville Place
$matchNames = array_map(fn($m) => strtoupper($m['company_name']), $matches);
test('Contains INFLEXION LIMITED PARTNERSHIP', true, in_array('INFLEXION LIMITED PARTNERSHIP', $matchNames));
echo "   Matched companies:\n";
foreach ($matches as $m) {
    echo "   - {$m['company_name']} ({$m['company_number']}) — {$m['match_reason']}\n";
}
echo "\n";

// 3. Brand search with officer matching
echo "4. Brand Search: 'Inflexion' with known officer HAZELL-SMITH:\n";
// Use a postcode that won't match anything, to test officer-only matching
$matches2 = $tools->companiesHouseBrandSearch('Inflexion', ['ZZ99 9ZZ'], ['HAZELL-SMITH'], []);
echo "   Found " . count($matches2) . " matching companies (officer match only)\n";
test('Returns array', true, is_array($matches2));
echo "\n";

// 4. Brand search with no matching criteria should return empty
echo "5. Brand Search: 'Inflexion' with unrelated postcode and officer:\n";
$matches3 = $tools->companiesHouseBrandSearch('Inflexion', ['ZZ99 9ZZ'], ['ZZZZNONEXISTENT'], []);
test('No matches', true, count($matches3) === 0);
echo "\n";

echo "=== RESULTS ===\n";
echo "Passed: {$passed}\n";
echo "Failed: {$failed}\n";
exit($failed > 0 ? 1 : 0);
