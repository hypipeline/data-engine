<?php
/**
 * Entity Lookup — Configuration
 * Loads from settings.json (editable via web UI).
 */

$settingsFile = __DIR__ . '/settings.json';
$settings = json_decode(file_get_contents($settingsFile), true);

return [
    'anthropic_api_key' => getenv('ANTHROPIC_API_KEY') ?: ($settings['anthropic_api_key'] ?? ''),
    'browserbase_api_key' => getenv('BROWSERBASE_API_KEY') ?: $settings['browserbase_api_key'],
    'browserbase_project_id' => getenv('BROWSERBASE_PROJECT_ID') ?: $settings['browserbase_project_id'],
    'model' => $settings['model'],
    'sec_user_agent' => $settings['sec_user_agent'],
    'max_page_chars' => (int) $settings['max_page_chars'],
    'max_entity_names' => (int) $settings['max_entity_names'],
    'max_ciks' => (int) $settings['max_ciks'],
    'max_ownership_levels' => (int) $settings['max_ownership_levels'],
    'twocaptcha_api_key' => $settings['twocaptcha_api_key'] ?? '',
    'brightdata_api_key' => $settings['brightdata_api_key'] ?? '',
    'brightdata_zone' => $settings['brightdata_zone'] ?? 'web_unlocker1',
    'brightdata_scraping_browser_ws' => $settings['brightdata_scraping_browser_ws'] ?? '',
    'companies_house_api_key' => $settings['companies_house_api_key'] ?? '',
    'openai_api_key' => getenv('OPENAI_API_KEY') ?: ($settings['openai_api_key'] ?? ''),
    'northdata_email' => $settings['northdata_email'] ?? '',
    'northdata_password' => $settings['northdata_password'] ?? '',
    'blocked_entity_names' => $settings['blocked_entity_names'] ?? [],
];
