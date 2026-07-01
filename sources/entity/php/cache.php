<?php
/**
 * File-based cache for entity lookup results.
 */

class LookupCache
{
    private string $cacheDir;

    public function __construct(?string $cacheDir = null)
    {
        $this->cacheDir = $cacheDir ?? __DIR__ . '/cache';
        if (!is_dir($this->cacheDir)) {
            mkdir($this->cacheDir, 0755, true);
        }
    }

    private function keyForUrl(string $url): string
    {
        // Support model-specific cache keys: "url#model=xxx"
        $suffix = '';
        if (str_contains($url, '#model=')) {
            [$url, $fragment] = explode('#', $url, 2);
            $suffix = '__' . preg_replace('/[^a-zA-Z0-9._-]/', '_', $fragment);
        }
        $parsed = parse_url($url);
        $domain = preg_replace('/^www\./', '', $parsed['host'] ?? 'unknown');
        // Use domain as filename (safe characters only)
        return preg_replace('/[^a-zA-Z0-9._-]/', '_', $domain) . $suffix;
    }

    public function get(string $url): ?array
    {
        $path = $this->cacheDir . '/' . $this->keyForUrl($url) . '.json';
        if (!file_exists($path) || filesize($path) === 0) {
            return null;
        }
        $data = json_decode(file_get_contents($path), true);
        return is_array($data) ? $data : null;
    }

    public function set(string $url, array $result): void
    {
        $entry = [
            'url' => $url,
            'cached_at' => date('Y-m-d H:i:s'),
            'result' => $result,
        ];
        $path = $this->cacheDir . '/' . $this->keyForUrl($url) . '.json';
        $json = json_encode($entry, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES | JSON_INVALID_UTF8_SUBSTITUTE);
        if (!$json || strlen($json) <= 2) {
            error_log("CACHE SET FAILED: json_encode returned " . var_export($json, true) . " for URL {$url} — json error: " . json_last_error_msg());
            return;
        }
        $written = file_put_contents($path, $json, LOCK_EX);
        if ($written === false) {
            error_log("CACHE SET FAILED: file_put_contents returned false for {$path}");
        }
    }

    public function delete(string $url): void
    {
        $path = $this->cacheDir . '/' . $this->keyForUrl($url) . '.json';
        if (file_exists($path)) {
            unlink($path);
        }
    }

    /**
     * Check if a lookup is currently in progress for this URL.
     */
    public function isLocked(string $url): bool
    {
        $path = $this->cacheDir . '/' . $this->keyForUrl($url) . '.lock';
        if (!file_exists($path)) return false;
        // Stale lock protection: if lock is older than 3 minutes, ignore it
        if (time() - filemtime($path) > 180) {
            unlink($path);
            return false;
        }
        return true;
    }

    public function lock(string $url): void
    {
        $path = $this->cacheDir . '/' . $this->keyForUrl($url) . '.lock';
        file_put_contents($path, (string) getmypid());
    }

    public function unlock(string $url): void
    {
        $path = $this->cacheDir . '/' . $this->keyForUrl($url) . '.lock';
        if (file_exists($path)) {
            unlink($path);
        }
    }

    /**
     * Get all cached results.
     */
    public function getAll(): array
    {
        $results = [];
        foreach (glob($this->cacheDir . '/*.json') as $path) {
            $data = json_decode(file_get_contents($path), true);
            if (is_array($data)) {
                $results[] = $data;
            }
        }
        // Sort by cached_at descending
        usort($results, fn($a, $b) => strcmp($b['cached_at'] ?? '', $a['cached_at'] ?? ''));
        return $results;
    }

    /**
     * Get total cost across all cached results.
     */
    public function getTotalCost(): float
    {
        $total = 0;
        foreach ($this->getAll() as $entry) {
            $total += $entry['result']['meta']['cost_usd'] ?? 0;
        }
        return $total;
    }

    /**
     * Get total number of lookups.
     */
    public function getCount(): int
    {
        $files = glob($this->cacheDir . '/*.json');
        return $files ? count($files) : 0;
    }
}
