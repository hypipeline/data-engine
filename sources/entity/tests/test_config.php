<?php
/**
 * Test that config.php passes through all required settings keys.
 * This prevents the bug where new settings are added to settings.json
 * but not to config.php, causing features to silently fail in production.
 *
 * Run: php tests/test_config.php
 */

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

echo "=== Config Key Passthrough ===\n\n";

$settings = json_decode(file_get_contents(__DIR__ . '/../php/settings.json'), true);
$config = require __DIR__ . '/../php/config.php';

// These keys MUST be present in config.php output
$requiredKeys = [
    'anthropic_api_key',
    'openai_api_key',
    'browserbase_api_key',
    'browserbase_project_id',
    'model',
    'sec_user_agent',
    'max_page_chars',
    'max_entity_names',
    'max_ciks',
    'max_ownership_levels',
    'brightdata_api_key',
    'brightdata_zone',
    'companies_house_api_key',
    'northdata_email',
    'northdata_password',
    'blocked_entity_names',
];

echo "1. All required keys present in config output:\n";
foreach ($requiredKeys as $key) {
    test("Key '{$key}' exists in config", true, array_key_exists($key, $config));
}

echo "\n2. Config values match settings.json:\n";
foreach ($requiredKeys as $key) {
    if (!isset($settings[$key])) continue;
    $settingsVal = $settings[$key];
    $configVal = $config[$key] ?? null;
    // For numeric values, settings.json has strings but config.php casts to int
    if (is_int($configVal) && is_string($settingsVal)) {
        $settingsVal = (int) $settingsVal;
    }
    test("Key '{$key}' value matches", $settingsVal, $configVal);
}

echo "\n3. Model dispatch:\n";
require_once __DIR__ . '/../php/lookup.php';
// Test with Claude model
$claudeConfig = $config;
$claudeConfig['model'] = 'claude-sonnet-4-6';
$lookup = new EntityLookup($claudeConfig);
$ref = new ReflectionClass($lookup);
$method = $ref->getMethod('isOpenAIModel');
test('claude-sonnet-4-6 is NOT OpenAI', false, $method->invoke($lookup));

// Test with OpenAI model
$openaiConfig = $config;
$openaiConfig['model'] = 'gpt-4o';
$lookup2 = new EntityLookup($openaiConfig);
test('gpt-4o IS OpenAI', true, $method->invoke($lookup2));

$openaiConfig['model'] = 'o3';
$lookup3 = new EntityLookup($openaiConfig);
test('o3 IS OpenAI', true, $method->invoke($lookup3));

$openaiConfig['model'] = 'o4-mini';
$lookup4 = new EntityLookup($openaiConfig);
test('o4-mini IS OpenAI', true, $method->invoke($lookup4));

echo "\n=== Results: {$passed} passed, {$failed} failed ===\n";
exit($failed > 0 ? 1 : 0);
