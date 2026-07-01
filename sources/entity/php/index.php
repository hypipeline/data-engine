<?php
/**
 * Entity Lookup — Web Interface
 *
 * Usage: index.php?url=https://www.example.com/
 *        index.php?url=...&refresh=1  (break cache)
 *        index.php?url=...&delete=1   (delete from cache)
 *        index.php?format=json&url=...
 */

require_once __DIR__ . '/lookup.php';
require_once __DIR__ . '/cache.php';

function colorizeLogMsg(string $msg): string
{
    // Format JSON blocks as pretty-printed code blocks
    $safe = preg_replace_callback('/```json\s*([\s\S]*?)```|(\{[\s\S]*\}|\[[\s\S]*\])/', function ($m) {
        $json = $m[1] ?: $m[2];
        $decoded = json_decode($json);
        if ($decoded !== null) {
            $pretty = json_encode($decoded, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);
            return '<pre class="log-json">' . htmlspecialchars($pretty) . '</pre>';
        }
        return htmlspecialchars($m[0]);
    }, $msg);

    // If we already inserted <pre> blocks, only escape the non-HTML parts
    if (str_contains($safe, '<pre class="log-json">')) {
        // Split on pre blocks, escape only the text parts
        $parts = preg_split('/(<pre class="log-json">.*?<\/pre>)/s', $safe, -1, PREG_SPLIT_DELIM_CAPTURE);
        $safe = '';
        foreach ($parts as $part) {
            if (str_starts_with($part, '<pre class="log-json">')) {
                $safe .= $part;
            } else {
                $safe .= htmlspecialchars($part);
            }
        }
    } else {
        $safe = htmlspecialchars($msg);
    }

    $safe = preg_replace_callback('/HTTP (\d{3})/', function ($m) {
        $code = (int) $m[1];
        if ($code >= 500) $class = 'http-5xx';
        elseif ($code >= 400) $class = 'http-4xx';
        elseif ($code >= 300) $class = 'http-3xx';
        elseif ($code >= 200) $class = 'http-2xx';
        else $class = 'http-0';
        return "<span class=\"{$class}\">HTTP {$code}</span>";
    }, $safe);
    $safe = preg_replace('/HTTP 0(?![\d])/', '<span class="http-0">HTTP 0</span>', $safe);
    $safe = str_replace('[Browserbase]', '<span class="tag-browserbase">[Browserbase]</span>', $safe);
    $safe = str_replace('[Browserbase failed]', '<span class="tag-browserbase">[Browserbase failed]</span>', $safe);
    // Make URLs clickable (skip truncated URLs ending in ...)
    $safe = preg_replace_callback('#(https?://[^\s<]+)#', function ($m) {
        $url = $m[1];
        if (str_ends_with($url, '...')) return $url; // truncated, not a valid link
        return '<a href="' . $url . '" target="_blank" class="log-link">' . $url . '</a>';
    }, $safe);
    return $safe;
}

function renderExpandableDetail(?array $detail): string
{
    if (!$detail || empty($detail['expandable']) || empty($detail['sections'])) return '';
    $html = '<div class="log-expandable">';
    foreach ($detail['sections'] as $section) {
        $label = htmlspecialchars($section['label'] ?? 'Details');
        $raw = $section['content'] ?? '';
        // Try to pretty-print if content is JSON
        $decoded = json_decode($raw);
        if ($decoded !== null) {
            $content = htmlspecialchars(json_encode($decoded, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE));
            $html .= "<details><summary>{$label}</summary><pre class=\"log-json\">{$content}</pre></details>";
        } else {
            $content = htmlspecialchars($raw);
            $html .= "<details><summary>{$label}</summary><pre>{$content}</pre></details>";
        }
    }
    $html .= '</div>';
    return $html;
}

function renderLogEntry(array $entry): string
{
    if ($entry['phase'] === 'phase') {
        return "<div class=\"log-phase-header\">{$entry['message']}</div>\n";
    }
    if ($entry['phase'] === 'entity_header') {
        $num = $entry['detail']['entity_num'] ?? '';
        $total = $entry['detail']['entity_total'] ?? '';
        $name = htmlspecialchars($entry['message']);
        return "<div class=\"log-entity-header\"><span class=\"log-entity-num\">{$num}/{$total}</span>{$name}</div>\n";
    }
    $time = number_format($entry['time'], 1);
    $phase = htmlspecialchars($entry['phase']);
    // Show entity name in phase label if present
    $entityLabel = '';
    if (!empty($entry['detail']['entity_name'])) {
        $entityLabel = '<span class="log-phase-entity">' . htmlspecialchars($entry['detail']['entity_name']) . '</span>';
    }
    $msg = colorizeLogMsg($entry['message']);
    $expandable = renderExpandableDetail($entry['detail'] ?? null);
    return "<div class=\"log-entry\"><span class=\"log-time\">{$time}s</span><span class=\"log-phase log-phase-{$phase}\">{$phase}{$entityLabel}</span><span class=\"log-msg\">{$msg}{$expandable}</span></div>\n";
}

$cache = new LookupCache();
$url = $_GET['url'] ?? '';
$refresh = isset($_GET['refresh']);
$modelOverride = $_GET['model'] ?? '';

// Handle delete from cache
if (isset($_GET['delete']) && $url) {
    $cache->delete($url);
    header('Location: /');
    exit;
}

function buildEmbedHtml(array $report, string $url): string
{
    $entity = $report['recommended_entity'] ?? null;
    $entityName = $entity['legal_entity_name'] ?? 'No match found';
    $confidence = $report['confidence'] ?? 'insufficient';
    $rv = $report['registry_validation'] ?? null;

    $confColors = [
        'high' => ['bg' => '#d4edda', 'fg' => '#155724', 'border' => '#27ae60'],
        'medium' => ['bg' => '#fff3cd', 'fg' => '#856404', 'border' => '#f39c12'],
        'low' => ['bg' => '#ffeeba', 'fg' => '#856404', 'border' => '#e67e22'],
        'insufficient' => ['bg' => '#f8d7da', 'fg' => '#721c24', 'border' => '#e74c3c'],
    ];
    $cc = $confColors[$confidence] ?? $confColors['insufficient'];

    $rvBadge = '';
    if ($rv) {
        $rvStatus = $rv['status'] ?? '';
        $rvColors = match($rvStatus) {
            'verified' => ['bg' => '#d4edda', 'fg' => '#155724'],
            'name_match_bad_status' => ['bg' => '#ffeeba', 'fg' => '#856404'],
            default => ['bg' => '#f8d7da', 'fg' => '#721c24'],
        };
        $rvLabel = match($rvStatus) {
            'verified' => 'Registry Verified',
            'name_match_bad_status' => 'Inactive in Registry',
            'name_mismatch' => 'Registry Mismatch',
            'fictitious_name' => 'Fictitious Name',
            'branch_registration' => 'Branch Registration',
            default => 'Not Found in Registry',
        };
        $rvBadge = '<span style="display:inline-block;padding:3px 10px;border-radius:4px;font-size:11px;font-weight:700;text-transform:uppercase;background:' . $rvColors['bg'] . ';color:' . $rvColors['fg'] . ';">' . htmlspecialchars($rvLabel) . '</span>';
    }

    $warningBadge = '';
    if (!empty($report['validation_warning'])) {
        $warningBadge = '<span style="display:inline-block;padding:3px 10px;border-radius:4px;font-size:11px;font-weight:700;text-transform:uppercase;background:#f8d7da;color:#721c24;" title="' . htmlspecialchars($report['validation_warning']) . '">⚠ Validation Failed</span>';
    }

    $rows = '';
    if ($entity) {
        $fields = [
            'Jurisdiction' => htmlspecialchars($entity['jurisdiction_description'] ?? $entity['jurisdiction'] ?? '—'),
            'Registry ID' => htmlspecialchars($entity['registry_id'] ?? '—') . (!empty($entity['jurisdiction_state']) ? ' (' . htmlspecialchars($entity['jurisdiction_state']) . ')' : ''),
            'Address' => htmlspecialchars($entity['address'] ?? '—'),
            'Source' => htmlspecialchars($entity['source'] ?? '—'),
        ];
        foreach ($fields as $label => $value) {
            $rows .= '<div style="display:flex;padding:4px 0;font-size:14px;"><span style="width:140px;color:#888;flex-shrink:0;">' . $label . '</span><span style="color:#333;">' . $value . '</span></div>';
        }
    }

    $note = '';
    if (!empty($report['note'])) {
        $note = '<div style="font-size:13px;color:#555;line-height:1.6;background:#f8f8fc;padding:12px;border-radius:6px;margin-bottom:16px;">' . htmlspecialchars($report['note']) . '</div>';
    }

    $affiliatesHtml = '';
    $affiliates = $report['contractable_affiliates'] ?? [];
    if (!empty($affiliates)) {
        $affiliatesHtml .= '<div style="margin-top:16px;padding-top:12px;border-top:1px solid #eee;">'
            . '<div style="font-size:11px;font-weight:600;color:#888;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:8px;">Contractable Affiliates</div>';
        foreach ($affiliates as $ca) {
            $caName = htmlspecialchars($ca['legal_entity_name'] ?? '');
            $caValidated = $ca['registry_validated'] ?? false;
            if ($caValidated) {
                $caBadge = '<span style="display:inline-block;padding:2px 6px;border-radius:3px;font-size:10px;font-weight:700;text-transform:uppercase;background:#d4edda;color:#155724;">Verified</span>';
            } else {
                $caBadge = '<span style="display:inline-block;padding:2px 6px;border-radius:3px;font-size:10px;font-weight:700;text-transform:uppercase;background:#f8d7da;color:#721c24;">Failed</span>';
            }
            $caRole = !empty($ca['role']) ? '<div style="color:#888;font-size:12px;margin-left:0.5em;">' . htmlspecialchars($ca['role']) . '</div>' : '';
            $affiliatesHtml .= '<div style="padding:4px 0;font-size:13px;border-bottom:1px solid #f5f5f5;">'
                . '<strong>' . $caName . '</strong> ' . $caBadge . $caRole . '</div>';
        }
        $affiliatesHtml .= '</div>';
    }

    $domain = preg_replace('/^www\./', '', parse_url($url, PHP_URL_HOST) ?? '');

    return '<div style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,sans-serif;background:#fff;border-radius:12px;border:2px solid ' . $cc['border'] . ';overflow:hidden;max-width:600px;">'
        . '<div style="padding:20px;border-bottom:1px solid #f0f0f0;">'
        . '<div style="font-size:11px;color:#888;margin-bottom:4px;">' . htmlspecialchars($domain) . '</div>'
        . '<div style="font-size:20px;font-weight:700;color:#1a1a2e;">'
        . htmlspecialchars($entityName)
        . ' <span style="display:inline-block;padding:3px 10px;border-radius:4px;font-size:11px;font-weight:700;text-transform:uppercase;background:' . $cc['bg'] . ';color:' . $cc['fg'] . ';">' . $confidence . '</span>'
        . ' ' . $rvBadge . $warningBadge
        . '</div>'
        . '</div>'
        . '<div style="padding:20px;">'
        . $note
        . $rows
        . $affiliatesHtml
        . '</div>'
        . '</div>';
}

// JSON API mode (async)
if (isset($_GET['format']) && $_GET['format'] === 'json') {
    header('Content-Type: application/json');
    if (!$url || !filter_var($url, FILTER_VALIDATE_URL)) {
        http_response_code(400);
        echo json_encode(['status' => 'error', 'error' => 'Invalid or missing URL']);
        exit;
    }

    // When a model override is set, use model-specific cache key
    $cacheKey = $modelOverride ? "{$url}#model={$modelOverride}" : $url;

    // Return cached result immediately
    $cached = $cache->get($cacheKey);
    if ($cached && !$refresh) {
        $output = array_merge($cached['result'], ['status' => 'complete', 'from_cache' => true, 'cached_at' => $cached['cached_at']]);
        $output['embed_html'] = buildEmbedHtml($output['report'] ?? [], $url);
        echo json_encode($output, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES);
        exit;
    }

    // Already processing — tell caller to keep polling
    if ($cache->isLocked($cacheKey)) {
        http_response_code(202);
        echo json_encode(['status' => 'processing', 'message' => 'Lookup in progress. Poll this URL until status is complete.']);
        exit;
    }

    // Not cached, not processing — kick off background lookup
    // Send "processing" response immediately, then run lookup after connection closes
    ignore_user_abort(true);
    $cache->lock($cacheKey);

    http_response_code(202);
    echo json_encode(['status' => 'processing', 'message' => 'Lookup started. Poll this URL until status is complete.']);

    // Flush response to client and close connection
    if (function_exists('fastcgi_finish_request')) {
        fastcgi_finish_request();
    } else {
        header('Connection: close');
        header('Content-Length: ' . ob_get_length());
        if (ob_get_level()) ob_end_flush();
        flush();
    }

    // Run lookup in background
    try {
        $config = require __DIR__ . '/config.php';
        if ($modelOverride) $config['model'] = $modelOverride;
        $lookup = new EntityLookup($config);
        $result = $lookup->run($url);
        $cache->set($cacheKey, $result);
    } catch (Throwable $e) {
        // Nothing to send — caller will poll and get not-found
    } finally {
        $cache->unlock($cacheKey);
    }
    exit;
}

// Determine state
$result = null;
$error = null;
$fromCache = false;
$cachedAt = null;
$needsLookup = false;

if ($url) {
    if (!filter_var($url, FILTER_VALIDATE_URL)) {
        $error = 'Invalid URL provided.';
    } else {
        if (!$refresh) {
            $cached = $cache->get($url);
            if ($cached) {
                $result = $cached['result'];
                $fromCache = true;
                $cachedAt = $cached['cached_at'];
            }
        }
        if (!$result && !$error) {
            if ($cache->isLocked($url)) {
                $error = 'A lookup for this URL is already in progress. Please wait and reload shortly.';
            } else {
                $needsLookup = true;
            }
        }
    }
}

// Get history for sidebar
$history = $cache->getAll();
$totalCost = $cache->getTotalCost();
$totalLookups = $cache->getCount();

// Start outputting HTML
?>
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Entity Lookup<?= $url ? ' — ' . htmlspecialchars(preg_replace('/^www\./', '', parse_url($url, PHP_URL_HOST) ?? '')) : '' ?></title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><circle cx='26' cy='26' r='18' fill='none' stroke='%231a1a2e' stroke-width='5'/><line x1='39' y1='39' x2='56' y2='56' stroke='%231a1a2e' stroke-width='5' stroke-linecap='round'/><circle cx='26' cy='26' r='8' fill='%2327ae60' opacity='0.6'/></svg>">
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f5f5f7; color: #333; min-height: 100vh; }

  .header { background: #1a1a2e; color: #fff; padding: 24px 40px; display: flex; justify-content: space-between; align-items: center; }
  .header h1 { font-size: 22px; font-weight: 600; }
  .header h1 a { text-decoration: none; color: inherit; }
  .header p { font-size: 13px; color: #8a8aaf; margin-top: 4px; }
  .header-stats { display: flex; gap: 20px; }
  .header-stat { text-align: center; }
  .header-stat-value { font-size: 18px; font-weight: 700; color: #3fb950; }
  .header-stat-label { font-size: 10px; color: #8a8aaf; text-transform: uppercase; }

  .main { display: flex; min-height: calc(100vh - 80px); }
  .content { flex: 1; padding: 32px 40px; }
  .sidebar { width: 320px; background: #fff; border-left: 1px solid #e0e0e0; padding: 24px; overflow-y: auto; max-height: calc(100vh - 80px); }

  .search-form { display: flex; gap: 12px; max-width: 800px; margin-bottom: 24px; }
  .search-input { flex: 1; padding: 12px 16px; font-size: 15px; border: 2px solid #e0e0e0; border-radius: 8px; outline: none; }
  .search-input:focus { border-color: #4a90d9; }
  .model-select { padding: 10px 12px; font-size: 13px; border: 2px solid #e0e0e0; border-radius: 8px; outline: none; background: #fff; color: #555; cursor: pointer; min-width: 160px; }
  .model-select:focus { border-color: #4a90d9; }
  .search-btn { padding: 12px 28px; background: #4a90d9; color: #fff; border: none; border-radius: 8px; font-size: 15px; font-weight: 600; cursor: pointer; }
  .search-btn:hover { background: #3a7bc8; }

  .error { background: #fef2f2; border: 1px solid #fecaca; border-radius: 8px; padding: 16px; color: #991b1b; margin-bottom: 20px; }

  .cache-banner { background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 8px; padding: 12px 16px; margin-bottom: 16px; display: flex; justify-content: space-between; align-items: center; font-size: 13px; color: #1e40af; }
  .cache-banner a { color: #1e40af; font-weight: 600; text-decoration: underline; }
  .cache-banner .delete-link { color: #991b1b; }

  .report-card { background: #fff; border-radius: 12px; border: 2px solid #e0e0e0; overflow: hidden; max-width: 900px; }
  .report-card.conf-high { border-color: #27ae60; }
  .report-card.conf-medium { border-color: #f39c12; }
  .report-card.conf-low { border-color: #e67e22; }
  .report-card.conf-insufficient { border-color: #e74c3c; }

  .report-header { padding: 24px; border-bottom: 1px solid #f0f0f0; }
  .report-entity { font-size: 22px; font-weight: 700; color: #1a1a2e; }
  .report-meta { display: flex; gap: 16px; margin-top: 10px; font-size: 13px; color: #666; flex-wrap: wrap; }
  .report-meta span { display: inline-flex; align-items: center; gap: 4px; }
  .cost-badge { background: #d4edda; color: #155724; padding: 2px 8px; border-radius: 4px; font-weight: 700; }

  .badge { display: inline-block; padding: 3px 10px; border-radius: 4px; font-size: 11px; font-weight: 700; text-transform: uppercase; }
  .badge-high { background: #d4edda; color: #155724; }
  .badge-medium { background: #fff3cd; color: #856404; }
  .badge-low { background: #ffeeba; color: #856404; }
  .badge-insufficient { background: #f8d7da; color: #721c24; }
  .badge-neutral { background: #e2e3e5; color: #383d41; }

  .report-body { padding: 24px; }
  .report-section { margin-bottom: 20px; }
  .report-section h3 { font-size: 13px; font-weight: 600; color: #888; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 8px; }
  .report-row { display: flex; padding: 4px 0; font-size: 14px; }
  .report-label { width: 140px; color: #888; flex-shrink: 0; }
  .report-value { color: #333; }
  .report-note { font-size: 13px; color: #555; line-height: 1.6; background: #f8f8fc; padding: 12px; border-radius: 6px; }

  .evidence-item { font-size: 13px; padding: 6px 0; border-bottom: 1px solid #f5f5f5; }
  .evidence-item:last-child { border-bottom: none; }
  .evidence-step { font-weight: 600; }
  .evidence-link { color: #4a90d9; text-decoration: none; }

  .report-timing { margin-top: 20px; padding-top: 16px; border-top: 1px solid #eee; font-size: 12px; color: #888; }

  .raw-json { margin-top: 24px; }
  .raw-json summary { cursor: pointer; font-size: 13px; color: #4a90d9; font-weight: 600; }
  .raw-json pre { margin-top: 8px; padding: 16px; background: #1a1a2e; color: #c9d1d9; border-radius: 8px; font-size: 11px; line-height: 1.5; overflow-x: auto; max-height: 500px; overflow-y: auto; }

  .progress-log { margin-top: 24px; background: #fff; border-radius: 12px; border: 1px solid #e0e0e0; overflow: hidden; max-width: 900px; }
  .progress-log-header { padding: 16px 20px; font-size: 14px; font-weight: 600; border-bottom: 1px solid #e0e0e0; background: #f8f8fc; }
  .progress-log-body { padding: 0; max-height: 600px; overflow-y: auto; }
  .log-entry { display: flex; gap: 10px; padding: 6px 20px; border-bottom: 1px solid #f5f5f5; font-size: 12px; font-family: 'SF Mono', 'Fira Code', monospace; line-height: 1.6; }
  .log-entry:last-child { border-bottom: none; }
  .log-time { color: #888; width: 50px; flex-shrink: 0; text-align: right; }
  .log-phase { width: 90px; flex-shrink: 0; font-weight: 600; text-transform: uppercase; font-size: 10px; padding-top: 2px; }
  .log-phase-start { color: #4a90d9; }
  .log-phase-phase { color: #1a1a2e; }
  .log-phase-fetch { color: #8b5cf6; }
  .log-phase-extract { color: #d97706; }
  .log-phase-llm { color: #d97706; }
  .log-phase-registry { color: #059669; }
  .log-phase-ch { color: #059669; }
  .log-phase-sec { color: #0369a1; }
  .log-phase-edgar { color: #6d28d9; }
  .log-phase-delaware { color: #b45309; }
  .log-phase-bizapedia { color: #0891b2; }
  .log-phase-northdata { color: #be185d; }
  .log-phase-crossref { color: #0d9488; }
  .log-phase-validate { color: #7c3aed; }
  .log-phase-sec_iapd { color: #0369a1; }
  .log-phase-brightdata { color: #e67e22; }
  .log-phase-scraping_browser { color: #e67e22; }
  .log-phase-done { color: #27ae60; }
  .log-json { background: #1e1e2e; color: #a6e3a1; padding: 10px 14px; border-radius: 6px; font-size: 11.5px; line-height: 1.5; margin: 6px 0 2px; overflow-x: auto; white-space: pre; }
  .log-msg { color: #333; flex: 1; word-break: break-word; }
  .log-msg-text { white-space: pre-wrap; display: block; }
  .log-msg-text.collapsed { max-height: 7.8em; overflow: hidden; position: relative; }
  .log-msg-text.collapsed::after { content: ''; position: absolute; bottom: 0; left: 0; right: 0; height: 1.5em; background: linear-gradient(transparent, #fff); }
  .log-toggle { color: #4a90d9; cursor: pointer; font-size: 11px; font-weight: 600; margin-top: 2px; display: block; user-select: none; }
  .log-toggle:hover { text-decoration: underline; }
  .log-detail { margin-top: 2px; color: #666; font-size: 11px; }

  .log-phase-header { background: #f0f0f5; padding: 8px 20px; font-size: 13px; font-weight: 700; color: #1a1a2e; border-bottom: 1px solid #e0e0e0; border-top: 1px solid #e0e0e0; letter-spacing: 0.3px; }
  .log-phase-header:first-child { border-top: none; }

  .log-entity-header { background: #e8f5e9; padding: 6px 20px; font-size: 12px; font-weight: 700; color: #1b5e20; border-bottom: 1px solid #c8e6c9; border-top: 2px solid #a5d6a7; display: flex; align-items: center; gap: 10px; font-family: 'SF Mono', 'Fira Code', monospace; }
  .log-entity-num { background: #1b5e20; color: #fff; padding: 1px 7px; border-radius: 4px; font-size: 10px; font-weight: 700; flex-shrink: 0; }
  .log-phase-entity { display: block; font-size: 9px; font-weight: 400; color: #666; margin-top: 1px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 80px; }

  .log-expandable { margin-top: 4px; }
  .log-expandable details { margin-top: 2px; }
  .log-expandable summary { cursor: pointer; font-size: 11px; color: #4a90d9; font-weight: 600; user-select: none; padding: 1px 0; }
  .log-expandable summary:hover { text-decoration: underline; }
  .log-expandable pre { margin-top: 4px; padding: 10px 12px; background: #1a1a2e; color: #c9d1d9; border-radius: 6px; font-size: 11px; line-height: 1.5; overflow-x: auto; max-height: 400px; overflow-y: auto; white-space: pre-wrap; word-break: break-word; }

  .http-2xx { color: #16a34a; font-weight: 700; }
  .http-3xx { color: #2563eb; font-weight: 700; }
  .http-4xx { color: #dc2626; font-weight: 700; }
  .http-5xx { color: #7c3aed; font-weight: 700; }
  .http-0 { color: #991b1b; font-weight: 700; }
  .tag-browserbase { color: #d97706; font-weight: 700; }
  .log-link { color: #4a90d9; text-decoration: none; }
  .log-link:hover { text-decoration: underline; }

  .sidebar-search { display: flex; gap: 6px; margin-bottom: 16px; }
  .sidebar-search-input { flex: 1; padding: 8px 10px; font-size: 12px; border: 2px solid #e0e0e0; border-radius: 6px; outline: none; font-family: inherit; }
  .sidebar-search-input:focus { border-color: #4a90d9; }
  .sidebar-search-btn { padding: 8px 14px; background: #4a90d9; color: #fff; border: none; border-radius: 6px; font-size: 12px; font-weight: 600; cursor: pointer; }
  .sidebar-search-btn:hover { background: #3a7bc8; }
  .sidebar h2 { font-size: 14px; font-weight: 600; color: #1a1a2e; margin-bottom: 16px; text-transform: uppercase; letter-spacing: 0.5px; }
  .sidebar-stats { display: flex; gap: 12px; margin-bottom: 20px; }
  .sidebar-stat { flex: 1; background: #f8f8fc; border-radius: 6px; padding: 10px; text-align: center; }
  .sidebar-stat-value { font-size: 16px; font-weight: 700; color: #1a1a2e; }
  .sidebar-stat-label { font-size: 10px; color: #888; text-transform: uppercase; }

  .history-item { padding: 12px; border: 1px solid #e0e0e0; border-radius: 8px; margin-bottom: 8px; cursor: pointer; transition: background 0.15s; }
  .history-item:hover { background: #f0f4ff; }
  .history-item-name { font-size: 13px; font-weight: 600; color: #1a1a2e; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .history-item-entity { font-size: 12px; color: #555; margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .history-item-meta { display: flex; justify-content: space-between; margin-top: 4px; font-size: 11px; color: #888; }
  .history-item a { text-decoration: none; color: inherit; display: block; }

  .tools-section { margin-bottom: 24px; padding-bottom: 20px; border-bottom: 1px solid #e0e0e0; }
  .tools-section h2 { font-size: 14px; font-weight: 600; color: #1a1a2e; margin-bottom: 12px; text-transform: uppercase; letter-spacing: 0.5px; }
  .tool-link { display: flex; align-items: center; gap: 10px; padding: 10px 12px; border: 1px solid #e0e0e0; border-radius: 8px; margin-bottom: 6px; text-decoration: none; color: #333; transition: background 0.15s; font-size: 13px; }
  .tool-link:hover { background: #f0f4ff; border-color: #bfdbfe; }
  .tool-icon { width: 28px; height: 28px; border-radius: 6px; display: flex; align-items: center; justify-content: center; font-size: 14px; flex-shrink: 0; }
  .tool-icon-company { background: #dbeafe; color: #1e40af; }
  .tool-icon-tm { background: #ede9fe; color: #6d28d9; }
  .tool-icon-val { background: #fef3c7; color: #92400e; }

  .tool-name { font-weight: 600; }
  .tool-desc { font-size: 11px; color: #888; }
</style>
</head>
<body>

<div class="header">
  <div>
    <h1><a href="/">Entity Lookup</a></h1>
    <p>Identify the contracting legal entity from a company website</p>
    <?php
      $build = json_decode(@file_get_contents(__DIR__ . '/build.json'), true);
      $currentSettings = json_decode(file_get_contents(__DIR__ . '/settings.json'), true);
      $currentModel = $currentSettings['model'] ?? 'unknown';
    ?>
    <p style="font-size:13px;color:#aaa;margin:4px 0 0;">
      Model: <span style="color:#4a90d9"><?= htmlspecialchars($currentModel) ?></span><?php
      if ($build && $build['sha'] !== 'dev'):
        $dt = new DateTime($build['time'], new DateTimeZone('UTC'));
        $dt->setTimezone(new DateTimeZone('Europe/London'));
      ?> · Build: <?= substr($build['sha'], 0, 7) ?> · <?= $dt->format('H:i, j M Y') ?><?php endif; ?>
    </p>
  </div>
  <nav>
    <a href="/" style="color:#fff;text-decoration:none;font-size:13px;margin-left:16px;">Lookup</a>
    <a href="/compare.php" style="color:#8a8aaf;text-decoration:none;font-size:13px;margin-left:16px;">Compare</a>
    <a href="/settings.php" style="color:#8a8aaf;text-decoration:none;font-size:13px;margin-left:16px;">Settings</a>
  </nav>
  <div class="header-stats">
    <div class="header-stat">
      <div class="header-stat-value"><?= $totalLookups ?></div>
      <div class="header-stat-label">Lookups</div>
    </div>
    <div class="header-stat">
      <div class="header-stat-value">$<?= number_format($totalCost, 2) ?></div>
      <div class="header-stat-label">Total Cost</div>
    </div>
    <div class="header-stat">
      <div class="header-stat-value">$<?= $totalLookups > 0 ? number_format($totalCost / $totalLookups, 2) : '0.00' ?></div>
      <div class="header-stat-label">Avg Cost</div>
    </div>
  </div>
</div>

<div class="main">
<div class="content">

<?php
  $models = [
      'Claude' => [
          'claude-sonnet-4-6' => 'Sonnet 4.6',
          'claude-sonnet-4-5-20250514' => 'Sonnet 4.5',
          'claude-haiku-4-5-20251001' => 'Haiku 4.5',
          'claude-opus-4-6' => 'Opus 4.6',
      ],
      'OpenAI' => [
          'gpt-4o' => 'GPT-4o',
          'gpt-4o-mini' => 'GPT-4o Mini',
          'o3' => 'o3',
          'o4-mini' => 'o4-mini',
      ],
  ];
  $settings = json_decode(file_get_contents(__DIR__ . '/settings.json'), true);
  $defaultModel = $settings['model'] ?? '';
?>
<form class="search-form" method="get">
  <input type="url" name="url" class="search-input" placeholder="https://www.example.com/" value="<?= htmlspecialchars($url) ?>" required>
  <select name="model" class="model-select">
    <option value="">Default (<?= htmlspecialchars($models['Claude'][$defaultModel] ?? $models['OpenAI'][$defaultModel] ?? $defaultModel) ?>)</option>
    <?php foreach ($models as $group => $groupModels): ?>
      <optgroup label="<?= $group ?>">
        <?php foreach ($groupModels as $id => $label): ?>
          <option value="<?= $id ?>" <?= $id === $modelOverride ? 'selected' : '' ?>><?= $label ?></option>
        <?php endforeach; ?>
      </optgroup>
    <?php endforeach; ?>
  </select>
  <button type="submit" class="search-btn">Lookup</button>
</form>

<?php if ($error): ?>
  <div class="error"><?= htmlspecialchars($error) ?></div>

<?php elseif ($needsLookup): ?>
  <?php
    // Flush the page so far (header + form + progress container start)
    // Then stream progress as it happens
  ?>
  <div class="progress-log">
    <div class="progress-log-header">Live Progress</div>
    <div class="progress-log-body" id="progress-body">
  <?php
    // Disable output buffering and flush what we have so far
    if (ob_get_level()) ob_end_flush();
    flush();

    // Progress callback that flushes each entry to the browser
    $progressCallback = function(array $entry) {
        echo renderLogEntry($entry);
        flush();
    };

    ignore_user_abort(true);
    $cache->lock($url);
    $config = require __DIR__ . '/config.php';
    if ($modelOverride) $config['model'] = $modelOverride;
    $lookup = new EntityLookup($config, $progressCallback);
    try {
        $result = $lookup->run($url);
        $cache->set($url, $result);
    } catch (Throwable $e) {
        $error = 'Lookup failed: ' . $e->getMessage();
    } finally {
        $cache->unlock($url);
    }
  ?>
    </div>
  </div>
  <?php if ($refresh): ?>
  <script>history.replaceState(null, '', '?url=<?= urlencode($url) ?>');</script>
  <?php endif; ?>

  <?php if ($error): ?>
    <div class="error"><?= htmlspecialchars($error) ?></div>
  <?php endif; ?>

<?php elseif ($fromCache): ?>
  <div class="cache-banner">
    <span>Cached result from <?= htmlspecialchars($cachedAt) ?></span>
    <span>
      <a href="?url=<?= urlencode($url) ?>&refresh=1">Refresh</a>
      &nbsp;|&nbsp;
      <a href="?url=<?= urlencode($url) ?>&delete=1" class="delete-link">Delete</a>
    </span>
  </div>
<?php endif; ?>

<?php if ($result): ?>
  <?php
    $report = $result['report'];
    $meta = $result['meta'];
    $entity = $report['recommended_entity'] ?? null;
    $confidence = $report['confidence'] ?? 'insufficient';
    $entityName = $entity['legal_entity_name'] ?? 'No match found';
    $cost = $meta['cost_usd'] ?? 0;
  ?>

  <div class="report-card conf-<?= $confidence ?>">
    <div class="report-header">
      <div class="report-entity">
        <?= htmlspecialchars($entityName) ?>
        <span class="badge badge-<?= $confidence ?>"><?= $confidence ?></span>
      </div>
      <div class="report-meta">
        <?php if ($entity): ?>
          <span><?= htmlspecialchars($entity['jurisdiction_description'] ?? $entity['jurisdiction'] ?? '') ?></span>
          <?php if (!empty($entity['registry_id'])): ?>
            <span><?= htmlspecialchars($entity['registry_id']) ?></span>
          <?php endif; ?>
          <?php
            $rv = $report['registry_validation'] ?? null;
            if ($rv):
              $rvStatus = $rv['status'] ?? '';
              $rvClass = match($rvStatus) {
                'verified' => 'badge-high',
                'name_match_bad_status' => 'badge-low',
                'name_mismatch' => 'badge-insufficient',
                default => 'badge-insufficient',
              };
              $rvLabel = match($rvStatus) {
                'verified' => 'Registry Verified',
                'name_match_bad_status' => 'Inactive in Registry',
                'name_mismatch' => 'Registry Mismatch',
                'fictitious_name' => 'Fictitious Name',
                'branch_registration' => 'Branch Registration',
                default => 'Not Found in Registry',
              };
          ?>
            <?php if (!empty($rv['validation_url'])): ?>
              <a href="<?= htmlspecialchars($rv['validation_url']) ?>" target="_blank" class="badge <?= $rvClass ?>" title="<?= htmlspecialchars($rv['message'] ?? '') ?>" style="text-decoration:none;"><?= $rvLabel ?></a>
            <?php else: ?>
              <span class="badge <?= $rvClass ?>" title="<?= htmlspecialchars($rv['message'] ?? '') ?>"><?= $rvLabel ?></span>
            <?php endif; ?>
          <?php endif; ?>
          <?php if (!empty($report['validation_warning'])): ?>
            <span class="badge badge-insufficient" title="<?= htmlspecialchars($report['validation_warning']) ?>">⚠ Validation Failed</span>
          <?php endif; ?>
        <?php endif; ?>
        <span class="cost-badge">$<?= number_format($cost, 2) ?></span>
        <span><?= round($meta['total_time_s'], 1) ?>s</span>
        <span><?= number_format($meta['input_tokens'] ?? 0) ?> in / <?= number_format($meta['output_tokens'] ?? 0) ?> out tokens</span>
        <span><?= htmlspecialchars($meta['model']) ?></span>
        <a href="/?url=<?= urlencode($url) ?>&format=json" target="_blank" class="evidence-link">View API</a>
      </div>
    </div>

    <div class="report-body">
      <?php if (!empty($report['note'])): ?>
        <div class="report-section">
          <div class="report-note"><?= htmlspecialchars($report['note']) ?></div>
        </div>
      <?php endif; ?>

      <?php if ($entity): ?>
        <div class="report-section">
          <h3>Entity Details</h3>
          <div class="report-row"><span class="report-label">Name</span><span class="report-value"><?= htmlspecialchars($entity['legal_entity_name']) ?></span></div>
          <div class="report-row"><span class="report-label">Jurisdiction</span><span class="report-value"><?= htmlspecialchars($entity['jurisdiction_description'] ?? $entity['jurisdiction'] ?? '—') ?></span></div>
          <div class="report-row"><span class="report-label">Registry ID</span><span class="report-value"><?= htmlspecialchars($entity['registry_id'] ?? '—') ?><?php if (!empty($entity['jurisdiction_state'])): ?> (<?= htmlspecialchars($entity['jurisdiction_state']) ?>)<?php endif; ?></span></div>
          <div class="report-row"><span class="report-label">Address</span><span class="report-value"><?= htmlspecialchars($entity['address'] ?? '—') ?></span></div>
          <div class="report-row"><span class="report-label">Source</span><span class="report-value"><a href="<?= htmlspecialchars($entity['source_url'] ?? '#') ?>" target="_blank" class="evidence-link"><?= htmlspecialchars($entity['source'] ?? '—') ?></a></span></div>
        </div>
      <?php endif; ?>

      <?php if (!empty($report['evidence_forward'])): ?>
        <div class="report-section">
          <h3>Forward Evidence (<?= count($report['evidence_forward']) ?>)</h3>
          <?php foreach ($report['evidence_forward'] as $ev): ?>
            <div class="evidence-item">
              <span class="evidence-step"><?= htmlspecialchars($ev['step'] ?? '') ?></span>
              <span> — <?= htmlspecialchars($ev['description'] ?? '') ?></span>
              <?php if (!empty($ev['source_url'])): ?>
                <a href="<?= htmlspecialchars($ev['source_url']) ?>" target="_blank" class="evidence-link">[src]</a>
              <?php endif; ?>
            </div>
          <?php endforeach; ?>
        </div>
      <?php endif; ?>

      <?php if (!empty($report['evidence_reverse'])): ?>
        <div class="report-section">
          <h3>Reverse Validation (<?= count($report['evidence_reverse']) ?>)</h3>
          <?php foreach ($report['evidence_reverse'] as $ev): ?>
            <div class="evidence-item">
              <span class="evidence-step"><?= htmlspecialchars($ev['step'] ?? '') ?></span>
              <span class="badge badge-<?= $ev['strength'] ?? 'none' ?>"><?= $ev['strength'] ?? '—' ?></span>
              <span> — <?= htmlspecialchars($ev['description'] ?? '') ?></span>
            </div>
          <?php endforeach; ?>
        </div>
      <?php endif; ?>

      <?php if (!empty($report['key_people'])): ?>
        <div class="report-section">
          <h3>Key People (<?= count($report['key_people']) ?>)</h3>
          <?php foreach ($report['key_people'] as $p): ?>
            <div class="evidence-item"><?= htmlspecialchars($p['name'] ?? '') ?> — <?= htmlspecialchars($p['role'] ?? '') ?></div>
          <?php endforeach; ?>
        </div>
      <?php endif; ?>

      <?php if (!empty($report['contractable_affiliates'])): ?>
        <div class="report-section">
          <h3>Contractable Affiliates (<?= count($report['contractable_affiliates']) ?>)</h3>
          <?php foreach ($report['contractable_affiliates'] as $ca): ?>
            <div class="evidence-item">
              <strong><?= htmlspecialchars($ca['legal_entity_name'] ?? '') ?></strong>
              <?php
                $caValidated = $ca['registry_validated'] ?? false;
                $caVStatus = $ca['validation_status'] ?? '';
                if ($caValidated):
              ?>
                <?php if (!empty($ca['validation_url'])): ?>
                  <a href="<?= htmlspecialchars($ca['validation_url']) ?>" target="_blank" class="badge badge-high" style="text-decoration:none;">Registry Verified</a>
                <?php else: ?>
                  <span class="badge badge-high">Registry Verified</span>
                <?php endif; ?>
              <?php else: ?>
                <?php
                  $caFailLabel = match($caVStatus) {
                      'inactive' => 'Inactive in Registry',
                      'name_mismatch' => 'Registry Name Mismatch' . (!empty($ca['registry_name']) ? ' ("' . htmlspecialchars($ca['registry_name']) . '")' : ''),
                      'not_found' => 'Not Found in Registry',
                      'no_registry_id' => 'No Registry ID',
                      default => 'Validation Failed',
                  };
                ?>
                <?php if (!empty($ca['validation_url'])): ?>
                  <a href="<?= htmlspecialchars($ca['validation_url']) ?>" target="_blank" class="badge badge-insufficient" style="text-decoration:none;"><?= $caFailLabel ?></a>
                <?php else: ?>
                  <span class="badge badge-insufficient"><?= $caFailLabel ?></span>
                <?php endif; ?>
              <?php endif; ?>
              <?php if (!empty($ca['jurisdiction_country'])): ?>
                <span class="badge badge-neutral"><?= htmlspecialchars($ca['jurisdiction_country']) ?><?php if (!empty($ca['jurisdiction_state'])): ?>/<?= htmlspecialchars($ca['jurisdiction_state']) ?><?php endif; ?></span>
              <?php endif; ?>
              <?php if (!empty($ca['registry_id'])): ?>
                <span style="color:#666;"> — #<?= htmlspecialchars($ca['registry_id']) ?></span>
              <?php endif; ?>
              <?php if (!empty($ca['validation_source'])): ?>
                <span style="color:#666; font-size:0.85em;"> (<?= htmlspecialchars($ca['validation_source']) ?>)</span>
              <?php endif; ?>
              <?php if (!empty($ca['role'])): ?>
                <div style="color:#888; margin-left:1em; font-size:0.9em;"><?= htmlspecialchars($ca['role']) ?></div>
              <?php endif; ?>
            </div>
          <?php endforeach; ?>
        </div>
      <?php endif; ?>

      <?php if (!empty($report['other_entities'])): ?>
        <div class="report-section">
          <h3>Other Entities Considered (<?= count($report['other_entities']) ?>)</h3>
          <?php foreach ($report['other_entities'] as $oe): ?>
            <div class="evidence-item">
              <strong><?= htmlspecialchars($oe['legal_entity_name'] ?? '') ?></strong>
              <?php if (!empty($oe['jurisdiction_country'])): ?>
                <span class="badge badge-neutral"><?= htmlspecialchars($oe['jurisdiction_country']) ?><?php if (!empty($oe['jurisdiction_state'])): ?>/<?= htmlspecialchars($oe['jurisdiction_state']) ?><?php endif; ?></span>
              <?php endif; ?>
              <?php if (!empty($oe['registry_id'])): ?>
                <span style="color:#666;"> — #<?= htmlspecialchars($oe['registry_id']) ?></span>
              <?php endif; ?>
              <?php if (!empty($oe['why_not_recommended'])): ?>
                <div style="color:#888; margin-left:1em; font-size:0.9em;"><?= htmlspecialchars($oe['why_not_recommended']) ?></div>
              <?php endif; ?>
              <?php if (!empty($oe['verify_url'])): ?>
                <a href="<?= htmlspecialchars($oe['verify_url']) ?>" target="_blank" class="evidence-link" style="margin-left:1em;">[verify]</a>
              <?php endif; ?>
            </div>
          <?php endforeach; ?>
        </div>
      <?php endif; ?>

      <div class="report-timing">
        Completed in <?= round($meta['total_time_s'], 1) ?>s
        (fetch: <?= round($meta['phase_times']['fetch'] ?? 0, 1) ?>s,
         extract: <?= round($meta['phase_times']['extraction'] ?? 0, 1) ?>s,
         registries: <?= round($meta['phase_times']['registries'] ?? 0, 1) ?>s,
         analysis: <?= round($meta['phase_times']['analysis'] ?? 0, 1) ?>s<?php if (!empty($meta['phase_times']['reanalysis'])): ?>,
         reanalysis: <?= round($meta['phase_times']['reanalysis'], 1) ?>s<?php endif; ?>)
        | Cost: $<?= number_format($cost, 2) ?>
        (<?= number_format($meta['input_tokens'] ?? 0) ?> input + <?= number_format($meta['output_tokens'] ?? 0) ?> output tokens)
        <?php if (!empty($meta['api_calls'])):
          $ac = $meta['api_calls'];
          $parts = [];
          foreach ($ac as $svc => $count) {
            if ($count > 0) $parts[] = "{$count} {$svc}";
          }
          if ($parts): ?>
        | API calls: <?= implode(', ', $parts) ?>
          <?php endif; endif; ?>
      </div>
    </div>
  </div>

  <?php if (!empty($report['original_report'])): ?>
    <?php
      $origReport = $report['original_report'];
      $origEntity = $origReport['recommended_entity'] ?? null;
      $origConfidence = $origReport['confidence'] ?? 'insufficient';
      $origEntityName = $origEntity['legal_entity_name'] ?? 'No match found';
      $origRv = $origReport['registry_validation'] ?? null;
    ?>
    <div class="report-card conf-<?= $origConfidence ?>" style="opacity: 0.7; margin-top: 16px;">
      <div class="report-header">
        <div class="report-entity">
          <span style="font-size: 11px; text-transform: uppercase; color: #999; letter-spacing: 1px;">Original Analysis (before re-analysis)</span><br>
          <?= htmlspecialchars($origEntityName) ?>
          <span class="badge badge-<?= $origConfidence ?>"><?= $origConfidence ?></span>
        </div>
        <div class="report-meta">
          <?php if ($origEntity): ?>
            <span><?= htmlspecialchars($origEntity['jurisdiction_description'] ?? '') ?></span>
            <?php if (!empty($origEntity['registry_id'])): ?>
              <span><?= htmlspecialchars($origEntity['registry_id']) ?></span>
            <?php endif; ?>
            <?php if ($origRv):
              $oRvStatus = $origRv['status'] ?? '';
              $oRvClass = match($oRvStatus) {
                'verified' => 'badge-high',
                'name_match_bad_status' => 'badge-low',
                default => 'badge-insufficient',
              };
              $oRvLabel = match($oRvStatus) {
                'verified' => 'Registry Verified',
                'name_match_bad_status' => 'Inactive in Registry',
                'name_mismatch' => 'Registry Mismatch',
                'fictitious_name' => 'Fictitious Name',
                'branch_registration' => 'Branch Registration',
                default => 'Not Found in Registry',
              };
            ?>
              <span class="badge <?= $oRvClass ?>" title="<?= htmlspecialchars($origRv['message'] ?? '') ?>"><?= $oRvLabel ?></span>
            <?php endif; ?>
          <?php endif; ?>
        </div>
      </div>
      <div class="report-body">
        <?php if (!empty($origReport['note'])): ?>
          <div class="report-section">
            <div class="report-note"><?= htmlspecialchars($origReport['note']) ?></div>
          </div>
        <?php endif; ?>
        <?php if ($origEntity): ?>
          <div class="report-section">
            <h3>Entity Details</h3>
            <div class="report-row"><span class="report-label">Name</span><span class="report-value"><?= htmlspecialchars($origEntity['legal_entity_name']) ?></span></div>
            <div class="report-row"><span class="report-label">Registry ID</span><span class="report-value"><?= htmlspecialchars($origEntity['registry_id'] ?? '—') ?><?php if (!empty($origEntity['jurisdiction_state'])): ?> (<?= htmlspecialchars($origEntity['jurisdiction_state']) ?>)<?php endif; ?></span></div>
            <div class="report-row"><span class="report-label">Source</span><span class="report-value"><?= htmlspecialchars($origEntity['source'] ?? '—') ?></span></div>
          </div>
        <?php endif; ?>
      </div>
    </div>
  <?php endif; ?>

  <?php if (!empty($result['progress_log']) && $fromCache): ?>
  <div class="progress-log">
    <div class="progress-log-header">Progress Log (<?= count($result['progress_log']) ?> steps)</div>
    <div class="progress-log-body">
      <?php foreach ($result['progress_log'] as $entry): ?>
        <?= renderLogEntry($entry) ?>
      <?php endforeach; ?>
    </div>
  </div>
  <?php endif; ?>

<?php endif; ?>

</div>

<div class="sidebar">
  <form class="sidebar-search" action="/" method="get" target="_blank">
    <input type="url" name="url" class="sidebar-search-input" placeholder="New lookup URL..." required>
    <button type="submit" class="sidebar-search-btn">Go</button>
  </form>

  <div class="tools-section">
    <h2>Tools</h2>
    <a href="/bizapedia.php" class="tool-link" target="_blank">
      <span class="tool-icon tool-icon-company">B</span>
      <div>
        <div class="tool-name">Company Search</div>
        <div class="tool-desc">US state registries via Bizapedia</div>
      </div>
    </a>
    <a href="/bizapedia_tm.php" class="tool-link" target="_blank">
      <span class="tool-icon tool-icon-tm">TM</span>
      <div>
        <div class="tool-name">Trademark Search</div>
        <div class="tool-desc">US trademarks by name or owner</div>
      </div>
    </a>
    <a href="/validate.php" class="tool-link" target="_blank">
      <span class="tool-icon tool-icon-val">&#x2713;</span>
      <div>
        <div class="tool-name">Registry Validation</div>
        <div class="tool-desc">Verify entity by name + registry ID</div>
      </div>
    </a>

  </div>

  <h2>History</h2>
  <div class="sidebar-stats">
    <div class="sidebar-stat">
      <div class="sidebar-stat-value"><?= $totalLookups ?></div>
      <div class="sidebar-stat-label">Lookups</div>
    </div>
    <div class="sidebar-stat">
      <div class="sidebar-stat-value">$<?= number_format($totalCost, 2) ?></div>
      <div class="sidebar-stat-label">Total</div>
    </div>
  </div>

  <?php if (empty($history)): ?>
    <p style="font-size: 13px; color: #888;">No lookups yet. Enter a URL above to get started.</p>
  <?php else: ?>
    <?php foreach ($history as $entry): ?>
      <?php
        $hUrl = $entry['url'] ?? '';
        $hResult = $entry['result'] ?? [];
        $hReport = $hResult['report'] ?? [];
        $hMeta = $hResult['meta'] ?? [];
        $hEntity = $hReport['recommended_entity'] ?? null;
        $hName = $hEntity['legal_entity_name'] ?? 'No match';
        $hConf = $hReport['confidence'] ?? 'insufficient';
        $hCost = $hMeta['cost_usd'] ?? 0;
        $hDate = $entry['cached_at'] ?? '';
        $hDomain = preg_replace('/^www\./', '', parse_url($hUrl, PHP_URL_HOST) ?? '');
      ?>
      <div class="history-item">
        <a href="?url=<?= urlencode($hUrl) ?>">
          <div class="history-item-name"><?= htmlspecialchars($hDomain) ?></div>
          <div class="history-item-entity"><?= htmlspecialchars($hName) ?> <span class="badge badge-<?= $hConf ?>" style="font-size:9px;padding:1px 5px;"><?= $hConf ?></span></div>
          <div class="history-item-meta">
            <span>$<?= number_format($hCost, 2) ?></span>
            <span><?= htmlspecialchars($hDate) ?></span>
          </div>
        </a>
      </div>
    <?php endforeach; ?>
  <?php endif; ?>

</div>
</div>

<script>
// Auto-scroll progress log during live lookup
const progressBody = document.getElementById('progress-body');
if (progressBody) {
  const observer = new MutationObserver(() => {
    progressBody.scrollTop = progressBody.scrollHeight;
  });
  observer.observe(progressBody, { childList: true });
}
</script>

</body>
</html>
