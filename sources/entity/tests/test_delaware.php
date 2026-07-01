<?php
/**
 * Test suite for Delaware Division of Corporations search.
 * Run: php tests/test_delaware.php
 *
 * Uses Browserbase to search the Delaware ICIS website.
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

echo "=== Delaware Division of Corporations Search ===\n\n";

// 1. LEEDS EQUITY ADVISORS, LLC — exists in Delaware but not in OC/Bizapedia
echo "1. LEEDS EQUITY ADVISORS, LLC:\n";
$result = $tools->searchDelaware('LEEDS EQUITY ADVISORS');
echo "   Raw result:\n";
foreach (explode("\n", $result) as $line) {
    echo "     $line\n";
}
test('Found results', true, !str_contains($result, 'No Delaware entities found'));
test('Contains LEEDS EQUITY', true, stripos($result, 'LEEDS EQUITY') !== false);
test('Contains file number 3094669', true, str_contains($result, '3094669'));
echo "\n";

// 2. Alphabet Inc — well-known Delaware entity
echo "2. ALPHABET INC:\n";
$result = $tools->searchDelaware('ALPHABET INC');
echo "   Raw result:\n";
foreach (explode("\n", $result) as $line) {
    echo "     $line\n";
}
test('Found results', true, !str_contains($result, 'No Delaware entities found'));
test('Contains ALPHABET', true, stripos($result, 'ALPHABET') !== false);
echo "\n";

// 3. Nonexistent company
echo "3. Nonexistent company:\n";
$result = $tools->searchDelaware('ZZZYYYXXX NONEXISTENT CORP');
echo "   Raw result:\n";
foreach (explode("\n", $result) as $line) {
    echo "     $line\n";
}
test('No results found', true, str_contains($result, 'No Delaware entities found'));
echo "\n";

// 4. Amazon — should find AMAZON.COM, INC.
echo "4. AMAZON:\n";
$result = $tools->searchDelaware('AMAZON');
echo "   Raw result:\n";
foreach (explode("\n", $result) as $line) {
    echo "     $line\n";
}
test('Found results', true, !str_contains($result, 'No Delaware entities found'));
test('Contains AMAZON', true, stripos($result, 'AMAZON') !== false);
echo "\n";

// 5. NIKE — known Delaware entity
echo "5. NIKE:\n";
$result = $tools->searchDelaware('NIKE');
echo "   Raw result:\n";
foreach (explode("\n", $result) as $line) {
    echo "     $line\n";
}
test('Found results', true, !str_contains($result, 'No Delaware entities found'));
test('Contains NIKE', true, stripos($result, 'NIKE') !== false);
echo "\n";

echo "=== File Number Lookup (Validation) ===\n\n";

// 6. LEEDS EQUITY ADVISORS by file number
echo "6. File #3094669 (LEEDS EQUITY ADVISORS, LLC):\n";
$r = $tools->lookupDelawareByFileNumber('3094669');
test('Has result', true, $r !== null);
test('Name is LEEDS EQUITY ADVISORS, LLC', 'LEEDS EQUITY ADVISORS, LLC', $r['name'] ?? null);
test('File number matches', '3094669', $r['file_number'] ?? null);
test('Has status', true, !empty($r['status']));
echo "\n";

// 7. Nonexistent file number
echo "7. File #9999999999 (nonexistent):\n";
$r = $tools->lookupDelawareByFileNumber('9999999999');
test('No result', true, $r === null);
echo "\n";

echo "=== RESULTS ===\n";
echo "Passed: {$passed}\n";
echo "Failed: {$failed}\n";
exit($failed > 0 ? 1 : 0);
