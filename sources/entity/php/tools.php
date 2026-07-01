<?php
/**
 * Entity Lookup — Registry & Fetching Tools
 *
 * Exact replica of the Python tool implementations.
 */

class LookupTools
{
    private const PROXY_COUNTRIES = [
        'al','ar','at','au','be','bg','br','ca','ch','cl','co','cr','cy','cz',
        'de','dk','ec','ee','es','fi','fr','gb','gr','hk','hr','hu','id','ie',
        'il','in','is','it','jp','ke','kr','lt','lu','lv','mk','mt','mx','my',
        'ng','nl','no','nz','pa','pe','ph','pk','pl','pt','ro','rs','se','sg',
        'si','sk','th','tr','tw','ua','us','uy','vn','za',
    ];

    private array $config;
    private array $log = [];
    private $progressCallback = null;
    private ?string $northdataAuthCookie = null;
    private array $apiCalls = [
        'claude' => 0,
        'browserbase' => 0,
        'brightdata' => 0,
        'openai' => 0,
        'bizapedia' => 0,
    ];

    public function __construct(array $config, ?callable $progressCallback = null)
    {
        $this->config = $config;
        $this->progressCallback = $progressCallback;
    }

    public function getApiCalls(): array
    {
        return $this->apiCalls;
    }

    public function incrementApiCall(string $service): void
    {
        $this->apiCalls[$service] = ($this->apiCalls[$service] ?? 0) + 1;
    }

    private function progress(string $phase, string $message): void
    {
        if ($this->progressCallback) {
            ($this->progressCallback)(['phase' => $phase, 'message' => $message]);
        }
    }

    private static function randomCountry(): string
    {
        return self::PROXY_COUNTRIES[array_rand(self::PROXY_COUNTRIES)];
    }

    public function getLog(): array
    {
        return $this->log;
    }

    // ── Webpage Fetching ─────────────────────────────────────────────────────

    public function fetchWebpage(string $url, ?array &$meta = null): string
    {
        $meta = ['http_code' => 0, 'source' => 'curl', 'error' => null];

        // Level 1: Direct curl fetch
        $this->progress('fetch', "Trying direct curl for {$url}...");
        $text = $this->httpFetchText($url, $meta);
        if ($text !== null && strlen($text) >= 200) {
            $this->progress('fetch', "Direct curl succeeded — HTTP {$meta['http_code']}, " . strlen($text) . " chars");
        } else {
            $reason = $meta['http_code'] === 404 ? 'HTTP 404' : ($meta['http_code'] !== 200 ? "HTTP {$meta['http_code']}" : 'insufficient content');
            $this->progress('fetch', "Direct curl failed — {$reason}");
        }

        // Level 2: Bright Data Web Unlocker (handles bot protection)
        if (($text === null || strlen($text) < 200) && $meta['http_code'] !== 404) {
            $this->progress('brightdata', "Trying Bright Data Web Unlocker...");
            $meta['source'] = 'brightdata';
            $text = $this->brightdataFetch($url);
            if (str_starts_with($text ?? '', 'Error:')) {
                $this->progress('brightdata', "Web Unlocker failed — {$text}");
                $meta['error'] = $text;
                $text = null;
            } else {
                $meta['http_code'] = 200;
                $this->progress('brightdata', "Web Unlocker succeeded — " . strlen($text) . " chars");
            }
        }

        // Level 3: Browserbase WebDriver (remote browser fallback)
        if (($text === null || strlen($text) < 200) && $meta['http_code'] !== 404) {
            $this->progress('fetch', "Trying Browserbase remote browser...");
            $meta['source'] = 'browserbase';
            $html = $this->browserbaseFetchHtml($url);
            if ($html === null || str_starts_with($html, 'Error:')) {
                $this->progress('fetch', "Browserbase failed — {$html}");
                $meta['error'] = $html;
                $text = null;
            } else {
                $text = $this->htmlToText($html);
            }
            // Check if Browserbase got a real page or just a bot-block/error page
            if ($text !== null && (strlen($text) < 500 || preg_match('/access denied|security.*(issue|check)|captcha|403 error|unable to give you access/i', $text))) {
                $this->progress('fetch', "Browserbase returned a blocked/error page — discarding");
                $text = null;
            }
            if ($text !== null) {
                $meta['http_code'] = 200;
                $this->progress('fetch', "Browserbase succeeded — " . strlen($text) . " chars");
            }
        }

        // Level 4: Wayback Machine (last resort for hard-blocked sites)
        if (($text === null || strlen($text) < 200) && $meta['http_code'] !== 404) {
            $this->progress('fetch', "Trying Wayback Machine...");
            $meta['source'] = 'wayback';
            $text = $this->waybackFetch($url);
            if (str_starts_with($text ?? '', 'Error:')) {
                $this->progress('fetch', "Wayback Machine failed — {$text}");
                $meta['error'] = $text;
                $text = null;
            } else {
                $meta['http_code'] = 200;
                $this->progress('fetch', "Wayback Machine succeeded — " . strlen($text) . " chars");
            }
        }

        $text = $text ?? '';

        if (strlen($text) > $this->config['max_page_chars']) {
            $text = substr($text, 0, $this->config['max_page_chars']) . "\n... [truncated]";
        }

        return $text;
    }

    /**
     * Parallel curl fetch for multiple URLs. Returns [url => ['text' => ..., 'http_code' => ...]].
     */
    public function curlFetchMulti(array $urls): array
    {
        $mh = curl_multi_init();
        $handles = [];
        foreach ($urls as $url) {
            $ch = curl_init($url);
            curl_setopt_array($ch, [
                CURLOPT_RETURNTRANSFER => true,
                CURLOPT_FOLLOWLOCATION => true,
                CURLOPT_TIMEOUT => 15,
                CURLOPT_USERAGENT => 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                CURLOPT_SSL_VERIFYPEER => true,
                CURLOPT_ENCODING => '',
            ]);
            curl_multi_add_handle($mh, $ch);
            $handles[$url] = $ch;
        }
        do {
            $status = curl_multi_exec($mh, $active);
            if ($active) curl_multi_select($mh);
        } while ($active && $status === CURLM_OK);

        $results = [];
        foreach ($handles as $url => $ch) {
            $httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);
            $html = curl_multi_getcontent($ch);
            curl_multi_remove_handle($mh, $ch);
            $text = null;
            if ($httpCode === 200 && $html) {
                $text = $this->htmlToText($html);
                if (strlen($text) < 200) $text = null;
            }
            $results[$url] = ['text' => $text, 'http_code' => $httpCode, 'html' => ($httpCode === 200 && $html) ? $html : null];
        }
        curl_multi_close($mh);
        return $results;
    }

    public function singleBrightdataFetch(string $url, ?string &$rawHtml = null): ?string
    {
        $apiKey = $this->config['brightdata_api_key'] ?? '';
        $zone = $this->config['brightdata_zone'] ?? 'web_unlocker1';
        if (!$apiKey) return null;

        $this->apiCalls['brightdata']++;
        $ch = curl_init('https://api.brightdata.com/request');
        curl_setopt_array($ch, [
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_POST => true,
            CURLOPT_HTTPHEADER => [
                'Content-Type: application/json',
                "Authorization: Bearer {$apiKey}",
            ],
            CURLOPT_POSTFIELDS => json_encode([
                'zone' => $zone,
                'url' => $url,
                'format' => 'raw',
            ]),
            CURLOPT_TIMEOUT => 240,
        ]);
        $html = curl_exec($ch);
        $httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);

        if ($httpCode !== 200 || !$html || strlen($html) < 200) {
            $curlError = curl_error($ch);
            $detail = $curlError ? " ({$curlError})" : '';
            return "Error: Bright Data returned HTTP {$httpCode}{$detail}";
        }

        $rawHtml = $html;
        return $this->htmlToText($html);
    }

    public function singleBrowserbaseFetch(string $url, ?string &$rawHtml = null): ?string
    {
        $html = $this->browserbaseFetchHtml($url);
        if ($html === null || str_starts_with($html, 'Error:')) return null;
        $text = $this->htmlToText($html);
        if (strlen($text) < 500 || preg_match('/access denied|security.*(issue|check)|captcha|403 error|unable to give you access/i', $text)) {
            return null;
        }
        $rawHtml = $html;
        return $text;
    }

    /**
     * Fetch multiple URLs via Browserbase in parallel using background PHP processes.
     * Returns [url => ['text' => ..., 'html' => ...]] for successes.
     */
    public function browserbaseFetchParallel(array $urls): array
    {
        $this->apiCalls['browserbase'] += count($urls);
        $script = __DIR__ . '/browserbase_fetch.php';
        $procs = [];
        $pipes = [];

        // Launch all fetches in parallel
        foreach ($urls as $url) {
            $escapedUrl = escapeshellarg($url);
            $proc = proc_open(
                "php {$script} {$escapedUrl}",
                [1 => ['pipe', 'w'], 2 => ['pipe', 'w']],
                $p
            );
            $procs[$url] = $proc;
            $pipes[$url] = $p;
        }

        // Collect results
        $results = [];
        foreach ($procs as $url => $proc) {
            $output = stream_get_contents($pipes[$url][1]);
            fclose($pipes[$url][1]);
            fclose($pipes[$url][2]);
            proc_close($proc);

            $data = json_decode($output, true);
            if ($data && isset($data['text'])) {
                $results[$url] = $data;
            }
        }

        return $results;
    }

    /**
     * Fetch multiple URLs via Scraping Browser in parallel using background PHP processes.
     * Returns [url => ['text' => ..., 'html' => ...]] for successes.
     */
    public function scrapingBrowserFetchParallel(array $urls): array
    {
        $script = __DIR__ . '/scraping_browser_fetch.php';
        $procs = [];
        $pipes = [];

        foreach ($urls as $url) {
            $escapedUrl = escapeshellarg($url);
            $proc = proc_open(
                "php {$script} {$escapedUrl}",
                [1 => ['pipe', 'w'], 2 => ['pipe', 'w']],
                $p
            );
            $procs[$url] = $proc;
            $pipes[$url] = $p;
        }

        $results = [];
        foreach ($procs as $url => $proc) {
            $output = stream_get_contents($pipes[$url][1]);
            fclose($pipes[$url][1]);
            fclose($pipes[$url][2]);
            proc_close($proc);

            $data = json_decode($output, true);
            if ($data && isset($data['text'])) {
                $results[$url] = $data;
            }
        }

        return $results;
    }

    public function singleWaybackFetch(string $url, ?string &$rawHtml = null): ?string
    {
        $domain = parse_url($url, PHP_URL_HOST) ?: $url;
        $years = [(string)date('Y'), (string)(date('Y') - 1), (string)(date('Y') - 2)];

        foreach ($years as $year) {
            $archiveUrl = "https://web.archive.org/web/{$year}id_/{$url}";
            $ch = curl_init($archiveUrl);
            curl_setopt_array($ch, [
                CURLOPT_RETURNTRANSFER => true,
                CURLOPT_FOLLOWLOCATION => true,
                CURLOPT_TIMEOUT => 20,
                CURLOPT_ENCODING => '',
            ]);
            $html = curl_exec($ch);
            $httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);

            if ($httpCode === 200 && $html && strlen($html) > 2000) {
                $text = $this->htmlToText($html);
                if (strlen($text) > 500 && !preg_match('/access denied|security.*(issue|check)|captcha|unable to give you access/i', $text)) {
                    $rawHtml = $html;
                    return $text;
                }
            }
        }
        return null;
    }

    private function httpFetchText(string $url, array &$meta): ?string
    {
        $ch = curl_init($url);
        curl_setopt_array($ch, [
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_FOLLOWLOCATION => true,
            CURLOPT_TIMEOUT => 15,
            CURLOPT_USERAGENT => 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            CURLOPT_SSL_VERIFYPEER => true,
            CURLOPT_ENCODING => '',  // accept compressed responses
        ]);
        $html = curl_exec($ch);
        $httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);
        $meta['http_code'] = $httpCode;

        if ($httpCode !== 200 || $html === false) {
            return null;
        }

        return $this->htmlToText($html);
    }

    private function brightdataFetch(string $url): ?string
    {
        $apiKey = $this->config['brightdata_api_key'] ?? '';
        $zone = $this->config['brightdata_zone'] ?? 'web_unlocker1';
        if (!$apiKey) {
            return "Error: Bright Data not configured.";
        }

        $this->apiCalls['brightdata']++;
        $ch = curl_init('https://api.brightdata.com/request');
        curl_setopt_array($ch, [
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_POST => true,
            CURLOPT_HTTPHEADER => [
                'Content-Type: application/json',
                "Authorization: Bearer {$apiKey}",
            ],
            CURLOPT_POSTFIELDS => json_encode([
                'zone' => $zone,
                'url' => $url,
                'format' => 'raw',
            ]),
            CURLOPT_TIMEOUT => 240,
        ]);
        $html = curl_exec($ch);
        $httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);
        $curlError = curl_error($ch);

        if ($httpCode !== 200 || !$html || strlen($html) < 200) {
            $detail = $curlError ? " ({$curlError})" : '';
            return "Error: Bright Data returned HTTP {$httpCode}{$detail}";
        }

        return $this->htmlToText($html);
    }

    public function singleScrapingBrowserFetch(string $url, ?string &$rawHtml = null): ?string
    {
        $ws = $this->config['brightdata_scraping_browser_ws'] ?? '';
        if (!$ws) {
            return "Error: Scraping Browser not configured.";
        }

        $script = __DIR__ . '/scraping_browser.mjs';
        $escapedUrl = escapeshellarg($url);
        $escapedWs = escapeshellarg($ws);

        // Use --json mode to get both text and HTML
        $cmd = "SBR_WS={$escapedWs} node {$script} {$escapedUrl} --json 2>/dev/null";
        $output = shell_exec($cmd);

        $data = json_decode(trim($output ?? ''), true);
        if (!$data || empty($data['text'])) {
            return "Error: Scraping Browser returned no content.";
        }

        $text = trim($data['text']);
        if (strlen($text) < 200) {
            return "Error: Scraping Browser returned insufficient content.";
        }

        // Reject bot-block / error pages
        if (preg_match('/access denied|security.*(issue|check)|captcha|403 error|unable to give you access|service unavailable|dns failure/i', $text) && strlen($text) < 1000) {
            return "Error: Scraping Browser got a blocked/error page.";
        }

        $rawHtml = $data['html'] ?? null;
        return $text;
    }

    private function browserbaseFetchHtml(string $url): ?string
    {
        $apiKey = $this->config['browserbase_api_key'] ?? '';
        $projectId = $this->config['browserbase_project_id'] ?? '';
        if (!$apiKey || !$projectId) {
            return "Error: Browserbase not configured.";
        }
        $this->apiCalls['browserbase']++;

        $seleniumBase = 'http://connect.usw2.browserbase.com/webdriver';

        // Create Browserbase session
        $ch = curl_init('https://api.browserbase.com/v1/sessions');
        curl_setopt_array($ch, [
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_POST => true,
            CURLOPT_HTTPHEADER => ["x-bb-api-key: $apiKey", 'Content-Type: application/json'],
            CURLOPT_POSTFIELDS => json_encode(['projectId' => $projectId]),
            CURLOPT_TIMEOUT => 30,
        ]);
        $bbSession = json_decode(curl_exec($ch), true);
        $bbSessionId = $bbSession['id'] ?? '';
        if (!$bbSessionId) {
            return "Error: Could not create Browserbase session.";
        }

        // Create WebDriver session
        $ch = curl_init($seleniumBase . '/session');
        curl_setopt_array($ch, [
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_POST => true,
            CURLOPT_HTTPHEADER => ['Content-Type: application/json', "x-bb-api-key: $apiKey", "session-id: $bbSessionId"],
            CURLOPT_POSTFIELDS => json_encode(['capabilities' => ['alwaysMatch' => ['browserName' => 'chrome']]]),
            CURLOPT_TIMEOUT => 30,
        ]);
        $wd = json_decode(curl_exec($ch), true);
        $wdSessionId = $wd['value']['sessionId'] ?? '';
        if (!$wdSessionId) {
            return "Error: Could not create WebDriver session.";
        }

        // Navigate to URL
        $ch = curl_init($seleniumBase . "/session/$wdSessionId/url");
        curl_setopt_array($ch, [
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_POST => true,
            CURLOPT_HTTPHEADER => ['Content-Type: application/json', "x-bb-api-key: $apiKey", "session-id: $bbSessionId"],
            CURLOPT_POSTFIELDS => json_encode(['url' => $url]),
            CURLOPT_TIMEOUT => 240,
        ]);
        curl_exec($ch);

        sleep(5);

        // Get rendered page source
        $ch = curl_init($seleniumBase . "/session/$wdSessionId/source");
        curl_setopt_array($ch, [
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_HTTPHEADER => ["x-bb-api-key: $apiKey", "session-id: $bbSessionId"],
            CURLOPT_TIMEOUT => 60,
        ]);
        $resp = curl_exec($ch);
        $data = json_decode($resp, true);
        $html = $data['value'] ?? '';

        if (!$html) {
            return "Error: Browserbase returned empty page.";
        }

        return $html;
    }

    private function waybackFetch(string $url): ?string
    {
        $domain = parse_url($url, PHP_URL_HOST) ?: $url;

        $years = [(string)date('Y'), (string)(date('Y') - 1), (string)(date('Y') - 2)];

        foreach ($years as $year) {
            $archiveUrl = "https://web.archive.org/web/{$year}id_/{$url}";
            $ch = curl_init($archiveUrl);
            curl_setopt_array($ch, [
                CURLOPT_RETURNTRANSFER => true,
                CURLOPT_FOLLOWLOCATION => true,
                CURLOPT_TIMEOUT => 20,
                CURLOPT_ENCODING => '',
            ]);
            $html = curl_exec($ch);
            $httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);

            if ($httpCode === 200 && $html && strlen($html) > 2000) {
                // Check it's not a bot-block page archived by Wayback
                $text = $this->htmlToText($html);
                if (strlen($text) > 500 && !preg_match('/access denied|security.*(issue|check)|captcha|unable to give you access/i', $text)) {
                    return $text;
                }
            }
        }

        return "Error: No usable Wayback Machine snapshot found for {$domain}.";
    }

    public function htmlToText(string $html): string
    {
        // Remove script, style, noscript tags
        $html = preg_replace('/<(script|style|noscript)\b[^>]*>.*?<\/\1>/is', '', $html) ?? $html;
        // Insert newlines before block-level elements so text doesn't concatenate
        $html = preg_replace('/<\/?(?:div|p|br|hr|h[1-6]|li|tr|td|th|dt|dd|blockquote|section|article|header|footer|nav|ul|ol|table|figcaption)\b[^>]*>/i', "\n", $html);
        // Remove remaining HTML tags
        $text = strip_tags($html);
        // Decode entities
        $text = html_entity_decode($text, ENT_QUOTES | ENT_HTML5, 'UTF-8');
        // Collapse whitespace on each line
        $text = preg_replace('/[ \t]+/', ' ', $text);
        // Collapse blank lines
        $lines = array_filter(array_map('trim', explode("\n", $text)), fn($l) => $l !== '');
        return implode("\n", $lines);
    }

    // ── WHOIS ────────────────────────────────────────────────────────────────

    public function whoisLookup(string $domain): string
    {
        $output = shell_exec("timeout 10 whois " . escapeshellarg($domain) . " 2>/dev/null");
        if (!$output) {
            return "WHOIS data not available.";
        }

        $keywords = ['registrant', 'creation', 'domain name', 'registrar', 'name server'];
        $lines = [];
        foreach (explode("\n", $output) as $line) {
            $lower = strtolower($line);
            foreach ($keywords as $kw) {
                if (str_contains($lower, $kw)) {
                    $lines[] = trim($line);
                    break;
                }
            }
        }

        return $lines ? implode("\n", array_slice($lines, 0, 30)) : "WHOIS data not available or fully redacted.";
    }

    // ── Companies House ──────────────────────────────────────────────────────

    public function searchCompaniesHouse(string $query): string
    {
        $url = "https://find-and-update.company-information.service.gov.uk/search?q=" . urlencode($query);
        $html = $this->httpGet($url);
        if (!$html) {
            return "Error: Could not fetch Companies House.";
        }

        $results = [];
        if (preg_match_all('/<li class="type-company"[^>]*>.*?<a[^>]*href="([^"]*)"[^>]*>([^<]*)<\/a>/is', $html, $matches, PREG_SET_ORDER)) {
            foreach ($matches as $match) {
                $href = $match[1];
                $name = trim(html_entity_decode($match[2]));
                // Skip template placeholders
                if (str_contains($name, '{{') || strlen($name) < 2) continue;
                if (!str_starts_with($href, '/company/')) continue;
                $fullUrl = "https://find-and-update.company-information.service.gov.uk{$href}";
                $results[] = "{$name} | {$fullUrl}";
                if (count($results) >= 10) break;
            }
        }

        $result = $results ? implode("\n", $results) : "No Companies House results found.";
        $this->log[] = ['tool' => 'search_companies_house', 'input' => $query, 'output' => $result];
        return $result;
    }

    /**
     * Look up a specific company on Companies House by company number.
     * Returns associative array with name, status, etc. or null if not found.
     */
    public function lookupCompaniesHouseByNumber(string $companyNumber): ?array
    {
        $url = "https://find-and-update.company-information.service.gov.uk/company/" . urlencode($companyNumber);
        $html = $this->httpGet($url);
        if (!$html) return null;

        // Extract company name from the page title or heading
        $name = null;
        if (preg_match('/<h1[^>]*class="[^"]*heading-xlarge[^"]*"[^>]*>\s*([^<]+)/i', $html, $m)) {
            $name = trim(html_entity_decode($m[1]));
        } elseif (preg_match('/<title>([^<]+)<\/title>/i', $html, $m)) {
            $title = trim(html_entity_decode($m[1]));
            // Title format is usually "COMPANY NAME - Find and update company information"
            $name = preg_replace('/\s*[-–].*$/', '', $title);
        }
        if (!$name) return null;

        // Extract company status
        $status = null;
        if (preg_match('/Company status\s*<\/dt>\s*<dd[^>]*>\s*([^<]+)/i', $html, $m)) {
            $status = trim(html_entity_decode($m[1]));
        }

        $this->log[] = ['tool' => 'lookup_companies_house_by_number', 'input' => $companyNumber, 'output' => "Name: {$name}, Status: {$status}"];

        return [
            'company_name' => $name,
            'company_status' => $status,
            'company_number' => $companyNumber,
            'url' => $url,
        ];
    }

    /**
     * Find companies where a UK entity serves as a corporate officer (director).
     * Searches the CH officers index for the entity name, then fetches appointments.
     * Returns array of ['company_name' => ..., 'company_number' => ..., 'role' => ..., 'appointed' => ..., 'status' => 'active|resigned']
     */
    public function companiesHouseCorporateAppointments(string $entityName): array
    {
        // Step 1: Search officers index for the corporate entity
        $searchUrl = "https://find-and-update.company-information.service.gov.uk/search/officers?q=" . urlencode($entityName);
        $html = $this->httpGet($searchUrl);
        if (!$html) {
            $this->log[] = ['tool' => 'ch_corporate_appointments', 'input' => $entityName, 'output' => 'Error: could not fetch officers search'];
            return [];
        }

        // Parse results — look for exact name match (case-insensitive)
        $officerId = null;
        $normTarget = strtoupper(trim($entityName));

        // Each result: <li> with <a href="/officers/{id}/appointments">{name}</a>
        if (preg_match_all('/<a[^>]*href="\/officers\/([^"\/]+)\/appointments"[^>]*>\s*([^<]+)/i', $html, $matches, PREG_SET_ORDER)) {
            foreach ($matches as $m) {
                $candidateName = strtoupper(trim(html_entity_decode($m[2])));
                if ($candidateName === $normTarget) {
                    $officerId = $m[1];
                    break;
                }
            }
        }

        if (!$officerId) {
            $this->log[] = ['tool' => 'ch_corporate_appointments', 'input' => $entityName, 'output' => 'No corporate officer match found'];
            return [];
        }

        // Step 2: Fetch appointments page
        $appointmentsUrl = "https://find-and-update.company-information.service.gov.uk/officers/{$officerId}/appointments";
        $html = $this->httpGet($appointmentsUrl);
        if (!$html) {
            $this->log[] = ['tool' => 'ch_corporate_appointments', 'input' => $entityName, 'output' => 'Error: could not fetch appointments page'];
            return [];
        }

        // Parse appointments — split HTML by company links, each section is an appointment
        $appointments = [];
        // Split by company links to get sections
        $sections = preg_split('/(?=<a[^>]*href="\/company\/\d+")/', $html);
        foreach ($sections as $section) {
            $companyName = null;
            $companyNumber = null;
            if (preg_match('/<a[^>]*href="\/company\/(\d+)"[^>]*>\s*([^<]+)/i', $section, $cm)) {
                $companyNumber = $cm[1];
                $companyName = trim(html_entity_decode($cm[2]));
                // Strip company number in parentheses from name, e.g. "GLOBAL HOLDCO LIMITED (14194682)"
                $companyName = preg_replace('/\s*\(\d+\)\s*$/', '', $companyName);
            }
            if (!$companyName || !$companyNumber || strlen($companyName) < 2) continue;

            // Role — from <dd> after "Role" <dt>
            $role = 'Director';
            if (preg_match('/appointment-type-value\d*"[^>]*>\s*([^<]+)/i', $section, $rm)) {
                $role = trim($rm[1]);
            }

            // Appointment status — "Resigned" or "Active" in status-tag
            $appointmentStatus = 'active';
            if (preg_match('/class="status-tag[^"]*"[^>]*>\s*(Resigned|Active)\s*</i', $section, $sm)) {
                $appointmentStatus = strtolower(trim($sm[1]));
            }

            // Company status — Active/Dissolved
            $companyStatus = null;
            if (preg_match('/company-status-value[^>]*>\s*([^<]+)/i', $section, $csm)) {
                $companyStatus = trim($csm[1]);
            }

            // Appointed date
            $appointed = null;
            if (preg_match('/appointed-value\d*"[^>]*>\s*([^<]+)/i', $section, $am)) {
                $appointed = trim($am[1]);
            }

            $appointments[] = [
                'company_name' => $companyName,
                'company_number' => $companyNumber,
                'role' => $role,
                'appointed' => $appointed,
                'status' => $appointmentStatus,
                'company_status' => $companyStatus,
            ];
        }

        $activeCount = count(array_filter($appointments, fn($a) => $a['status'] === 'active'));
        $this->log[] = ['tool' => 'ch_corporate_appointments', 'input' => $entityName,
            'output' => count($appointments) . " appointments found ({$activeCount} active) via officer ID {$officerId}"];

        return $appointments;
    }

    // ── Companies House Brand Search (API) ─────────────────────────────────

    /**
     * Search CH API for a brand/short name, then filter results to only those
     * sharing a postcode or director with the known entities.
     *
     * @param string $brandName  Short/brand name to search (e.g. "Inflexion")
     * @param array $knownPostcodes  Postcodes from entities already found (e.g. ["W1H 2HR"])
     * @param array $knownOfficers  Officer surnames from entities already found (e.g. ["HAZELL-SMITH", "SEGAL"])
     * @param array $knownCompanyNumbers  Company numbers already found (to skip)
     * @return array  Matching companies with name, number, address, status, match reason, officers
     */
    public function companiesHouseBrandSearch(string $brandName, array $knownPostcodes, array $knownOfficers, array $knownCompanyNumbers = []): array
    {
        $apiKey = $this->config['companies_house_api_key'] ?? '';
        if (!$apiKey) return [];

        // Normalise known data for comparison
        $knownPostcodesNorm = array_map(fn($p) => strtoupper(preg_replace('/\s+/', '', $p)), $knownPostcodes);
        $knownOfficersNorm = array_map('strtoupper', $knownOfficers);
        $knownNumbersSet = array_flip($knownCompanyNumbers);

        // Search CH API — fetch up to 40 results (2 pages)
        $candidates = [];
        for ($page = 0; $page < 2; $page++) {
            $startIndex = $page * 20;
            $url = "https://api.company-information.service.gov.uk/search/companies?"
                . http_build_query(['q' => $brandName, 'items_per_page' => 20, 'start_index' => $startIndex]);
            $json = $this->chApiGet($url, $apiKey);
            if (!$json) break;
            $data = json_decode($json, true);
            $items = $data['items'] ?? [];
            if (empty($items)) break;
            foreach ($items as $item) {
                $number = $item['company_number'] ?? '';
                $status = $item['company_status'] ?? 'unknown';
                if (isset($knownNumbersSet[$number])) continue;
                if ($status === 'dissolved') continue;
                $candidates[] = [
                    'company_name' => $item['title'] ?? '',
                    'company_number' => $number,
                    'company_status' => $status,
                    'address' => $item['address_snippet'] ?? '',
                    'postal_code' => strtoupper(preg_replace('/\s+/', '', $item['address']['postal_code'] ?? '')),
                ];
            }
        }

        // Phase 1: Filter by postcode match
        $matched = [];
        $needOfficerCheck = [];
        foreach ($candidates as $c) {
            if ($c['postal_code'] && in_array($c['postal_code'], $knownPostcodesNorm)) {
                $c['match_reason'] = 'address (shared postcode)';
                $matched[] = $c;
            } else {
                $needOfficerCheck[] = $c;
            }
        }

        // Phase 2: For non-postcode matches, check officers (limit API calls)
        $officerChecks = 0;
        $maxOfficerChecks = 10;
        foreach ($needOfficerCheck as $c) {
            if ($officerChecks >= $maxOfficerChecks) break;
            $officerChecks++;
            $officersUrl = "https://api.company-information.service.gov.uk/company/{$c['company_number']}/officers?items_per_page=50";
            $officersJson = $this->chApiGet($officersUrl, $apiKey);
            if (!$officersJson) continue;
            $officersData = json_decode($officersJson, true);
            $officers = $officersData['items'] ?? [];
            $officerNames = [];
            foreach ($officers as $o) {
                if ($o['resigned_on'] ?? null) continue; // skip resigned
                $name = strtoupper($o['name'] ?? '');
                $officerNames[] = $name;
                // CH format is "SURNAME, Forename" — extract surname
                $surname = trim(explode(',', $name)[0]);
                $role = $o['officer_role'] ?? 'officer';
                if ($surname && in_array($surname, $knownOfficersNorm)) {
                    $c['match_reason'] = "shared {$role} ({$o['name']})";
                    $c['officers'] = array_map(fn($o2) => $o2['name'] ?? '', $officers);
                    $matched[] = $c;
                    break;
                }
            }
        }

        $this->log[] = ['tool' => 'ch_brand_search', 'input' => $brandName,
            'output' => count($candidates) . " candidates, " . count($matched) . " matched (postcodes: " . implode(', ', $knownPostcodes) . ", officers: " . implode(', ', $knownOfficers) . ")"];

        return $matched;
    }

    /**
     * Fetch company officers from CH API. Returns array of officer name strings.
     */
    public function companiesHouseGetOfficers(string $companyNumber): array
    {
        $apiKey = $this->config['companies_house_api_key'] ?? '';
        if (!$apiKey) return [];
        $url = "https://api.company-information.service.gov.uk/company/{$companyNumber}/officers?items_per_page=50";
        $json = $this->chApiGet($url, $apiKey);
        if (!$json) return [];
        $data = json_decode($json, true);
        $names = [];
        foreach ($data['items'] ?? [] as $o) {
            if (($o['resigned_on'] ?? null)) continue; // skip resigned
            $names[] = $o['name'] ?? '';
        }
        return $names;
    }

    /**
     * Fetch company details from CH API. Returns registered address postcode and company name.
     */
    public function companiesHouseGetCompany(string $companyNumber): ?array
    {
        $apiKey = $this->config['companies_house_api_key'] ?? '';
        if (!$apiKey) return null;
        $url = "https://api.company-information.service.gov.uk/company/{$companyNumber}";
        $json = $this->chApiGet($url, $apiKey);
        if (!$json) return null;
        $data = json_decode($json, true);
        return [
            'company_name' => $data['company_name'] ?? '',
            'company_number' => $data['company_number'] ?? $companyNumber,
            'company_status' => $data['company_status'] ?? 'unknown',
            'postal_code' => $data['registered_office_address']['postal_code'] ?? '',
            'address' => implode(', ', array_filter([
                $data['registered_office_address']['address_line_1'] ?? '',
                $data['registered_office_address']['locality'] ?? '',
                $data['registered_office_address']['postal_code'] ?? '',
            ])),
        ];
    }

    private function chApiGet(string $url, string $apiKey): ?string
    {
        $ch = curl_init($url);
        curl_setopt_array($ch, [
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_TIMEOUT => 10,
            CURLOPT_USERPWD => "{$apiKey}:",
            CURLOPT_HTTPAUTH => CURLAUTH_BASIC,
        ]);
        $result = curl_exec($ch);
        $httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);
        return ($httpCode === 200 && $result !== false) ? $result : null;
    }

    // ── Companies House Ownership Chain ──────────────────────────────────────

    public function companiesHouseOwnershipChain(string $companyNumber): string
    {
        $chain = [];
        $visited = [];
        $current = $companyNumber;

        for ($level = 0; $level < $this->config['max_ownership_levels']; $level++) {
            if (in_array($current, $visited)) {
                $chain[] = "  [circular reference to {$current}]";
                break;
            }
            $visited[] = $current;

            // Fetch company overview for name
            $overviewUrl = "https://find-and-update.company-information.service.gov.uk/company/{$current}";
            $html = $this->httpGet($overviewUrl);
            $companyName = $current;
            if ($html && preg_match('/<h1[^>]*>([^<]+)<\/h1>/i', $html, $m)) {
                $companyName = trim(html_entity_decode($m[1]));
            }
            $confirmed = ($html && $companyName !== $current) ? ' (confirmed)' : '';
            $chain[] = "{$companyName} (#{$current}){$confirmed} | {$overviewUrl}";

            // Fetch PSC page
            $pscUrl = "https://find-and-update.company-information.service.gov.uk/company/{$current}/persons-with-significant-control";
            $pscHtml = $this->httpGet($pscUrl);
            if (!$pscHtml) {
                $chain[] = "  [TOP OF CHAIN]";
                break;
            }

            $pscText = $this->htmlToText($pscHtml);
            $lines = explode("\n", $pscText);

            // Parse the first Active corporate PSC entry
            $inActivePsc = false;
            $pscName = '';
            $pscRegNum = '';
            $pscOwnership = '';
            $pscIncorporatedIn = '';

            for ($i = 0; $i < count($lines); $i++) {
                $line = trim($lines[$i]);

                if (!$inActivePsc && $line === 'Active' && $i > 0) {
                    $prev = trim($lines[$i - 1]);
                    if ($this->looksLikeCompany($prev)) {
                        $inActivePsc = true;
                        $pscName = $prev;
                        continue;
                    }
                }

                if (!$inActivePsc) continue;

                // Collect fields
                if ($line === 'Registration number' && $i + 1 < count($lines)) {
                    $pscRegNum = trim($lines[$i + 1]);
                }
                if (str_starts_with($line, 'Incorporated in') && $i + 1 < count($lines)) {
                    $pscIncorporatedIn = strtolower(trim($lines[$i + 1]));
                } elseif (str_starts_with(strtolower($line), 'incorporated in')) {
                    $parts = explode('in', $line, 2);
                    $pscIncorporatedIn = strtolower(trim($parts[1] ?? ''));
                }
                if (stripos($line, 'ownership of shares') !== false) {
                    $pscOwnership = trim($line);
                }
                // End of PSC entry
                if (in_array($line, ['Ceased', 'Ceased on']) && $i > 0) {
                    break;
                }
                if ($line === 'Active' && $pscRegNum) {
                    break;
                }
            }

            $corporateOwner = null;

            if ($inActivePsc && $pscRegNum) {
                // Check ownership > 50%
                $ownershipLower = strtolower($pscOwnership);
                $ownsMajority = (str_contains($ownershipLower, '75%') ||
                    (str_contains($ownershipLower, 'more than 50%') &&
                     !str_contains($ownershipLower, 'not more than 50%')));
                if (!$pscOwnership) {
                    $ownsMajority = true; // No info = assume majority
                }

                // Check UK-registered
                $prefix = strtoupper(substr($pscRegNum, 0, 2));
                $isUk = ctype_digit($pscRegNum) || in_array($prefix, ['OC', 'SC', 'NI', 'SO', 'NC']);
                if ($pscIncorporatedIn && !in_array($pscIncorporatedIn, [
                    'uk', 'united kingdom', 'england', 'wales',
                    'scotland', 'northern ireland', 'england and wales'
                ])) {
                    $isUk = false;
                }

                if ($ownsMajority && $isUk) {
                    if (ctype_digit($pscRegNum)) {
                        $pscRegNum = str_pad($pscRegNum, 8, '0', STR_PAD_LEFT);
                    } else {
                        $pscRegNum = strtoupper($pscRegNum);
                    }
                    $corporateOwner = $pscRegNum;
                    $chain[] = "  ↑ owned ({$pscOwnership}) by: {$pscName} (#{$pscRegNum})";
                } elseif (!$ownsMajority) {
                    $chain[] = "  [STOP: {$pscName} owns ≤50%: {$pscOwnership}]";
                } elseif ($ownsMajority && !$isUk) {
                    // Non-UK parent — record it as unconfirmed, stop here
                    $country = $pscIncorporatedIn ?: 'unknown';
                    $chain[] = "  ↑ owned ({$pscOwnership}) by: {$pscName} (#{$pscRegNum}, incorporated in: {$country}) (unconfirmed)";
                    $chain[] = "  [TOP OF CHAIN — non-UK entity, cannot trace further via Companies House]";
                    break;
                }
            }

            if ($corporateOwner) {
                $current = $corporateOwner;
            } else {
                // No corporate owner — list individuals
                $individuals = [];
                for ($i = 0; $i < count($lines); $i++) {
                    if (trim($lines[$i]) === 'Active' && $i > 0) {
                        $prev = trim($lines[$i - 1]);
                        if (preg_match('/^(Mr|Ms|Mrs|Dr)\s/', $prev)) {
                            $individuals[] = $prev;
                        }
                    }
                }
                if ($individuals) {
                    $chain[] = "  ↑ owned by individuals: " . implode(', ', $individuals);
                }
                $chain[] = "  [TOP OF CHAIN]";
                break;
            }
        }

        $result = implode("\n", $chain);
        $this->log[] = ['tool' => 'companies_house_ownership_chain', 'input' => $companyNumber, 'output' => $result];
        return $result;
    }

    private function looksLikeCompany(string $name): bool
    {
        $lower = strtolower($name);
        foreach (['ltd', 'limited', 'llp', 'plc', 'inc', 'ag', 'gmbh', 'sa', 'sarl', 'bv', 'nv', 'se', 'srl', 'spa', 'as', 'aps', 'ab', 'oy', 'corp', 'llc', 'lp'] as $suffix) {
            if (preg_match('/\b' . preg_quote($suffix, '/') . '\b/', $lower)) return true;
        }
        return false;
    }

    // ── SEC EDGAR ────────────────────────────────────────────────────────────

    public function searchSecCompany(string $query): string
    {
        // SEC EDGAR stores names without periods or commas (e.g. "AMAZON COM INC" not "Amazon.com, Inc.")
        $cleanQuery = str_replace(['.', ','], [' ', ' '], $query);
        $cleanQuery = preg_replace('/\s+/', ' ', trim($cleanQuery));
        $url = "https://www.sec.gov/cgi-bin/browse-edgar?company=" . urlencode($cleanQuery)
            . "&CIK=&type=&dateb=&owner=include&count=20&search_text=&action=getcompany";
        $html = $this->httpGet($url, $this->config['sec_user_agent']);
        if (!$html) {
            return "Error: Could not reach SEC EDGAR.";
        }

        $results = [];
        // Multi-result: table rows with CIK + name
        if (preg_match_all('/<tr[^>]*>.*?<td[^>]*><a[^>]*>([^<]+)<\/a><\/td>\s*<td[^>]*>([^<]*)<\/td>/is', $html, $matches, PREG_SET_ORDER)) {
            foreach ($matches as $match) {
                $cik = trim($match[1]);
                $name = trim(html_entity_decode($match[2]));
                if ($cik && $name) {
                    $results[] = "CIK: {$cik} | {$name}";
                }
            }
        }
        // Single-result: SEC redirects to company detail page with companyName span
        if (empty($results) && preg_match('/<span class="companyName">(.+?)<acronym.*?CIK.*?(\d{10})/is', $html, $m)) {
            $name = trim(html_entity_decode($m[1]));
            $cik = trim($m[2]);
            if ($name && $cik) {
                $results[] = "CIK: {$cik} | {$name}";
            }
        }

        $result = $results ? implode("\n", array_slice($results, 0, 20)) : "No SEC company results found.";
        $this->log[] = ['tool' => 'search_sec_company', 'input' => $query, 'output' => $result];
        return $result;
    }

    public function searchSecFulltext(string $query): string
    {
        $url = "https://efts.sec.gov/LATEST/search-index?q=" . urlencode($query);
        $json = $this->httpGet($url, $this->config['sec_user_agent']);
        if (!$json) {
            return "Error: Could not reach SEC fulltext.";
        }

        $data = json_decode($json, true);
        if (!$data) {
            return "Error: Invalid SEC fulltext response.";
        }

        $total = $data['hits']['total']['value'] ?? 0;
        $hits = $data['hits']['hits'] ?? [];
        $lines = ["Total hits: {$total}"];
        foreach (array_slice($hits, 0, 10) as $h) {
            $s = $h['_source'] ?? [];
            $names = implode(', ', $s['display_names'] ?? []);
            $lines[] = "  " . ($s['file_date'] ?? '') . " | {$names} | " . ($s['form_type'] ?? '');
        }

        $result = implode("\n", $lines);
        $this->log[] = ['tool' => 'search_sec_fulltext', 'input' => $query, 'output' => $result];
        return $result;
    }

    public function fetchSecSubmissions(string $cik): string
    {
        $cikPadded = str_pad($cik, 10, '0', STR_PAD_LEFT);
        $url = "https://data.sec.gov/submissions/CIK{$cikPadded}.json";
        $json = $this->httpGet($url, $this->config['sec_user_agent']);
        if (!$json) {
            return "Error: Could not fetch SEC submissions.";
        }

        $data = json_decode($json, true);
        if (!$data) {
            return "Error: Invalid SEC submissions response.";
        }

        $info = [
            'name' => $data['name'] ?? null,
            'cik' => $data['cik'] ?? null,
            'tickers' => $data['tickers'] ?? [],
            'exchanges' => $data['exchanges'] ?? [],
            'sic' => $data['sic'] ?? null,
            'sicDescription' => $data['sicDescription'] ?? null,
            'entityType' => $data['entityType'] ?? null,
            'formerNames' => $data['formerNames'] ?? [],
            'addresses' => $data['addresses'] ?? [],
            'phone' => $data['phone'] ?? null,
        ];

        $recent = $data['filings']['recent'] ?? [];
        $forms = $recent['form'] ?? [];
        $info['total_filings'] = count($forms);

        if ($forms) {
            $info['latest_filings'] = [];
            for ($i = 0; $i < min(20, count($forms)); $i++) {
                $formType = $forms[$i];
                // Keep first 5 of any type, plus any 8-K in the first 20
                if (count($info['latest_filings']) < 5 || $formType === '8-K') {
                    $info['latest_filings'][] = [
                        'form' => $formType,
                        'date' => ($recent['filingDate'] ?? [])[$i] ?? '',
                        'accession' => ($recent['accessionNumber'] ?? [])[$i] ?? '',
                        'primaryDocument' => ($recent['primaryDocument'] ?? [])[$i] ?? '',
                    ];
                }
                // Stop once we have the basics + at least one 8-K
                if (count($info['latest_filings']) >= 5) {
                    $has8K = false;
                    foreach ($info['latest_filings'] as $f) {
                        if ($f['form'] === '8-K') { $has8K = true; break; }
                    }
                    if ($has8K) break;
                }
            }
        }

        $result = json_encode($info, JSON_PRETTY_PRINT);
        $this->log[] = ['tool' => 'fetch_sec_submissions', 'input' => $cik, 'output' => $result];
        return $result;
    }

    /**
     * Fetch and parse the cover page of the most recent 8-K filing for a CIK.
     * Returns structured entity data (name, state of incorporation, EIN, address, phone).
     */
    public function fetchSec8K(string $cik, array $submissions): ?array
    {
        // Find the most recent 8-K in submissions
        $recent = $submissions['latest_filings'] ?? [];
        $filing = null;
        foreach ($recent as $f) {
            if (($f['form'] ?? '') === '8-K' && !empty($f['primaryDocument'])) {
                $filing = $f;
                break;
            }
        }
        if (!$filing) return null;

        $accession = str_replace('-', '', $filing['accession']);
        $cikClean = ltrim($cik, '0');
        $url = "https://www.sec.gov/Archives/edgar/data/{$cikClean}/{$accession}/{$filing['primaryDocument']}";

        $html = $this->httpGet($url, $this->config['sec_user_agent']);
        if (!$html) return null;

        // Strip HTML tags, decode entities, split into lines
        $text = preg_replace('/<[^>]+>/', "\n", $html);
        $text = html_entity_decode($text, ENT_QUOTES | ENT_HTML5, 'UTF-8');
        // Filter out empty lines and lines that are just whitespace/nbsp
        $lines = array_values(array_filter(array_map('trim', explode("\n", $text)), function($l) {
            $clean = preg_replace('/[\s\x{00A0}]+/u', '', $l);
            return $clean !== '';
        }));

        $result = ['filing_url' => $url, 'filing_date' => $filing['date'] ?? ''];

        // Find key lines by their label text, then grab the line(s) before them
        foreach ($lines as $i => $line) {
            if (stripos($line, 'Exact name of registrant') !== false && $i > 0) {
                $result['registered_name'] = trim($lines[$i - 1]);
            }
            if (stripos($line, 'State or other jurisdiction') !== false) {
                // State, file number, EIN are on 3 separate lines between "(Exact name..." and this label
                // Walk back, skip "(Exact name..." line and noise
                $vals = [];
                for ($j = $i - 1; $j >= max(0, $i - 6); $j--) {
                    $v = trim($lines[$j]);
                    if (str_starts_with($v, '(')) continue;
                    if ($v && strlen($v) < 50) {
                        $vals[] = $v;
                    }
                    if (count($vals) >= 3) break;
                }
                $vals = array_reverse($vals);
                if (count($vals) >= 3) {
                    $result['state_of_incorporation'] = $vals[0];
                    $result['commission_file_number'] = $vals[1];
                    $result['irs_ein'] = $vals[2];
                }
            }
            if (stripos($line, 'Address of principal executive offices') !== false && $i > 0) {
                // Address spans multiple lines above — collect until we hit a label or known field
                $addr = [];
                for ($j = $i - 1; $j >= max(0, $i - 6); $j--) {
                    $v = trim($lines[$j]);
                    if (!$v || str_starts_with($v, '(') || stripos($v, 'Identification') !== false) break;
                    // Skip bare punctuation
                    if ($v === ',' || $v === '.') { continue; }
                    // Skip bare state abbreviations that will be combined
                    array_unshift($addr, $v);
                }
                if ($addr) {
                    // Join and clean up: "1600 Amphitheatre Parkway" "Mountain View" "CA" "94043"
                    $result['address'] = preg_replace('/\s+/', ' ', implode(' ', $addr));
                }
            }
            if (stripos($line, 'telephone') !== false && stripos($line, 'Registrant') !== false && $i > 0) {
                // Phone digits may be split across lines: "(" "650" ")" "253-0000"
                $phoneParts = [];
                for ($j = $i - 1; $j >= max(0, $i - 5); $j--) {
                    $v = trim($lines[$j]);
                    if (str_starts_with($v, '(') && stripos($v, 'Address') !== false) break;
                    if (preg_match('/[\d\(\)\-]/', $v)) {
                        array_unshift($phoneParts, $v);
                    } else {
                        break;
                    }
                }
                if ($phoneParts) {
                    $phone = implode('', $phoneParts);
                    $phone = preg_replace('/[^\d\(\)\-\s]/', '', $phone);
                    $result['phone'] = trim($phone);
                }
            }
            if (stripos($line, 'Former name or former address') !== false && $i > 0) {
                $former = trim($lines[$i - 1]);
                if ($former && !preg_match('/^(Not Applicable|No Change|N\/A|None)$/i', $former)) {
                    $result['former_name'] = $former;
                }
            }
        }

        $this->log[] = ['tool' => 'fetch_sec_8k', 'input' => $cik, 'output' => json_encode($result)];
        return $result;
    }

    public function fetchSecFiling(string $url): string
    {
        $content = $this->httpGet($url, $this->config['sec_user_agent']);
        if (!$content) {
            return "Error: Could not fetch SEC filing.";
        }

        if (stripos($content, '<html') !== false) {
            $content = $this->htmlToText($content);
        }

        if (strlen($content) > 10000) {
            $content = substr($content, 0, 10000) . "\n... [truncated]";
        }

        $this->log[] = ['tool' => 'fetch_sec_filing', 'input' => $url, 'output' => $content];
        return $content;
    }

    public function secEdgarFinancials(string $cik): string
    {
        $paddedCik = str_pad($cik, 10, '0', STR_PAD_LEFT);
        $url = "https://data.sec.gov/api/xbrl/companyfacts/CIK{$paddedCik}.json";
        $json = $this->httpGet($url, $this->config['sec_user_agent']);
        if (!$json) {
            $this->log[] = ['tool' => 'sec_edgar_financials', 'input' => $cik, 'output' => 'No data'];
            return '';
        }

        $data = json_decode($json, true);
        if (!$data || empty($data['facts'])) {
            $this->log[] = ['tool' => 'sec_edgar_financials', 'input' => $cik, 'output' => 'Invalid JSON'];
            return '';
        }

        $entityName = $data['entityName'] ?? 'Unknown';
        $gaap = $data['facts']['us-gaap'] ?? [];
        $dei = $data['facts']['dei'] ?? [];
        $allFacts = array_merge($gaap, $dei);

        $targets = [
            'Revenue' => ['RevenueFromContractWithCustomerExcludingAssessedTax', 'RevenueFromContractWithCustomerIncludingAssessedTax', 'Revenues', 'RevenuesNetOfInterestExpense', 'SalesRevenueNet'],
            'Net Income' => ['NetIncomeLoss'],
            'Total Assets' => ['Assets'],
            'Total Equity' => ['StockholdersEquity'],
            'Operating Income' => ['OperatingIncomeLoss'],
            'Cash' => ['CashAndCashEquivalentsAtCarryingValue'],
            'Total Liabilities' => ['Liabilities'],
        ];

        $yearData = []; // metric => year => value
        $allYears = [];

        foreach ($targets as $label => $tags) {
            foreach ($tags as $tag) {
                if (!isset($allFacts[$tag])) continue;
                $units = $allFacts[$tag]['units'] ?? [];
                foreach ($units as $unitName => $entries) {
                    $annual = array_filter($entries, function ($e) {
                        return ($e['form'] ?? '') === '10-K' && ($e['fp'] ?? '') === 'FY';
                    });
                    foreach ($annual as $entry) {
                        $year = substr($entry['end'] ?? '', 0, 4);
                        if (!$year) continue;
                        $allYears[$year] = true;
                        if (!isset($yearData[$label][$year])) {
                            $yearData[$label][$year] = $entry['val'];
                        }
                    }
                }
                if (!empty($yearData[$label])) break; // use first matching tag
            }
        }

        if (empty($yearData) || empty($allYears)) {
            $this->log[] = ['tool' => 'sec_edgar_financials', 'input' => $cik, 'output' => 'No annual data'];
            return '';
        }

        // Most recent 3 years
        $years = array_keys($allYears);
        sort($years);
        $years = array_slice($years, -3);

        $md = [];
        $md[] = "### SEC EDGAR Financials — {$entityName}";
        $md[] = '';
        $header = '| Metric | ' . implode(' | ', $years) . ' |';
        $sep = '|---|' . implode('|', array_fill(0, count($years), '---')) . '|';
        $md[] = $header;
        $md[] = $sep;

        foreach ($targets as $label => $tags) {
            if (!isset($yearData[$label])) continue;
            $vals = [];
            foreach ($years as $y) {
                if (isset($yearData[$label][$y])) {
                    $v = $yearData[$label][$y];
                    if (abs($v) >= 1e9) {
                        $vals[] = '$' . number_format($v / 1e9, 1) . 'B';
                    } elseif (abs($v) >= 1e6) {
                        $vals[] = '$' . number_format($v / 1e6, 1) . 'M';
                    } else {
                        $vals[] = '$' . number_format($v);
                    }
                } else {
                    $vals[] = '—';
                }
            }
            $md[] = '| ' . $label . ' | ' . implode(' | ', $vals) . ' |';
        }

        $result = implode("\n", $md);
        $this->log[] = ['tool' => 'sec_edgar_financials', 'input' => $cik, 'output' => substr($result, 0, 500)];
        return $result;
    }

    // ── Google Intelligence ─────────────────────────────────────────────────

    /**
     * Run 3 Google searches in a single Bright Data SERP batch:
     *   1. {domain} — general Google results
     *   2. site:finance.yahoo.com {domain} — find Yahoo Finance ticker
     *   3. {domain} linkedin — find LinkedIn company page
     *
     * Returns ['google_results' => string, 'yahoo_ticker' => ?string, 'linkedin_url' => ?string]
     */
    public function googleIntelligence(string $domain): array
    {
        $apiKey = $this->config['brightdata_api_key'] ?? '';
        $result = ['google_results' => '', 'yahoo_ticker' => null, 'linkedin_url' => null];

        if (!$apiKey) {
            $this->progress('google', "Google Intelligence: Bright Data not configured");
            return $result;
        }

        $this->progress('google', "Google Intelligence: searching 3 queries for {$domain}...");
        $this->apiCalls['brightdata']++;

        $ch = curl_init('https://api.brightdata.com/datasets/v3/scrape?dataset_id=gd_mfz5x93lmsjjjylob&notify=false&include_errors=true');
        curl_setopt_array($ch, [
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_POST => true,
            CURLOPT_HTTPHEADER => [
                'Content-Type: application/json',
                "Authorization: Bearer {$apiKey}",
            ],
            CURLOPT_POSTFIELDS => json_encode([
                'input' => [
                    ['url' => 'https://www.google.com/', 'keyword' => $domain],
                    ['url' => 'https://www.google.com/', 'keyword' => "site:finance.yahoo.com {$domain}"],
                    ['url' => 'https://www.google.com/', 'keyword' => "{$domain} linkedin"],
                ],
                'limit_per_input' => 10,
            ]),
            CURLOPT_TIMEOUT => 90,
        ]);
        $resp = curl_exec($ch);
        $httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);

        if ($httpCode !== 200 || !$resp) {
            $this->progress('google', "Google Intelligence: SERP failed (HTTP {$httpCode})");
            $this->log[] = ['tool' => 'google_intelligence', 'input' => $domain, 'output' => "HTTP {$httpCode}"];
            return $result;
        }

        // Response is NDJSON (one JSON object per line)
        $lines = explode("\n", trim($resp));
        $serpResults = [];
        foreach ($lines as $line) {
            $line = trim($line);
            if (!$line) continue;
            $parsed = json_decode($line, true);
            if ($parsed) $serpResults[] = $parsed;
        }

        $this->progress('google', "Google Intelligence: got " . count($serpResults) . " SERP results");

        // Process each result by keyword
        $googleMd = [];
        foreach ($serpResults as $serp) {
            $keyword = $serp['keyword'] ?? '';
            $organic = $serp['organic'] ?? [];

            if ($keyword === $domain) {
                // General Google results — format top results as markdown
                $googleMd[] = "### Google Search Results for {$domain}";
                $googleMd[] = '';
                foreach (array_slice($organic, 0, 10) as $r) {
                    $title = $r['title'] ?? '';
                    $link = $r['link'] ?? '';
                    $desc = $r['description'] ?? '';
                    $googleMd[] = "- **{$title}**";
                    $googleMd[] = "  {$link}";
                    if ($desc) $googleMd[] = "  {$desc}";
                }
            } elseif (str_starts_with($keyword, 'site:finance.yahoo.com')) {
                // Yahoo Finance ticker extraction
                foreach ($organic as $r) {
                    $link = $r['link'] ?? '';
                    if (preg_match('#finance\.yahoo\.com/quote/([A-Z0-9a-z.\-]+)#', $link, $m)) {
                        $result['yahoo_ticker'] = $m[1];
                        $this->progress('google', "Found Yahoo Finance ticker: {$m[1]}");
                        break;
                    }
                }
            } elseif (str_contains($keyword, 'linkedin')) {
                // LinkedIn URL extraction — find company page
                foreach ($organic as $r) {
                    $link = $r['link'] ?? '';
                    if (preg_match('#linkedin\.com/company/[a-z0-9\-]+#i', $link)) {
                        $result['linkedin_url'] = $link;
                        $this->progress('google', "Found LinkedIn: {$link}");
                        break;
                    }
                }
            }
        }

        $result['google_results'] = implode("\n", $googleMd);
        $this->log[] = ['tool' => 'google_intelligence', 'input' => $domain, 'output' =>
            "google:" . strlen($result['google_results']) . " chars, " .
            "yahoo:" . ($result['yahoo_ticker'] ?? 'none') . ", " .
            "linkedin:" . ($result['linkedin_url'] ?? 'none')
        ];
        return $result;
    }

    /**
     * Fetch LinkedIn company page LD+JSON Organization data via Bright Data Web Unlocker.
     * Returns markdown string or empty string.
     */
    public function fetchLinkedInCompany(string $linkedinUrl): string
    {
        $apiKey = $this->config['brightdata_api_key'] ?? '';
        if (!$apiKey) return '';

        $this->progress('linkedin', "Fetching LinkedIn: {$linkedinUrl}...");
        $this->apiCalls['brightdata']++;

        $ch = curl_init('https://api.brightdata.com/request');
        curl_setopt_array($ch, [
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_POST => true,
            CURLOPT_HTTPHEADER => [
                'Content-Type: application/json',
                "Authorization: Bearer {$apiKey}",
            ],
            CURLOPT_POSTFIELDS => json_encode([
                'zone' => $this->config['brightdata_zone'] ?? 'web_unlocker1',
                'url' => $linkedinUrl,
                'format' => 'raw',
            ]),
            CURLOPT_TIMEOUT => 60,
        ]);
        $html = curl_exec($ch);
        $httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);

        if ($httpCode !== 200 || !$html) {
            $this->progress('linkedin', "LinkedIn fetch failed (HTTP {$httpCode})");
            return '';
        }

        // Extract LD+JSON Organization data
        $org = null;
        if (preg_match_all('#<script type="application/ld\+json">(.*?)</script>#s', $html, $matches)) {
            foreach ($matches[1] as $jsonStr) {
                $data = json_decode($jsonStr, true);
                if (!$data) continue;
                if (isset($data['@type']) && $data['@type'] === 'Organization') {
                    $org = $data;
                    break;
                }
                if (isset($data['@graph'])) {
                    foreach ($data['@graph'] as $item) {
                        if (isset($item['@type']) && $item['@type'] === 'Organization') {
                            $org = $item;
                            break 2;
                        }
                    }
                }
            }
        }

        if (!$org) {
            $this->progress('linkedin', "LinkedIn: no Organization data found");
            $this->log[] = ['tool' => 'linkedin', 'input' => $linkedinUrl, 'output' => 'No LD+JSON'];
            return '';
        }

        // Format as markdown
        $md = [];
        $md[] = "### LinkedIn Company Profile";
        $md[] = "Source: {$linkedinUrl}";
        $md[] = '';
        if (!empty($org['name'])) $md[] = "- Name: {$org['name']}";
        if (!empty($org['address'])) {
            $addr = $org['address'];
            $parts = array_filter([
                $addr['streetAddress'] ?? '',
                $addr['addressLocality'] ?? '',
                $addr['postalCode'] ?? '',
                $addr['addressCountry'] ?? '',
            ]);
            $md[] = "- Address: " . implode(', ', $parts);
        }
        if (!empty($org['numberOfEmployees']['value'])) {
            $md[] = "- Employees: " . $org['numberOfEmployees']['value'];
        }
        if (!empty($org['sameAs'])) $md[] = "- Website: {$org['sameAs']}";
        if (!empty($org['slogan'])) $md[] = "- Slogan: {$org['slogan']}";
        if (!empty($org['description'])) {
            $desc = $org['description'];
            $md[] = '';
            $md[] = '**Description**';
            $md[] = substr($desc, 0, 800) . (strlen($desc) > 800 ? '...' : '');
        }

        $result = implode("\n", $md);
        $this->progress('linkedin', "LinkedIn: got " . substr_count($result, "\n") . " lines for " . ($org['name'] ?? 'unknown'));
        $this->log[] = ['tool' => 'linkedin', 'input' => $linkedinUrl, 'output' => substr($result, 0, 300)];
        return $result;
    }

    // ── Yahoo Finance ────────────────────────────────────────────────────────

    /**
     * Fetch Yahoo Finance profile + financials for a ticker.
     * Returns markdown string or empty string if unavailable.
     */
    public function yahooFinanceData(string $ticker): string
    {
        $this->progress('yahoo', "Yahoo Finance: fetching data for {$ticker}...");

        // Step 1: Get crumb + cookies
        $cookieFile = tempnam(sys_get_temp_dir(), 'yf_');
        $ch = curl_init('https://fc.yahoo.com/t');
        curl_setopt_array($ch, [
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_COOKIEJAR => $cookieFile,
            CURLOPT_COOKIEFILE => $cookieFile,
            CURLOPT_USERAGENT => 'Mozilla/5.0',
            CURLOPT_TIMEOUT => 15,
        ]);
        curl_exec($ch);
        curl_close($ch);

        $ch = curl_init('https://query2.finance.yahoo.com/v1/test/getcrumb');
        curl_setopt_array($ch, [
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_COOKIEFILE => $cookieFile,
            CURLOPT_USERAGENT => 'Mozilla/5.0',
            CURLOPT_TIMEOUT => 15,
        ]);
        $crumb = curl_exec($ch);
        curl_close($ch);

        if (!$crumb || strlen($crumb) > 50) {
            $this->progress('yahoo', "Yahoo Finance: failed to get crumb");
            @unlink($cookieFile);
            return '';
        }

        // Step 2: Fetch profile + financials
        $modules = 'assetProfile,incomeStatementHistory,balanceSheetHistory';
        $url = "https://query2.finance.yahoo.com/v10/finance/quoteSummary/{$ticker}?modules={$modules}&crumb=" . urlencode($crumb);
        $ch = curl_init($url);
        curl_setopt_array($ch, [
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_COOKIEFILE => $cookieFile,
            CURLOPT_USERAGENT => 'Mozilla/5.0',
            CURLOPT_TIMEOUT => 20,
        ]);
        $resp = curl_exec($ch);
        curl_close($ch);
        @unlink($cookieFile);

        $data = json_decode($resp, true);
        $results = $data['quoteSummary']['result'] ?? [];
        if (empty($results)) {
            $this->progress('yahoo', "Yahoo Finance: no data for {$ticker}");
            $this->log[] = ['tool' => 'yahoo_finance_data', 'input' => $ticker, 'output' => 'No data'];
            return '';
        }

        $result = $results[0];
        $md = [];
        $md[] = "### Yahoo Finance — {$ticker}";
        $md[] = "Source: https://finance.yahoo.com/quote/{$ticker}/";
        $md[] = '';

        // Profile
        $profile = $result['assetProfile'] ?? [];
        if ($profile) {
            $md[] = '**Company Profile**';
            $fields = [
                'website' => 'Website',
                'sector' => 'Sector',
                'industry' => 'Industry',
                'fullTimeEmployees' => 'Employees',
                'city' => 'City',
                'state' => 'State',
                'country' => 'Country',
            ];
            foreach ($fields as $key => $label) {
                $val = $profile[$key] ?? null;
                if ($val !== null && $val !== '') {
                    if ($key === 'fullTimeEmployees') $val = number_format($val);
                    $md[] = "- {$label}: {$val}";
                }
            }

            // Officers
            $officers = $profile['companyOfficers'] ?? [];
            if ($officers) {
                $md[] = '';
                $md[] = '**Key Officers**';
                foreach (array_slice($officers, 0, 5) as $officer) {
                    $name = $officer['name'] ?? 'Unknown';
                    $title = $officer['title'] ?? '';
                    $md[] = "- {$name}" . ($title ? " — {$title}" : '');
                }
            }

            // Business summary
            $summary = $profile['longBusinessSummary'] ?? '';
            if ($summary) {
                $md[] = '';
                $md[] = '**Description**';
                $md[] = substr($summary, 0, 500) . (strlen($summary) > 500 ? '...' : '');
            }
            $md[] = '';
        }

        // Income Statement
        $incomeStmts = $result['incomeStatementHistory']['incomeStatementHistory'] ?? [];
        if ($incomeStmts) {
            $md[] = '**Income Statement (Annual)**';
            $md[] = '| Period | Revenue | Net Income |';
            $md[] = '|---|---|---|';
            foreach (array_slice($incomeStmts, 0, 3) as $stmt) {
                $date = $stmt['endDate']['fmt'] ?? '?';
                $rev = $this->yahooFormatVal($stmt['totalRevenue'] ?? []);
                $ni = $this->yahooFormatVal($stmt['netIncome'] ?? []);
                $md[] = "| {$date} | {$rev} | {$ni} |";
            }
            $md[] = '';
        }

        // Balance Sheet
        $balanceStmts = $result['balanceSheetHistory']['balanceSheetStatements'] ?? [];
        if ($balanceStmts) {
            $md[] = '**Balance Sheet (Most Recent)**';
            $bs = $balanceStmts[0];
            $date = $bs['endDate']['fmt'] ?? '?';
            $md[] = "As of {$date}:";
            $bsFields = [
                'totalAssets' => 'Total Assets',
                'totalLiab' => 'Total Liabilities',
                'totalStockholderEquity' => 'Stockholder Equity',
                'cash' => 'Cash',
            ];
            foreach ($bsFields as $key => $label) {
                $val = $this->yahooFormatVal($bs[$key] ?? []);
                if ($val !== '—') {
                    $md[] = "- {$label}: {$val}";
                }
            }
        }

        $result = implode("\n", $md);
        $lineCount = substr_count($result, "\n");
        $this->progress('yahoo', "Yahoo Finance: got {$lineCount} lines for {$ticker}");
        $this->log[] = ['tool' => 'yahoo_finance_data', 'input' => $ticker, 'output' => substr($result, 0, 500)];
        return $result;
    }

    private function yahooFormatVal(array $field): string
    {
        $raw = $field['raw'] ?? null;
        if ($raw === null) return '—';
        $abs = abs($raw);
        $sign = $raw < 0 ? '-' : '';
        if ($abs >= 1e12) return $sign . number_format($abs / 1e12, 1) . 'T';
        if ($abs >= 1e9) return $sign . number_format($abs / 1e9, 1) . 'B';
        if ($abs >= 1e6) return $sign . number_format($abs / 1e6, 1) . 'M';
        if ($abs >= 1e3) return $sign . number_format($abs / 1e3, 1) . 'K';
        return $sign . number_format($abs);
    }

    // ── North Data ───────────────────────────────────────────────────────────

    private function getNorthdataAuthCookie(): ?string
    {
        if ($this->northdataAuthCookie !== null) {
            return $this->northdataAuthCookie ?: null;
        }

        $email = $this->config['northdata_email'] ?? '';
        $password = $this->config['northdata_password'] ?? '';
        if (!$email || !$password) {
            $this->progress('northdata', "NorthData auth: no credentials configured");
            $this->northdataAuthCookie = '';
            return null;
        }

        $ch = curl_init('https://www.northdata.com/rpc.json/user/login');
        curl_setopt_array($ch, [
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_POST => true,
            CURLOPT_HTTPHEADER => ['Content-Type: application/x-www-form-urlencoded'],
            CURLOPT_POSTFIELDS => http_build_query([
                'email' => $email,
                'password' => $password,
            ]),
            CURLOPT_HEADER => true,
            CURLOPT_TIMEOUT => 15,
        ]);
        $response = curl_exec($ch);
        $httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);
        $curlError = curl_error($ch);
        curl_close($ch);

        if (preg_match('/set-cookie:\s*auth=([^;]+)/i', $response, $m)) {
            $this->northdataAuthCookie = $m[1];
            $this->progress('northdata', "NorthData auth: logged in successfully");
            return $this->northdataAuthCookie;
        }

        $this->progress('northdata', "NorthData auth: login failed (HTTP {$httpCode}" . ($curlError ? ", {$curlError}" : "") . ")");
        $this->northdataAuthCookie = '';
        return null;
    }

    private function northdataGet(string $url): ?string
    {
        $authCookie = $this->getNorthdataAuthCookie();
        $ch = curl_init($url);
        $headers = ['User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'];
        if ($authCookie) {
            $headers[] = "Cookie: auth={$authCookie}";
        }
        $this->log[] = ['tool' => 'northdata_get', 'input' => $url, 'output' => 'auth=' . ($authCookie ? 'yes' : 'no')];
        curl_setopt_array($ch, [
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_HTTPHEADER => $headers,
            CURLOPT_FOLLOWLOCATION => true,
            CURLOPT_TIMEOUT => 30,
        ]);
        $result = curl_exec($ch);
        $httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);
        curl_close($ch);

        if ($httpCode !== 200 || !$result) {
            return null;
        }
        return $result;
    }

    public function searchNorthdata(string $entityName): string
    {
        // Clean parenthetical content
        $clean = trim(preg_replace('/\([^)]*\)/', '', $entityName));

        // Strategy 1: Direct URL — NorthData resolves company names to their pages
        $directUrl = "https://www.northdata.com/" . urlencode($clean);
        $directHtml = $this->northdataGet($directUrl);
        if ($directHtml && strlen($directHtml) > 2000) {
            // Check if we got a company page (has the company name in the title)
            $isCompanyPage = false;
            $pageTitle = '';
            if (preg_match('/<title>([^<]+)<\/title>/i', $directHtml, $titleMatch)) {
                $pageTitle = $titleMatch[1];
                // Company pages have titles like "Scanfil Oyj, Sievi, Finland, PRH 2422742-9: Network..."
                // Search pages have titles like 'Search for "Siemens AG"'
                $isCompanyPage = !str_contains($pageTitle, 'Search for')
                    && str_contains($pageTitle, ',')
                    && (stripos($pageTitle, $clean) !== false
                        || stripos($pageTitle, explode(' ', $clean)[0]) !== false);
            }

            if ($isCompanyPage) {
                $pageText = $this->parseNorthdataHtml($directHtml);
                $result = $pageText;
                $this->log[] = ['tool' => 'search_northdata', 'input' => $entityName, 'output' => substr($result, 0, 500)];
                return $result;
            }
        }

        // Strategy 2: Browserbase search (JS-rendered results)
        $parsed = $this->northdataSearchParse($clean);
        if (empty($parsed)) {
            $stripped = preg_replace('/,?\s*\b(S\.A\.?|GmbH|AG|B\.V\.?|Ltd\.?|Inc\.?|LLC|Oyj|Oy|AB|AS|ApS)\s*$/i', '', $clean);
            $stripped = trim($stripped);
            if ($stripped !== $clean) {
                $parsed = $this->northdataSearchParse($stripped);
            }
        }

        if (empty($parsed)) {
            $result = "No North Data results found.";
            $this->log[] = ['tool' => 'search_northdata', 'input' => $entityName, 'output' => $result];
            return $result;
        }

        // List all matches
        $matchList = [];
        foreach ($parsed as $r) {
            $matchList[] = "{$r['name']} → {$r['url']}";
        }
        $header = "=== NorthData Search Results ===\n" . implode("\n", $matchList);

        // Fetch full company page for the best (first) match
        $bestUrl = $parsed[0]['url'];
        $pageHtml = $this->northdataGet($bestUrl);
        $pageText = '';
        if ($pageHtml) {
            $pageText = $this->parseNorthdataHtml($pageHtml);
        }

        $result = $header;
        if ($pageText) {
            $result .= "\n\n{$pageText}";
        }

        $this->log[] = ['tool' => 'search_northdata', 'input' => $entityName, 'output' => substr($result, 0, 500)];
        return $result;
    }

    /**
     * Parse NorthData company page HTML into structured markdown.
     * Extracts: company info, network graph, financials, publications.
     */
    private function parseNorthdataHtml(string $html): string
    {
        $md = [];

        // === Company Info ===
        // Title gives us: "Scanfil Oyj, Sievi, Finland, PRH 2422742-9: Network, Financial information"
        $companyName = '';
        if (preg_match('/<title>([^<]+)<\/title>/i', $html, $m)) {
            $titleParts = explode(':', $m[1]);
            $companyName = trim($titleParts[0]);
        }

        // Extract name variants from the company info section
        // Look for the heading with the company name, then extract "also known as" names
        $names = [];
        if (preg_match_all('/<div[^>]*class="[^"]*alias[^"]*"[^>]*>([^<]+)</i', $html, $aliasMatches)) {
            $names = array_map('trim', $aliasMatches[1]);
        }
        // Also look for names in the info section after the heading
        if (preg_match('/<div[^>]*class="[^"]*company-name[^"]*"[^>]*>(.*?)<\/div>/is', $html, $nameBlock)) {
            if (preg_match_all('/<[^>]+>([^<]{2,})<\/[^>]+>/i', $nameBlock[1], $nm)) {
                foreach ($nm[1] as $n) {
                    $n = trim(html_entity_decode($n));
                    if ($n && !in_array($n, $names)) $names[] = $n;
                }
            }
        }

        // Registry IDs
        $registryIds = [];
        if (preg_match_all('/(?:PRH|HRB|Siren|CVR|KVK|RIK|Registro Mercantil|Companies House|ON)\s*[\w\d\-]+/i', $html, $regMatches)) {
            $registryIds = array_unique($regMatches[0]);
        }
        // LEI
        $lei = '';
        if (preg_match('/LEI[^>]*>.*?([A-Z0-9]{20})/is', $html, $leiMatch)) {
            $lei = $leiMatch[1];
        } elseif (preg_match('/\b([A-Z0-9]{20})\b/', $html, $leiMatch2)) {
            // LEI is always 20 alphanumeric chars — but be careful not to match random strings
        }

        // Address — look for structured address data
        $address = '';
        if (preg_match('/<span[^>]*class="[^"]*address[^"]*"[^>]*>(.*?)<\/span>/is', $html, $addrMatch)) {
            $address = trim(strip_tags($addrMatch[1]));
        }

        // Corporate purpose
        $purpose = '';
        if (preg_match('/Corporate purpose.*?<p[^>]*>(.*?)<\/p>/is', $html, $purpMatch)) {
            $purpose = trim(strip_tags(html_entity_decode($purpMatch[1])));
        }

        // Status
        $status = 'Active';
        if (preg_match('/title="(in liquidation|terminated|dissolved)"/i', $html, $sm)) {
            $status = ucfirst(trim($sm[1]));
        }

        // Build header from the plain-text version of the info section
        // Parse the text between the company heading and the network section
        $text = $this->htmlToText($html);
        $lines = explode("\n", $text);

        // Find the "Name" section in text and extract structured fields
        $infoFields = $this->extractNorthdataInfoFields($lines);

        $md[] = "## {$infoFields['name']} — NorthData";
        $md[] = '';
        $md[] = "**Name:** {$infoFields['name']}";
        if (!empty($infoFields['also_known_as'])) {
            $md[] = "**Also known as:** " . implode(', ', $infoFields['also_known_as']);
        }
        if (!empty($infoFields['registry_id'])) {
            $md[] = "**Registry ID:** {$infoFields['registry_id']}";
        }
        if (!empty($infoFields['lei'])) {
            $md[] = "**LEI:** {$infoFields['lei']}";
        }
        if (!empty($infoFields['address'])) {
            $md[] = "**Address:** {$infoFields['address']}";
        }
        if (!empty($infoFields['country'])) {
            $md[] = "**Country:** {$infoFields['country']}";
        }
        $md[] = "**Status:** {$status}";
        if (!empty($infoFields['industry'])) {
            $md[] = "**Industry:** {$infoFields['industry']}";
        }
        if (!empty($infoFields['purpose'])) {
            $md[] = "**Corporate purpose:** {$infoFields['purpose']}";
        }

        // === Network Graph ===
        $network = $this->extractNorthdataNetwork($html);
        if (!empty($network)) {
            $md[] = '';
            $md[] = $network;
        }

        // === Financials ===
        $financials = $this->extractNorthdataFinancials($html);
        if (!empty($financials)) {
            $md[] = '';
            $md[] = $financials;
        }

        // === Publications (trademarks, shareholdings) ===
        $pubs = $this->extractNorthdataPublications($lines);
        if (!empty($pubs)) {
            $md[] = '';
            $md[] = $pubs;
        }

        return implode("\n", $md);
    }

    /**
     * Extract structured info fields from NorthData page text lines.
     */
    private function extractNorthdataInfoFields(array $lines): array
    {
        $fields = [
            'name' => '', 'also_known_as' => [], 'registry_id' => '',
            'lei' => '', 'address' => '', 'country' => '',
            'industry' => '', 'purpose' => '',
        ];

        $section = null;
        $nameLines = [];
        foreach ($lines as $i => $line) {
            $t = trim($line);

            // Detect section headers
            if ($t === 'Name') { $section = 'name'; continue; }
            if ($t === 'Identification') { $section = 'id'; continue; }
            if ($t === 'Address') { $section = 'address'; continue; }
            if ($t === 'Corporate purpose') { $section = 'purpose'; continue; }
            if (in_array($t, ['Financial performance', 'History', 'Network', 'Legal Structure', 'Financials', 'Publications'])) {
                $section = null;
                continue;
            }

            if ($section === 'name' && $t) {
                // Skip noise
                if (preg_match('/^(Dossier|Watch|Premium|Upgrade|Learn more|Set watch|Cancel|Create dossier|STAY UP)/i', $t)) continue;
                if (str_contains($t, 'maximum number of watches')) continue;
                if (str_contains($t, 'subscription plan')) continue;
                if (str_contains($t, 'printable PDF')) continue;
                if (str_contains($t, 'email address')) continue;
                if (str_contains($t, 'feature is only available')) continue;
                if (str_contains($t, 'Subscribe to our newsletter')) continue;
                // Skip language prefixes like "(englanti):"
                if (preg_match('/^\([a-z]+\):\s*/i', $t)) {
                    $t = preg_replace('/^\([a-z]+\):\s*/i', '', $t);
                }
                $nameLines[] = $t;
            }

            if ($section === 'id' && $t) {
                // Label-only lines — skip
                if (preg_match('/^(Bis|Lei|Cvrcom|EUID|Siret)$/i', $t)) continue;
                // Registry ID value: "PRH 2422742-9", "HRB 6684", etc.
                if (preg_match('/^(PRH|HRB|Siren|CVR|KVK|RIK|ON|Registro Mercantil|Companies House)\s+[\w\d\-]+/i', $t, $rm)) {
                    if (!$fields['registry_id']) $fields['registry_id'] = $rm[0];
                }
                // LEI (20 alphanumeric chars)
                if (preg_match('/^[A-Z0-9]{20}$/', $t)) {
                    $fields['lei'] = $t;
                }
                // EUID value
                if (preg_match('/^[A-Z]{2}[A-Z]+\.\d[\d\-]+$/', $t)) {
                    // EUID like FIFPRO.2422742-9 — skip, not needed
                }
            }

            if ($section === 'address' && $t) {
                if (!$fields['address']) {
                    $fields['address'] = $t;
                    // Extract country from address (last word after last comma)
                    $parts = array_map('trim', explode(',', $t));
                    $lastPart = end($parts);
                    // Country is often the last segment
                    if (preg_match('/^(Finland|Germany|France|Netherlands|Austria|Switzerland|Belgium|Luxembourg|Italy|Spain|Denmark|Sweden|Norway|Poland|Czech Republic|Ireland|Estonia|United Kingdom)$/i', $lastPart)) {
                        $fields['country'] = $lastPart;
                    }
                }
            }

            if ($section === 'purpose' && $t) {
                // First line is the NACE code (e.g. "26.11.0")
                if (!$fields['industry'] && preg_match('/^\d+\.\d+/', $t)) {
                    $fields['industry'] = $t;
                    continue;
                }
                // Second line is the short industry description (e.g. "Manufacture of electronic components")
                if ($fields['industry'] && !str_contains($fields['industry'], '—') && !preg_match('/^\d+\.\d+/', $t) && strlen($t) > 3) {
                    $fields['industry'] .= ' — ' . $t;
                    continue;
                }
                // Remaining lines are the full corporate purpose
                if (strlen($t) > 20) {
                    $fields['purpose'] = $fields['purpose'] ? $fields['purpose'] . ' ' . $t : $t;
                }
            }
        }

        // Process name lines — deduplicate
        if (!empty($nameLines)) {
            $fields['name'] = $nameLines[0];
            $aka = [];
            foreach (array_slice($nameLines, 1) as $n) {
                if (strtolower($n) !== strtolower($fields['name']) && !in_array(strtolower($n), array_map('strtolower', $aka))) {
                    $aka[] = $n;
                }
            }
            $fields['also_known_as'] = $aka;
        }

        // If industry has a code but no description, check the next purpose line
        if ($fields['industry'] && !str_contains($fields['industry'], '—')) {
            // Look for the description line that follows the NACE code
            $industryFound = false;
            foreach ($lines as $line) {
                $t = trim($line);
                if ($industryFound && strlen($t) > 5 && !preg_match('/^\d/', $t)) {
                    $fields['industry'] .= ' — ' . $t;
                    break;
                }
                if ($t === $fields['industry']) $industryFound = true;
            }
        }

        return $fields;
    }

    /**
     * Extract network/ownership graph from NorthData SVG data attributes.
     */
    private function extractNorthdataNetwork(string $html): string
    {
        // Extract nodes: <a class="node" data-id="1" data-text="Scanfil Oyj" data-type="c" data-description="..." data-root="true" data-old="">
        $nodes = [];
        if (preg_match_all('/class="node"[^>]*data-id=(?:3D)?"(\d+)"[^>]*data-text=(?:3D)?"([^"]+)"[^>]*data-type=(?:3D)?"([cp])"[^>]*data-description=(?:3D)?"([^"]*)"[^>]*data-root=(?:3D)?"([^"]*)"[^>]*data-old=(?:3D)?"([^"]*)"/i', $html, $nodeMatches, PREG_SET_ORDER)) {
            foreach ($nodeMatches as $nm) {
                $nodes[$nm[1]] = [
                    'name' => html_entity_decode($this->decodeMhtml($nm[2])),
                    'type' => $nm[3], // c=company, p=person
                    'description' => html_entity_decode($this->decodeMhtml($nm[4])),
                    'root' => ($nm[5] === 'true' || $nm[5] === '3Dtrue'),
                    'old' => ($nm[6] === 'true' || $nm[6] === '3Dtrue' || !empty($nm[6])),
                ];
            }
        }

        // Try simpler pattern if MHTML encoding differs
        if (empty($nodes)) {
            if (preg_match_all('/class="node"[^>]*data-id="(\d+)"[^>]*data-text="([^"]+)"[^>]*data-type="([cp])"/i', $html, $nodeMatches2, PREG_SET_ORDER)) {
                foreach ($nodeMatches2 as $nm) {
                    $desc = '';
                    if (preg_match('/data-description="([^"]*)"/', $nm[0], $dm)) $desc = $dm[1];
                    $root = str_contains($nm[0], 'data-root="true"');
                    $old = str_contains($nm[0], 'data-old="true"');
                    $nodes[$nm[1]] = [
                        'name' => html_entity_decode($nm[2]),
                        'type' => $nm[3],
                        'description' => html_entity_decode($desc),
                        'root' => $root,
                        'old' => $old,
                    ];
                }
            }
        }

        if (empty($nodes)) return '';

        // Extract edges: data-source-id="1" data-target-id="12" data-description="Ultimate parent, prev. 100%"
        $edges = [];
        if (preg_match_all('/data-source-id=(?:3D)?"(\d+)"[^>]*data-target-id=(?:3D)?"(\d+)"[^>]*data-description=(?:3D)?"([^"]+)"/i', $html, $edgeMatches, PREG_SET_ORDER)) {
            foreach ($edgeMatches as $em) {
                $old = str_contains($em[0], 'data-old=3D"true"') || str_contains($em[0], 'data-old="true"');
                $edges[] = [
                    'source' => $em[1],
                    'target' => $em[2],
                    'description' => html_entity_decode($this->decodeMhtml($em[3])),
                    'old' => $old,
                ];
            }
        }

        // Find root node
        $rootId = null;
        foreach ($nodes as $id => $node) {
            if ($node['root']) { $rootId = $id; break; }
        }
        if (!$rootId) return '';

        // Categorise relationships from root
        $subsidiaries = [];
        $people = [];
        $subSubsidiaries = []; // subsidiaries of subsidiaries

        foreach ($edges as $edge) {
            $sourceNode = $nodes[$edge['source']] ?? null;
            $targetNode = $nodes[$edge['target']] ?? null;
            if (!$sourceNode || !$targetNode) continue;

            // Edges FROM root node
            if ($edge['source'] === $rootId) {
                if ($targetNode['type'] === 'c') {
                    $subsidiaries[] = [
                        'name' => $targetNode['name'],
                        'location' => $targetNode['description'],
                        'relationship' => $edge['description'],
                        'old' => $edge['old'],
                    ];
                } elseif ($targetNode['type'] === 'p') {
                    $people[] = [
                        'name' => $targetNode['name'],
                        'location' => $targetNode['description'],
                        'role' => $edge['description'],
                        'old' => $edge['old'],
                    ];
                }
            }
            // Edges from subsidiaries (sub-subsidiaries or people at subsidiaries)
            elseif ($sourceNode['type'] === 'c' && $edge['source'] !== $rootId) {
                if ($targetNode['type'] === 'c') {
                    $subSubsidiaries[] = [
                        'parent' => $sourceNode['name'],
                        'name' => $targetNode['name'],
                        'location' => $targetNode['description'],
                        'relationship' => $edge['description'],
                        'old' => $edge['old'],
                    ];
                }
                // Person roles at subsidiaries — add as note to existing person
                if ($targetNode['type'] === 'p') {
                    foreach ($people as &$p) {
                        if ($p['name'] === $targetNode['name']) {
                            $p['also'][] = $edge['description'] . ' at ' . $sourceNode['name'];
                        }
                    }
                    unset($p);
                }
            }
        }

        $md = [];
        $md[] = '### Network — Corporate Structure';

        // Current subsidiaries
        $current = array_filter($subsidiaries, fn($s) => !$s['old']);
        $previous = array_filter($subsidiaries, fn($s) => $s['old']);

        if (!empty($current)) {
            $md[] = '**Subsidiaries (current):**';
            foreach ($current as $sub) {
                $md[] = "- {$sub['location']} — {$sub['relationship']}";
                // Check for sub-subsidiaries
                foreach ($subSubsidiaries as $ss) {
                    if ($ss['parent'] === $sub['name']) {
                        $md[] = "  - {$ss['location']} — {$ss['relationship']}";
                    }
                }
            }
        }
        if (!empty($previous)) {
            $md[] = '**Subsidiaries (previous):**';
            foreach ($previous as $sub) {
                $md[] = "- {$sub['location']} — {$sub['relationship']}";
                foreach ($subSubsidiaries as $ss) {
                    if ($ss['parent'] === $sub['name']) {
                        $md[] = "  - {$ss['location']} — {$ss['relationship']}";
                    }
                }
            }
        }

        // People
        $currentPeople = array_filter($people, fn($p) => !$p['old']);
        if (!empty($currentPeople)) {
            $md[] = '';
            $md[] = '### Network — People';
            foreach ($currentPeople as $p) {
                $line = "- {$p['name']} ({$p['location']}) — {$p['role']}";
                // Clean location to just city/country
                $loc = preg_replace('/^' . preg_quote($p['name'], '/') . ',\s*/i', '', $p['location']);
                $line = "- {$p['name']} ({$loc}) — {$p['role']}";
                $md[] = $line;
                if (!empty($p['also'])) {
                    foreach ($p['also'] as $also) {
                        $md[] = "  - Also: {$also}";
                    }
                }
            }
        }

        return implode("\n", $md);
    }

    /**
     * Decode MHTML quoted-printable encoded strings (=C3=B6 → ö etc.)
     */
    private function decodeMhtml(string $s): string
    {
        // Handle =XX sequences
        return preg_replace_callback('/=([0-9A-F]{2})/i', fn($m) => chr(hexdec($m[1])), $s);
    }

    /**
     * Extract financials from NorthData embedded JSON (data-data attribute).
     * Handles two formats:
     * 1. httpGet: item[].title + item[].data.data[].year/formattedValue
     * 2. MHTML/Browserbase: financials[].date + financials[].items[].name/formattedValue
     */
    private function extractNorthdataFinancials(string $html): string
    {
        // Find all data-data JSON blocks
        if (!preg_match_all('/data-data="([^"]{100,})"/s', $html, $matches)) {
            return '';
        }

        $yearData = []; // metric => year => formattedValue
        $allYears = [];

        foreach ($matches[1] as $raw) {
            $decoded = html_entity_decode($this->decodeMhtml($raw), ENT_QUOTES | ENT_HTML5, 'UTF-8');
            $json = json_decode($decoded, true);
            if (!$json) continue;

            // Format 1: item[] with title and data.data[]
            if (isset($json['item']) && is_array($json['item'])) {
                foreach ($json['item'] as $item) {
                    $metric = $item['title'] ?? '';
                    $dataPoints = $item['data']['data'] ?? [];
                    if (!$metric || !is_array($dataPoints)) continue;
                    foreach ($dataPoints as $dp) {
                        $year = $dp['year'] ?? '';
                        $val = $dp['formattedValue'] ?? '';
                        if ($year && $val) {
                            $yearData[$metric][$year] = $val;
                            $allYears[$year] = true;
                        }
                    }
                }
            }

            // Format 2: financials[] with date and items[]
            if (isset($json['financials']) && is_array($json['financials'])) {
                foreach ($json['financials'] as $fy) {
                    $year = substr($fy['date'] ?? '', 0, 4);
                    if (!$year) continue;
                    $allYears[$year] = true;
                    foreach ($fy['items'] ?? [] as $item) {
                        $metric = $item['name'] ?? '';
                        $val = $item['formattedValue'] ?? '';
                        if ($metric && $val) {
                            $yearData[$metric][$year] = $val;
                        }
                    }
                }
            }
        }

        if (empty($yearData) || empty($allYears)) return '';

        // Take most recent 3 years
        $years = array_keys($allYears);
        sort($years);
        $years = array_slice($years, -3);

        $metricOrder = ['Revenue', 'Earnings', 'Total assets', 'Equity', 'Equity ratio',
            'Return on equity', 'Return on sales', 'Taxes', 'Cash on hand', 'Receivables', 'Liabilities',
            'Employee number', 'Revenue per employee', 'Base/share capital', 'Real estate'];

        $md = [];
        $md[] = '### Financials';
        $header = '| Metric | ' . implode(' | ', $years) . ' |';
        $sep = '|---|' . implode('|', array_fill(0, count($years), '---')) . '|';
        $md[] = $header;
        $md[] = $sep;

        foreach ($metricOrder as $metric) {
            if (!isset($yearData[$metric])) continue;
            $vals = [];
            foreach ($years as $y) {
                $vals[] = $yearData[$metric][$y] ?? '—';
            }
            $md[] = '| ' . $metric . ' | ' . implode(' | ', $vals) . ' |';
        }

        return implode("\n", $md);
    }

    /**
     * Extract notable publications from NorthData page text.
     */
    private function extractNorthdataPublications(array $lines): string
    {
        $trademarks = [];
        $relationships = [];
        $inPubs = false;
        $inMentions = false;

        foreach ($lines as $line) {
            $t = trim($line);
            if ($t === 'Publications') { $inPubs = true; $inMentions = false; continue; }
            if ($t === 'Mentions') { $inMentions = true; $inPubs = false; continue; }
            if (in_array($t, ['', 'Premium', 'Loading network', 'There are no publications matching your search.'])) continue;
            if (preg_match('/^(Upgrade|Learn more|€\d|Premium plans|STAY UP|Subscribe)/i', $t)) continue;

            if ($inPubs && $t) {
                // Skip dates, balance sheets
                if (preg_match('/^\d{1,2}\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}$/i', $t)) continue;
                if (preg_match('/Balance sheet|Earnings statement/i', $t)) continue;
                if (preg_match('/^via$/i', $t)) continue;
                // Trademarks
                if (preg_match('/mark:\s*"([^"]+)"/i', $t, $tm)) {
                    $trademarks[] = "- {$t}";
                }
                // Shareholdings
                if (preg_match('/Shareholding:/i', $t)) {
                    $relationships[] = "- {$t}";
                }
            }

            if ($inMentions && $t) {
                if (preg_match('/^\d{1,2}\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}$/i', $t)) continue;
                if (preg_match('/^(in|also|via)$/i', $t)) continue;
                if (preg_match('/^(Ler|Hrdk|Rne)$/i', $t)) continue;
                if (preg_match('/^\d{2}\/\d{2}\/\d{4}$/', $t)) continue;
                if (preg_match('/Direct parent:|Ultimate parent|Shareholding:/i', $t)) {
                    $relationships[] = "- {$t}";
                }
                if (preg_match('/Liste der Gesellschafter|Gesellschafterliste/i', $t)) {
                    $relationships[] = "- {$t}";
                }
            }
        }

        $md = [];

        if (!empty($relationships)) {
            // Deduplicate
            $relationships = array_values(array_unique($relationships));
            $md[] = '### Subsidiaries & Relationships';
            $md = array_merge($md, $relationships);
        }

        if (!empty($trademarks)) {
            $trademarks = array_values(array_unique($trademarks));
            $md[] = '';
            $md[] = '### Trademarks';
            $md = array_merge($md, $trademarks);
        }

        return implode("\n", $md);
    }

    private const NORTHDATA_COUNTRY_NAMES = [
        'DE' => 'Germany', 'NL' => 'Netherlands', 'FR' => 'France', 'AT' => 'Austria',
        'CH' => 'Switzerland', 'BE' => 'Belgium', 'LU' => 'Luxembourg', 'IT' => 'Italy',
        'ES' => 'Spain', 'DK' => 'Denmark', 'SE' => 'Sweden', 'NO' => 'Norway',
        'FI' => 'Finland', 'PL' => 'Poland', 'CZ' => 'Czech Republic', 'IE' => 'Ireland',
        'GB' => 'United Kingdom',
    ];

    private function northdataSearchParse(string $query): array
    {
        $url = "https://www.northdata.com/search?query=" . urlencode($query);
        // NorthData search results are JS-rendered — need Browserbase
        $html = $this->browserbaseFetchHtml($url);
        if (!$html) return [];

        $results = [];
        if (preg_match_all('/<a[^>]*href="(\/[^"]+)"[^>]*>([^<]+)<\/a>/i', $html, $matches, PREG_SET_ORDER)) {
            $seen = [];
            foreach ($matches as $match) {
                $href = $match[1];
                $text = trim(html_entity_decode($match[2]));
                if (strlen($text) < 5 || strlen($text) > 120) continue;
                if (!str_contains($text, ',')) continue;
                if (str_starts_with($href, '/search')) continue;
                if (str_starts_with($href, '/_')) continue;
                if (str_starts_with($href, '/?')) continue;
                // Company URLs have 2+ path segments (/Name/RegistryID), address URLs have 1
                $pathSegments = explode('/', trim($href, '/'));
                if (count($pathSegments) < 2) continue;
                $key = strtolower($text);
                if (isset($seen[$key])) continue;
                $seen[$key] = true;
                $results[] = [
                    'name' => $text,
                    'url' => "https://www.northdata.com{$href}",
                ];
                if (count($results) >= 15) break;
            }
        }
        return $results;
    }

    /**
     * Validate an entity on NorthData by searching for its name.
     * Primary check: entity exists and country matches.
     * Secondary check: registry ID matches (from page title).
     * Returns array with 'name', 'url', 'country_match', 'registry_id_match', 'page_registry_id', 'status', or null.
     */
    public function validateNorthdataEntity(string $entityName, string $registryId, string $countryCode = ''): ?array
    {
        $clean = trim(preg_replace('/\([^)]*\)/', '', $entityName));
        $countryName = self::NORTHDATA_COUNTRY_NAMES[strtoupper($countryCode)] ?? '';

        // Strategy 1: Direct URL — NorthData resolves company names to their pages
        $directUrl = "https://www.northdata.com/" . urlencode($clean);
        $directHtml = $this->northdataGet($directUrl);
        if ($directHtml && strlen($directHtml) > 2000) {
            $isCompanyPage = false;
            if (preg_match('/<title>([^<]+)<\/title>/i', $directHtml, $titleMatch)) {
                $pageTitle = $titleMatch[1];
                $isCompanyPage = !str_contains($pageTitle, 'Search for')
                    && str_contains($pageTitle, ',');
            }

            if ($isCompanyPage) {
                // Extract name from title (e.g. "Scanfil Oyj, Sievi, Finland, PRH 2422742-9: ...")
                // Strip known suffixes from right: registry ID, country, city
                $titleName = explode(':', $pageTitle)[0];
                $countryMatch = $countryName ? stripos($titleName, $countryName) !== false : null;
                if ($registryId) {
                    $titleName = preg_replace('/,?\s*' . preg_quote($registryId, '/') . '\s*$/', '', $titleName);
                }
                if ($countryName) {
                    $titleName = preg_replace('/,?\s*' . preg_quote($countryName, '/') . '\s*$/i', '', $titleName);
                }
                // Strip city (last comma-segment after removing country/registry)
                $titleName = preg_replace('/,\s*[^,]+\s*$/', '', $titleName);

                // Status from heading suffix
                $status = null;
                if (preg_match('/title="(in liquidation|terminated|dissolved)"/i', $directHtml, $sm)) {
                    $status = trim($sm[1]);
                } elseif (preg_match('/class="heading"[^>]*>\s*[^<]+/i', $directHtml)) {
                    $status = 'active';
                }

                $registryIdOnPage = stripos($directHtml, $registryId) !== false;

                $best = [
                    'name' => trim($titleName),
                    'url' => $directUrl,
                    'country_match' => $countryMatch,
                    'status' => $status,
                    'registry_id_match' => $registryIdOnPage,
                ];
                $this->log[] = ['tool' => 'validate_northdata_entity', 'input' => "{$entityName} / {$registryId} / {$countryCode}", 'output' => json_encode($best)];
                return $best;
            }
        }

        // Strategy 2: Browserbase search — try full name, then stripped suffix
        $results = $this->northdataSearchParse($clean);

        $hasCountryMatch = false;
        if ($countryName) {
            foreach ($results as $r) {
                if (stripos($r['name'], $countryName) !== false) {
                    $hasCountryMatch = true;
                    break;
                }
            }
        }

        if (empty($results) || !$hasCountryMatch) {
            $stripped = preg_replace('/,?\s*\b(S\.?L\.?|S\.A\.?|GmbH|AG|B\.V\.?|Ltd\.?|Inc\.?|LLC|S\.?R\.?L\.?|Oyj|Oy|AB|AS|ApS)\s*$/i', '', $clean);
            $stripped = trim($stripped);
            if ($stripped !== $clean) {
                $fallback = $this->northdataSearchParse($stripped);
                if (!empty($fallback)) {
                    $results = $fallback;
                }
            }
        }

        if (empty($results)) return null;

        // Match on country name in the result text
        $best = null;
        if ($countryName) {
            foreach ($results as $r) {
                if (stripos($r['name'], $countryName) !== false) {
                    $best = $r;
                    $best['country_match'] = true;
                    break;
                }
            }
        }

        // Fallback to first result
        if (!$best) {
            $best = $results[0];
            $best['country_match'] = !empty($countryName) ? false : null;
        }

        // Fetch company page — extract status and check registry ID appears on page
        $pageHtml = $this->northdataGet($best['url']);
        $status = null;
        $registryIdOnPage = false;
        if ($pageHtml) {
            if (preg_match('/title="(in liquidation|terminated|dissolved)"/i', $pageHtml, $sm)) {
                $status = trim($sm[1]);
            } elseif (preg_match('/class="heading"[^>]*>\s*[^<]+/i', $pageHtml)) {
                $status = 'active';
            }
            $registryIdOnPage = stripos($pageHtml, $registryId) !== false;
        }
        $best['status'] = $status;
        $best['registry_id_match'] = $registryIdOnPage;

        $this->log[] = ['tool' => 'validate_northdata_entity', 'input' => "{$entityName} / {$registryId} / {$countryCode}", 'output' => json_encode($best)];
        return $best;
    }

    // ── NorthData Network (Browserbase) ────────────────────────────────────

    /**
     * Load a NorthData company page via Browserbase and extract the network
     * ownership graph. Returns formatted ownership/relationship data.
     *
     * @param string $northdataUrl Full URL like https://www.northdata.com/Siemens+AG,+München/HRB+6684
     */
    public function northdataNetwork(string $northdataUrl): string
    {
        $apiKey = $this->config['browserbase_api_key'] ?? '';
        $projectId = $this->config['browserbase_project_id'] ?? '';
        if (!$apiKey || !$projectId) {
            return "Error: Browserbase not configured.";
        }

        $seleniumBase = 'http://connect.usw2.browserbase.com/webdriver';

        // Create Browserbase session
        $ch = curl_init('https://api.browserbase.com/v1/sessions');
        curl_setopt_array($ch, [
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_POST => true,
            CURLOPT_HTTPHEADER => ["x-bb-api-key: $apiKey", 'Content-Type: application/json'],
            CURLOPT_POSTFIELDS => json_encode(['projectId' => $projectId]),
            CURLOPT_TIMEOUT => 30,
        ]);
        $bbSession = json_decode(curl_exec($ch), true);
        $bbSessionId = $bbSession['id'] ?? '';
        if (!$bbSessionId) {
            return "Error: Could not create Browserbase session.";
        }

        // Create WebDriver session
        $ch = curl_init($seleniumBase . '/session');
        curl_setopt_array($ch, [
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_POST => true,
            CURLOPT_HTTPHEADER => ['Content-Type: application/json', "x-bb-api-key: $apiKey", "session-id: $bbSessionId"],
            CURLOPT_POSTFIELDS => json_encode(['capabilities' => ['alwaysMatch' => ['browserName' => 'chrome']]]),
            CURLOPT_TIMEOUT => 30,
        ]);
        $wd = json_decode(curl_exec($ch), true);
        $wdSessionId = $wd['value']['sessionId'] ?? '';
        if (!$wdSessionId) {
            return "Error: Could not create WebDriver session.";
        }

        // Navigate to the NorthData page
        $ch = curl_init($seleniumBase . "/session/$wdSessionId/url");
        curl_setopt_array($ch, [
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_POST => true,
            CURLOPT_HTTPHEADER => ['Content-Type: application/json', "x-bb-api-key: $apiKey", "session-id: $bbSessionId"],
            CURLOPT_POSTFIELDS => json_encode(['url' => $northdataUrl]),
            CURLOPT_TIMEOUT => 30,
        ]);
        curl_exec($ch);

        // Wait for JS to render the network graph
        sleep(10);

        // Get rendered page source
        $ch = curl_init($seleniumBase . "/session/$wdSessionId/source");
        curl_setopt_array($ch, [
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_HTTPHEADER => ["x-bb-api-key: $apiKey", "session-id: $bbSessionId"],
            CURLOPT_TIMEOUT => 30,
        ]);
        $resp = curl_exec($ch);
        $data = json_decode($resp, true);
        $html = $data['value'] ?? '';

        if (!$html) {
            return "Error: Could not get rendered page from Browserbase.";
        }

        // Extract the network SVG
        if (!preg_match('/<svg[^>]*aria-label="Network"[^>]*>(.*?)<\/svg>/s', $html, $svgMatch)) {
            $this->log[] = ['tool' => 'northdata_network', 'input' => $northdataUrl, 'output' => 'No network graph found on page.'];
            return "No network graph found on this NorthData page.";
        }
        $svg = $svgMatch[1];

        // Extract nodes
        preg_match_all('/<a[^>]*class="node"[^>]*>/', $svg, $allNodes);
        $nodeMap = [];
        foreach ($allNodes[0] as $nodeTag) {
            preg_match('/data-id="(\d+)"/', $nodeTag, $idM);
            preg_match('/data-text="([^"]+)"/', $nodeTag, $textM);
            preg_match('/data-description="([^"]+)"/', $nodeTag, $descM);
            preg_match('/data-root="([^"]*)"/', $nodeTag, $rootM);
            $id = $idM[1] ?? '?';
            $nodeMap[$id] = [
                'text' => html_entity_decode($textM[1] ?? '?'),
                'desc' => html_entity_decode($descM[1] ?? ''),
                'root' => ($rootM[1] ?? '') !== '',
            ];
        }

        // Find the root entity
        $rootId = null;
        $rootName = '';
        $rootDesc = '';
        foreach ($nodeMap as $id => $n) {
            if ($n['root']) {
                $rootId = (string)$id;
                $rootName = $n['text'];
                $rootDesc = $n['desc'];
                break;
            }
        }

        // Extract links
        preg_match_all('/data-source-id="(\d+)"[^>]*data-target-id="(\d+)"[^>]*data-description="([^"]+)"/', $svg, $links, PREG_SET_ORDER);

        // Build output
        $lines = [];
        $lines[] = "=== NorthData Network: {$rootDesc} ===";
        $lines[] = "";
        $lines[] = "Entity: {$rootName} [ROOT]";
        $lines[] = "";

        // Categorise relationships
        // NorthData edge convention: the description on edge A→B describes
        // B's relationship to A. So "Direct parent" on A→B means B is parent of A.
        // "≥ 75%" on A→B means B holds ≥75% stake in A.
        $ownedBy = [];   // someone owns the root
        $owns = [];      // root owns someone
        $other = [];     // other relationships

        foreach ($links as $l) {
            $srcId = $l[1];
            $tgtId = $l[2];
            $desc = html_entity_decode($l[3]);
            $srcName = $nodeMap[$srcId]['text'] ?? "ID:$srcId";
            $tgtName = $nodeMap[$tgtId]['text'] ?? "ID:$tgtId";
            $tgtDesc = $nodeMap[$tgtId]['desc'] ?? '';
            $srcDesc = $nodeMap[$srcId]['desc'] ?? '';

            $hasParentWord = preg_match('/\b(parent|Ultimate parent|Direct parent)\b/i', $desc);
            $hasStake = preg_match('/(?:≥\s*)?\d+%|Control|Profit Transfer/iu', $desc);
            $isOwnership = $hasParentWord || $hasStake;

            if ($srcId === $rootId && $isOwnership) {
                // Edge FROM root TO target: target has this relationship to root
                // "Direct parent" = target is parent of root
                // "≥ 75%" = target holds stake in root
                $ownedBy[] = "  {$tgtName} ({$tgtDesc}): {$desc}";
            } elseif ($tgtId === $rootId && $isOwnership) {
                // Edge FROM source TO root: root has this relationship to source
                // "Direct parent" = root is parent of source
                // "≥ 75%" = root holds stake in source
                $owns[] = "  {$srcName} ({$srcDesc}): {$desc}";
            } else {
                $other[] = "  {$srcName} → {$tgtName}: {$desc}";
            }
        }

        if ($ownedBy) {
            $lines[] = "OWNED BY (parent entities):";
            foreach ($ownedBy as $l) $lines[] = $l;
            $lines[] = "";
        }

        if ($owns) {
            $lines[] = "Subsidiaries/controlled entities:";
            foreach ($owns as $l) $lines[] = $l;
            $lines[] = "";
        }

        if ($other) {
            $lines[] = "Other relationships:";
            foreach ($other as $l) $lines[] = $l;
            $lines[] = "";
        }

        // Conclusion
        if ($ownedBy) {
            $lines[] = "Conclusion: {$rootName} has parent/owner entities above it (see OWNED BY section). Consider contracting with the parent instead.";
        } else {
            $lines[] = "Conclusion: {$rootName} appears to be the ultimate parent/TopCo — no parent entity found above it in the network.";
        }

        $result = implode("\n", $lines);
        $this->log[] = ['tool' => 'northdata_network', 'input' => $northdataUrl, 'output' => $result];
        return $result;
    }

    // ── Delaware ─────────────────────────────────────────────────────────────

    public function searchDelaware(string $entityName): string
    {
        $apiKey = $this->config['browserbase_api_key'] ?? '';
        $projectId = $this->config['browserbase_project_id'] ?? '';
        if (!$apiKey || !$projectId) {
            $result = "Delaware search unavailable (Browserbase not configured).";
            $this->log[] = ['tool' => 'search_delaware', 'input' => $entityName, 'output' => $result];
            return $result;
        }

        $seleniumBase = 'http://connect.usw2.browserbase.com/webdriver';

        // Create Browserbase session
        $ch = curl_init('https://api.browserbase.com/v1/sessions');
        curl_setopt_array($ch, [
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_POST => true,
            CURLOPT_HTTPHEADER => ["x-bb-api-key: $apiKey", 'Content-Type: application/json'],
            CURLOPT_POSTFIELDS => json_encode(['projectId' => $projectId]),
            CURLOPT_TIMEOUT => 30,
        ]);
        $bbSession = json_decode(curl_exec($ch), true);
        $bbSessionId = $bbSession['id'] ?? '';
        if (!$bbSessionId) {
            $result = "Delaware search unavailable (could not create browser session).";
            $this->log[] = ['tool' => 'search_delaware', 'input' => $entityName, 'output' => $result];
            return $result;
        }

        // Create WebDriver session
        $ch = curl_init($seleniumBase . '/session');
        curl_setopt_array($ch, [
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_POST => true,
            CURLOPT_HTTPHEADER => ['Content-Type: application/json', "x-bb-api-key: $apiKey", "session-id: $bbSessionId"],
            CURLOPT_POSTFIELDS => json_encode(['capabilities' => ['alwaysMatch' => ['browserName' => 'chrome']]]),
            CURLOPT_TIMEOUT => 30,
        ]);
        $wd = json_decode(curl_exec($ch), true);
        $wdSessionId = $wd['value']['sessionId'] ?? '';
        if (!$wdSessionId) {
            $result = "Delaware search unavailable (WebDriver failed).";
            $this->log[] = ['tool' => 'search_delaware', 'input' => $entityName, 'output' => $result];
            return $result;
        }

        $headers = ['Content-Type: application/json', "x-bb-api-key: $apiKey", "session-id: $bbSessionId"];
        $base = "$seleniumBase/session/$wdSessionId";

        // Navigate to Delaware entity search
        $this->wdPost("$base/url", ['url' => 'https://icis.corp.delaware.gov/ecorp/entitysearch/namesearch.aspx'], $headers);
        sleep(3);

        // Find and fill the entity name input
        $el = $this->wdPost("$base/element", ['using' => 'css selector', 'value' => '#ctl00_ContentPlaceHolder1_frmEntityName'], $headers);
        $elId = array_values($el['value'] ?? [])[0] ?? '';
        if (!$elId) {
            $result = "Delaware search failed (could not find search input).";
            $this->log[] = ['tool' => 'search_delaware', 'input' => $entityName, 'output' => $result];
            return $result;
        }

        $this->wdPost("$base/element/$elId/value", ['text' => $entityName], $headers);
        sleep(1);

        // Click submit
        $btn = $this->wdPost("$base/element", ['using' => 'css selector', 'value' => '#ctl00_ContentPlaceHolder1_btnSubmit'], $headers);
        $btnId = array_values($btn['value'] ?? [])[0] ?? '';
        if ($btnId) {
            $this->wdPost("$base/element/$btnId/click", new \stdClass(), $headers);
        }
        sleep(5);

        // Get results page
        $ch = curl_init("$base/source");
        curl_setopt_array($ch, [
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_HTTPHEADER => ["x-bb-api-key: $apiKey", "session-id: $bbSessionId"],
            CURLOPT_TIMEOUT => 30,
        ]);
        $source = json_decode(curl_exec($ch), true);
        $html = $source['value'] ?? '';

        // Parse results: each row has a file number span and entity name link
        $results = [];
        preg_match_all('/<tr[^>]*>(.*?)<\/tr>/is', $html, $rows);
        foreach ($rows[1] as $row) {
            if (!preg_match('/<a[^>]*>([^<]+)<\/a>/i', $row, $linkM)) continue;
            $name = trim(html_entity_decode($linkM[1]));
            if (strlen($name) < 3) continue;
            // Extract file number
            $fileNum = '';
            if (preg_match('/<span[^>]*lblFileNumber[^>]*>(\d+)<\/span>/', $row, $fnM)) {
                $fileNum = $fnM[1];
            }
            if (!$fileNum) continue; // skip non-result rows
            $results[] = "File #{$fileNum}: {$name}";
            if (count($results) >= 15) break;
        }

        $result = $results ? implode("\n", $results) : "No Delaware entities found for \"{$entityName}\".";
        $this->log[] = ['tool' => 'search_delaware', 'input' => $entityName, 'output' => $result];
        return $result;
    }

    /**
     * Look up a Delaware entity by file number for validation.
     * Searches by file number and parses the results list (detail page requires
     * JS postback which doesn't work via WebDriver).
     * Returns ['name' => ..., 'file_number' => ...] or null.
     */
    public function lookupDelawareByFileNumber(string $fileNumber): ?array
    {
        $apiKey = $this->config['browserbase_api_key'] ?? '';
        $projectId = $this->config['browserbase_project_id'] ?? '';
        if (!$apiKey || !$projectId) {
            $this->log[] = ['tool' => 'lookup_delaware', 'input' => $fileNumber, 'output' => 'Browserbase not configured'];
            return null;
        }

        $seleniumBase = 'http://connect.usw2.browserbase.com/webdriver';

        // Create Browserbase session
        $ch = curl_init('https://api.browserbase.com/v1/sessions');
        curl_setopt_array($ch, [
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_POST => true,
            CURLOPT_HTTPHEADER => ["x-bb-api-key: $apiKey", 'Content-Type: application/json'],
            CURLOPT_POSTFIELDS => json_encode(['projectId' => $projectId]),
            CURLOPT_TIMEOUT => 30,
        ]);
        $bbSession = json_decode(curl_exec($ch), true);
        $bbSessionId = $bbSession['id'] ?? '';
        if (!$bbSessionId) {
            $this->log[] = ['tool' => 'lookup_delaware', 'input' => $fileNumber, 'output' => 'Could not create browser session'];
            return null;
        }

        // Create WebDriver session
        $ch = curl_init($seleniumBase . '/session');
        curl_setopt_array($ch, [
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_POST => true,
            CURLOPT_HTTPHEADER => ['Content-Type: application/json', "x-bb-api-key: $apiKey", "session-id: $bbSessionId"],
            CURLOPT_POSTFIELDS => json_encode(['capabilities' => ['alwaysMatch' => ['browserName' => 'chrome']]]),
            CURLOPT_TIMEOUT => 30,
        ]);
        $wd = json_decode(curl_exec($ch), true);
        $wdSessionId = $wd['value']['sessionId'] ?? '';
        if (!$wdSessionId) {
            $this->log[] = ['tool' => 'lookup_delaware', 'input' => $fileNumber, 'output' => 'WebDriver failed'];
            return null;
        }

        $headers = ['Content-Type: application/json', "x-bb-api-key: $apiKey", "session-id: $bbSessionId"];
        $base = "$seleniumBase/session/$wdSessionId";

        // Navigate to Delaware entity search
        $this->wdPost("$base/url", ['url' => 'https://icis.corp.delaware.gov/ecorp/entitysearch/namesearch.aspx'], $headers);
        sleep(3);

        // Fill in the file number field
        $el = $this->wdPost("$base/element", ['using' => 'css selector', 'value' => '#ctl00_ContentPlaceHolder1_frmFileNumber'], $headers);
        $elId = array_values($el['value'] ?? [])[0] ?? '';
        if (!$elId) {
            $this->log[] = ['tool' => 'lookup_delaware', 'input' => $fileNumber, 'output' => 'Could not find file number input'];
            return null;
        }

        $this->wdPost("$base/element/$elId/value", ['text' => $fileNumber], $headers);
        sleep(1);

        // Click submit
        $btn = $this->wdPost("$base/element", ['using' => 'css selector', 'value' => '#ctl00_ContentPlaceHolder1_btnSubmit'], $headers);
        $btnId = array_values($btn['value'] ?? [])[0] ?? '';
        if ($btnId) {
            $this->wdPost("$base/element/$btnId/click", new \stdClass(), $headers);
        }
        sleep(5);

        // Get the page source
        $ch = curl_init("$base/source");
        curl_setopt_array($ch, [
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_HTTPHEADER => ["x-bb-api-key: $apiKey", "session-id: $bbSessionId"],
            CURLOPT_TIMEOUT => 30,
        ]);
        $source = json_decode(curl_exec($ch), true);
        $html = $source['value'] ?? '';

        // Parse results list — file number search returns same format as name search
        $result = null;
        preg_match_all('/<tr[^>]*>(.*?)<\/tr>/is', $html, $rows);
        foreach ($rows[1] as $row) {
            $fn = '';
            if (preg_match('/<span[^>]*lblFileNumber[^>]*>(\d+)<\/span>/', $row, $fnM)) {
                $fn = $fnM[1];
            }
            if ($fn !== $fileNumber) continue;

            if (preg_match('/<a[^>]*>([^<]+)<\/a>/i', $row, $linkM)) {
                $name = trim(html_entity_decode($linkM[1]));
                $result = [
                    'name' => $name,
                    'file_number' => $fileNumber,
                    'status' => 'Active', // entity appears in search results = exists in registry
                ];
                break;
            }
        }

        $this->log[] = ['tool' => 'lookup_delaware', 'input' => $fileNumber, 'output' => $result ? json_encode($result) : 'No entity found'];
        return $result;
    }

    private function wdPost(string $url, $body, array $headers): array
    {
        $ch = curl_init($url);
        curl_setopt_array($ch, [
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_POST => true,
            CURLOPT_HTTPHEADER => $headers,
            CURLOPT_POSTFIELDS => json_encode($body),
            CURLOPT_TIMEOUT => 30,
        ]);
        return json_decode(curl_exec($ch), true) ?: [];
    }

    // ── HTTP Helper ──────────────────────────────────────────────────────────

    // ── SEC EDGAR Parent Search (Exhibit 21) ───────────────────────────────

    /**
     * Search EDGAR for the parent/registrant of a subsidiary by looking for
     * Exhibit 21 ("Subsidiaries of the Registrant") filings that mention the name.
     *
     * Returns structured text: parent name, CIK, filing date, confirmation
     * of whether the entity appears in the actual Exhibit 21 document.
     */
    public function edgarParentSearch(string $subsidiaryName): string
    {
        $ua = $this->config['sec_user_agent'];
        $lines = [];

        // Step 1: Search EFTS for the subsidiary name in 10-K filings
        // Generate name variants (with/without comma before legal suffix)
        $nameVariants = [$subsidiaryName];
        if (preg_match('/^(.+),\s*(LLC|Inc|Ltd|Corp|LP|LLP)\.?$/i', $subsidiaryName, $m)) {
            $nameVariants[] = $m[1] . ' ' . $m[2];
        } elseif (preg_match('/^(.+)\s+(LLC|Inc|Ltd|Corp|LP|LLP)\.?$/i', $subsidiaryName, $m)) {
            $nameVariants[] = $m[1] . ', ' . $m[2];
        }
        $nameVariants = array_unique($nameVariants);

        // Try with "subsidiaries of the registrant" first (strongest signal)
        $searches = [];
        foreach ($nameVariants as $name) {
            $searches[] = '"' . $name . '" "subsidiaries of the registrant"';
        }
        foreach ($nameVariants as $name) {
            $searches[] = '"' . $name . '" "Exhibit 21"';
        }
        foreach ($nameVariants as $name) {
            $searches[] = '"' . $name . '" "significant subsidiaries"';
        }
        foreach ($nameVariants as $name) {
            $searches[] = '"' . $name . '"';
        }

        $exhibit21Hits = [];
        $otherHits = [];

        foreach ($searches as $query) {
            $url = "https://efts.sec.gov/LATEST/search-index?q=" . urlencode($query)
                . "&forms=10-K&dateRange=custom&startdt=2022-01-01&enddt=2026-12-31";
            $json = $this->httpGet($url, $ua);
            if (!$json) continue;

            $data = json_decode($json, true);
            if (!$data) continue;

            foreach ($data['hits']['hits'] ?? [] as $h) {
                $s = $h['_source'] ?? [];
                $cik = $s['ciks'][0] ?? '';
                $fileType = $s['file_type'] ?? '';
                $fileDesc = $s['file_description'] ?? '';
                $displayName = $s['display_names'][0] ?? '';
                $fileDate = $s['file_date'] ?? '';
                $adsh = $s['adsh'] ?? '';

                // Deduplicate by CIK+date
                $key = $cik . '|' . $fileDate;

                $isExhibit21 = stripos($fileType, '21') !== false
                    || stripos($fileDesc, 'subsidiar') !== false
                    || stripos($fileDesc, '21') !== false;

                if ($isExhibit21 && !isset($exhibit21Hits[$key])) {
                    $exhibit21Hits[$key] = [
                        'cik' => $cik,
                        'name' => $displayName,
                        'date' => $fileDate,
                        'adsh' => $adsh,
                        'file_type' => $fileType,
                    ];
                } elseif (!$isExhibit21 && !isset($otherHits[$key])) {
                    $otherHits[$key] = [
                        'cik' => $cik,
                        'name' => $displayName,
                        'date' => $fileDate,
                        'file_type' => $fileType,
                    ];
                }
            }
            // Stop searching if we found Exhibit 21 hits
            if (!empty($exhibit21Hits)) break;
        }

        if (empty($exhibit21Hits) && empty($otherHits)) {
            $result = "No EDGAR parent found for \"{$subsidiaryName}\". Entity may not be a subsidiary of a US public company.";
            $this->log[] = ['tool' => 'edgar_parent_search', 'input' => $subsidiaryName, 'output' => $result];
            return $result;
        }

        // Step 2: For Exhibit 21 hits, fetch the actual exhibit to confirm
        // Sort by date descending (most recent first)
        usort($exhibit21Hits, fn($a, $b) => strcmp($b['date'], $a['date']));

        $confirmed = false;
        foreach (array_slice($exhibit21Hits, 0, 3) as $hit) {
            $cikClean = ltrim($hit['cik'], '0');
            $adshClean = str_replace('-', '', $hit['adsh']);

            // Fetch filing index to find Exhibit 21 document URL
            $indexUrl = "https://www.sec.gov/Archives/edgar/data/{$cikClean}/{$adshClean}/{$hit['adsh']}-index.htm";
            $indexHtml = $this->httpGet($indexUrl, $ua);

            $exhibitUrl = null;
            if ($indexHtml && preg_match('/href="(\/Archives[^"]*ex[^"]*21[^"]*?)"/i', $indexHtml, $m)) {
                $exhibitUrl = "https://www.sec.gov" . $m[1];
            }

            if ($exhibitUrl) {
                // Fetch exhibit content and check for subsidiary name
                $exhibitHtml = $this->httpGet($exhibitUrl, $ua);
                if ($exhibitHtml) {
                    $exhibitText = $this->htmlToText($exhibitHtml);
                    $nameInExhibit = false;
                    foreach ($nameVariants as $variant) {
                        if (stripos($exhibitText, $variant) !== false) {
                            $nameInExhibit = true;
                            break;
                        }
                    }

                    // Extract parent name from display_names (strip ticker/CIK suffix)
                    $parentName = preg_replace('/\s*\(.*$/', '', $hit['name']);
                    $parentName = trim($parentName);

                    $lines[] = "EDGAR PARENT FOUND (Exhibit 21):";
                    $lines[] = "  Parent: {$parentName}";
                    $lines[] = "  Parent CIK: {$hit['cik']}";
                    $lines[] = "  Filing date: {$hit['date']}";
                    $lines[] = "  Exhibit URL: {$exhibitUrl}";

                    if ($nameInExhibit) {
                        $lines[] = "  Confirmation: \"{$subsidiaryName}\" appears in Exhibit 21 — STRONG evidence of subsidiary relationship.";
                        $confirmed = true;
                    } else {
                        $lines[] = "  Confirmation: Name not found verbatim in exhibit text. May use a variant name.";
                    }

                    // Extract other subsidiaries listed (for context)
                    $exhibitLines = explode("\n", $exhibitText);
                    $subsidiaryList = [];
                    $inList = false;
                    foreach ($exhibitLines as $el) {
                        $el = trim($el);
                        if (stripos($el, 'subsidiaries of the registrant') !== false
                            || stripos($el, 'name of subsidiary') !== false) {
                            $inList = true;
                            continue;
                        }
                        if ($inList && strlen($el) > 3 && strlen($el) < 120
                            && !preg_match('/^(jurisdiction|name of|exhibit|document|ex-)/i', $el)) {
                            $subsidiaryList[] = $el;
                        }
                    }
                    if ($subsidiaryList) {
                        $lines[] = "  Other subsidiaries listed: " . implode(', ', array_slice($subsidiaryList, 0, 10));
                    }

                    break; // We found and confirmed, stop
                }
            }

            // If we couldn't fetch the exhibit, still report the finding
            if (empty($lines)) {
                $parentName = preg_replace('/\s*\(.*$/', '', $hit['name']);
                $lines[] = "EDGAR PARENT FOUND (Exhibit 21 reference):";
                $lines[] = "  Parent: " . trim($parentName);
                $lines[] = "  Parent CIK: {$hit['cik']}";
                $lines[] = "  Filing date: {$hit['date']}";
                $lines[] = "  Note: Could not fetch exhibit to confirm. MEDIUM evidence.";
                break;
            }
        }

        // If no Exhibit 21 hits but other mentions exist
        if (empty($lines) && !empty($otherHits)) {
            $hit = array_values($otherHits)[0];
            $parentName = preg_replace('/\s*\(.*$/', '', $hit['name']);
            $lines[] = "EDGAR MENTION ONLY (no Exhibit 21):";
            $lines[] = "  Mentioned in filings of: " . trim($parentName);
            $lines[] = "  CIK: {$hit['cik']}";
            $lines[] = "  Filing date: {$hit['date']}";
            $lines[] = "  File type: {$hit['file_type']}";
            $lines[] = "  Note: Mentioned in filing but not confirmed as subsidiary. WEAK evidence — could be customer, supplier, counterparty, etc.";
        }

        $result = implode("\n", $lines);
        $this->log[] = ['tool' => 'edgar_parent_search', 'input' => $subsidiaryName, 'output' => $result];
        return $result;
    }

    // ── Bizapedia ─────────────────────────────────────────────────────────────

    private const BIZAPEDIA_API_KEY = 'YBUIWJDRQYMBKXCQDA';

    /**
     * Search SEC IAPD (Investment Adviser Public Disclosure) for a firm name.
     * Returns formatted text with firm details from the SEC adviser registry.
     */
    public function searchSecIapd(string $firmName): string
    {
        $this->progress('registry', "Searching SEC IAPD for \"{$firmName}\"...");

        $params = [
            'query' => $firmName,
            'offset' => 0,
            'count' => 10,
        ];
        $url = 'https://api.adviserinfo.sec.gov/search/firm?' . http_build_query($params);

        $ch = curl_init($url);
        curl_setopt_array($ch, [
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_TIMEOUT => 15,
            CURLOPT_ENCODING => '',
            CURLOPT_HTTPHEADER => ['Accept: application/json'],
        ]);
        $response = curl_exec($ch);
        $httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);
        curl_close($ch);

        if ($httpCode !== 200 || !$response) {
            $result = "No SEC IAPD results (HTTP {$httpCode}).";
            $this->log[] = ['tool' => 'sec_iapd', 'input' => $firmName, 'output' => $result];
            return $result;
        }

        $data = json_decode($response, true);
        $hits = $data['hits']['hits'] ?? [];
        $total = $data['hits']['total'] ?? 0;

        if (empty($hits)) {
            $result = "No SEC IAPD results for \"{$firmName}\".";
            $this->log[] = ['tool' => 'sec_iapd', 'input' => $firmName, 'output' => $result];
            return $result;
        }

        $this->progress('registry', "SEC IAPD: {$total} results for \"{$firmName}\"");

        $lines = ["SEC IAPD: {$total} result(s) for \"{$firmName}\"", ""];
        foreach ($hits as $hit) {
            $src = $hit['_source'] ?? [];
            $name = $src['firm_name'] ?? 'Unknown';
            $secNum = $src['firm_ia_full_sec_number'] ?? 'N/A';
            $scope = $src['firm_ia_scope'] ?? 'Unknown';
            $otherNames = $src['firm_other_names'] ?? [];
            $branches = $src['firm_branches_count'] ?? 0;
            $hasDisclosures = ($src['firm_ia_disclosure_fl'] ?? 'N') === 'Y';

            $lines[] = "• {$name}";
            $lines[] = "  SEC#: {$secNum} | Status: {$scope} | Branches: {$branches}";
            if ($hasDisclosures) {
                $lines[] = "  ⚠ Has regulatory disclosures";
            }

            // Parse address
            $addrJson = $src['firm_ia_address_details'] ?? '';
            if ($addrJson) {
                $addrData = json_decode($addrJson, true);
                $office = $addrData['officeAddress'] ?? [];
                $addrParts = array_filter([
                    $office['street1'] ?? '', $office['street2'] ?? '',
                    $office['city'] ?? '', $office['state'] ?? '',
                    $office['postalCode'] ?? '', $office['country'] ?? '',
                ]);
                if ($addrParts) {
                    $lines[] = "  Address: " . implode(', ', $addrParts);
                }
            }

            // Other names (skip if same as firm_name)
            $otherFiltered = array_filter($otherNames, fn($n) => strcasecmp($n, $name) !== 0);
            if ($otherFiltered) {
                $lines[] = "  Also known as: " . implode('; ', $otherFiltered);
            }

            $lines[] = "  IAPD URL: https://adviserinfo.sec.gov/firm/summary/" . ($src['firm_source_id'] ?? '');
            $lines[] = "";
        }

        $result = implode("\n", $lines);
        $this->log[] = ['tool' => 'sec_iapd', 'input' => $firmName, 'output' => count($hits) . " results"];
        return $result;
    }

    /**
     * Look up a specific entity on Bizapedia by file number and state.
     * Returns the raw company record or null if not found.
     */
    public function lookupBizapediaByFileNumber(string $fileNumber, string $stateCode): ?array
    {
        $this->apiCalls['bizapedia']++;
        $params = [
            'ep' => 'LCBFN',
            'k' => self::BIZAPEDIA_API_KEY,
            'fn' => $fileNumber,
            'pa' => strtoupper($stateCode),
        ];
        $url = 'https://www.bizapedia.com/bdmservice-rest.aspx?' . http_build_query($params);

        $ch = curl_init($url);
        curl_setopt_array($ch, [
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_TIMEOUT => 15,
            CURLOPT_ENCODING => '',
        ]);
        $response = curl_exec($ch);
        $httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);
        curl_close($ch);

        if ($httpCode !== 200 || !$response) return null;

        $data = json_decode($response, true);
        if (!$data || !($data['Success'] ?? false) || empty($data['EntityName'])) return null;

        return $data;
    }

    /**
     * Search Bizapedia for a US entity name. Returns array of company records.
     * Each record has: EntityName, FileNumber, FilingJurisdictionName,
     * FilingStatus, EntityType, FilingDate, principal address, registered agent,
     * principals/officers, etc.
     */
    public function searchBizapedia(string $entityName): array
    {
        $this->apiCalls['bizapedia']++;
        $this->progress('registry', "Searching Bizapedia for \"{$entityName}\"...");

        $params = [
            'ep' => 'LCSBN',
            'k' => self::BIZAPEDIA_API_KEY,
            'n' => $entityName,
        ];
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

        if ($httpCode !== 200 || !$response) {
            $this->log[] = ['tool' => 'bizapedia', 'input' => $entityName, 'output' => "HTTP {$httpCode}"];
            return [];
        }

        $data = json_decode($response, true);
        if (!$data || !$data['Success']) {
            $this->log[] = ['tool' => 'bizapedia', 'input' => $entityName, 'output' => 'API error: ' . ($data['ErrorMessage'] ?? 'unknown')];
            return [];
        }

        $companies = $data['Companies'] ?? [];
        $this->log[] = ['tool' => 'bizapedia', 'input' => $entityName, 'output' => count($companies) . ' results'];
        $this->progress('registry', "Bizapedia: " . count($companies) . " results for \"{$entityName}\"");
        return $companies;
    }

    /**
     * Search Bizapedia trademarks by owner name. Returns a formatted string
     * summarising trademarks owned by the given entity.
     */
    public function searchBizapediaTrademark(string $ownerName): string
    {
        $this->apiCalls['bizapedia']++;
        $this->progress('registry', "Searching Bizapedia trademarks for owner \"{$ownerName}\"...");

        $params = [
            'ep' => 'LT',
            'k' => self::BIZAPEDIA_API_KEY,
            'tm' => '',
            'tmo' => $ownerName,
        ];
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

        if ($httpCode !== 200 || !$response) {
            $result = "No trademark results (HTTP {$httpCode}).";
            $this->log[] = ['tool' => 'bizapedia_tm', 'input' => $ownerName, 'output' => $result];
            return $result;
        }

        $data = json_decode($response, true);
        if (!$data || !$data['Success'] || empty($data['Trademarks'])) {
            $result = "No trademarks found for owner \"{$ownerName}\".";
            $this->log[] = ['tool' => 'bizapedia_tm', 'input' => $ownerName, 'output' => $result];
            return $result;
        }

        $trademarks = $data['Trademarks'];
        $this->progress('registry', "Bizapedia TM: " . count($trademarks) . " trademarks for \"{$ownerName}\"");

        // Build compact summary grouped by owner
        $byOwner = [];
        foreach ($trademarks as $t) {
            $owner = $t['OwnerName'] ?? 'Unknown';
            $byOwner[$owner][] = $t;
        }

        $lines = [];
        foreach ($byOwner as $owner => $marks) {
            $active = array_filter($marks, fn($t) => str_contains(strtolower($t['StatusDescription'] ?? ''), 'registered'));
            $lines[] = "{$owner} — " . count($marks) . " trademarks (" . count($active) . " active)";

            // Show owner address from first mark that has one
            foreach ($marks as $t) {
                $addr = array_filter([
                    $t['OwnerAddressLine1'] ?? '', $t['OwnerAddressLine2'] ?? '',
                    $t['OwnerAddressCity'] ?? '', $t['OwnerAddressState'] ?? '',
                ]);
                if ($addr) {
                    $lines[] = "  Address: " . implode(', ', $addr);
                    break;
                }
            }

            // State of incorporation
            if ($marks[0]['OwnerNationalityStateName'] ?? '') {
                $lines[] = "  State: " . $marks[0]['OwnerNationalityStateName'];
            }

            // List active marks (up to 10)
            $activeMarks = array_slice($active, 0, 10);
            foreach ($activeMarks as $t) {
                $lines[] = "  TM: {$t['MarkIdentification']} (Reg #{$t['RegistrationNumber']}, filed " . substr($t['FilingDate']['Date'] ?? '', 0, 10) . ")";
            }
            if (count($active) > 10) {
                $lines[] = "  ... and " . (count($active) - 10) . " more active trademarks";
            }
        }

        $result = implode("\n", $lines);
        $this->log[] = ['tool' => 'bizapedia_tm', 'input' => $ownerName, 'output' => $result];
        return $result;
    }

    /**
     * Deduplicate Bizapedia results across multiple name searches.
     * Uses FileNumber + FilingJurisdictionPostalAbbreviation as unique key.
     * Returns a JSON string suitable for feeding to the AI.
     */
    public static function deduplicateBizapediaResults(array $allResults): string
    {
        $seen = [];
        $unique = [];

        foreach ($allResults as $r) {
            $key = ($r['FilingJurisdictionPostalAbbreviation'] ?? '') . ':' . ($r['FileNumber'] ?? '');
            if (isset($seen[$key])) continue;
            $seen[$key] = true;

            // Build a compact record for the AI
            $record = [
                'name' => $r['EntityName'] ?? '',
                'status' => $r['FilingStatus'] ?? 'Unknown',
                'type' => $r['EntityType'] ?? '',
                'jurisdiction' => $r['FilingJurisdictionName'] ?? '',
                'jurisdiction_code' => $r['FilingJurisdictionPostalAbbreviation'] ?? '',
                'file_number' => $r['FileNumber'] ?? '',
                'filing_date' => substr($r['FilingDate']['Date'] ?? '', 0, 10) ?: null,
                'domestic_jurisdiction' => $r['DomesticJurisdictionName'] ?? '',
            ];

            // Address
            $addrParts = array_filter([
                $r['PrincipalAddressLine1'] ?? '',
                $r['PrincipalAddressLine2'] ?? '',
                $r['PrincipalAddressCity'] ?? '',
                $r['PrincipalAddressState'] ?? '',
                $r['PrincipalAddressPostalCode'] ?? '',
            ]);
            if ($addrParts) $record['address'] = implode(', ', $addrParts);

            // Registered agent
            if ($r['RegisteredAgentName'] ?? '') {
                $record['registered_agent'] = $r['RegisteredAgentName'];
            }

            // Alternative names
            $akas = array_filter([
                $r['OtherEntityName1'] ?? '',
                $r['OtherEntityName2'] ?? '',
                $r['OtherEntityName3'] ?? '',
            ]);
            if ($akas) $record['alternative_names'] = $akas;

            // Principals (compact)
            $principals = [];
            foreach ($r['Principals'] ?? [] as $p) {
                $entry = $p['PrincipalName'] ?? '';
                if ($p['Titles'] ?? '') $entry .= ' (' . $p['Titles'] . ')';
                if ($entry) $principals[] = $entry;
            }
            if ($principals) $record['principals'] = $principals;

            // Optional fields
            if ($r['PrimaryDomainName'] ?? '') $record['website'] = $r['PrimaryDomainName'];
            if ($r['PrimaryEmail'] ?? '') $record['email'] = $r['PrimaryEmail'];
            if ($r['PrimaryPhone'] ?? '') $record['phone'] = $r['PrimaryPhone'];
            if ($r['BusinessDescription'] ?? '') $record['description'] = $r['BusinessDescription'];

            $unique[] = $record;
        }

        if (empty($unique)) {
            return 'No Bizapedia results found.';
        }

        // Sort: Active first, then real entities over fictitious, domestic over foreign
        usort($unique, function ($a, $b) {
            // 1. Status: Active/Unknown > everything else (DE and LPs often show "Unknown" for active entities)
            $aActive = in_array(strtolower($a['status'] ?? ''), ['active', 'unknown']) ? 0 : 1;
            $bActive = in_array(strtolower($b['status'] ?? ''), ['active', 'unknown']) ? 0 : 1;
            if ($aActive !== $bActive) return $aActive - $bActive;

            // 2. Entity type: domestic corp/LLC > foreign > fictitious
            $aType = self::bizapediaTypeRank($a['type'] ?? '');
            $bType = self::bizapediaTypeRank($b['type'] ?? '');
            if ($aType !== $bType) return $aType - $bType;

            // 3. Domestic jurisdiction matches filing jurisdiction (home filing first)
            $aDomestic = strtolower($a['domestic_jurisdiction'] ?? '') === strtolower($a['jurisdiction'] ?? '') ? 0 : 1;
            $bDomestic = strtolower($b['domestic_jurisdiction'] ?? '') === strtolower($b['jurisdiction'] ?? '') ? 0 : 1;
            if ($aDomestic !== $bDomestic) return $aDomestic - $bDomestic;

            return 0;
        });

        return json_encode($unique, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);
    }

    public static function sortBizapediaResults(array &$results): void
    {
        usort($results, function ($a, $b) {
            $aActive = in_array(strtolower($a['FilingStatus'] ?? $a['status'] ?? ''), ['active', 'unknown']) ? 0 : 1;
            $bActive = in_array(strtolower($b['FilingStatus'] ?? $b['status'] ?? ''), ['active', 'unknown']) ? 0 : 1;
            if ($aActive !== $bActive) return $aActive - $bActive;

            $aType = self::bizapediaTypeRank($a['EntityType'] ?? $a['type'] ?? '');
            $bType = self::bizapediaTypeRank($b['EntityType'] ?? $b['type'] ?? '');
            if ($aType !== $bType) return $aType - $bType;

            $aDomestic = strtolower($a['DomesticJurisdiction'] ?? $a['domestic_jurisdiction'] ?? '') === strtolower($a['Jurisdiction'] ?? $a['jurisdiction'] ?? '') ? 0 : 1;
            $bDomestic = strtolower($b['DomesticJurisdiction'] ?? $b['domestic_jurisdiction'] ?? '') === strtolower($b['Jurisdiction'] ?? $b['jurisdiction'] ?? '') ? 0 : 1;
            if ($aDomestic !== $bDomestic) return $aDomestic - $bDomestic;

            return 0;
        });
    }

    private static function bizapediaTypeRank(string $type): int
    {
        $upper = strtoupper($type);
        if (str_contains($upper, 'FICTITIOUS')) return 2;
        if (str_contains($upper, 'FOREIGN') || str_contains($upper, 'OUT OF STATE')) return 1;
        return 0; // domestic / normal entity
    }

    // ── OpenCorporates (Browserbase) ────────────────────────────────────────

    /**
     * Search OpenCorporates for a company name. Returns structured results
     * from 140+ jurisdictions. Uses 2Captcha to solve HAProxy hCaptcha.
     */
    public function searchOpenCorporates(string $entityName, ?string $jurisdictionCode = null): string
    {
        $searchUrl = 'https://opencorporates.com/companies?q=' . urlencode($entityName) . '&type=companies';
        if ($jurisdictionCode) {
            $searchUrl .= '&jurisdiction_code=' . urlencode($jurisdictionCode);
        }

        $html = $this->ocFetchWithCaptcha($searchUrl);

        if (!$html || strlen($html) < 200) {
            $result = "Error: OpenCorporates returned empty page (may be CAPTCHA-blocked).";
            $this->log[] = ['tool' => 'opencorporates', 'input' => $entityName, 'output' => $result];
            return $result;
        }

        // Check if still on CAPTCHA page
        if (stripos($html, 'captcha') !== false && stripos($html, '/companies/') === false) {
            $result = "Error: OpenCorporates CAPTCHA not solved — Browserbase could not bypass it.";
            $this->log[] = ['tool' => 'opencorporates', 'input' => $entityName, 'output' => $result];
            return $result;
        }

        // Parse search results from HTML
        $results = $this->parseOpenCorporatesResults($html);

        if (empty($results)) {
            $result = "No OpenCorporates results found for \"{$entityName}\".";
            $this->log[] = ['tool' => 'opencorporates', 'input' => $entityName, 'output' => $result];
            return $result;
        }

        $lines = [];
        foreach ($results as $r) {
            $parts = [$r['name']];
            if ($r['jurisdiction_name']) $parts[] = $r['jurisdiction_name'];
            elseif ($r['jurisdiction']) $parts[] = $r['jurisdiction'];
            if ($r['company_number']) $parts[] = "#{$r['company_number']}";
            if ($r['status']) $parts[] = "status: {$r['status']}";
            if ($r['detailed_status']) $parts[] = "({$r['detailed_status']})";
            if ($r['is_branch']) $parts[] = "BRANCH";
            if ($r['address']) $parts[] = "address: {$r['address']}";
            if (!empty($r['alternative_names'])) $parts[] = "aka: " . implode(', ', $r['alternative_names']);
            $parts[] = "url: {$r['url']}";
            $lines[] = implode(' | ', $parts);
        }

        $result = implode("\n", $lines);
        $this->log[] = ['tool' => 'opencorporates', 'input' => $entityName, 'output' => $result];
        return $result;
    }

    /**
     * Fetch full details for a single OpenCorporates company page.
     */
    public function openCorporatesDetail(string $jurisdiction, string $companyNumber): string
    {
        $url = "https://opencorporates.com/companies/{$jurisdiction}/{$companyNumber}";
        $html = $this->ocFetchWithCaptcha($url);

        if (!$html || str_starts_with($html, 'Error:')) {
            $result = $html ?: "Error: OpenCorporates detail page returned empty.";
            $this->log[] = ['tool' => 'opencorporates_detail', 'input' => "{$jurisdiction}/{$companyNumber}", 'output' => $result];
            return $result;
        }

        $text = $this->htmlToText($html);
        $this->log[] = ['tool' => 'opencorporates_detail', 'input' => "{$jurisdiction}/{$companyNumber}", 'output' => $text];
        return $text;
    }

    /**
     * Fetch an OpenCorporates URL via Bright Data Web Unlocker.
     * Handles CAPTCHAs automatically — no manual solving needed.
     */
    /**
     * Fetch OpenCorporates URL via Bright Data Web Unlocker.
     */
    public function ocFetchWithCaptcha(string $url): string
    {
        $apiKey = $this->config['brightdata_api_key'] ?? '';
        $zone = $this->config['brightdata_zone'] ?? 'web_unlocker1';
        if (!$apiKey) {
            return "Error: Bright Data API key not configured.";
        }

        $basePayload = [
            'zone' => $zone,
            'url' => $url,
            'format' => 'raw',
            'headers' => [
                'User-Agent' => 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
                'Accept' => 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language' => 'en-GB,en;q=0.9',
            ],
        ];

        $lastError = '';
        for ($attempt = 0; $attempt < 3; $attempt++) {
            $basePayload['country'] = self::randomCountry();
            $payload = json_encode($basePayload);
            $responseHeaders = [];
            $ch = curl_init('https://api.brightdata.com/request');
            curl_setopt_array($ch, [
                CURLOPT_RETURNTRANSFER => true,
                CURLOPT_POST => true,
                CURLOPT_HTTPHEADER => [
                    'Content-Type: application/json',
                    "Authorization: Bearer {$apiKey}",
                ],
                CURLOPT_POSTFIELDS => $payload,
                CURLOPT_TIMEOUT => 60,
                CURLOPT_ENCODING => '',
                CURLOPT_HEADERFUNCTION => function ($ch, $header) use (&$responseHeaders) {
                    if (stripos($header, 'x-brd-') === 0) {
                        $responseHeaders[] = trim($header);
                    }
                    return strlen($header);
                },
            ]);
            $html = curl_exec($ch);
            $httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);
            curl_close($ch);

            // Check for Bright Data error headers
            $brdError = '';
            foreach ($responseHeaders as $h) {
                if (stripos($h, 'x-brd-error:') === 0) {
                    $brdError = trim(substr($h, strlen('x-brd-error:')));
                    break;
                }
            }

            if (!$brdError && $httpCode === 200 && $html && strlen($html) >= 200) {
                return $html;
            }

            $lastError = $brdError ?: ($httpCode !== 200 ? "HTTP {$httpCode}" : 'empty response');
            if ($attempt < 3) {
                sleep(2);
            }
        }

        return "Error: Bright Data Web Unlocker — {$lastError} (after 3 attempts)";

        return $html;
    }

    /**
     * Fetch OpenCorporates URL via Bright Data Scraping Browser (Playwright).
     * Returns raw HTML or "Error: ..." string.
     * Bypasses the normal text-length check since we need the HTML, not rendered text.
     */
    public function ocFetchWithScrapingBrowser(string $url): string
    {
        $ws = $this->config['brightdata_scraping_browser_ws'] ?? '';
        if (!$ws) {
            return "Error: Scraping Browser not configured.";
        }

        $script = __DIR__ . '/scraping_browser.mjs';
        $escapedUrl = escapeshellarg($url);
        $escapedWs = escapeshellarg($ws);

        $cmd = "timeout 120 env SBR_WS={$escapedWs} node {$script} {$escapedUrl} --json 2>/dev/null";
        $output = shell_exec($cmd);

        $data = json_decode(trim($output ?? ''), true);
        if (!$data) {
            return "Error: Scraping Browser returned no response.";
        }

        $html = $data['html'] ?? '';
        if (strlen($html) < 200) {
            return "Error: Scraping Browser returned empty page.";
        }

        return $html;
    }

    public function parseOpenCorporatesResults(string $html): array
    {
        $results = [];

        // Extract total count
        $totalCount = null;
        if (preg_match('/Found (\d+) compan/i', $html, $cm)) {
            $totalCount = (int) $cm[1];
        }

        // Each result is an <li class="search-result company ...">
        if (!preg_match_all('/<li class=[\'"]search-result company([^"\']*)[\'"]>(.*?)<\/li>/is', $html, $rows, PREG_SET_ORDER)) {
            return [];
        }

        foreach ($rows as $row) {
            $classes = $row[1];
            $content = $row[2];

            // Status from CSS classes
            $isBranch = str_contains($classes, 'branch');
            $inactive = str_contains($classes, 'inactive');
            $classWords = preg_split('/\s+/', trim($classes));
            // Detailed status is the last word(s) after active/inactive (e.g. dissolved, struck_off)
            $detailedStatus = null;
            $knownStatuses = ['dissolved', 'deregistered', 'struck_off', 'removed', 'liquidated',
                              'registered', 'active', 'in_existence', 'live'];
            foreach ($classWords as $w) {
                if (in_array($w, $knownStatuses) && $w !== 'active' && $w !== 'inactive') {
                    $detailedStatus = ucfirst(str_replace('_', ' ', $w));
                }
            }

            // Company name and link
            if (!preg_match('/<a[^>]+class="company_search_result[^"]*"[^>]+href="(\/companies\/([a-z_]+)\/([^"]+))"[^>]*>([^<]+)<\/a>/i', $content, $linkMatch)) {
                continue;
            }
            $href = $linkMatch[1];
            $jurisdiction = $linkMatch[2];
            $companyNumber = $linkMatch[3];
            $name = trim(html_entity_decode($linkMatch[4]));
            $name = trim($name, '"'); // OC wraps some names in quotes

            // Skip non-company links
            if (in_array($jurisdiction, ['search', 'users', 'events', 'statements'])) continue;

            // Jurisdiction display name from the title attribute on jurisdiction link
            $jurisdictionName = null;
            if (preg_match('/title="[^"]*(?:Data On|Companies In)\s+([^"]+?)\s*Companies?"/', $content, $jm)) {
                $jurisdictionName = trim($jm[1]);
            } elseif (preg_match('/\(([A-Z][a-z][\w\s]+(?:\s*\([^)]+\))?),/', $content, $jm)) {
                $jurisdictionName = trim($jm[1]);
            }

            // Address
            $address = null;
            if (preg_match('/<span class=[\'"]address[\'"]>(?:<a[^>]*>.*?<\/a>)?([^<]+)<\/span>/is', $content, $am)) {
                $address = trim($am[1]);
            }

            // Alternative/previous names
            $altNames = [];
            if (preg_match_all('/Previously\/Alternatively known as ([^<]+)/i', $content, $anm)) {
                foreach ($anm[1] as $an) {
                    $altNames[] = trim(html_entity_decode($an));
                }
            }

            // Trademarks
            $trademarks = [];
            if (preg_match_all('/<span class=[\'"]slight_highlight[\'"]>([^<]+)<\/span>/i', $content, $tm)) {
                foreach ($tm[1] as $t) {
                    $t = trim(html_entity_decode($t));
                    // Filter out alt names already captured
                    if (!str_starts_with($t, 'Previously') && !in_array($t, $altNames)) {
                        $trademarks[] = $t;
                    }
                }
            }

            $active = !$inactive && (str_contains($classes, 'active') || str_contains($classes, 'registered')
                || str_contains($classes, 'in_existence') || str_contains($classes, 'live'));
            $status = $inactive ? 'Inactive' : ($active ? 'Active' : 'Unknown');

            $results[] = [
                'name' => $name,
                'jurisdiction' => $jurisdiction,
                'jurisdiction_name' => $jurisdictionName,
                'company_number' => $companyNumber,
                'status' => $status,
                'detailed_status' => $detailedStatus,
                'is_branch' => $isBranch,
                'address' => $address,
                'alternative_names' => $altNames,
                'trademarks' => $trademarks,
                'url' => "https://opencorporates.com{$href}",
            ];
        }

        return $results;
    }

    private function httpGet(string $url, ?string $userAgent = null): ?string
    {
        $ch = curl_init($url);
        curl_setopt_array($ch, [
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_FOLLOWLOCATION => true,
            CURLOPT_TIMEOUT => 15,
            CURLOPT_USERAGENT => $userAgent ?? 'Mozilla/5.0',
            CURLOPT_SSL_VERIFYPEER => true,
        ]);
        $result = curl_exec($ch);
        $httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);
        return ($httpCode === 200 && $result !== false) ? $result : null;
    }
}
