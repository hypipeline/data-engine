<?php
/**
 * Bizapedia Trademark Search Tool
 * Search US trademarks by mark name or owner name.
 */
$config = require __DIR__ . '/config.php';
$query = trim($_GET['q'] ?? '');
$mode = trim($_GET['mode'] ?? 'name'); // 'name' = by trademark name, 'owner' = by owner name
$results = null;
$error = null;
$elapsed = null;

$apiKey = 'YBUIWJDRQYMBKXCQDA';

if ($query) {
    $t0 = microtime(true);

    $params = ['ep' => 'LT', 'k' => $apiKey];
    if ($mode === 'owner') {
        $params['tm'] = '';
        $params['tmo'] = $query;
    } else {
        $params['tm'] = $query;
        $params['tmo'] = '';
    }

    $url = 'https://www.bizapedia.com/bdmservice-rest.aspx?' . http_build_query($params);

    $ch = curl_init($url);
    curl_setopt_array($ch, [
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_TIMEOUT => 30,
        CURLOPT_ENCODING => '',
    ]);
    $response = curl_exec($ch);
    $httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    curl_close($ch);

    $elapsed = microtime(true) - $t0;

    if ($httpCode !== 200 || !$response) {
        $error = "HTTP {$httpCode} — no response from Bizapedia API.";
    } else {
        $data = json_decode($response, true);
        if (!$data || !$data['Success']) {
            $error = 'API error: ' . ($data['ErrorMessage'] ?? 'unknown');
        } else {
            $results = $data['Trademarks'] ?? [];
            if (empty($results)) {
                $error = "No trademarks found for \"{$query}\".";
            }
        }
    }
}

// JSON API mode
if (isset($_GET['format']) && $_GET['format'] === 'json') {
    header('Content-Type: application/json');
    echo json_encode([
        'query' => $query,
        'mode' => $mode,
        'results' => $results ?? [],
        'error' => $error,
        'elapsed_s' => $elapsed ? round($elapsed, 2) : null,
    ], JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);
    exit;
}
?>
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Trademark Search<?= $query ? ' — ' . htmlspecialchars($query) : '' ?></title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f5f5f7; color: #333; min-height: 100vh; }

  .header { background: #1a1a2e; color: #fff; padding: 24px 40px; display: flex; justify-content: space-between; align-items: center; }
  .header h1 { font-size: 22px; font-weight: 600; }
  .header h1 a { text-decoration: none; color: inherit; }
  .header p { font-size: 13px; color: #8a8aaf; margin-top: 4px; }
  .header nav a { color: #8a8aaf; text-decoration: none; font-size: 13px; margin-left: 16px; }
  .header nav a.active { color: #fff; }

  .content { max-width: 1200px; margin: 0 auto; padding: 32px 40px; }

  .search-form { display: flex; gap: 12px; margin-bottom: 24px; flex-wrap: wrap; align-items: center; }
  .search-input { flex: 1; min-width: 280px; padding: 12px 16px; font-size: 15px; border: 2px solid #e0e0e0; border-radius: 8px; outline: none; }
  .search-input:focus { border-color: #4a90d9; }
  .search-btn { padding: 12px 28px; background: #4a90d9; color: #fff; border: none; border-radius: 8px; font-size: 15px; font-weight: 600; cursor: pointer; }
  .search-btn:hover { background: #3a7bc8; }

  .mode-toggle { display: flex; gap: 0; border: 2px solid #e0e0e0; border-radius: 8px; overflow: hidden; }
  .mode-toggle label { padding: 10px 18px; font-size: 13px; font-weight: 600; cursor: pointer; background: #fff; color: #666; transition: all 0.15s; }
  .mode-toggle input { display: none; }
  .mode-toggle input:checked + label { background: #4a90d9; color: #fff; }

  .meta-bar { display: flex; gap: 16px; align-items: center; margin-bottom: 20px; font-size: 13px; color: #666; }
  .meta-bar .count { font-weight: 700; color: #1a1a2e; font-size: 15px; }

  .error { background: #fef2f2; border: 1px solid #fecaca; border-radius: 8px; padding: 16px; color: #991b1b; margin-bottom: 20px; }

  .results-table { width: 100%; border-collapse: collapse; background: #fff; border-radius: 12px; overflow: hidden; border: 1px solid #e0e0e0; }
  .results-table th { background: #f8f8fc; padding: 10px 14px; text-align: left; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; color: #666; border-bottom: 2px solid #e0e0e0; white-space: nowrap; }
  .results-table td { padding: 10px 14px; font-size: 13px; border-bottom: 1px solid #f0f0f0; vertical-align: top; }
  .results-table tr:last-child td { border-bottom: none; }
  .results-table tr:hover td { background: #f8f9ff; }

  .mark-name { font-weight: 600; color: #1a1a2e; }
  .owner-name { font-weight: 600; color: #1a1a2e; }

  .badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.3px; }
  .badge-active { background: #d4edda; color: #155724; }
  .badge-inactive { background: #f8d7da; color: #721c24; }
  .badge-unknown { background: #e2e8f0; color: #475569; }

  .serial { font-family: monospace; font-size: 12px; }
  .address { font-size: 12px; color: #555; max-width: 200px; }

  .expandable { cursor: pointer; color: #4a90d9; font-size: 11px; }
  .expandable:hover { text-decoration: underline; }
  .detail-row { display: none; }
  .detail-row.open { display: table-row; }
  .detail-cell { background: #fafbff; padding: 16px 14px; }
  .detail-grid { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 16px; font-size: 12px; }
  .detail-grid dt { font-weight: 700; color: #555; font-size: 11px; text-transform: uppercase; margin-bottom: 2px; }
  .detail-grid dd { margin: 0 0 10px 0; color: #333; }
</style>
</head>
<body>

<div class="header">
  <div>
    <h1><a href="/">Entity Lookup</a></h1>
    <p>US trademark search via Bizapedia API</p>
  </div>
  <nav>
    <a href="/">Lookup</a>
    <a href="/bizapedia.php">Companies</a>
    <a href="/bizapedia_tm.php" class="active">Trademarks</a>
    <a href="/settings.php">Settings</a>
  </nav>
</div>

<div class="content">

<form class="search-form" method="get">
  <input type="text" name="q" class="search-input" placeholder="Search trademarks..." value="<?= htmlspecialchars($query) ?>" required autofocus>
  <div class="mode-toggle">
    <input type="radio" name="mode" value="name" id="mode-name" <?= $mode !== 'owner' ? 'checked' : '' ?>>
    <label for="mode-name">By Mark Name</label>
    <input type="radio" name="mode" value="owner" id="mode-owner" <?= $mode === 'owner' ? 'checked' : '' ?>>
    <label for="mode-owner">By Owner</label>
  </div>
  <button type="submit" class="search-btn">Search</button>
</form>

<?php if ($error): ?>
  <div class="error"><?= htmlspecialchars($error) ?></div>
<?php endif; ?>

<?php if ($results !== null && !empty($results)): ?>
  <div class="meta-bar">
    <span class="count"><?= count($results) ?> result<?= count($results) !== 1 ? 's' : '' ?></span>
    <span>for "<?= htmlspecialchars($query) ?>"</span>
    <span>(<?= $mode === 'owner' ? 'by owner' : 'by mark name' ?>)</span>
    <?php if ($elapsed): ?><span><?= number_format($elapsed, 1) ?>s</span><?php endif; ?>
  </div>

  <table class="results-table">
    <thead>
      <tr>
        <th>Mark</th>
        <th>Owner</th>
        <th>Status</th>
        <th>Serial #</th>
        <th>Reg #</th>
        <th>Filed</th>
        <th>Registered</th>
        <th>State</th>
        <th></th>
      </tr>
    </thead>
    <tbody>
    <?php foreach ($results as $i => $r): ?>
      <?php
        $statusDesc = $r['StatusDescription'] ?? 'Unknown';
        $statusLower = strtolower($statusDesc);
        if (str_contains($statusLower, 'registered') && !str_contains($statusLower, 'not')) {
            $statusClass = 'badge-active';
        } elseif (str_contains($statusLower, 'abandoned') || str_contains($statusLower, 'cancelled')
                  || str_contains($statusLower, 'dead') || str_contains($statusLower, 'expired')) {
            $statusClass = 'badge-inactive';
        } else {
            $statusClass = 'badge-unknown';
        }

        $ownerAddr = array_filter([
            $r['OwnerAddressLine1'] ?? '',
            $r['OwnerAddressLine2'] ?? '',
            implode(', ', array_filter([
                $r['OwnerAddressCity'] ?? '',
                $r['OwnerAddressState'] ?? '',
                $r['OwnerAddressPostalCode'] ?? '',
            ])),
        ]);
      ?>
      <tr>
        <td><span class="mark-name"><?= htmlspecialchars($r['MarkIdentification'] ?? '') ?></span></td>
        <td>
          <span class="owner-name"><?= htmlspecialchars($r['OwnerName'] ?? '') ?></span>
          <?php if ($ownerAddr): ?>
            <div class="address"><?= htmlspecialchars(implode(', ', $ownerAddr)) ?></div>
          <?php endif; ?>
        </td>
        <td><span class="badge <?= $statusClass ?>"><?= htmlspecialchars($statusDesc) ?></span></td>
        <td class="serial"><?= htmlspecialchars($r['SerialNumber'] ?? '') ?></td>
        <td class="serial"><?= htmlspecialchars($r['RegistrationNumber'] ?? '') ?></td>
        <td style="white-space:nowrap;font-size:12px;"><?= htmlspecialchars(substr($r['FilingDate']['Date'] ?? '', 0, 10) ?: '—') ?></td>
        <td style="white-space:nowrap;font-size:12px;"><?= htmlspecialchars(substr($r['RegistrationDate']['Date'] ?? '', 0, 10) ?: '—') ?></td>
        <td><?= htmlspecialchars($r['OwnerNationalityStateName'] ?? $r['OwnerNationalityState'] ?? '') ?></td>
        <td>
          <span class="expandable" onclick="document.getElementById('tm-detail-<?= $i ?>').classList.toggle('open')">Details</span>
        </td>
      </tr>
      <tr id="tm-detail-<?= $i ?>" class="detail-row">
        <td colspan="9" class="detail-cell">
          <div class="detail-grid">
            <div>
              <dt>Mark Drawing</dt>
              <dd><?= htmlspecialchars($r['MarkDrawingDescription'] ?? '—') ?></dd>
              <dt>Attorney</dt>
              <dd><?= htmlspecialchars($r['AttorneyName'] ?? '—') ?></dd>
              <dt>Law Office</dt>
              <dd><?= htmlspecialchars($r['LawOfficeAssignedLocationDescription'] ?? '—') ?></dd>
            </div>
            <div>
              <dt>Status Date</dt>
              <dd><?= htmlspecialchars(substr($r['StatusDate']['Date'] ?? '', 0, 10) ?: '—') ?></dd>
              <dt>Renewal Date</dt>
              <dd><?= htmlspecialchars(substr($r['RenewalDate']['Date'] ?? '', 0, 10) ?: '—') ?></dd>
              <dt>Last Transaction</dt>
              <dd><?= htmlspecialchars(substr($r['LastTransactionDate']['Date'] ?? '', 0, 10) ?: '—') ?></dd>
              <dt>Owner Country</dt>
              <dd><?= htmlspecialchars($r['OwnerNationalityCountry'] ?: $r['OwnerNationalityStateName'] ?? '—') ?></dd>
            </div>
            <div>
              <dt>Owner Type</dt>
              <dd><?= htmlspecialchars($r['OwnerPartyTypeName'] ?? '—') ?></dd>
              <dt>Current Location</dt>
              <dd><?= htmlspecialchars($r['CurrentLocation'] ?? '—') ?></dd>
              <?php if ($r['DomesticRepresentativeName'] ?? ''): ?>
                <dt>Domestic Rep</dt>
                <dd><?= htmlspecialchars($r['DomesticRepresentativeName']) ?></dd>
              <?php endif; ?>
            </div>
          </div>
        </td>
      </tr>
    <?php endforeach; ?>
    </tbody>
  </table>
<?php endif; ?>

</div>
</body>
</html>
