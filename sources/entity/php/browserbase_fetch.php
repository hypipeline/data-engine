<?php
/**
 * Standalone script to fetch a URL via Browserbase.
 * Usage: php browserbase_fetch.php <url>
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
$text = $tools->singleBrowserbaseFetch($url, $rawHtml);

if ($text !== null && strlen($text) >= 200) {
    echo json_encode(['text' => $text, 'html' => $rawHtml]);
} else {
    echo json_encode(['error' => 'Browserbase returned blocked/empty page']);
}
