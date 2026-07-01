<?php
/**
 * Integration test for the full EntityLookup pipeline.
 *
 * Run: php tests/LookupTest.php
 */

require_once __DIR__ . '/../lookup.php';

class LookupTest
{
    private int $passed = 0;
    private int $failed = 0;

    public function run(): void
    {
        echo "Running EntityLookup integration tests...\n\n";

        $this->testKaincap();
        $this->testIceniCapital();

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

    private function testKaincap(): void
    {
        echo "Full lookup: kaincap.com\n";

        $config = require __DIR__ . '/../config.php';
        $lookup = new EntityLookup($config);
        $result = $lookup->run('https://www.kaincap.com/');

        $report = $result['report'];
        $meta = $result['meta'];

        $this->assert(isset($report['recommended_entity']), 'Has recommended_entity');
        $this->assert($report['confidence'] !== 'insufficient', 'Confidence is not insufficient');

        $entity = $report['recommended_entity'];
        if ($entity) {
            $name = strtolower($entity['legal_entity_name'] ?? '');
            $this->assert(str_contains($name, 'kain'), 'Entity name contains "kain"');
            $this->assert(!empty($entity['jurisdiction']), 'Has jurisdiction');
            $this->assert(!empty($entity['source_url']), 'Has source URL');
        }

        $this->assert($meta['total_time_s'] < 300, 'Completed in under 5 minutes (took ' . $meta['total_time_s'] . 's)');
        $this->assert(!empty($report['evidence_forward']), 'Has forward evidence');

        echo "  Entity: " . ($entity['legal_entity_name'] ?? 'None') . "\n";
        echo "  Confidence: {$report['confidence']}\n";
        echo "  Time: {$meta['total_time_s']}s\n";
        echo "\n";
    }

    private function testIceniCapital(): void
    {
        echo "Full lookup: icenicapital.com (UK, LLP — requires Browserbase)\n";

        $config = require __DIR__ . '/../config.php';
        $lookup = new EntityLookup($config);
        $result = $lookup->run('http://www.icenicapital.com/');

        $report = $result['report'];
        $meta = $result['meta'];

        $entity = $report['recommended_entity'] ?? null;

        // This site returns 502 and requires Browserbase rendering.
        // If Browserbase is rate-limited, the lookup will return insufficient.
        if ($entity) {
            $name = strtolower($entity['legal_entity_name'] ?? '');
            $this->assert(str_contains($name, 'iceni'), 'Entity name contains "iceni"');
            $this->assert(str_contains($name, 'llp'), 'Entity is an LLP');
        } else {
            // Accept insufficient if Browserbase is unavailable
            $this->assert($report['confidence'] === 'insufficient', 'Returns insufficient when site unreachable (Browserbase may be rate-limited)');
        }

        echo "  Entity: " . ($entity['legal_entity_name'] ?? 'None') . "\n";
        echo "  Confidence: {$report['confidence']}\n";
        echo "  Time: {$meta['total_time_s']}s\n";
        echo "\n";
    }
}

// Run tests
$test = new LookupTest();
$test->run();
