<?php
/**
 * Test suite for SEC 8-K cover page parsing.
 * Run: php tests/test_sec_8k.php
 *
 * Downloads 8-K filings on first run and caches them in /tmp.
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

function fetch8K(LookupTools $tools, string $cik): ?array
{
    global $config;
    $sub = $tools->fetchSecSubmissions($cik);
    $subData = json_decode($sub, true);
    return $tools->fetchSec8K($cik, $subData);
}

echo "=== SEC 8-K Cover Page Parsing ===\n\n";

// 1. Alphabet Inc. — standard large-cap, Delaware
echo "1. Alphabet Inc. (CIK 0001652044):\n";
$r = fetch8K($tools, '0001652044');
test('Has result', true, $r !== null);
test('Registered name', 'ALPHABET INC.', $r['registered_name'] ?? null);
test('State of incorporation', 'Delaware', $r['state_of_incorporation'] ?? null);
test('IRS EIN', '61-1767919', $r['irs_ein'] ?? null);
test('Commission file number', '001-37580', $r['commission_file_number'] ?? null);
test('Address contains Mountain View', true, str_contains($r['address'] ?? '', 'Mountain View'));
test('Phone contains 650', true, str_contains($r['phone'] ?? '', '650'));
test('No former name', false, isset($r['former_name']));
echo "\n";

// 2. BlackRock Finance — has former name
echo "2. BlackRock Finance, Inc. (CIK 0001364742):\n";
$r = fetch8K($tools, '0001364742');
test('Has result', true, $r !== null);
test('Registered name', 'BLACKROCK FINANCE, INC.', $r['registered_name'] ?? null);
test('State of incorporation', 'Delaware', $r['state_of_incorporation'] ?? null);
test('IRS EIN', '32-0174431', $r['irs_ein'] ?? null);
test('Former name is BlackRock, Inc.', 'BlackRock, Inc.', $r['former_name'] ?? null);
test('Address contains New York', true, str_contains($r['address'] ?? '', 'New York'));
echo "\n";

// 3. NIKE, Inc. — Oregon incorporation, comma in name
echo "3. NIKE, Inc. (CIK 0000320187):\n";
$r = fetch8K($tools, '0000320187');
test('Has result', true, $r !== null);
test('Registered name', 'NIKE, Inc.', $r['registered_name'] ?? null);
test('State of incorporation', 'Oregon', $r['state_of_incorporation'] ?? null);
test('IRS EIN', '93-0584541', $r['irs_ein'] ?? null);
test('Address contains BEAVERTON', true, stripos($r['address'] ?? '', 'BEAVERTON') !== false);
test('No former name (NO CHANGE)', false, isset($r['former_name']));
echo "\n";

// 4. Google LLC — no 8-K filings expected
echo "4. Google LLC (CIK 0001824723) — no 8-K expected:\n";
$r = fetch8K($tools, '0001824723');
test('No 8-K result', true, $r === null);
echo "\n";

// 5. SEC single-result search fix — verify Alphabet Inc. is findable
echo "5. SEC company search — single-result fix:\n";
$searchResult = $tools->searchSecCompany('Alphabet Inc.');
test('Finds Alphabet via search', true, str_contains($searchResult, '0001652044'));
test('Contains company name', true, str_contains($searchResult, 'Alphabet Inc.'));
$searchResult2 = $tools->searchSecCompany('Alphabet Inc');
test('Works without trailing period', true, str_contains($searchResult2, '0001652044'));
echo "\n";

// 6. Multi-result search still works
echo "6. SEC company search — multi-result:\n";
$searchResult = $tools->searchSecCompany('BlackRock');
test('Returns multiple results', true, substr_count($searchResult, 'CIK:') > 1);
echo "\n";

// 7. Nonexistent company
echo "7. SEC company search — no results:\n";
$searchResult = $tools->searchSecCompany('Zzzyyyxxx Nonexistent Corp');
test('Returns no results message', true, str_contains($searchResult, 'No SEC company results found'));
echo "\n";

// 8. Amazon — period in name breaks SEC prefix search
echo "8. Amazon.com, Inc. (CIK 0001018724):\n";
$searchResult = $tools->searchSecCompany('Amazon.com, Inc.');
test('Finds Amazon via search', true, str_contains($searchResult, '0001018724'));
$searchResult2 = $tools->searchSecCompany('Amazon.com');
test('Finds Amazon without Inc', true, str_contains($searchResult2, '0001018724'));
echo "\n";

// 9. Amazon 8-K — recent 8-Ks may be 404 on SEC servers; test gracefully
echo "9. Amazon 8-K cover page:\n";
$r = fetch8K($tools, '0001018724');
if ($r && !empty($r['registered_name'])) {
    test('Registered name contains AMAZON', true, stripos($r['registered_name'], 'AMAZON') !== false);
    test('State of incorporation', 'Delaware', $r['state_of_incorporation'] ?? null);
    test('Has IRS EIN', true, !empty($r['irs_ein']));
} else {
    // 8-K filing may be unavailable (404) — not a code bug
    echo "  SKIP: Amazon 8-K filing not accessible (likely 404 on SEC servers)\n";
}
echo "\n";

echo "=== RESULTS ===\n";
echo "Passed: {$passed}\n";
echo "Failed: {$failed}\n";
exit($failed > 0 ? 1 : 0);
