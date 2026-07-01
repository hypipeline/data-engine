<?php
/**
 * Test the three registry validation lookups: Bizapedia, Companies House, NorthData.
 * Run: php tests/test_validation.php
 */

require_once __DIR__ . '/../php/tools.php';

$settingsPath = __DIR__ . '/../php/settings.json';
$settings = json_decode(file_get_contents($settingsPath), true);
$tools = new LookupTools($settings);

$pass = 0;
$fail = 0;

function test(string $label, bool $condition, string $detail = '') {
    global $pass, $fail;
    if ($condition) {
        echo "  PASS: {$label}\n";
        $pass++;
    } else {
        echo "  FAIL: {$label}" . ($detail ? " — {$detail}" : "") . "\n";
        $fail++;
    }
}

// ═══════════════════════════════════════════════════════════════════
echo "=== 1. Bizapedia — lookupBizapediaByFileNumber ===\n";
// Known entity: Apple Inc, California, file number 806592
$biz = $tools->lookupBizapediaByFileNumber('806592', 'CA');
test('Returns result', $biz !== null);
if ($biz) {
    test('Has EntityName', !empty($biz['EntityName']));
    test('Name contains Apple', stripos($biz['EntityName'], 'APPLE') !== false, $biz['EntityName'] ?? 'null');
    test('Has FilingStatus', !empty($biz['FilingStatus']));
    test('Status is Active', strtolower($biz['FilingStatus'] ?? '') === 'active', $biz['FilingStatus'] ?? 'null');
    echo "  Full record: " . json_encode($biz, JSON_PRETTY_PRINT) . "\n";
}

// Bizapedia — non-existent file number
echo "\n--- Bizapedia: non-existent entity ---\n";
$bizBad = $tools->lookupBizapediaByFileNumber('XXXXXXXXX', 'CA');
test('Returns null for bad file number', $bizBad === null);

// ═══════════════════════════════════════════════════════════════════
echo "\n=== 2. Companies House — lookupCompaniesHouseByNumber ===\n";
// Known entity: Vodafone Group Plc, company number 01833679
$ch = $tools->lookupCompaniesHouseByNumber('01833679');
test('Returns result', $ch !== null);
if ($ch) {
    test('Has company_name', !empty($ch['company_name']));
    test('Name contains VODAFONE', stripos($ch['company_name'], 'VODAFONE') !== false, $ch['company_name'] ?? 'null');
    test('Has company_status', !empty($ch['company_status']));
    test('Status is active', strtolower($ch['company_status'] ?? '') === 'active', $ch['company_status'] ?? 'null');
    echo "  Name: {$ch['company_name']}, Status: {$ch['company_status']}\n";
}

// Companies House — non-existent number
echo "\n--- Companies House: non-existent entity ---\n";
$chBad = $tools->lookupCompaniesHouseByNumber('99999999');
test('Returns null for bad company number', $chBad === null);

// ═══════════════════════════════════════════════════════════════════
echo "\n=== 3. NorthData — validateNorthdataEntity ===\n";

// 3a: German entity — Siemens AG, HRB 6684
echo "--- Siemens AG (DE, HRB 6684) ---\n";
$nd = $tools->validateNorthdataEntity('Siemens AG', 'HRB 6684', 'DE');
test('Returns result', $nd !== null);
if ($nd) {
    test('Name contains Siemens', stripos($nd['name'], 'Siemens') !== false, $nd['name'] ?? 'null');
    test('Country match', $nd['country_match'] === true);
    test('Registry ID found on page', $nd['registry_id_match'] === true);
    test('Status is active', strtolower($nd['status'] ?? '') === 'active', $nd['status'] ?? 'null');
    echo "  Name: {$nd['name']}\n";
    echo "  Status: {$nd['status']}\n";
}

// 3b: Spanish entity — Iberveg Spain SL, B63437917
echo "\n--- Iberveg Spain SL (ES, B63437917) ---\n";
$nd2 = $tools->validateNorthdataEntity('Iberveg Spain SL', 'B63437917', 'ES');
test('Returns result', $nd2 !== null);
if ($nd2) {
    test('Name contains Iberveg', stripos($nd2['name'], 'Iberveg') !== false, $nd2['name'] ?? 'null');
    test('Country match', $nd2['country_match'] === true);
    test('Registry ID found on page', $nd2['registry_id_match'] === true);
    echo "  Name: {$nd2['name']}\n";
    echo "  Status: " . ($nd2['status'] ?? 'null') . "\n";
}

// 3c: Valid name, wrong country — registry ID should not match
echo "\n--- Siemens AG with wrong country (ES) ---\n";
$ndWrongCountry = $tools->validateNorthdataEntity('Siemens AG', 'HRB 6684', 'ES');
test('Returns result', $ndWrongCountry !== null);
if ($ndWrongCountry) {
    test('Registry ID match is false', $ndWrongCountry['registry_id_match'] === false);
    echo "  Name: {$ndWrongCountry['name']} (country: " . ($ndWrongCountry['country_match'] ? 'true' : 'false') . ", regId: " . ($ndWrongCountry['registry_id_match'] ? 'true' : 'false') . ")\n";
}

// 3d: Non-existent entity
echo "\n--- Non-existent entity ---\n";
$ndBad = $tools->validateNorthdataEntity('Xyzzy Totally Fake Corp', 'XYZ 000000', 'DE');
if ($ndBad === null) {
    test('Returns null for unknown entity', true);
} else {
    test('Registry ID match is false for fake entity', $ndBad['registry_id_match'] === false);
}

// ═══════════════════════════════════════════════════════════════════
echo "\n=== 4. Bizapedia — Branch (Foreign) Detection ===\n";
// RADIAN CAPITAL LLC is a Foreign LLC in NY, domestic jurisdiction is Delaware
$bizBranch = $tools->lookupBizapediaByFileNumber('6001112', 'NY');
test('Returns result', $bizBranch !== null);
if ($bizBranch) {
    test('Has EntityName', !empty($bizBranch['EntityName']));
    test('Name contains RADIAN', stripos($bizBranch['EntityName'], 'RADIAN') !== false, $bizBranch['EntityName'] ?? 'null');
    $branchType = strtoupper($bizBranch['EntityType'] ?? '');
    test('EntityType contains FOREIGN', str_contains($branchType, 'FOREIGN'), $branchType);
    test('Domestic jurisdiction is DE', ($bizBranch['DomesticJurisdictionPostalAbbreviation'] ?? '') === 'DE', $bizBranch['DomesticJurisdictionPostalAbbreviation'] ?? 'null');
    echo "  EntityType: {$bizBranch['EntityType']}\n";
    echo "  Domestic: {$bizBranch['DomesticJurisdictionPostalAbbreviation']}\n";
}

// ═══════════════════════════════════════════════════════════════════
echo "\n=== 5. NorthData — searchNorthdata ===\n";

// 5a: German entity search (existing coverage)
echo "--- Search: Siemens (DE) ---\n";
$ndSearch1 = $tools->searchNorthdata('Siemens AG');
test('Returns results', !str_contains($ndSearch1, 'No North Data results'));
test('Contains Siemens', stripos($ndSearch1, 'Siemens') !== false, substr($ndSearch1, 0, 200));

// 5b: Finnish entity search (new coverage)
echo "\n--- Search: Scanfil (FI) ---\n";
$ndSearch2 = $tools->searchNorthdata('Scanfil Oyj');
test('Returns results', !str_contains($ndSearch2, 'No North Data results'), $ndSearch2);
test('Contains Scanfil', stripos($ndSearch2, 'Scanfil') !== false, substr($ndSearch2, 0, 200));

// 5c: Finnish entity validation — Scanfil Oyj (parent company, should be active)
echo "\n--- Validate: Scanfil Oyj (FI, PRH 2422742-9) ---\n";
$ndFi = $tools->validateNorthdataEntity('Scanfil Oyj', '2422742-9', 'FI');
test('Returns result', $ndFi !== null);
if ($ndFi) {
    test('Name contains Scanfil', stripos($ndFi['name'], 'Scanfil') !== false, $ndFi['name'] ?? 'null');
    test('Name contains Oyj', stripos($ndFi['name'], 'Oyj') !== false, $ndFi['name'] ?? 'null');
    test('Country match', $ndFi['country_match'] === true);
    test('Registry ID match', $ndFi['registry_id_match'] === true);
    test('Status is active', strtolower($ndFi['status'] ?? '') === 'active', $ndFi['status'] ?? 'null');
    echo "  Name: {$ndFi['name']}\n";
    echo "  Status: " . ($ndFi['status'] ?? 'null') . "\n";
    echo "  URL: " . ($ndFi['url'] ?? 'null') . "\n";
}

// ═══════════════════════════════════════════════════════════════════
echo "\n========================================\n";
echo "Results: {$pass} passed, {$fail} failed\n";
echo "========================================\n";
exit($fail > 0 ? 1 : 0);
