<?php
/**
 * Tests for LookupTools
 *
 * Run: php tests/LookupToolsTest.php
 */

require_once __DIR__ . '/../tools.php';

class LookupToolsTest
{
    private LookupTools $tools;
    private int $passed = 0;
    private int $failed = 0;

    public function __construct()
    {
        $config = require __DIR__ . '/../config.php';
        $this->tools = new LookupTools($config);
    }

    public function run(): void
    {
        echo "Running LookupTools tests...\n\n";

        $this->testFetchWebpage();
        $this->testWhoisLookup();
        $this->testSearchCompaniesHouse();
        $this->testOwnershipChain();
        $this->testSearchSecCompany();
        $this->testSearchSecFulltext();
        $this->testFetchSecSubmissions();
        $this->testSearchNorthdata();

        echo "\n" . str_repeat("─", 40) . "\n";
        echo "Results: {$this->passed} passed, {$this->failed} failed\n";
        exit($this->failed > 0 ? 1 : 0);
    }

    private function assert(bool $condition, string $message): void
    {
        if ($condition) {
            echo "  ✓ {$message}\n";
            $this->passed++;
        } else {
            echo "  ✗ FAIL: {$message}\n";
            $this->failed++;
        }
    }

    // ── Tests ────────────────────────────────────────────────────────────────

    private function testFetchWebpage(): void
    {
        echo "fetchWebpage:\n";

        $text = $this->tools->fetchWebpage('https://www.kaincap.com/');
        $this->assert(strlen($text) > 200, 'Fetches kaincap.com homepage (got ' . strlen($text) . ' chars)');
        $this->assert(stripos($text, 'kain') !== false, 'Contains "kain" in text');
        $this->assert(!str_starts_with($text, 'Error'), 'No error prefix');
        echo "\n";
    }

    private function testWhoisLookup(): void
    {
        echo "whoisLookup:\n";

        $result = $this->tools->whoisLookup('kaincap.com');
        $this->assert(strlen($result) > 50, 'Returns WHOIS data (got ' . strlen($result) . ' chars)');
        $this->assert(stripos($result, 'domain') !== false || stripos($result, 'registr') !== false, 'Contains domain/registrar info');
        echo "\n";
    }

    private function testSearchCompaniesHouse(): void
    {
        echo "searchCompaniesHouse:\n";

        $result = $this->tools->searchCompaniesHouse('ABCA Systems');
        $this->assert(str_contains($result, 'ABCA'), 'Finds ABCA in results');
        $this->assert(str_contains($result, 'find-and-update.company-information.service.gov.uk'), 'Contains Companies House URL');

        $result2 = $this->tools->searchCompaniesHouse('xyznonexistentcompany12345');
        $this->assert(str_contains($result2, 'No Companies House results'), 'Returns no results for gibberish query');
        echo "\n";
    }

    private function testOwnershipChain(): void
    {
        echo "companiesHouseOwnershipChain:\n";

        // ABCA Systems Limited
        $result = $this->tools->companiesHouseOwnershipChain('06294877');
        $this->assert(str_contains($result, 'ABCA SYSTEMS LIMITED') || str_contains($result, 'Abca Systems'), 'Starts with ABCA Systems');
        $this->assert(str_contains($result, 'Vulcan1 Topco') || str_contains($result, 'VULCAN1 TOPCO'), 'Reaches Vulcan1 Topco');
        $this->assert(str_contains($result, 'STOP') || str_contains($result, 'TOP OF CHAIN'), 'Chain terminates');
        $this->assert(!str_contains($result, 'Vulcan1 Jv Llp') || str_contains($result, 'STOP'), 'Does not follow JV LLP beyond 50% (stops or reports stop)');

        // Greensleeves
        $result2 = $this->tools->companiesHouseOwnershipChain('05107549');
        $this->assert(str_contains($result2, 'GREENSLEEVES') || str_contains($result2, 'Greensleeves'), 'Starts with Greensleeves');
        $this->assert(str_contains($result2, 'Neighbourly') || str_contains($result2, 'NEIGHBOURLY'), 'Reaches Neighbourly');
        $this->assert(str_contains($result2, 'TOP OF CHAIN'), 'Chain terminates at top');
        echo "\n";
    }

    private function testSearchSecCompany(): void
    {
        echo "searchSecCompany:\n";

        $result = $this->tools->searchSecCompany('Level Equity');
        $this->assert(str_contains($result, 'CIK:'), 'Finds CIK for Level Equity');
        $this->assert(str_contains(strtolower($result), 'level'), 'Contains "level" in results');

        $result2 = $this->tools->searchSecCompany('xyznonexistent12345');
        $this->assert(str_contains($result2, 'No SEC company results'), 'Returns no results for gibberish');
        echo "\n";
    }

    private function testSearchSecFulltext(): void
    {
        echo "searchSecFulltext:\n";

        $result = $this->tools->searchSecFulltext('kkr.com');
        $this->assert(str_contains($result, 'Total hits:'), 'Returns hit count');
        $this->assert(!str_starts_with($result, 'Error'), 'No error');
        echo "\n";
    }

    private function testFetchSecSubmissions(): void
    {
        echo "fetchSecSubmissions:\n";

        // KKR CIK
        $result = $this->tools->fetchSecSubmissions('0001404912');
        $this->assert(str_contains($result, 'KKR'), 'Finds KKR name');
        $this->assert(str_contains($result, 'latest_filings'), 'Contains filings');

        $data = json_decode($result, true);
        $this->assert(is_array($data), 'Returns valid JSON');
        $this->assert(!empty($data['name']), 'Has entity name');
        echo "\n";
    }

    private function testSearchNorthdata(): void
    {
        echo "searchNorthdata:\n";

        $result = $this->tools->searchNorthdata('Siemens AG');
        $this->assert(str_contains(strtolower($result), 'siemens') || str_contains($result, 'No North Data'), 'Searches for Siemens (may find results or not based on scraping)');
        $this->assert(!str_starts_with($result, 'Error:'), 'No hard error');
        echo "\n";
    }
}

// Run tests
$test = new LookupToolsTest();
$test->run();
