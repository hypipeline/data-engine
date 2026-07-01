<?php
/**
 * Registry Validation Tool
 * Replicates the Phase 7 validation logic as a standalone page.
 * Input: entity name, registry ID, country, state (US only)
 * Output: pass/fail with details
 */

require_once __DIR__ . '/tools.php';

$settings = json_decode(file_get_contents(__DIR__ . '/settings.json'), true);

// Handle AJAX validation request
if ($_SERVER['REQUEST_METHOD'] === 'POST' && isset($_POST['action']) && $_POST['action'] === 'validate') {
    header('Content-Type: application/json');

    $entityName = trim($_POST['entity_name'] ?? '');
    $registryId = trim($_POST['registry_id'] ?? '');
    $country = strtoupper(trim($_POST['country'] ?? ''));
    $state = strtoupper(trim($_POST['state'] ?? ''));

    if (!$registryId || !$country) {
        echo json_encode(['error' => 'registry_id and country are required']);
        exit;
    }

    $tools = new LookupTools($settings);
    $registryName = null;
    $registryStatus = null;
    $source = null;
    $rawData = null;

    $northdataCountries = ['DE', 'NL', 'FR', 'AT', 'CH', 'BE', 'LU', 'IT', 'ES', 'DK', 'SE', 'NO', 'FI', 'PL', 'CZ', 'IE'];

    // US → Bizapedia
    $isBranch = false;
    $isFictitious = false;
    $domesticState = null;
    $fictitiousOwner = null;
    if ($country === 'US' && $state) {
        $biz = $tools->lookupBizapediaByFileNumber($registryId, $state);
        if ($biz) {
            $registryName = $biz['EntityName'] ?? null;
            $registryStatus = $biz['FilingStatus'] ?? null;
            $source = 'Bizapedia';
            $rawData = $biz;
            $entityType = strtoupper($biz['EntityType'] ?? '');
            $domesticState = $biz['DomesticJurisdictionPostalAbbreviation'] ?? null;
            // Check if this is a branch (Foreign) registration
            if (str_contains($entityType, 'FOREIGN') || str_contains($entityType, 'OUT OF STATE')) {
                $isBranch = true;
            }
            // Check if this is a fictitious name (trade name, not a legal entity)
            if (str_contains($entityType, 'FICTITIOUS')) {
                $isFictitious = true;
                // Extract the owner from Principals
                $principals = $biz['Principals'] ?? [];
                foreach ($principals as $p) {
                    if (strtolower($p['Titles'] ?? '') === 'owner' && !empty($p['PrincipalName'])) {
                        $fictitiousOwner = $p['PrincipalName'];
                        break;
                    }
                }
            }
        }
    }

    // UK → Companies House
    if ($country === 'GB' && !$registryName) {
        $ch = $tools->lookupCompaniesHouseByNumber($registryId);
        if ($ch) {
            $registryName = $ch['company_name'] ?? null;
            $registryStatus = $ch['company_status'] ?? null;
            $source = 'Companies House';
            $rawData = $ch;
        }
    }

    // Europe → NorthData
    if (in_array($country, $northdataCountries) && !$registryName) {
        $nd = $tools->validateNorthdataEntity($entityName, $registryId, $country);
        if ($nd) {
            $fullNdName = preg_replace('/\s*\([^)]*\)\s*$/', '', $nd['name']);
            $parts = array_map('trim', explode(',', $fullNdName));
            $registryName = count($parts) >= 3 ? implode(', ', array_slice($parts, 0, -2)) : $parts[0];
            $registryStatus = $nd['status'] ?? 'unknown';
            $source = 'NorthData';
            $rawData = $nd;
            $countryMatch = $nd['country_match'] ?? false;

            // Primary check: country must match
            if (!$countryMatch) {
                $registryName = null;
            }
        }
    }

    // Not found
    if (!$registryName) {
        echo json_encode([
            'result' => false,
            'status' => 'not_found',
            'message' => "Registry ID \"{$registryId}\" not found in " . ($source ?? 'registry'),
            'source' => $source,
            'raw' => $rawData,
        ]);
        exit;
    }

    // Compare names (skip comparison if no entity name provided — just show registry result)
    $normLlm = strtoupper(preg_replace('/[^A-Z0-9 ]/', '', strtoupper($entityName)));
    $normReg = strtoupper(preg_replace('/[^A-Z0-9 ]/', '', strtoupper($registryName)));
    $nameMatch = !$entityName || $normLlm === $normReg;

    // Check status
    $statusLower = strtolower($registryStatus ?? '');
    $statusOk = in_array($statusLower, ['active', 'unknown']);

    // Check registry ID (NorthData returns this; Bizapedia/CH validate by ID directly so always true)
    $regIdOk = ($rawData['registry_id_match'] ?? null) !== false;

    $base = [
        'registry_name' => $registryName,
        'registry_status' => $registryStatus,
        'source' => $source,
        'name_match' => $nameMatch,
        'registry_id_match' => $regIdOk,
        'name_normalised' => ['input' => $normLlm, 'registry' => $normReg],
        'raw' => $rawData,
    ];

    $base['is_branch'] = $isBranch;
    $base['is_fictitious'] = $isFictitious;
    if ($isBranch) {
        $base['domestic_state'] = $domesticState;
    }
    if ($isFictitious) {
        $base['fictitious_owner'] = $fictitiousOwner;
    }

    if (!$nameMatch) {
        echo json_encode($base + [
            'result' => false,
            'status' => 'name_mismatch',
            'message' => "Name mismatch: input \"{$entityName}\" but registry has \"{$registryName}\"",
        ]);
    } elseif (!$regIdOk) {
        echo json_encode($base + [
            'result' => false,
            'status' => 'registry_id_mismatch',
            'message' => "Entity \"{$registryName}\" found in {$source} but registry ID \"{$registryId}\" not found on page",
        ]);
    } elseif ($isFictitious) {
        $ownerMsg = $fictitiousOwner ? " Owner: {$fictitiousOwner}." : "";
        $ownerLookup = null;
        // Automatically look up the owning entity in registries
        if ($fictitiousOwner && $country === 'US' && $state) {
            $ownerResults = $tools->searchBizapedia($fictitiousOwner);
            if (!empty($ownerResults)) {
                // Find results matching the same state that are actual entities (not fictitious)
                $ownerLookup = [];
                foreach ($ownerResults as $r) {
                    $rType = strtoupper($r['EntityType'] ?? '');
                    if (!str_contains($rType, 'FICTITIOUS')) {
                        $ownerLookup[] = [
                            'EntityName' => $r['EntityName'] ?? '',
                            'FileNumber' => $r['FileNumber'] ?? '',
                            'FilingStatus' => $r['FilingStatus'] ?? '',
                            'EntityType' => $r['EntityType'] ?? '',
                            'FilingJurisdiction' => $r['FilingJurisdictionPostalAbbreviation'] ?? '',
                            'DomesticJurisdiction' => $r['DomesticJurisdictionPostalAbbreviation'] ?? '',
                        ];
                    }
                }
            }
        }
        $extra = ['result' => false, 'status' => 'fictitious_name',
            'message' => "This is a fictitious name (trade name) registration, not a legal entity.{$ownerMsg} Look up the owning entity instead."];
        if ($ownerLookup !== null) {
            $extra['owner_registry_results'] = $ownerLookup;
        }
        echo json_encode($base + $extra);
    } elseif ($isBranch) {
        echo json_encode($base + [
            'result' => false,
            'status' => 'branch_registration',
            'message' => "This is a branch (Foreign) registration in {$state}. Home jurisdiction is {$domesticState}. Use the domestic filing instead.",
        ]);
    } elseif (!$statusOk) {
        echo json_encode($base + [
            'result' => false,
            'status' => 'name_match_bad_status',
            'message' => "Name and registry ID match but status is \"{$registryStatus}\" (not active) in {$source}",
        ]);
    } else {
        echo json_encode($base + [
            'result' => true,
            'status' => 'verified',
            'message' => "Verified: \"{$registryName}\" is {$registryStatus} in {$source}" . ($source === 'NorthData' ? " (registry ID confirmed on page)" : ""),
        ]);
    }
    exit;
}
?>
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Registry Validation Tool</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f5f5f5; padding: 40px 20px; }
  .container { max-width: 600px; margin: 0 auto; }
  h1 { font-size: 20px; margin-bottom: 20px; color: #333; }
  .form-group { margin-bottom: 14px; }
  label { display: block; font-size: 13px; font-weight: 600; color: #555; margin-bottom: 4px; }
  input, select { width: 100%; padding: 8px 10px; border: 1px solid #ccc; border-radius: 6px; font-size: 14px; }
  input:focus, select:focus { outline: none; border-color: #4a90d9; box-shadow: 0 0 0 2px rgba(74,144,217,0.2); }
  .row { display: flex; gap: 12px; }
  .row .form-group { flex: 1; }
  .state-group { max-width: 100px; }
  button { padding: 10px 24px; background: #4a90d9; color: #fff; border: none; border-radius: 6px; font-size: 14px; font-weight: 600; cursor: pointer; }
  button:hover { background: #3a7bc8; }
  button:disabled { opacity: 0.5; cursor: not-allowed; }
  .result { margin-top: 20px; padding: 16px; border-radius: 8px; font-size: 14px; display: none; }
  .result.pass { background: #d4edda; border: 1px solid #a3d9a5; }
  .result.fail { background: #f8d7da; border: 1px solid #f5c6cb; }
  .result .badge { display: inline-block; padding: 2px 10px; border-radius: 4px; font-weight: 700; font-size: 13px; margin-bottom: 8px; color: #fff; }
  .result.pass .badge { background: #28a745; }
  .result.fail .badge { background: #dc3545; }
  .result .message { margin-bottom: 10px; color: #333; }
  .result .details { font-size: 12px; color: #666; }
  .result .details dt { font-weight: 600; margin-top: 6px; }
  .result .details dd { margin-left: 0; }
  .raw-toggle { font-size: 12px; color: #4a90d9; cursor: pointer; margin-top: 10px; display: inline-block; }
  .raw-data { display: none; margin-top: 8px; background: #fff; border: 1px solid #ddd; border-radius: 4px; padding: 10px; font-family: monospace; font-size: 11px; white-space: pre-wrap; max-height: 300px; overflow: auto; }
  a.back { font-size: 13px; color: #4a90d9; text-decoration: none; display: inline-block; margin-bottom: 16px; }
</style>
</head>
<body>
<div class="container">
  <a class="back" href="/">&larr; Back to lookup</a>
  <h1>Registry Validation Tool</h1>
  <form id="validateForm">
    <div class="form-group">
      <label>Entity Name</label>
      <input type="text" name="entity_name" placeholder="e.g. APPLE INC." value="<?= htmlspecialchars($_GET['entity_name'] ?? '') ?>">
    </div>
    <div class="form-group">
      <label>Registry ID</label>
      <input type="text" name="registry_id" placeholder="e.g. 806592 or HRB 6684" required value="<?= htmlspecialchars($_GET['registry_id'] ?? $_GET['file_number'] ?? '') ?>">
    </div>
    <div class="row">
      <div class="form-group">
        <label>Country</label>
        <?php $getCountry = $_GET['country'] ?? (!empty($_GET['state']) ? 'US' : ''); ?>
        <select name="country" required>
          <option value="">Select...</option>
          <option value="US" <?= $getCountry === 'US' ? 'selected' : '' ?>>US</option>
          <option value="GB" <?= $getCountry === 'GB' ? 'selected' : '' ?>>GB</option>
          <optgroup label="Europe (NorthData)">
            <option value="DE">DE</option>
            <option value="NL">NL</option>
            <option value="FR">FR</option>
            <option value="AT">AT</option>
            <option value="CH">CH</option>
            <option value="BE">BE</option>
            <option value="LU">LU</option>
            <option value="IT">IT</option>
            <option value="ES">ES</option>
            <option value="DK">DK</option>
            <option value="SE">SE</option>
            <option value="NO">NO</option>
            <option value="FI">FI</option>
            <option value="PL">PL</option>
            <option value="CZ">CZ</option>
            <option value="IE">IE</option>
          </optgroup>
        </select>
      </div>
      <div class="form-group state-group">
        <label>State</label>
        <input type="text" name="state" placeholder="CA" maxlength="2" value="<?= htmlspecialchars($_GET['state'] ?? '') ?>">
      </div>
    </div>
    <button type="submit">Validate</button>
  </form>
  <div id="result" class="result">
    <div class="badge" id="resultBadge"></div>
    <div class="message" id="resultMessage"></div>
    <dl class="details" id="resultDetails"></dl>
    <span class="raw-toggle" id="rawToggle" onclick="document.getElementById('rawData').style.display = document.getElementById('rawData').style.display === 'none' ? 'block' : 'none'">Show raw data</span>
    <div class="raw-data" id="rawData"></div>
  </div>
</div>
<script>
document.getElementById('validateForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const form = e.target;
  const btn = form.querySelector('button');
  btn.disabled = true;
  btn.textContent = 'Validating...';
  const resultEl = document.getElementById('result');
  resultEl.style.display = 'none';

  const body = new FormData(form);
  body.append('action', 'validate');

  try {
    const resp = await fetch('/validate.php', { method: 'POST', body });
    const data = await resp.json();

    resultEl.style.display = 'block';
    resultEl.className = 'result ' + (data.result ? 'pass' : 'fail');
    document.getElementById('resultBadge').textContent = data.result ? 'PASS' : 'FAIL';
    document.getElementById('resultMessage').textContent = data.message;

    let details = '';
    if (data.source) details += `<dt>Source</dt><dd>${data.source}</dd>`;
    if (data.registry_name) details += `<dt>Registry Name</dt><dd>${data.registry_name}</dd>`;
    if (data.registry_status) details += `<dt>Registry Status</dt><dd>${data.registry_status}</dd>`;
    if (data.status) details += `<dt>Validation Status</dt><dd>${data.status}</dd>`;
    if ('name_match' in data) details += `<dt>Name Match</dt><dd>${data.name_match ? '✓' : '✗'}</dd>`;
    if ('registry_id_match' in data) details += `<dt>Registry ID Match</dt><dd>${data.registry_id_match ? '✓' : '✗'}</dd>`;
    if (data.name_normalised) details += `<dt>Normalised Input</dt><dd>${data.name_normalised.input}</dd><dt>Normalised Registry</dt><dd>${data.name_normalised.registry}</dd>`;
    document.getElementById('resultDetails').innerHTML = details;
    document.getElementById('rawData').textContent = data.raw ? JSON.stringify(data.raw, null, 2) : 'No raw data';
    document.getElementById('rawData').style.display = 'none';
  } catch (err) {
    resultEl.style.display = 'block';
    resultEl.className = 'result fail';
    document.getElementById('resultBadge').textContent = 'ERROR';
    document.getElementById('resultMessage').textContent = err.message;
    document.getElementById('resultDetails').innerHTML = '';
  }
  btn.disabled = false;
  btn.textContent = 'Validate';
});
// Auto-submit if all GET params provided
<?php if (!empty($_GET['registry_id'] ?? $_GET['file_number'] ?? '')): ?>
document.getElementById('validateForm').dispatchEvent(new Event('submit'));
<?php endif; ?>
</script>
</body>
</html>
