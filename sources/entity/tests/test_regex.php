<?php
/**
 * Test suite for extractCandidateNames regex.
 * Run: php tests/test_regex.php
 */

// Include the function under test
require_once __DIR__ . '/../php/lookup.php';

// Create a minimal instance to access the method via reflection
$config = require __DIR__ . '/../php/config.php';
$lookup = new EntityLookup($config);
$method = new ReflectionMethod($lookup, 'extractCandidateNames');
$method->setAccessible(true);

$passed = 0;
$failed = 0;

function assertContains(string $label, array $result, string $expected): void {
    global $passed, $failed;
    foreach ($result as $name) {
        if (stripos($name, $expected) !== false) {
            echo "  ✓ PASS: '{$expected}' found\n";
            $passed++;
            return;
        }
    }
    echo "  ✗ FAIL: '{$expected}' NOT found in " . json_encode($result) . "\n";
    $failed++;
}

function assertNotContains(string $label, array $result, string $expected): void {
    global $passed, $failed;
    foreach ($result as $name) {
        if (stripos($name, $expected) !== false) {
            echo "  ✗ FAIL: '{$expected}' should NOT be present but found '{$name}'\n";
            $failed++;
            return;
        }
    }
    echo "  ✓ PASS: '{$expected}' correctly absent\n";
    $passed++;
}

function assertEmpty(string $label, array $result): void {
    global $passed, $failed;
    if (empty($result)) {
        echo "  ✓ PASS: no candidates (expected)\n";
        $passed++;
    } else {
        echo "  ✗ FAIL: expected no candidates, got " . json_encode($result) . "\n";
        $failed++;
    }
}

// ── TEST CASES ──

echo "=== Copyright notices ===\n";

echo "Simple copyright with LLC:\n";
$r = $method->invoke($lookup, "© 2024 Google LLC. All rights reserved.");
assertContains('', $r, 'Google LLC');

echo "Copyright with Ltd:\n";
$r = $method->invoke($lookup, "Copyright 2023 Acme Trading Ltd. All rights reserved.");
assertContains('', $r, 'Acme Trading Ltd');

echo "Copyright with GmbH:\n";
$r = $method->invoke($lookup, "© 2024 adidas AG. Alle Rechte vorbehalten.");
assertContains('', $r, 'adidas AG');

echo "Copyright with Inc:\n";
$r = $method->invoke($lookup, "© 2023-2024 Nike, Inc. All rights reserved.");
assertContains('', $r, 'Nike, Inc');

echo "Copyright with long trailing text (should NOT capture junk):\n";
$r = $method->invoke($lookup, "© 2024 Google LLC — Help Centre, Safety Centre, Transparency Centre, and other pages accessible from our policies site");
assertContains('', $r, 'Google LLC');
assertNotContains('', $r, 'Help Centre');
assertNotContains('', $r, 'Transparency Centre');

echo "(c) format:\n";
$r = $method->invoke($lookup, "(c) 2024 Microsoft Corporation");
assertContains('', $r, 'Microsoft Corporation');

echo "\n=== Sentences ending in common words (false positives) ===\n";

echo "Sentence ending in 'as' (should NOT match):\n";
$r = $method->invoke($lookup, "We collect information about the apps, browsers and devices that you use");
assertNotContains('', $r, 'information');
assertNotContains('', $r, 'devices');

echo "Sentence ending in 'as' #2:\n";
$r = $method->invoke($lookup, "We want you to understand the types of information we collect as");
assertNotContains('', $r, 'information we collect as');

echo "Sentence ending in 'as' #3:\n";
$r = $method->invoke($lookup, "Some Google services have additional age requirements as");
assertNotContains('', $r, 'age requirements as');

echo "Sentence ending in 'as' #4:\n";
$r = $method->invoke($lookup, "Information about things near your device, such as");
assertNotContains('', $r, 'such as');

echo "Sentence with 'company' as generic word:\n";
$r = $method->invoke($lookup, "Our company is committed to providing excellent service");
assertNotContains('', $r, 'Our company');

echo "Sentence ending in 'se' (should NOT match):\n";
$r = $method->invoke($lookup, "The permission that we give to you to access and use");
assertNotContains('', $r, 'permission');

echo "\n=== Legitimate entity names in text ===\n";

echo "Entity at start of line:\n";
$r = $method->invoke($lookup, "Amazon Web Services LLC provides cloud computing.");
assertContains('', $r, 'Amazon Web Services LLC');

echo "Operated by pattern:\n";
$r = $method->invoke($lookup, "This site is operated by Acme Holdings Ltd on behalf of its subsidiaries.");
assertContains('', $r, 'Acme Holdings Ltd');

echo "Managed by pattern:\n";
$r = $method->invoke($lookup, "The fund is managed by BlackRock Investment Management (UK) Limited");
assertContains('', $r, 'BlackRock Investment Management (UK) Limited');

echo "German entity:\n";
$r = $method->invoke($lookup, "Betrieben von Siemens AG, München");
assertContains('', $r, 'Siemens AG');

echo "Dutch entity:\n";
$r = $method->invoke($lookup, "Shell International B.V. is registered in The Hague");
assertContains('', $r, 'Shell International B.V');

echo "Nordic entity (A/S):\n";
$r = $method->invoke($lookup, "Novo Nordisk A/S is a Danish pharmaceutical company");
assertContains('', $r, 'Novo Nordisk A/S');

echo "French entity (SAS):\n";
$r = $method->invoke($lookup, "Dior Couture SAS operates luxury retail stores");
assertContains('', $r, 'Dior Couture SAS');

echo "PLC:\n";
$r = $method->invoke($lookup, "Barclays PLC announced results today");
assertContains('', $r, 'Barclays PLC');

echo "LLP:\n";
$r = $method->invoke($lookup, "Clifford Chance LLP is a law firm");
assertContains('', $r, 'Clifford Chance LLP');

echo "Norwegian AS (legitimate, not sentence ending):\n";
$r = $method->invoke($lookup, "Equinor AS is headquartered in Stavanger");
assertContains('', $r, 'Equinor AS');

echo "\n=== Herculite false positives (sentence preamble before suffix) ===\n";

echo "U.S.A should not match S.A.:\n";
$r = $method->invoke($lookup, "All of our products are developed, produced, and checked for quality right here in the U.S.A");
assertEmpty('', $r);

echo "Long sentence ending in Inc (should capture only entity name):\n";
$r = $method->invoke($lookup, "You have the right at any time to stop Herculite Products Inc");
assertContains('', $r, 'Herculite Products Inc');
assertNotContains('', $r, 'You have the right');

echo "Privacy policy sentence ending in Inc:\n";
$r = $method->invoke($lookup, "This privacy policy will explain how Herculite Products Inc");
assertContains('', $r, 'Herculite Products Inc');
assertNotContains('', $r, 'This privacy policy');

echo "Legitimate Herculite entities:\n";
$r = $method->invoke($lookup, "Herculite Products Inc. is a leading manufacturer.\nHerculite, Inc. was founded in 1955.");
assertContains('', $r, 'Herculite Products Inc');
assertContains('', $r, 'Herculite, Inc');

echo "Privacy policy preamble (should only capture entity, not sentence):\n";
$r = $method->invoke($lookup, "This privacy policy will explain how Herculite Products Inc");
assertContains('', $r, 'Herculite Products Inc');
assertNotContains('', $r, 'policy will explain');

echo "Right to request preamble:\n";
$r = $method->invoke($lookup, "You have the right to request that Herculite Products Inc");
assertContains('', $r, 'Herculite Products Inc');
assertNotContains('', $r, 'right to request');

echo "Any time to stop preamble:\n";
$r = $method->invoke($lookup, "You have the right at any time to stop Herculite Products Inc");
assertContains('', $r, 'Herculite Products Inc');
assertNotContains('', $r, 'any time to stop');

echo "\n=== Google page text (real-world false positives) ===\n";

echo "Google privacy text with multiple 'as' endings:\n";
$r = $method->invoke($lookup, "We want you to understand the types of information we collect as\nSome Google services have additional age requirements as\nInformation about things near your device, such as\nThe permission that we give you to access and use\n© 2024 Google LLC. All rights reserved.");
assertContains('', $r, 'Google LLC');
assertNotContains('', $r, 'information we collect');
assertNotContains('', $r, 'age requirements');
assertNotContains('', $r, 'such as');
assertNotContains('', $r, 'access and use');

echo "Help Lp should not match:\n";
$r = $method->invoke($lookup, "Help\nLP records available\nHelp Lp");
assertNotContains('', $r, 'Help');

echo "Legitimate LP entity:\n";
$r = $method->invoke($lookup, "Blackstone Capital Partners LP is an investment fund");
assertContains('', $r, 'Blackstone Capital Partners LP');

echo "\n=== Edge cases ===\n";

echo "Multiple entities on one line:\n";
$r = $method->invoke($lookup, "Services provided by Acme Corp. and its subsidiary Acme UK Limited.");
assertContains('', $r, 'Acme Corp');
assertContains('', $r, 'Acme UK Limited');

echo "Empty text:\n";
$r = $method->invoke($lookup, "");
assertEmpty('', $r);

echo "No entity names:\n";
$r = $method->invoke($lookup, "Welcome to our website. We sell shoes and clothing worldwide.");
assertEmpty('', $r);

echo "\n=== Name deduplication ===\n";

// Test the dedup logic via reflection
$dedupMethod = new ReflectionMethod($lookup, 'deduplicateNames');
function deduplicateNames(array $names): array {
    global $lookup, $dedupMethod;
    return $dedupMethod->invoke($lookup, $names);
}

echo "Herculite dedup (punctuation variants):\n";
$names = ["Herculite Products Inc.", "Herculite, Inc.", "Herculite Products Inc", "Herculite, Inc", "Herculite"];
$deduped = deduplicateNames($names);
echo "  Input:  " . json_encode($names) . "\n";
echo "  Output: " . json_encode($deduped) . "\n";
// "Inc." vs "Inc" deduped, "Herculite" kept (different name, not just punctuation)
assertContains('', $deduped, 'Herculite Products Inc.');
assertContains('', $deduped, 'Herculite, Inc.');
assertContains('', $deduped, 'Herculite');
if (count($deduped) === 3) {
    echo "  ✓ PASS: exactly 3 unique names\n";
    $passed++;
} else {
    echo "  ✗ FAIL: expected 3 names, got " . count($deduped) . ": " . json_encode($deduped) . "\n";
    $failed++;
}

echo "Google dedup:\n";
$names = ["Google LLC", "Alphabet Inc.", "Alphabet Inc.", "Google"];
$deduped = deduplicateNames($names);
echo "  Input:  " . json_encode($names) . "\n";
echo "  Output: " . json_encode($deduped) . "\n";
assertContains('', $deduped, 'Google LLC');
assertContains('', $deduped, 'Alphabet Inc.');
assertContains('', $deduped, 'Google');
if (count($deduped) === 3) {
    echo "  ✓ PASS: exactly 3 unique names\n";
    $passed++;
} else {
    echo "  ✗ FAIL: expected 3 names, got " . count($deduped) . ": " . json_encode($deduped) . "\n";
    $failed++;
}

echo "Suffix variant dedup (Inc vs Incorporated, Ltd vs Limited):\n";
$names = ["Acme Inc.", "Acme Incorporated", "Acme Inc"];
$deduped = deduplicateNames($names);
echo "  Input:  " . json_encode($names) . "\n";
echo "  Output: " . json_encode($deduped) . "\n";
if (count($deduped) === 1) {
    echo "  ✓ PASS: exactly 1 unique name\n";
    $passed++;
} else {
    echo "  ✗ FAIL: expected 1 name, got " . count($deduped) . ": " . json_encode($deduped) . "\n";
    $failed++;
}

echo "UK ASDA vs ASDA (different names, should keep both):\n";
$names = ["UK ASDA Stores Limited", "ASDA Stores Limited"];
$deduped = deduplicateNames($names);
echo "  Input:  " . json_encode($names) . "\n";
echo "  Output: " . json_encode($deduped) . "\n";
if (count($deduped) === 2) {
    echo "  ✓ PASS: exactly 2 unique names\n";
    $passed++;
} else {
    echo "  ✗ FAIL: expected 2 names, got " . count($deduped) . ": " . json_encode($deduped) . "\n";
    $failed++;
}

echo "B.V. vs BV dedup:\n";
$names = ["Shell International B.V.", "Shell International BV"];
$deduped = deduplicateNames($names);
echo "  Input:  " . json_encode($names) . "\n";
echo "  Output: " . json_encode($deduped) . "\n";
if (count($deduped) === 1) {
    echo "  ✓ PASS: exactly 1 unique name\n";
    $passed++;
} else {
    echo "  ✗ FAIL: expected 1 name, got " . count($deduped) . ": " . json_encode($deduped) . "\n";
    $failed++;
}

echo "\n=== RESULTS ===\n";
echo "Passed: {$passed}\n";
echo "Failed: {$failed}\n";
exit($failed > 0 ? 1 : 0);
