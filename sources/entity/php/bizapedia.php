<?php
/**
 * Bizapedia Search Tool
 * Standalone search interface for Bizapedia API.
 */
$config = require __DIR__ . '/config.php';
$query = trim($_GET['q'] ?? '');
$city = trim($_GET['city'] ?? '');
$state = trim($_GET['state'] ?? '');
$results = null;
$error = null;
$elapsed = null;

$apiKey = 'YBUIWJDRQYMBKXCQDA';

if ($query) {
    $t0 = microtime(true);

    $params = [
        'ep' => 'LCSBN',
        'k' => $apiKey,
        'n' => $query,
    ];
    if ($city) $params['c'] = $city;
    if ($state) $params['pa'] = $state;

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
            $results = $data['Companies'] ?? [];
            if (empty($results)) {
                $error = "No results found for \"{$query}\".";
            }
        }
    }
}

// JSON API mode
if (isset($_GET['format']) && $_GET['format'] === 'json') {
    header('Content-Type: application/json');
    echo json_encode([
        'query' => $query,
        'city' => $city ?: null,
        'state' => $state ?: null,
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
<title>Bizapedia Search<?= $query ? ' — ' . htmlspecialchars($query) : '' ?></title>
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

  .search-form { display: flex; gap: 12px; margin-bottom: 24px; flex-wrap: wrap; }
  .search-input { flex: 1; min-width: 280px; padding: 12px 16px; font-size: 15px; border: 2px solid #e0e0e0; border-radius: 8px; outline: none; }
  .search-input:focus { border-color: #4a90d9; }
  .filter-input { width: 160px; padding: 12px 16px; font-size: 15px; border: 2px solid #e0e0e0; border-radius: 8px; outline: none; }
  .filter-input:focus { border-color: #4a90d9; }
  .search-btn { padding: 12px 28px; background: #4a90d9; color: #fff; border: none; border-radius: 8px; font-size: 15px; font-weight: 600; cursor: pointer; }
  .search-btn:hover { background: #3a7bc8; }

  .meta-bar { display: flex; gap: 16px; align-items: center; margin-bottom: 20px; font-size: 13px; color: #666; }
  .meta-bar .count { font-weight: 700; color: #1a1a2e; font-size: 15px; }

  .error { background: #fef2f2; border: 1px solid #fecaca; border-radius: 8px; padding: 16px; color: #991b1b; margin-bottom: 20px; }

  .results-table { width: 100%; border-collapse: collapse; background: #fff; border-radius: 12px; overflow: hidden; border: 1px solid #e0e0e0; }
  .results-table th { background: #f8f8fc; padding: 10px 14px; text-align: left; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; color: #666; border-bottom: 2px solid #e0e0e0; white-space: nowrap; }
  .results-table td { padding: 10px 14px; font-size: 13px; border-bottom: 1px solid #f0f0f0; vertical-align: top; }
  .results-table tr:last-child td { border-bottom: none; }
  .results-table tr:hover td { background: #f8f9ff; }

  .company-name { font-weight: 600; color: #1a1a2e; }
  .aka { font-size: 11px; color: #888; margin-top: 2px; }

  .badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.3px; }
  .badge-active { background: #d4edda; color: #155724; }
  .badge-inactive { background: #f8d7da; color: #721c24; }
  .badge-unknown { background: #e2e8f0; color: #475569; }

  .address { font-size: 12px; color: #555; max-width: 220px; }
  .agent { font-size: 12px; color: #555; }
  .principals { font-size: 11px; color: #666; margin-top: 4px; }
  .entity-type { font-size: 11px; color: #888; }
  .file-number { font-family: monospace; font-size: 12px; }

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
    <p>US business entity search via Bizapedia API</p>
  </div>
  <nav>
    <a href="/">Lookup</a>
    <a href="/bizapedia.php" class="active">Companies</a>
    <a href="/bizapedia_tm.php">Trademarks</a>
    <a href="/settings.php">Settings</a>
  </nav>
</div>

<div class="content">

<form class="search-form" method="get">
  <input type="text" name="q" class="search-input" placeholder="Company name..." value="<?= htmlspecialchars($query) ?>" required autofocus>
  <input type="text" name="city" class="filter-input" placeholder="City (optional)" value="<?= htmlspecialchars($city) ?>">
  <input type="text" name="state" class="filter-input" placeholder="State (e.g. CA)" value="<?= htmlspecialchars($state) ?>" style="width:100px;">
  <button type="submit" class="search-btn">Search</button>
</form>

<?php if ($error): ?>
  <div class="error"><?= htmlspecialchars($error) ?></div>
<?php endif; ?>

<?php if ($results !== null && !empty($results)): ?>
  <div class="meta-bar">
    <span class="count"><?= count($results) ?> result<?= count($results) !== 1 ? 's' : '' ?></span>
    <span>for "<?= htmlspecialchars($query) ?>"</span>
    <?php if ($city): ?><span>in <?= htmlspecialchars($city) ?></span><?php endif; ?>
    <?php if ($state): ?><span>(<?= htmlspecialchars(strtoupper($state)) ?>)</span><?php endif; ?>
    <?php if ($elapsed): ?><span><?= number_format($elapsed, 1) ?>s</span><?php endif; ?>
  </div>

  <table class="results-table">
    <thead>
      <tr>
        <th>Entity Name</th>
        <th>Status</th>
        <th>Type</th>
        <th>Jurisdiction</th>
        <th>File #</th>
        <th>Address</th>
        <th>Registered Agent</th>
        <th></th>
      </tr>
    </thead>
    <tbody>
    <?php foreach ($results as $i => $r): ?>
      <?php
        $statusRaw = strtolower($r['FilingStatus'] ?? '');
        if (str_contains($statusRaw, 'active') && !str_contains($statusRaw, 'inactive')) {
            $statusClass = 'badge-active';
        } elseif (str_contains($statusRaw, 'dissolved') || str_contains($statusRaw, 'inactive')
                  || str_contains($statusRaw, 'terminated') || str_contains($statusRaw, 'revoked')
                  || str_contains($statusRaw, 'cancelled') || str_contains($statusRaw, 'withdrawn')) {
            $statusClass = 'badge-inactive';
        } else {
            $statusClass = 'badge-unknown';
        }

        $addr = array_filter([
            $r['PrincipalAddressLine1'] ?? '',
            $r['PrincipalAddressLine2'] ?? '',
            implode(', ', array_filter([
                $r['PrincipalAddressCity'] ?? '',
                $r['PrincipalAddressState'] ?? '',
                $r['PrincipalAddressPostalCode'] ?? '',
            ])),
        ]);

        $akas = array_filter([
            $r['OtherEntityName1'] ?? '',
            $r['OtherEntityName2'] ?? '',
            $r['OtherEntityName3'] ?? '',
        ]);

        $principals = $r['Principals'] ?? [];
      ?>
      <tr>
        <td>
          <div class="company-name"><?= htmlspecialchars($r['EntityName'] ?? '') ?></div>
          <?php if ($akas): ?>
            <div class="aka">aka: <?= htmlspecialchars(implode(', ', $akas)) ?></div>
          <?php endif; ?>
        </td>
        <td><span class="badge <?= $statusClass ?>"><?= htmlspecialchars($r['FilingStatus'] ?? 'Unknown') ?></span></td>
        <td><span class="entity-type"><?= htmlspecialchars($r['EntityType'] ?? '') ?></span></td>
        <td><?= htmlspecialchars($r['FilingJurisdictionName'] ?? '') ?></td>
        <td class="file-number"><?= htmlspecialchars($r['FileNumber'] ?? '') ?></td>
        <td>
          <?php if ($addr): ?>
            <div class="address"><?= htmlspecialchars(implode(', ', $addr)) ?></div>
          <?php else: ?>
            <span style="color:#ccc;">—</span>
          <?php endif; ?>
        </td>
        <td>
          <?php if ($r['RegisteredAgentName'] ?? ''): ?>
            <div class="agent"><?= htmlspecialchars($r['RegisteredAgentName']) ?></div>
          <?php else: ?>
            <span style="color:#ccc;">—</span>
          <?php endif; ?>
        </td>
        <td>
          <?php if ($principals): ?>
            <span class="expandable" onclick="document.getElementById('detail-<?= $i ?>').classList.toggle('open')">Details</span>
          <?php endif; ?>
        </td>
      </tr>
      <?php if ($principals || ($r['MailingAddressLine1'] ?? '') || ($r['PrimaryEmail'] ?? '') || ($r['PrimaryPhone'] ?? '') || ($r['PrimaryDomainName'] ?? '')): ?>
      <tr id="detail-<?= $i ?>" class="detail-row">
        <td colspan="8" class="detail-cell">
          <div class="detail-grid">
            <div>
              <dt>Filing Date</dt>
              <dd><?= htmlspecialchars(substr($r['FilingDate']['Date'] ?? '', 0, 10) ?: '—') ?></dd>
              <dt>Domestic Jurisdiction</dt>
              <dd><?= htmlspecialchars($r['DomesticJurisdictionName'] ?? '—') ?></dd>
              <?php if ($r['PrimaryDomainName'] ?? ''): ?>
                <dt>Website</dt>
                <dd><?= htmlspecialchars($r['PrimaryDomainName']) ?></dd>
              <?php endif; ?>
              <?php if ($r['PrimaryEmail'] ?? ''): ?>
                <dt>Email</dt>
                <dd><?= htmlspecialchars($r['PrimaryEmail']) ?></dd>
              <?php endif; ?>
              <?php if ($r['PrimaryPhone'] ?? ''): ?>
                <dt>Phone</dt>
                <dd><?= htmlspecialchars($r['PrimaryPhone']) ?></dd>
              <?php endif; ?>
              <?php if ($r['BusinessDescription'] ?? ''): ?>
                <dt>Description</dt>
                <dd><?= htmlspecialchars($r['BusinessDescription']) ?></dd>
              <?php endif; ?>
            </div>
            <div>
              <dt>Registered Agent</dt>
              <dd>
                <?= htmlspecialchars($r['RegisteredAgentName'] ?? '—') ?><br>
                <?php
                  $raAddr = array_filter([
                      $r['RegisteredAgentAddressLine1'] ?? '',
                      $r['RegisteredAgentAddressLine2'] ?? '',
                      implode(', ', array_filter([
                          $r['RegisteredAgentCity'] ?? '',
                          $r['RegisteredAgentState'] ?? '',
                          $r['RegisteredAgentPostalCode'] ?? '',
                      ])),
                  ]);
                  echo htmlspecialchars(implode(', ', $raAddr));
                ?>
              </dd>
              <?php if ($r['MailingAddressLine1'] ?? ''): ?>
                <dt>Mailing Address</dt>
                <dd><?php
                  $mAddr = array_filter([
                      $r['MailingAddressLine1'],
                      $r['MailingAddressLine2'] ?? '',
                      implode(', ', array_filter([
                          $r['MailingAddressCity'] ?? '',
                          $r['MailingAddressState'] ?? '',
                          $r['MailingAddressPostalCode'] ?? '',
                      ])),
                  ]);
                  echo htmlspecialchars(implode(', ', $mAddr));
                ?></dd>
              <?php endif; ?>
            </div>
            <div>
              <?php if ($principals): ?>
                <dt>Principals / Officers</dt>
                <?php foreach ($principals as $p): ?>
                  <dd>
                    <strong><?= htmlspecialchars($p['PrincipalName'] ?? '') ?></strong>
                    <?php if ($p['Titles'] ?? ''): ?> — <?= htmlspecialchars($p['Titles']) ?><?php endif; ?>
                    <?php
                      $pAddr = array_filter([
                          $p['AddressLine1'] ?? '',
                          implode(', ', array_filter([
                              $p['City'] ?? '',
                              $p['StateProvince'] ?? '',
                              $p['PostalCode'] ?? '',
                          ])),
                      ]);
                      if ($pAddr): ?>
                        <br><span style="color:#888;"><?= htmlspecialchars(implode(', ', $pAddr)) ?></span>
                    <?php endif; ?>
                  </dd>
                <?php endforeach; ?>
              <?php endif; ?>
            </div>
          </div>
        </td>
      </tr>
      <?php endif; ?>
    <?php endforeach; ?>
    </tbody>
  </table>
<?php endif; ?>

</div>
</body>
</html>
