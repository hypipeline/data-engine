<?php
/**
 * Test suite for NorthData title name extraction.
 * Run: php tests/test_northdata_title.php
 */

$passed = 0;
$failed = 0;

function extractNameFromTitle(string $pageTitle, string $registryId, string $countryName): string
{
    $titleName = explode(':', $pageTitle)[0];
    // Strip registry ID suffix (e.g. "PRH 2422742-9", "HRB 6469")
    if ($registryId) {
        $titleName = preg_replace('/,?\s*' . preg_quote($registryId, '/') . '\s*$/', '', $titleName);
    }
    // Strip country name
    if ($countryName) {
        $titleName = preg_replace('/,?\s*' . preg_quote($countryName, '/') . '\s*$/i', '', $titleName);
    }
    // Strip city (last comma-segment)
    $titleName = preg_replace('/,\s*[^,]+\s*$/', '', $titleName);
    return trim($titleName);
}

function test(string $label, string $expected, string $actual): void
{
    global $passed, $failed;
    if ($expected === $actual) {
        echo "  PASS: {$label}\n";
        $passed++;
    } else {
        echo "  FAIL: {$label}\n    Expected: \"{$expected}\"\n    Got:      \"{$actual}\"\n";
        $failed++;
    }
}

echo "=== NorthData Title Name Extraction ===\n\n";

// 1. Standard Finnish company
echo "1. Finnish company (Scanfil Oyj):\n";
$result = extractNameFromTitle(
    'Scanfil Oyj, Sievi, Finland, PRH 2422742-9: Network, Financial information',
    'PRH 2422742-9', 'Finland'
);
test('Name is Scanfil Oyj', 'Scanfil Oyj', $result);

// 2. German company with HRB registry ID
echo "2. German company (Siemens AG):\n";
$result = extractNameFromTitle(
    'Siemens AG, München, Germany, HRB 6684: Network, Financial information',
    'HRB 6684', 'Germany'
);
test('Name is Siemens AG', 'Siemens AG', $result);

// 3. Company name containing a comma (e.g. "Nike, Inc.")
echo "3. Company with comma in name (Nike, Inc.):\n";
$result = extractNameFromTitle(
    'Nike, Inc., Beaverton, United States: Network, Financial information',
    '', 'United States'
);
test('Name is Nike, Inc.', 'Nike, Inc.', $result);

// 4. Dutch company with KVK registry
echo "4. Dutch company (Shell International B.V.):\n";
$result = extractNameFromTitle(
    'Shell International B.V., Den Haag, Netherlands, KVK 27155369: Network, Financial information',
    'KVK 27155369', 'Netherlands'
);
test('Name is Shell International B.V.', 'Shell International B.V.', $result);

// 5. Company with no registry ID in title
echo "5. No registry ID (adidas AG):\n";
$result = extractNameFromTitle(
    'adidas AG, Herzogenaurach, Germany: Network, Financial information',
    '', 'Germany'
);
test('Name is adidas AG', 'adidas AG', $result);

// 6. Estonian subsidiary (Scanfil OÜ)
echo "6. Estonian company with unicode (Scanfil OÜ):\n";
$result = extractNameFromTitle(
    'Scanfil OÜ, Pärnu, Estonia, RK 11348482: Network, Financial information',
    'RK 11348482', 'Estonia'
);
test('Name is Scanfil OÜ', 'Scanfil OÜ', $result);

// 7. Terminated company (still has same title format)
echo "7. Terminated company:\n";
$result = extractNameFromTitle(
    'Scanfil Oy, Helsinki, Finland, PRH 0830882-6: Network, Financial information',
    'PRH 0830882-6', 'Finland'
);
test('Name is Scanfil Oy', 'Scanfil Oy', $result);

// 8. Polish company with Sp. z o.o. suffix
echo "8. Polish company (Scanfil Poland Sp. z o.o.):\n";
$result = extractNameFromTitle(
    'Scanfil Poland sp. z o.o., Mysłowice, Poland, KRS 0000071022: Network, Financial information',
    'KRS 0000071022', 'Poland'
);
test('Name is Scanfil Poland sp. z o.o.', 'Scanfil Poland sp. z o.o.', $result);

// 9. Italian company with S.r.l. suffix
echo "9. Italian company (Hi-Tech Elettronica S.r.l.):\n";
$result = extractNameFromTitle(
    'Hi-Tech Elettronica S.r.l., Sala Bolognese, Italy, BO 305549: Network, Financial information',
    'BO 305549', 'Italy'
);
test('Name is Hi-Tech Elettronica S.r.l.', 'Hi-Tech Elettronica S.r.l.', $result);

// 10. Company name with multiple commas (edge case)
echo "10. Company with multiple commas in name:\n";
$result = extractNameFromTitle(
    'Smith, Jones & Partners, Ltd., London, United Kingdom, CH 12345678: Network, Financial information',
    'CH 12345678', 'United Kingdom'
);
test('Name is Smith, Jones & Partners, Ltd.', 'Smith, Jones & Partners, Ltd.', $result);

echo "\n=== RESULTS ===\n";
echo "Passed: {$passed}\n";
echo "Failed: {$failed}\n";
exit($failed > 0 ? 1 : 0);
