<?php
/**
 * OpenCorporates Search Tool
 * Standalone search interface for OpenCorporates with 2Captcha CAPTCHA solving.
 */
require_once __DIR__ . '/tools.php';

$config = require __DIR__ . '/config.php';
$query = trim($_GET['q'] ?? '');
$jurisdiction = trim($_GET['jurisdiction'] ?? '');
$fetchMethod = 'web_unlocker';
$results = null;
$error = null;
$elapsed = null;
$rawHtml = null;

$totalFound = null;

if ($query) {
    $tools = new LookupTools($config);
    $t0 = microtime(true);

    $baseUrl = 'https://opencorporates.com/companies?q=' . urlencode($query) . '&type=companies';
    $fetchFn = fn($u) => $tools->ocFetchWithCaptcha($u);

    $html = $fetchFn($baseUrl);
    $elapsed = microtime(true) - $t0;

    if (str_starts_with($html, 'Error:')) {
        $error = $html;
    } elseif (stripos($html, 'captcha_frame') !== false && stripos($html, '/companies/') === false) {
        $error = 'CAPTCHA was not solved — please try again.';
    } else {
        $rawHtml = $html;
        $results = $tools->parseOpenCorporatesResults($html);

        // Extract total count
        if (preg_match('/Found (\d+) compan/i', $html, $cm)) {
            $totalFound = (int) $cm[1];
        }

        // Pagination disabled to avoid excessive requests

        if (empty($results)) {
            $error = "No results found for \"{$query}\".";
        }
    }
}

// JSON API mode
if (isset($_GET['format']) && $_GET['format'] === 'json') {
    header('Content-Type: application/json');
    echo json_encode([
        'query' => $query,
        'jurisdiction' => $jurisdiction ?: null,
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
<title>OpenCorporates Search<?= $query ? ' — ' . htmlspecialchars($query) : '' ?></title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f5f5f7; color: #333; min-height: 100vh; }

  .header { background: #1a1a2e; color: #fff; padding: 24px 40px; display: flex; justify-content: space-between; align-items: center; }
  .header h1 { font-size: 22px; font-weight: 600; }
  .header h1 a { text-decoration: none; color: inherit; }
  .header p { font-size: 13px; color: #8a8aaf; margin-top: 4px; }
  .header nav a { color: #8a8aaf; text-decoration: none; font-size: 13px; margin-left: 16px; }
  .header nav a.active { color: #fff; }

  .content { max-width: 1100px; margin: 0 auto; padding: 32px 40px; }

  .search-form { display: flex; gap: 12px; margin-bottom: 24px; flex-wrap: wrap; }
  .search-input { flex: 1; min-width: 300px; padding: 12px 16px; font-size: 15px; border: 2px solid #e0e0e0; border-radius: 8px; outline: none; }
  .search-input:focus { border-color: #4a90d9; }
  .jurisdiction-input { width: 180px; padding: 12px 16px; font-size: 15px; border: 2px solid #e0e0e0; border-radius: 8px; outline: none; }
  .jurisdiction-input:focus { border-color: #4a90d9; }
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
  .company-name a { color: inherit; text-decoration: none; }
  .company-name a:hover { text-decoration: underline; color: #4a90d9; }
  .alt-names { font-size: 11px; color: #888; margin-top: 2px; }
  .trademarks { font-size: 11px; color: #6d28d9; margin-top: 2px; }

  .badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.3px; }
  .badge-active { background: #d4edda; color: #155724; }
  .badge-inactive { background: #f8d7da; color: #721c24; }
  .badge-unknown { background: #e2e8f0; color: #475569; }
  .badge-branch { background: #dbeafe; color: #1e40af; }

  .detailed-status { font-size: 10px; color: #888; display: block; margin-top: 2px; }
  .address { font-size: 12px; color: #555; max-width: 250px; }
  .jurisdiction { white-space: nowrap; }

  .oc-link { color: #4a90d9; text-decoration: none; font-size: 12px; }
  .oc-link:hover { text-decoration: underline; }

  .advanced-options { width: 100%; margin-top: -8px; }
  .advanced-options summary { font-size: 13px; color: #4a90d9; cursor: pointer; font-weight: 600; user-select: none; }
  .advanced-options summary:hover { text-decoration: underline; }
  .options-grid { display: flex; flex-wrap: wrap; gap: 20px 40px; padding: 12px 0 4px; font-size: 13px; }
  .option-check { display: flex; align-items: center; gap: 6px; cursor: pointer; }
  .option-group { display: flex; flex-direction: column; gap: 4px; }
  .option-group label { display: flex; align-items: center; gap: 6px; cursor: pointer; }
  .option-label { font-weight: 600; color: #555; font-size: 12px; text-transform: uppercase; letter-spacing: 0.3px; margin-bottom: 2px; }
</style>
</head>
<body>

<div class="header">
  <div>
    <h1><a href="/">Entity Lookup</a></h1>
    <p>Search 200M+ companies across 140+ jurisdictions</p>
  </div>
  <nav>
    <a href="/">Lookup</a>
    <a href="/oc.php" class="active">OpenCorporates</a>
    <a href="/settings.php">Settings</a>
  </nav>
</div>

<div class="content">

<form class="search-form" method="get">
  <div style="display:flex;gap:12px;width:100%;flex-wrap:wrap;">
    <input type="text" name="q" class="search-input" placeholder="Company name..." value="<?= htmlspecialchars($query) ?>" required autofocus>
    <input type="text" name="jurisdiction" class="jurisdiction-input" placeholder="Jurisdiction (e.g. gb)" value="<?= htmlspecialchars($jurisdiction) ?>" title="Optional: jurisdiction code like us_de, gb, de, etc.">
    <button type="submit" class="search-btn">Search</button>
  </div>
</form>

<?php if ($error): ?>
  <div class="error"><?= htmlspecialchars($error) ?></div>
<?php endif; ?>

<?php if ($results !== null && !empty($results)): ?>
  <div class="meta-bar">
    <span class="count"><?= count($results) ?><?= $totalFound && $totalFound > count($results) ? ' of ' . $totalFound : '' ?> result<?= count($results) !== 1 ? 's' : '' ?></span>
    <span>for "<?= htmlspecialchars($query) ?>"</span>
    <?php if ($jurisdiction): ?><span>in <?= htmlspecialchars($jurisdiction) ?></span><?php endif; ?>
    <?php if ($elapsed): ?><span><?= number_format($elapsed, 1) ?>s</span><?php endif; ?>
  </div>

  <table class="results-table">
    <thead>
      <tr>
        <th>Company Name</th>
        <th>Status</th>
        <th>Type</th>
        <th>Jurisdiction</th>
        <th>Company #</th>
        <th>Address</th>
        <th>Link</th>
      </tr>
    </thead>
    <tbody>
    <?php foreach ($results as $r): ?>
      <tr>
        <td>
          <div class="company-name"><a href="<?= htmlspecialchars($r['url']) ?>" target="_blank"><?= htmlspecialchars($r['name']) ?></a></div>
          <?php if (!empty($r['alternative_names'])): ?>
            <div class="alt-names">aka: <?= htmlspecialchars(implode(', ', $r['alternative_names'])) ?></div>
          <?php endif; ?>
          <?php if (!empty($r['trademarks'])): ?>
            <div class="trademarks">TM: <?= htmlspecialchars(implode(', ', $r['trademarks'])) ?></div>
          <?php endif; ?>
        </td>
        <td>
          <?php
            $statusClass = match(strtolower($r['status'])) {
              'active' => 'badge-active',
              'inactive' => 'badge-inactive',
              default => 'badge-unknown',
            };
          ?>
          <span class="badge <?= $statusClass ?>"><?= htmlspecialchars($r['status']) ?></span>
          <?php if ($r['detailed_status']): ?>
            <span class="detailed-status"><?= htmlspecialchars($r['detailed_status']) ?></span>
          <?php endif; ?>
        </td>
        <td>
          <?php if ($r['is_branch']): ?>
            <span class="badge badge-branch">Branch</span>
          <?php else: ?>
            <span style="font-size:12px;color:#555;">Domestic</span>
          <?php endif; ?>
        </td>
        <td class="jurisdiction">
          <?= htmlspecialchars($r['jurisdiction_name'] ?? $r['jurisdiction']) ?>
        </td>
        <td style="font-family:monospace;font-size:12px;">
          <?= htmlspecialchars($r['company_number']) ?>
        </td>
        <td>
          <?php if ($r['address']): ?>
            <div class="address"><?= htmlspecialchars($r['address']) ?></div>
          <?php else: ?>
            <span style="color:#ccc;">—</span>
          <?php endif; ?>
        </td>
        <td>
          <a href="<?= htmlspecialchars($r['url']) ?>" target="_blank" class="oc-link">View</a>
        </td>
      </tr>
    <?php endforeach; ?>
    </tbody>
  </table>
<?php endif; ?>

</div>
</body>
</html>
