<?php
/**
 * Standalone script to fetch a URL via Scraping Browser.
 * Usage: php scraping_browser_fetch.php <url>
 * Outputs JSON: {"text": "...", "html": "..."} or {"error": "..."}
 */
if ($argc < 2) {
    echo json_encode(['error' => 'No URL provided']);
    exit(1);
}

$url = $argv[1];
$config = require __DIR__ . '/config.php';
require __DIR__ . '/tools.php';

$tools = new LookupTools($config);
$rawHtml = null;
$text = $tools->singleScrapingBrowserFetch($url, $rawHtml);

if ($text !== null && !str_starts_with($text, 'Error:') && strlen($text) >= 200) {
    echo json_encode(['text' => $text, 'html' => $rawHtml]);
} else {
    echo json_encode(['error' => $text ?? 'No content']);
}
