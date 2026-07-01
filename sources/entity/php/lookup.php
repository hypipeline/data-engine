<?php
/**
 * Entity Lookup v3b — PHP Implementation
 *
 * Phase 1 (scripted): Fetch website pages + WHOIS
 * Phase 2 (cheap LLM call): Extract entity names + jurisdiction
 * Phase 3 (scripted): Search registries using LLM-extracted names
 * Phase 4 (LLM call): Analyze all evidence, produce final report
 */

require_once __DIR__ . '/tools.php';

class EntityLookup
{
    private array $config;
    private LookupTools $tools;
    private array $timings = [];
    private int $totalInputTokens = 0;
    private int $totalOutputTokens = 0;
    private array $progressLog = [];
    private float $startTime = 0;
    /** @var callable|null */
    private $progressCallback = null;

    private string $extractionPrompt;
    private string $analysisPrompt;


    private string $jsonSchema;

    public function __construct(array $config, ?callable $progressCallback = null)
    {
        $this->config = $config;
        $toolsProgress = $progressCallback ? function(array $entry) use ($progressCallback) {
            $entry['time'] = $entry['time'] ?? round(microtime(true) - $this->startTime, 2);
            $entry['detail'] = $entry['detail'] ?? null;
            $this->progressLog[] = $entry;
            $progressCallback($entry);
        } : null;
        $this->tools = new LookupTools($config, $toolsProgress);
        $this->progressCallback = $progressCallback;

        $promptDir = __DIR__ . '/prompts';
        $this->extractionPrompt = file_get_contents("{$promptDir}/extraction.txt");
        $this->analysisPrompt = file_get_contents("{$promptDir}/analysis.txt");
        $this->jsonSchema = file_get_contents("{$promptDir}/schema.txt");
    }

    /**
     * Run the full lookup pipeline.
     */
    private function log(string $phase, string $message, ?array $detail = null): void
    {
        $entry = [
            'time' => round(microtime(true) - $this->startTime, 2),
            'phase' => $phase,
            'message' => $message,
            'detail' => $detail,
        ];
        $this->progressLog[] = $entry;
        if ($this->progressCallback) {
            ($this->progressCallback)($entry);
        }
    }

    public function run(string $url): array
    {
        $this->startTime = microtime(true);
        $t0 = $this->startTime;
        $parsed = parse_url($url);
        $domain = preg_replace('/^www\./', '', $parsed['host'] ?? '');

        $this->log('start', "Beginning lookup for {$domain}");

        // Phase 1: Google Intelligence — search Google for domain, Yahoo Finance, LinkedIn
        $this->log('phase', "Phase 1: Google Intelligence", ['phase_num' => 1]);
        $tGoogle = microtime(true);
        $googleIntel = $this->tools->googleIntelligence($domain);
        $registries = [];

        // Store Google search results
        if ($googleIntel['google_results']) {
            $registries['google_search'] = $googleIntel['google_results'];
            $this->log('google', "Google search: " . substr_count($googleIntel['google_results'], "\n") . " lines", [
                'expandable' => true,
                'sections' => [['label' => 'Google Results', 'content' => $googleIntel['google_results']]],
            ]);
        }

        // Fetch LinkedIn company data
        if ($googleIntel['linkedin_url']) {
            $linkedinData = $this->tools->fetchLinkedInCompany($googleIntel['linkedin_url']);
            if ($linkedinData) {
                $registries['linkedin'] = $linkedinData;
                $this->log('google', "LinkedIn: " . substr_count($linkedinData, "\n") . " lines", [
                    'expandable' => true,
                    'sections' => [['label' => 'LinkedIn Data', 'content' => $linkedinData]],
                ]);
            }
        }

        // Fetch Yahoo Finance data
        if ($googleIntel['yahoo_ticker']) {
            $yahooData = $this->tools->yahooFinanceData($googleIntel['yahoo_ticker']);
            if ($yahooData) {
                $ticker = $googleIntel['yahoo_ticker'];
                $registries["yahoo_finance:{$ticker}"] = $yahooData;
                $this->log('google', "Yahoo Finance: {$ticker} — " . substr_count($yahooData, "\n") . " lines", [
                    'expandable' => true,
                    'sections' => [['label' => 'Yahoo Finance Data', 'content' => $yahooData]],
                ]);
            }
        }

        $this->timings['google_intel'] = microtime(true) - $tGoogle;
        $this->log('google', "Google Intelligence complete in " . round($this->timings['google_intel'], 1) . "s");

        // Phase 2: Fetch website
        $this->log('phase', "Phase 2: Fetch Website Data", ['phase_num' => 2]);
        $websiteData = $this->fetchWebsiteData($url, $domain);
        $this->timings['fetch'] = microtime(true) - $t0;
        $this->log('fetch', "Website fetched in " . round($this->timings['fetch'], 1) . "s — pages: " . implode(', ', array_keys($websiteData['pages'])));

        // Phase 3: Extract entities with LLM (include Google Intelligence data)
        $this->log('phase', "Phase 3: Extract Entity Names", ['phase_num' => 3]);
        $t1 = microtime(true);
        $entityInfo = $this->extractEntitiesWithLlm($websiteData, $registries);

        $this->timings['extraction'] = microtime(true) - $t1;
        $this->log('extract', "Extraction complete in " . round($this->timings['extraction'], 1) . "s — names: " . json_encode($entityInfo['entity_names']) . ", short_names: " . json_encode($entityInfo['short_names'] ?? []) . ", jurisdiction: " . ($entityInfo['jurisdiction'] ?? 'unknown'));

        // Deduplicate entity names before registry search (keep short_names separate for trademark search)
        $deduped = $this->deduplicateNames($entityInfo['entity_names'] ?? []);
        $this->log('extract', "Deduplicated names: " . count($entityInfo['entity_names'] ?? []) . " → " . count($deduped) . "\n  Before: " . json_encode($entityInfo['entity_names'] ?? []) . "\n  After:  " . json_encode($deduped));
        $entityInfo['entity_names'] = $deduped;
        // short_names kept separate — used for trademark search only

        // Phase 4: Search registries
        $this->log('phase', "Phase 4: Search Registries", ['phase_num' => 4]);
        $t2 = microtime(true);
        $newRegistries = $this->searchRegistries($entityInfo, $domain);
        $registries = array_merge($registries, $newRegistries);
        $this->timings['registries'] = microtime(true) - $t2;
        $this->log('registry', "All registry searches complete in " . round($this->timings['registries'], 1) . "s — " . count($newRegistries) . " results");

        // Phase 5: Evidence chain — cross-reference SEC data against website content
        $this->log('phase', "Phase 5: Evidence Chain — Connecting Website to Entities", ['phase_num' => 5]);
        $crossRef = $this->crossReferenceSecData($websiteData, $registries, $entityInfo);
        if ($crossRef) {
            $registries['sec_cross_reference'] = $crossRef;
        }

        // Phase 6: Final LLM analysis
        $this->log('phase', "Phase 6: Final Analysis", ['phase_num' => 6]);
        $t3 = microtime(true);
        $inputTokensBefore = $this->totalInputTokens;
        $outputTokensBefore = $this->totalOutputTokens;
        $report = $this->analyzeEvidence($url, $domain, $websiteData, $entityInfo, $registries);
        $this->timings['analysis'] = microtime(true) - $t3;
        $analysisCost = (($this->totalInputTokens - $inputTokensBefore) * 3.0 / 1_000_000) + (($this->totalOutputTokens - $outputTokensBefore) * 15.0 / 1_000_000);
        $this->log('llm', "Analysis complete in " . round($this->timings['analysis'], 1) . "s — " . number_format($this->totalInputTokens - $inputTokensBefore) . " input / " . number_format($this->totalOutputTokens - $outputTokensBefore) . " output tokens — $" . number_format($analysisCost, 4));

        // Phase 7: Registry validation — verify the recommended entity against the registry
        $entity = $report['recommended_entity'] ?? null;
        $hasRegistryId = $entity && !empty($entity['registry_id']);
        if ($hasRegistryId) {
            $this->log('phase', "Phase 7: Registry Validation", ['phase_num' => 7]);
            $report = $this->validateEntityInRegistry($report);
        }

        // Phase 8: Re-analysis when validation failed OR when recommended entity
        // has no registry_id (LLM couldn't find one — supplementary searches may help)
        $rvStatus = $report['registry_validation']['status'] ?? null;
        $needsReanalysis = ($rvStatus && $rvStatus !== 'verified')
            || ($entity && !$hasRegistryId);
        if ($needsReanalysis) {
            $reason = $rvStatus ? "validation failed: {$rvStatus}" : "no registry_id on recommended entity";
            $this->log('phase', "Phase 8: Re-analysis ({$reason})", ['phase_num' => 8]);
            $t8 = microtime(true);
            $report = $this->reanalyzeAfterValidationFailure(
                $report, $url, $domain, $websiteData, $entityInfo, $registries
            );
            $this->timings['reanalysis'] = microtime(true) - $t8;
        }

        // Auto-downgrade confidence if re-validation also failed
        $rvStatus = $report['registry_validation']['status'] ?? null;
        if ($needsReanalysis && $rvStatus && $rvStatus !== 'verified') {
            $report['confidence'] = 'low';
            $report['validation_warning'] = 'Registry validation failed after re-analysis — confidence auto-downgraded';
            $this->log('warning', "Confidence downgraded to 'low': re-validation status is '{$rvStatus}'");
        }

        // Validate contractable affiliates
        $affiliates = $report['contractable_affiliates'] ?? [];
        if (!empty($affiliates)) {
            $this->log('validate', "Validating " . count($affiliates) . " contractable affiliate(s)...");
            foreach ($affiliates as $i => &$aff) {
                $affName = $aff['legal_entity_name'] ?? '';
                $affRegId = $aff['registry_id'] ?? '';
                if (!$affName || !$affRegId) {
                    $aff['validation_status'] = 'no_registry_id';
                    $aff['registry_validated'] = false;
                    continue;
                }
                $affCountry = strtoupper($aff['jurisdiction_country'] ?? '');
                $affState = strtoupper($aff['jurisdiction_state'] ?? '');
                $v = $this->validateSingleEntity($affName, $affRegId, $affCountry, $affState ?: null);
                $aff['validation_status'] = $v['status'];
                $aff['registry_validated'] = ($v['status'] === 'verified');
                if (!empty($v['validation_url'])) {
                    $aff['validation_url'] = $v['validation_url'];
                }
                if (!empty($v['registry_name'])) {
                    $aff['registry_name'] = $v['registry_name'];
                }
                if (!empty($v['source'])) {
                    $aff['validation_source'] = $v['source'];
                }
                $this->log('validate', "Affiliate \"{$affName}\": {$v['status']}" . (!empty($v['source']) ? " ({$v['source']})" : ''));
            }
            unset($aff);
            $report['contractable_affiliates'] = $affiliates;
        }

        $entity = $report['recommended_entity'] ?? null;
        $confidence = $report['confidence'] ?? 'unknown';
        $this->log('done', $entity
            ? "Result: {$entity['legal_entity_name']} ({$confidence})"
            : "Result: No entity found ({$confidence})");

        $totalTime = microtime(true) - $t0;

        // Cost per model ($ per 1M tokens)
        $model = $this->config['model'] ?? '';
        $rates = match(true) {
            str_contains($model, 'haiku') => [0.80, 4.00],
            str_contains($model, 'opus') => [15.00, 75.00],
            str_contains($model, 'sonnet') => [3.00, 15.00],
            $model === 'gpt-4o-mini' => [0.15, 0.60],
            $model === 'gpt-4o' => [2.50, 10.00],
            $model === 'o3' => [2.00, 8.00],
            $model === 'o4-mini' => [1.10, 4.40],
            default => [3.00, 15.00],
        };
        $cost = ($this->totalInputTokens * $rates[0] / 1_000_000) + ($this->totalOutputTokens * $rates[1] / 1_000_000);

        return [
            'report' => $report,
            'meta' => [
                'total_time_s' => round($totalTime, 1),
                'phase_times' => array_map(fn($t) => round($t, 1), $this->timings),
                'model' => $this->config['model'],
                'input_tokens' => $this->totalInputTokens,
                'output_tokens' => $this->totalOutputTokens,
                'cost_usd' => round($cost, 4),
                'api_calls' => $this->tools->getApiCalls(),
            ],
            'progress_log' => $this->progressLog,
        ];
    }

    // ── Phase 1: Fetch Website ───────────────────────────────────────────────

    /**
     * Extract links from raw homepage HTML and categorise by page type.
     * Returns ['terms' => [...], 'privacy' => [...], 'about' => [...], 'contact' => [...]]
     */
    private function discoverLinksFromHomepage(string $html, string $base): array
    {
        $skipExtensions = '/\.(?:pdf|doc|docx|xls|xlsx|ppt|pptx|zip|png|jpg|jpeg|gif|svg|mp4|mp3|csv)$/i';

        $patterns = [
            'terms'      => '/\b(terms|conditions|tos)\b/i',
            'privacy'    => '/\b(privacy|datenschutz|gdpr)\b/i',
            'about'      => '/\b(about|company|who.we.are|ueber.uns)\b/i',
            'contact'    => '/\b(contact|kontakt)\b/i',
            'leadership' => '/\b(leadership|management|team|executive|board|directors|officers)\b/i',
            'disclosures' => '/\b(disclosures?|regulatory|form.adv|form.crs)\b/i',
            'news'       => '/\b(news|press|media|announcements?|blog)\b/i',
            'impressum'  => '/\b(impressum|imprint)\b/i',
        ];

        $discovered = ['terms' => [], 'privacy' => [], 'about' => [], 'contact' => [], 'leadership' => [], 'disclosures' => [], 'news' => [], 'impressum' => []];

        // Extract all href values from <a> tags
        if (!preg_match_all('/<a\s[^>]*href=["\']([^"\'#]+)/i', $html, $matches)) {
            return $discovered;
        }

        $seen = [];
        foreach ($matches[1] as $href) {
            // Resolve relative URLs
            if (str_starts_with($href, '/')) {
                $href = $base . $href;
            } elseif (!str_starts_with($href, 'http')) {
                continue; // skip mailto:, javascript:, etc.
            }

            // Only keep URLs on the same domain
            $hrefHost = parse_url($href, PHP_URL_HOST);
            $baseHost = parse_url($base, PHP_URL_HOST);
            if ($hrefHost && $baseHost && !str_ends_with($hrefHost, preg_replace('/^www\./', '', $baseHost))) {
                continue;
            }

            // Skip non-HTML file extensions
            $path = parse_url($href, PHP_URL_PATH) ?? '';
            if (preg_match($skipExtensions, $path)) {
                continue;
            }

            // Deduplicate
            $normalised = rtrim($href, '/');
            if (isset($seen[$normalised])) continue;
            $seen[$normalised] = true;

            // Categorise by URL path + link text (we match on the href itself)
            foreach ($patterns as $cat => $regex) {
                if (preg_match($regex, $path)) {
                    $discovered[$cat][] = $normalised;
                }
            }
        }

        return $discovered;
    }

    /**
     * Discover links from homepage HTML and merge into pending categories.
     * Returns newly-discovered URLs (not already in $pending).
     */
    private function discoverAndMerge(string $html, string $base, array &$pending, array $pages, string $logPhase): array
    {
        $discovered = $this->discoverLinksFromHomepage($html, $base);
        $totalFound = array_sum(array_map('count', $discovered));
        if ($totalFound === 0) return [];

        $summary = [];
        foreach ($discovered as $cat => $dUrls) {
            foreach ($dUrls as $u) {
                $summary[] = "{$cat}: {$u}";
            }
        }
        $this->log($logPhase, "  Discovered links from homepage:\n    " . implode("\n    ", $summary));

        $newUrls = [];
        foreach ($discovered as $cat => $dUrls) {
            if (isset($pages[$cat])) continue; // already resolved
            if (!isset($pending[$cat])) $pending[$cat] = [];
            foreach ($dUrls as $dUrl) {
                if (!in_array($dUrl, $pending[$cat])) {
                    array_unshift($pending[$cat], $dUrl);
                    $newUrls[] = $dUrl;
                }
            }
        }
        return $newUrls;
    }

    /**
     * Check if sub-page text is just the homepage repeated (redirect/bot block).
     * Compares first 500 chars after stripping whitespace.
     */
    private function isDuplicateOfHomepage(string $text, array $pages): bool
    {
        if (!isset($pages['homepage'])) return false;
        $normalize = fn($s) => preg_replace('/\s+/', '', substr($s, 0, 500));
        $subNorm = $normalize($text);
        $homeNorm = $normalize($pages['homepage']);
        if (strlen($subNorm) < 100) return false;
        return $subNorm === $homeNorm;
    }

    private function fetchWebsiteData(string $url, string $domain): array
    {
        $base = rtrim($url, '/');
        $maxChars = $this->config['max_page_chars'];

        // ── WHOIS (runs independently) ──
        $this->log('fetch', "WHOIS lookup: {$domain}");
        $whois = $this->tools->whoisLookup($domain);
        $this->log('fetch', "WHOIS: " . strlen($whois) . " chars", [
            'expandable' => true,
            'sections' => [['label' => 'WHOIS Data', 'content' => $whois]],
        ]);

        // ── Fallback URL guesses (only used for categories NOT discovered from homepage) ──
        $fallbackUrls = [
            'terms'      => ["{$base}/terms-of-use", "{$base}/terms-of-service", "{$base}/terms"],
            'privacy'    => ["{$base}/privacy-policy", "{$base}/privacy"],
            'about'      => ["{$base}/about", "{$base}/about-us", "{$base}/company"],
            'contact'    => ["{$base}/contact", "{$base}/contact-us"],
            'leadership' => ["{$base}/leadership", "{$base}/team", "{$base}/about/team"],
            'disclosures' => ["{$base}/disclosures", "{$base}/regulatory-disclosures"],
            'news'       => ["{$base}/news", "{$base}/press"],
            'impressum'  => ["{$base}/impressum", "{$base}/imprint"],
        ];

        // ── LEVEL 1a: Fetch homepage first ──
        $this->log('fetch', "Level 1a: Fetching homepage...");
        $curlResults = $this->tools->curlFetchMulti([$url]);

        // ── Discover links from homepage HTML ──
        $discovered = [];
        $homepageHtml = $curlResults[$url]['html'] ?? null;
        if ($homepageHtml) {
            $discovered = $this->discoverLinksFromHomepage($homepageHtml, $base);
            $totalFound = array_sum(array_map('count', $discovered));
            if ($totalFound > 0) {
                $summary = [];
                foreach ($discovered as $cat => $dUrls) {
                    foreach ($dUrls as $u) {
                        $summary[] = "{$cat}: {$u}";
                    }
                }
                $this->log('fetch', "  Discovered links from homepage:\n    " . implode("\n    ", $summary));
            }
        }

        // ── Build URL list: discovered links take priority, fallback guesses fill gaps ──
        $categories = [];
        foreach ($fallbackUrls as $cat => $_) {
            if (!empty($discovered[$cat])) {
                // Homepage had links for this category — use only those
                $categories[$cat] = $discovered[$cat];
            } else {
                // No links discovered — use fallback guesses
                $categories[$cat] = $fallbackUrls[$cat];
            }
        }

        // ── LEVEL 1b: Parallel curl for all sub-page URLs ──
        $allUrls = [];
        foreach ($categories as $cat => $catUrls) {
            foreach ($catUrls as $catUrl) {
                if (!isset($curlResults[$catUrl])) {
                    $allUrls[] = $catUrl;
                }
            }
        }

        $this->log('fetch', "Level 1b: Fetching " . count($allUrls) . " sub-page URLs...");
        $subResults = $this->tools->curlFetchMulti($allUrls);
        $curlResults = array_merge($curlResults, $subResults);

        $pages = [];      // cat => text (resolved pages)
        $pageSources = []; // cat => source label
        $pageUrls = [];    // cat => resolved URL
        $pending = [];     // cat => [url, url, ...] (still need fetching)
        $is404 = [];       // cat => true (skip further attempts)

        // Process homepage
        $hr = $curlResults[$url];
        if ($hr['text'] !== null) {
            $pages['homepage'] = substr($hr['text'], 0, $maxChars);
            $pageSources['homepage'] = 'curl';
            $pageUrls['homepage'] = $url;
            $this->log('fetch', "  homepage: / → HTTP {$hr['http_code']} ✓ " . strlen($pages['homepage']) . " chars", [
                'expandable' => true,
                'sections' => [['label' => 'Page Text', 'content' => substr($pages['homepage'], 0, 5000)]],
            ]);
        } else {
            $this->log('fetch', "  homepage: / → HTTP {$hr['http_code']}");
            if ($hr['http_code'] === 404) {
                $is404['homepage'] = true;
            } else {
                $pending['homepage'] = [$url];
            }
        }

        // Process sub-pages: log every URL with its status, find first success per category
        foreach ($categories as $cat => $catUrls) {
            if (isset($pages[$cat])) continue;
            $found = false;
            $sbrCandidates = [];
            foreach ($catUrls as $catUrl) {
                $cr = $curlResults[$catUrl] ?? ['text' => null, 'http_code' => 0, 'html' => null];
                $path = parse_url($catUrl, PHP_URL_PATH) ?: $catUrl;
                if (!$found && $cr['text'] !== null) {
                    $candidate = substr($cr['text'], 0, $maxChars);
                    if ($this->isDuplicateOfHomepage($candidate, $pages)) {
                        $this->log('fetch', "  {$cat}: {$path} → HTTP {$cr['http_code']} — skipped (duplicate of homepage)");
                        $sbrCandidates[] = $catUrl;
                        continue;
                    }
                    $pages[$cat] = $candidate;
                    $pageSources[$cat] = 'curl';
                    $pageUrls[$cat] = $catUrl;
                    $this->log('fetch', "  {$cat}: {$path} → HTTP {$cr['http_code']} ✓ " . strlen($pages[$cat]) . " chars", [
                        'expandable' => true,
                        'sections' => [['label' => 'Page Text', 'content' => substr($pages[$cat], 0, 5000)]],
                    ]);
                    $found = true;
                } else {
                    $this->log('fetch', "  {$cat}: {$path} → HTTP {$cr['http_code']}");
                    if ($cr['http_code'] !== 404) {
                        $sbrCandidates[] = $catUrl;
                    }
                }
            }
            if (!$found) {
                if (!empty($sbrCandidates)) {
                    $pending[$cat] = $sbrCandidates;
                } else {
                    $is404[$cat] = true;
                }
            }
        }

        // Summary with remaining candidates
        $resolved = array_keys($pages);
        $skipped = array_keys($is404);
        $summaryParts = [];
        $summaryParts[] = "Resolved: " . (empty($resolved) ? 'none' : implode(', ', $resolved));
        if (!empty($skipped)) {
            $summaryParts[] = "Skipped (all 404): " . implode(', ', $skipped);
        }
        if (!empty($pending)) {
            $pendingLines = [];
            foreach ($pending as $cat => $catUrls) {
                $paths = array_map(fn($u) => parse_url($u, PHP_URL_PATH) ?: $u, $catUrls);
                $pendingLines[] = "    {$cat}: " . implode(', ', $paths);
            }
            $summaryParts[] = "Remaining candidates:\n" . implode("\n", $pendingLines);
        }
        $this->log('fetch', "Curl summary:\n  " . implode("\n  ", $summaryParts));

        // If everything resolved with curl, skip fallbacks
        if (empty($pending)) {
            $pages = $this->fetchNewsArticles($pages, $pageUrls, $curlResults, $categories, $base, $maxChars);
            return ['url' => $url, 'domain' => $domain, 'pages' => $pages, 'pageUrls' => $pageUrls, 'whois' => $whois];
        }

        // ── LEVEL 2: Bright Data Scraping Browser (JS rendering + bot bypass, parallel) ──
        $sbrUrls = [];
        foreach ($pending as $cat => $catUrls) {
            $sbrUrls[($cat === 'homepage') ? $url : $catUrls[0]] = $cat;
        }
        $sbrUrlList = array_keys($sbrUrls);
        $sbrPaths = array_map(fn($u) => parse_url($u, PHP_URL_PATH) ?: '/', $sbrUrlList);
        $this->log('scraping_browser', "Level 2: Trying Bright Data Scraping Browser for " . count($sbrUrls) . " categories in parallel (" . implode(', ', $sbrPaths) . ")...");

        $sbrResults = $this->tools->scrapingBrowserFetchParallel($sbrUrlList);

        // Process homepage first (for link discovery)
        if (isset($pending['homepage'])) {
            $hr = $sbrResults[$url] ?? null;
            if ($hr && strlen($hr['text']) >= 200) {
                $pages['homepage'] = substr($hr['text'], 0, $maxChars);
                $pageSources['homepage'] = 'scraping_browser';
                $pageUrls['homepage'] = $url;
                $this->log('scraping_browser', "  Found homepage (" . strlen($pages['homepage']) . " chars)", [
                    'expandable' => true,
                    'sections' => [['label' => 'Page Text', 'content' => substr($pages['homepage'], 0, 5000)]],
                ]);
                unset($pending['homepage']);

                if (!empty($hr['html'])) {
                    $this->discoverAndMerge($hr['html'], $base, $pending, $pages, 'scraping_browser');
                }
            } else {
                $this->log('scraping_browser', "  Miss homepage — Bright Data Scraping Browser returned no usable content");
            }
        }

        // Fetch any newly-discovered URLs not in the first batch
        $newSbrUrls = [];
        foreach ($pending as $cat => $catUrls) {
            if ($cat === 'homepage' || isset($pages[$cat]) || empty($catUrls)) continue;
            $catUrl = $catUrls[0];
            if (!isset($sbrResults[$catUrl])) {
                $newSbrUrls[$catUrl] = $cat;
            }
        }
        if (!empty($newSbrUrls)) {
            $newPaths = array_map(fn($u) => parse_url($u, PHP_URL_PATH) ?: '/', array_keys($newSbrUrls));
            $this->log('scraping_browser', "  Fetching " . count($newSbrUrls) . " discovered URLs in parallel (" . implode(', ', $newPaths) . ")...");
            $extraResults = $this->tools->scrapingBrowserFetchParallel(array_keys($newSbrUrls));
            $sbrResults = array_merge($sbrResults, $extraResults);
        }

        // Process sub-pages
        $stillPending = [];
        foreach ($pending as $cat => $catUrls) {
            if ($cat === 'homepage') { $stillPending[$cat] = $catUrls; continue; }
            if (isset($pages[$cat]) || empty($catUrls)) continue;
            $catUrl = $catUrls[0];
            $path = parse_url($catUrl, PHP_URL_PATH) ?: $catUrl;
            $sr = $sbrResults[$catUrl] ?? null;
            if ($sr && strlen($sr['text']) >= 200) {
                $candidate = substr($sr['text'], 0, $maxChars);
                if ($this->isDuplicateOfHomepage($candidate, $pages)) {
                    $this->log('scraping_browser', "  Miss {$cat}: {$path} — duplicate of homepage (likely redirect)");
                    $stillPending[$cat] = $catUrls;
                } else {
                    $pages[$cat] = $candidate;
                    $pageSources[$cat] = 'scraping_browser';
                    $pageUrls[$cat] = $catUrl;
                    $this->log('scraping_browser', "  Found {$cat}: {$path} (" . strlen($pages[$cat]) . " chars)", [
                        'expandable' => true,
                        'sections' => [['label' => 'Page Text', 'content' => substr($pages[$cat], 0, 5000)]],
                    ]);
                }
            } else {
                $this->log('scraping_browser', "  Miss {$cat}: {$path} — Bright Data Scraping Browser returned no usable content");
                $stillPending[$cat] = $catUrls;
            }
        }

        if (empty($stillPending)) {
            return ['url' => $url, 'domain' => $domain, 'pages' => $pages, 'pageUrls' => $pageUrls, 'whois' => $whois];
        }

        /* ── LEVELS 3-5 DISABLED ──
         * Level 3: Bright Data Web Unlocker
         * Level 4: Browserbase
         * Level 5: Wayback Machine
         * Currently only using Level 1 (Curl) and Level 2 (Bright Data Scraping Browser).
         */

        // Summary
        $foundCats = array_keys($pages);
        $sources = [];
        foreach ($pageSources as $cat => $src) {
            if ($src !== 'curl') $sources[] = "{$cat}={$src}";
        }
        $sourceNote = $sources ? ' (' . implode(', ', $sources) . ')' : '';
        $this->log('fetch', "Fetch complete: " . count($pages) . " pages" . $sourceNote);

        // ── News articles: if we got a news index page, extract and fetch up to 3 articles ──
        $pages = $this->fetchNewsArticles($pages, $pageUrls, $curlResults, $categories, $base, $maxChars);

        return ['url' => $url, 'domain' => $domain, 'pages' => $pages, 'pageUrls' => $pageUrls, 'whois' => $whois];
    }

    /**
     * Extract article links from the news index page HTML and fetch up to 3.
     */
    private function fetchNewsArticles(array $pages, array &$pageUrls, array $curlResults, array $categories, string $base, int $maxChars): array
    {
        if (!isset($pages['news'])) return $pages;

        // Find the news index URL that succeeded — we need its HTML
        $newsHtml = null;
        foreach ($categories['news'] ?? [] as $newsUrl) {
            if (isset($curlResults[$newsUrl]) && $curlResults[$newsUrl]['html']) {
                $newsHtml = $curlResults[$newsUrl]['html'];
                break;
            }
        }
        if (!$newsHtml) {
            $this->log('fetch', "News: index page text found but raw HTML not available (fetched via Scraping Browser?) — cannot extract article links");
            return $pages;
        }
        $this->log('fetch', "News: scanning index page for article links (" . strlen($newsHtml) . " chars of HTML)");

        // Extract article links: look for <a> tags with paths that look like articles
        // (contain dates, slugs, or are deeper than the news index)
        $articleUrls = [];
        if (preg_match_all('/<a\s[^>]*href=["\']([^"\'#]+)/i', $newsHtml, $matches)) {
            $newsPath = null;
            foreach ($categories['news'] as $nu) {
                if (isset($curlResults[$nu]) && $curlResults[$nu]['html']) {
                    $newsPath = parse_url($nu, PHP_URL_PATH);
                    break;
                }
            }

            $seen = [];
            foreach ($matches[1] as $href) {
                if (str_starts_with($href, '/')) {
                    $href = rtrim($base, '/') . $href;
                } elseif (!str_starts_with($href, 'http')) {
                    continue;
                }

                $parsedHost = parse_url($href, PHP_URL_HOST);
                $baseHost = parse_url($base, PHP_URL_HOST);
                if ($parsedHost && $parsedHost !== $baseHost) continue;

                $path = parse_url($href, PHP_URL_PATH) ?? '';

                // Skip non-article links (assets, anchors, index itself)
                if (preg_match('/\.(?:pdf|png|jpg|jpeg|gif|svg|css|js|xml|zip)$/i', $path)) continue;
                if ($newsPath && $path === $newsPath) continue;
                if (in_array($href, $seen)) continue;

                // Article heuristic: path is deeper than news index, or contains date-like segments
                if ($newsPath && str_starts_with($path, $newsPath) && $path !== $newsPath && substr_count($path, '/') > substr_count($newsPath, '/')) {
                    $seen[] = $href;
                    $articleUrls[] = $href;
                } elseif (preg_match('/\/\d{4}\//', $path) || preg_match('/\/\d{4}-\d{2}/', $path)) {
                    $seen[] = $href;
                    $articleUrls[] = $href;
                }

                if (count($articleUrls) >= 3) break;
            }
        }

        if (empty($articleUrls)) {
            $totalLinks = isset($matches[1]) ? count($matches[1]) : 0;
            $this->log('fetch', "News: scanned {$totalLinks} links on index page — no article links matched (need sub-paths of news index or date-patterned URLs)");
            return $pages;
        }

        $articlePaths = array_map(fn($u) => parse_url($u, PHP_URL_PATH) ?: $u, $articleUrls);
        $this->log('fetch', "News: found " . count($articleUrls) . " article links on index page:\n    " . implode("\n    ", $articlePaths));
        $articleResults = $this->tools->curlFetchMulti($articleUrls);

        $i = 0;
        foreach ($articleUrls as $aUrl) {
            $ar = $articleResults[$aUrl] ?? null;
            if ($ar && $ar['text'] !== null && strlen($ar['text']) >= 200) {
                $i++;
                $pages["news_{$i}"] = substr($ar['text'], 0, $maxChars);
                $pageUrls["news_{$i}"] = $aUrl;
                $path = parse_url($aUrl, PHP_URL_PATH) ?: $aUrl;
                $this->log('fetch', "  news_{$i}: {$path} ✓ " . strlen($pages["news_{$i}"]) . " chars", [
                    'expandable' => true,
                    'sections' => [['label' => 'Article Text', 'content' => substr($pages["news_{$i}"], 0, 5000)]],
                ]);
            }
        }

        if ($i === 0) {
            $this->log('fetch', "News: articles could not be fetched");
        }

        return $pages;
    }

    // ── Regex Candidate Name Extraction (mirrors Python extract_candidate_names) ──

    private function scrubBlockedNames(string $text): string
    {
        $blocked = $this->config['blocked_entity_names'] ?? [];
        if (!$blocked) return $text;

        $lines = explode("\n", $text);
        $scrubbed = [];
        $removed = 0;
        foreach ($lines as $line) {
            $skip = false;
            foreach ($blocked as $term) {
                if (stripos($line, $term) !== false) {
                    $skip = true;
                    $removed++;
                    break;
                }
            }
            if (!$skip) $scrubbed[] = $line;
        }
        if ($removed) {
            $this->log('extract', "Scrubbed {$removed} WHOIS line(s) matching blocked names");
        }
        return implode("\n", $scrubbed);
    }

    private function extractCandidateNames(string $text, int $limit = 5): array
    {
        // Unambiguous suffixes (case-insensitive, safe to match freely)
        // Note: S.A. requires negative lookbehind to avoid matching inside U.S.A.
        $suffixPattern = '(?:Ltd\.?|Limited|LLC|L\.L\.C\.?|Inc\.?|Incorporated|'
            . 'Corp\.?|Corporation|LLP|L\.L\.P\.?|L\.P\.?|'
            . 'PLC|Plc|p\.l\.c\.?|GmbH|mbH|KGaA|OHG|'
            . 'S\.A\.S\.?|SAS|S\.A\.R\.L\.?|SARL|'
            . 'B\.V\.?|BV|N\.V\.?|NV|C\.V\.?|CV|'
            . 'S\.r\.l\.?|SRL|S\.p\.A\.?|SpA|'
            . 'A\.S\.?|ApS|A\/S|Oyj|'
            . '(?<!\.)S\.A\.?|(?<!\.)S\.L\.?|(?<!\.)S\.C\.?|e\.V\.?|'
            . 'Pty\.?\s*Ltd\.?|Co\.?\s*Ltd\.?)';

        // Ambiguous suffixes — short uppercase words that are also common English words.
        // Matched separately: require preceding word to start with uppercase.
        $ambiguousPattern = '/([A-Z][A-Za-z0-9\s&,\'\.]+?)\s+(A[Gg]|S[Aa]|S[Ee]|K[Gg]|U[Gg]|A[Ss]|A[Bb]|S[Ll]|S[Cc]|O[Yy]|LP|Company)\b/';

        $candidates = [];

        // 1. Copyright notices — capture up to legal suffix only
        if (preg_match_all('/(?:©|\(c\)|copyright)\s*(?:\d{4}[–\-\s,]*\d{0,4}\s*)?(.+?' . $suffixPattern . ')\b/i', $text, $matches)) {
            foreach ($matches[1] as $name) {
                $name = trim($name, " .\t\n\r—–-");
                if (strlen($name) > 3 && strlen($name) < 120) {
                    $candidates[$name] = true;
                }
            }
        }
        // Copyright with ambiguous suffix (case-sensitive): "© 2024 adidas AG"
        if (preg_match_all('/(?:©|\(c\)|copyright)\s*(?:\d{4}[–\-\s,]*\d{0,4}\s*)?(.+?\s+(?:A[Gg]|S[Aa]|S[Ee]|K[Gg]|U[Gg]|A[Ss]|A[Bb]|S[Ll]|S[Cc]|O[Yy]|LP))\b/', $text, $matches)) {
            foreach ($matches[1] as $name) {
                $name = trim($name, " .\t\n\r—–-");
                if (strlen($name) > 3 && strlen($name) < 120) {
                    $candidates[$name] = true;
                }
            }
        }

        // 2. Lines containing legal suffixes
        foreach (explode("\n", $text) as $line) {
            $line = trim($line);
            if (!$line || strlen($line) > 300) continue;

            $hasSuffix = preg_match('/\b' . $suffixPattern . '\b/i', $line);
            $hasAmbiguous = preg_match($ambiguousPattern, $line);
            if (!$hasSuffix && !$hasAmbiguous) continue;

            // "operated by X Ltd" patterns (unambiguous suffixes)
            if (preg_match_all('/(?:operated|owned|run|managed|maintained)\s+by\s+(.+?' . $suffixPattern . ')\b/i', $line, $m)) {
                foreach ($m[1] as $name) {
                    $name = trim($name, " .\t\n\r");
                    if (strlen($name) > 3 && strlen($name) < 120) {
                        $candidates[$name] = true;
                    }
                }
            }
            // Entity name + suffix: 1-6 capitalised words followed by a legal suffix
            // e.g. "Herculite Products Inc", "Amazon Web Services LLC", "Herculite, Inc."
            // Name words must start uppercase (case-sensitive), suffix is case-insensitive via (?i:...)
            if (preg_match_all('/\b((?:[A-Z][A-Za-z0-9\'\.]*(?:\s+(?:&\s+)?|\s*,\s*)){0,5}[A-Z][A-Za-z0-9\'\.]*[\s,]+(?i:' . $suffixPattern . '))\.?\b/', $line, $m)) {
                foreach ($m[1] as $name) {
                    $name = trim($name, " .\t\n\r");
                    if (strlen($name) > 3 && strlen($name) < 120) {
                        $candidates[$name] = true;
                    }
                }
            }
            // Ambiguous suffixes: "Siemens AG", "Equinor AS" (case-sensitive, require uppercase preceding word)
            if (preg_match_all($ambiguousPattern, $line, $m, PREG_SET_ORDER)) {
                foreach ($m as $match) {
                    $name = trim($match[1] . ' ' . $match[2]);
                    if (strlen($name) > 3 && strlen($name) < 120) {
                        $candidates[$name] = true;
                    }
                }
            }
        }

        // 3. Filter out entity type descriptions (not actual entity names)
        $entityTypePattern = '/^(Domestic|Foreign|Registered|Gen\.?|Non-?Profit|For-?Profit|Mutual)\b/i';
        $entityTypeFragmentPattern = '/^(Liability|Profit|Stock|Business|General)\s+(Company|Corporation|Partnership|LLC)/i';
        $entityTypeExact = [
            'corporation', 'business corporation', 'profit corporation', 'stock corporation',
            'limited partnership', 'limited-liability company', 'limited liability company',
            'liability company', 'profit business corporation',
            'trade name', 'fictitious name', 'dba', 'dpc', 'business', 'partnership',
            'corporation for profit', 'general partnership', 'sole proprietorship',
        ];
        foreach ($candidates as $name => $_) {
            $norm = strtolower(trim($name, ' .'));
            if (preg_match($entityTypePattern, $name) || preg_match($entityTypeFragmentPattern, $name) || in_array($norm, $entityTypeExact)) {
                unset($candidates[$name]);
            }
        }

        // 4. Deduplicate: keep longest forms, remove substrings
        $result = array_keys($candidates);
        usort($result, fn($a, $b) => strlen($b) - strlen($a));
        $final = [];
        foreach ($result as $name) {
            $isSubstring = false;
            foreach ($final as $other) {
                if ($name !== $other && stripos($other, $name) !== false) {
                    $isSubstring = true;
                    break;
                }
            }
            if (!$isSubstring) $final[] = $name;
        }

        return array_slice($final, 0, $limit);
    }

    // ── Phase 2: LLM Entity Extraction ───────────────────────────────────────

    private function extractEntitiesWithLlm(array $websiteData, array $googleIntelRegistries = []): array
    {
        $parts = [];
        $seenLines = [];
        foreach ($websiteData['pages'] as $pageName => $text) {
            $truncated = substr($text, 0, 3000);
            // For contact/about pages, also include the tail (addresses often near bottom)
            if (strlen($text) > 3500 && preg_match('/contact|about|impressum|imprint|leadership|management|team|disclosures?|regulatory|news/i', $pageName)) {
                $tail = substr($text, -1000);
                $truncated .= "\n[...]\n" . $tail;
            }
            // Deduplicate lines that appeared on previous pages (nav, footer, cookie banners)
            $lines = explode("\n", $truncated);
            $filtered = [];
            foreach ($lines as $line) {
                $trimmed = trim($line);
                if (strlen($trimmed) >= 30 && isset($seenLines[$trimmed])) continue;
                $filtered[] = $line;
                if (strlen($trimmed) >= 30) $seenLines[$trimmed] = true;
            }
            $parts[] = "=== " . strtoupper($pageName) . " ===\n" . implode("\n", $filtered);
        }
        $parts[] = "=== WHOIS ===\n{$this->scrubBlockedNames($websiteData['whois'])}";

        // Include Google Intelligence data (LinkedIn, Yahoo Finance, Google results)
        foreach ($googleIntelRegistries as $key => $data) {
            if ($data) {
                $label = strtoupper(str_replace('_', ' ', explode(':', $key)[0]));
                $parts[] = "=== {$label} ===\n" . substr($data, 0, 3000);
            }
        }
        $userMsg = implode("\n\n", $parts);

        $this->log('llm', "LLM extraction — calling {$this->config['model']}", [
            'expandable' => true,
            'sections' => [
                ['label' => 'System Prompt', 'content' => $this->extractionPrompt],
                ['label' => 'User Input (' . number_format(strlen($userMsg)) . ' chars)', 'content' => $userMsg],
            ],
        ]);

        $responseText = $this->callLLM($this->extractionPrompt, $userMsg, 2048);

        $this->log('llm', "LLM extraction output:", [
            'expandable' => true,
            'sections' => [['label' => 'Response JSON', 'content' => $responseText]],
        ]);

        // Parse JSON from response
        $result = $this->parseJsonResponse($responseText, [
            'entity_names' => [],
            'short_names' => [],
            'jurisdiction' => 'unknown',
        ]);

        // If the LLM knows a parent entity, add it to entity_names
        if (!empty($result['known_parent'])) {
            $result['entity_names'][] = $result['known_parent'];
            $this->log('llm', "LLM knows parent entity: {$result['known_parent']}");
        }
        if (!empty($result['known_jurisdiction'])) {
            $this->log('llm', "LLM knows parent jurisdiction: {$result['known_jurisdiction']}");
        }
        if (!empty($result['llm_notes'])) {
            $this->log('llm', "LLM notes: {$result['llm_notes']}");
        }

        return $result;
    }

    // ── Phase 3: Registry Searches ───────────────────────────────────────────

    /**
     * Deduplicate entity names by normalizing punctuation and suffix variants.
     * "Inc." vs "Inc" vs "Incorporated" are treated as the same suffix.
     * Keeps the longest original form.
     */
    private function deduplicateNames(array $names): array
    {
        // Suffix equivalences: map short forms to canonical long form
        $suffixMap = [
            'inc' => 'incorporated', 'inc.' => 'incorporated',
            'corp' => 'corporation', 'corp.' => 'corporation',
            'ltd' => 'limited', 'ltd.' => 'limited',
            'co' => 'company', 'co.' => 'company',
            'plc' => 'public limited company', 'p.l.c.' => 'public limited company', 'p.l.c' => 'public limited company',
            'llc' => 'limited liability company', 'l.l.c.' => 'limited liability company', 'l.l.c' => 'limited liability company',
            'llp' => 'limited liability partnership', 'l.l.p.' => 'limited liability partnership', 'l.l.p' => 'limited liability partnership',
            'lp' => 'limited partnership', 'l.p.' => 'limited partnership', 'l.p' => 'limited partnership',
            'ag' => 'aktiengesellschaft',
            'gmbh' => 'gesellschaft mit beschraenkter haftung', 'mbh' => 'gesellschaft mit beschraenkter haftung',
            'sa' => 'societe anonyme', 's.a.' => 'societe anonyme', 's.a' => 'societe anonyme',
            'sas' => 'societe par actions simplifiee', 's.a.s.' => 'societe par actions simplifiee', 's.a.s' => 'societe par actions simplifiee',
            'sarl' => 'societe a responsabilite limitee', 's.a.r.l.' => 'societe a responsabilite limitee',
            'bv' => 'besloten vennootschap', 'b.v.' => 'besloten vennootschap', 'b.v' => 'besloten vennootschap',
            'nv' => 'naamloze vennootschap', 'n.v.' => 'naamloze vennootschap', 'n.v' => 'naamloze vennootschap',
            'se' => 'societas europaea',
            'kg' => 'kommanditgesellschaft', 'kgaa' => 'kommanditgesellschaft auf aktien',
            'ab' => 'aktiebolag',
            'as' => 'aksjeselskap', 'a.s.' => 'aksjeselskap', 'a.s' => 'aksjeselskap', 'a/s' => 'aksjeselskap',
            'aps' => 'anpartsselskab',
            'oy' => 'osakeyhtio', 'oyj' => 'osakeyhtio julkinen',
            'srl' => 'societa a responsabilita limitata', 's.r.l.' => 'societa a responsabilita limitata',
            'spa' => 'societa per azioni', 's.p.a.' => 'societa per azioni',
            'sl' => 'sociedad limitada', 's.l.' => 'sociedad limitada',
            'pty ltd' => 'proprietary limited', 'pty ltd.' => 'proprietary limited',
            'pty. ltd' => 'proprietary limited', 'pty. ltd.' => 'proprietary limited',
            'co ltd' => 'company limited', 'co. ltd' => 'company limited',
            'co ltd.' => 'company limited', 'co. ltd.' => 'company limited',
        ];

        $normalized = []; // normalizedKey => original name
        foreach ($names as $n) {
            $n = trim($n);
            if (strlen($n) <= 1) continue;

            // Build normalized key: lowercase, strip punctuation, normalize suffix
            $key = strtolower($n);
            $key = str_replace([',', '.'], '', $key);
            $key = preg_replace('/\s+/', ' ', trim($key));

            // Replace suffix at end of name with canonical form
            foreach ($suffixMap as $short => $long) {
                $shortClean = str_replace([',', '.'], '', $short);
                if (str_ends_with($key, ' ' . $shortClean)) {
                    $key = substr($key, 0, -strlen($shortClean)) . $long;
                    break;
                }
                if (str_ends_with($key, ' ' . str_replace([',', '.'], '', $long))) {
                    break; // already canonical
                }
            }

            if (!$key) continue;
            // Keep the longer original form
            if (!isset($normalized[$key]) || strlen($n) > strlen($normalized[$key])) {
                $normalized[$key] = $n;
            }
        }
        return array_values($normalized);
    }

    private function searchRegistries(array $entityInfo, string $domain): array
    {
        $uniqueNames = $entityInfo['entity_names'] ?? [];
        $uniqueNames = array_slice($uniqueNames, 0, $this->config['max_entity_names']);
        $shortNames = $entityInfo['short_names'] ?? [];
        $jurisdiction = strtolower($entityInfo['jurisdiction'] ?? 'unknown');
        $registries = [];

        // Build list of jurisdictions to search
        $jurisdictions = [$jurisdiction];
        if (!empty($entityInfo['known_jurisdiction'])) {
            $knownJur = strtolower($entityInfo['known_jurisdiction']);
            if ($knownJur !== $jurisdiction) {
                $jurisdictions[] = $knownJur;
            }
        }

        $isUS = array_intersect($jurisdictions, ['us', 'united states', 'delaware', 'new york', 'california']);
        $isUK = array_intersect($jurisdictions, ['uk', 'england', 'scotland', 'wales', 'united kingdom']);
        $isEU = array_intersect($jurisdictions, ['germany', 'france', 'netherlands', 'austria', 'switzerland', 'europe', 'finland', 'denmark', 'sweden', 'norway', 'poland', 'czech republic', 'belgium', 'luxembourg', 'italy', 'spain', 'ireland']);
        $isUnknown = in_array('unknown', $jurisdictions);

        // ── Declare search plan ──
        $registrySources = [];
        if ($isUK || $isUnknown) $registrySources[] = 'Companies House';
        if ($isUS || $isUnknown) $registrySources[] = 'SEC EDGAR';
        if ($isUS || $isUnknown) $registrySources[] = 'SEC IAPD';
        if ($isUS || $isUnknown) $registrySources[] = 'Bizapedia';
        if ($isUS || $isUnknown) $registrySources[] = 'Delaware Div. of Corps.';
        if ($isEU || $isUK || $isUnknown) $registrySources[] = 'North Data';
        if ($isUS || $isUnknown) $registrySources[] = 'EDGAR Exhibit 21';

        $this->log('registry', "Search plan: " . count($uniqueNames) . " entity name(s) + " . count($shortNames) . " short name(s) × " . count($registrySources) . " registries", [
            'names' => $uniqueNames,
        ]);
        $this->log('registry', "  Jurisdictions: " . implode(', ', $jurisdictions));
        $this->log('registry', "  Entity names: " . json_encode($uniqueNames));
        if ($shortNames) {
            $this->log('registry', "  Short names: " . json_encode($shortNames));
        }
        $this->log('registry', "  Registries: " . implode(', ', $registrySources));

        // ── Build combined search list: entity names + short names (deduplicated) ──
        $allSearchNames = $uniqueNames;
        $shortNameSet = []; // track which are short names (for trademark search)
        foreach ($shortNames as $sn) {
            $snNorm = strtolower(trim($sn));
            $isDuplicate = false;
            foreach ($allSearchNames as $existing) {
                if (strtolower(trim($existing)) === $snNorm) {
                    $isDuplicate = true;
                    break;
                }
            }
            if (!$isDuplicate) {
                $allSearchNames[] = $sn;
            }
            $shortNameSet[$snNorm] = true;
        }

        if (count($allSearchNames) > count($uniqueNames)) {
            $this->log('registry', "  Combined search list (" . count($allSearchNames) . " names including short names): " . json_encode($allSearchNames));
        }

        // ── Search entity by entity ──
        $northdataNetworkDone = false;

        foreach ($allSearchNames as $idx => $name) {
            $num = $idx + 1;
            $total = count($allSearchNames);
            $isShortName = isset($shortNameSet[strtolower(trim($name))]);
            $label = $isShortName ? "{$name} (short name)" : $name;
            $this->log('entity_header', $label, ['entity_num' => $num, 'entity_total' => $total]);

            // Companies House (UK)
            if ($isUK || $isUnknown) {
                $registries["companies_house:{$name}"] = $this->tools->searchCompaniesHouse($name);
                $this->logRegistryResult('ch', 'Companies House', $name, $registries["companies_house:{$name}"], $name);

                // Trace ownership chain from first CH result with a company number
                if (!isset($registries['ownership_chain']) &&
                    str_contains($registries["companies_house:{$name}"], 'find-and-update.company-information.service.gov.uk/company/')) {
                    if (preg_match('/\/company\/(\w+)/', $registries["companies_house:{$name}"], $m)) {
                        $this->log('ch', "Tracing ownership chain from #{$m[1]}...", ['entity_name' => $name]);
                        $registries['ownership_chain'] = $this->tools->companiesHouseOwnershipChain($m[1]);
                        $chainLines = substr_count($registries['ownership_chain'], "\n") + 1;
                        $this->log('ch', "Ownership chain complete ({$chainLines} lines)", [
                            'entity_name' => $name,
                            'expandable' => true,
                            'sections' => [['label' => 'Full Chain', 'content' => $registries['ownership_chain']]],
                        ]);
                    }
                }

                // Corporate appointments — find companies where this entity is a corporate director
                if (!isset($registries['ch_corporate_appointments'])) {
                    $this->log('ch', "Looking up corporate appointments for \"{$name}\"...", ['entity_name' => $name]);
                    $appointments = $this->tools->companiesHouseCorporateAppointments($name);
                    if (!empty($appointments)) {
                        $active = array_filter($appointments, fn($a) => $a['status'] === 'active' && strtolower($a['company_status'] ?? '') !== 'dissolved');
                        $resigned = array_filter($appointments, fn($a) => $a['status'] !== 'active' || strtolower($a['company_status'] ?? '') === 'dissolved');
                        $lines = ["Companies where \"{$name}\" serves as a corporate officer (i.e. the company itself is appointed as a director/secretary of other companies):"];
                        $lines[] = "";
                        foreach ($appointments as $appt) {
                            $status = $appt['status'] ?? 'unknown';
                            $companyStatus = $appt['company_status'] ?? 'unknown';
                            $lines[] = "- {$appt['company_name']} (#{$appt['company_number']}) — role: {$appt['role']}, appointment: {$status}, company status: {$companyStatus}";
                        }
                        $registries['ch_corporate_appointments'] = implode("\n", $lines);
                        // Build a chatty summary for the log
                        $activeNames = array_map(fn($a) => $a['company_name'], array_values($active));
                        $summary = "\"{$name}\" is a corporate officer of " . count($appointments) . " companies";
                        if (count($active) > 0) {
                            $summary .= " (" . count($active) . " active";
                            if (count($resigned) > 0) $summary .= ", " . count($resigned) . " resigned/dissolved";
                            $summary .= ")";
                        }
                        if (count($activeNames) <= 5) {
                            $summary .= ": " . implode(', ', $activeNames);
                        } else {
                            $summary .= ": " . implode(', ', array_slice($activeNames, 0, 4)) . " + " . (count($activeNames) - 4) . " more";
                        }
                        $this->log('ch', $summary, ['entity_name' => $name, 'expandable' => true, 'sections' => [['label' => 'All Appointments', 'content' => $registries['ch_corporate_appointments']]]]);
                    } else {
                        $this->log('ch', "No corporate appointments found — \"{$name}\" is not a director of other companies", ['entity_name' => $name]);
                    }
                }
            }

            // SEC EDGAR company search (US)
            if ($isUS || $isUnknown) {
                $registries["sec_company:{$name}"] = $this->tools->searchSecCompany($name);
                $this->logRegistryResult('sec', 'SEC Company', $name, $registries["sec_company:{$name}"], $name);
            }

            // SEC IAPD (US)
            if ($isUS || $isUnknown) {
                $iapdResult = $this->tools->searchSecIapd($name);
                $registries["sec_iapd:{$name}"] = $iapdResult;
                $this->logRegistryResult('sec_iapd', 'SEC IAPD', $name, $iapdResult, $name);
            }

            // Bizapedia (US)
            if ($isUS || $isUnknown) {
                $bizResults = $this->tools->searchBizapedia($name);
                if (!empty($bizResults)) {
                    LookupTools::sortBizapediaResults($bizResults);
                    $bizJson = json_encode($bizResults, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);
                    $registries["bizapedia:{$name}"] = substr($bizJson, 0, 5000);
                }
                $this->logRegistryResult('bizapedia', 'Bizapedia', $name, count($bizResults) . ' results', $name);

                // Delaware Division of Corporations
                $delawareResult = $this->tools->searchDelaware($name);
                $registries["delaware:{$name}"] = $delawareResult;
                $this->logRegistryResult('delaware', 'Delaware', $name, $delawareResult, $name);
            }

            // North Data (EU + UK — provides financial data for UK companies)
            if ($isEU || $isUK || $isUnknown) {
                $registries["northdata:{$name}"] = $this->tools->searchNorthdata($name);
                $this->logRegistryResult('northdata', 'North Data', $name, $registries["northdata:{$name}"], $name);

                // Follow up on first NorthData result to get ownership graph
                if (!$northdataNetworkDone && !str_contains($registries["northdata:{$name}"], 'No North Data results')
                    && !str_starts_with($registries["northdata:{$name}"], 'Error:')) {
                    // Extract URL — from search results (→ url) or reconstruct from direct page
                    $ndUrl = null;
                    if (preg_match('/→ (https:\/\/www\.northdata\.com\/[^\s]+)/', $registries["northdata:{$name}"], $urlMatch)) {
                        $ndUrl = $urlMatch[1];
                    } elseif (str_contains($registries["northdata:{$name}"], '=== NorthData Company Page:')) {
                        $ndUrl = "https://www.northdata.com/" . urlencode($name);
                    }
                    if ($ndUrl) {
                        $this->log('northdata', "Loading ownership graph via Browserbase...", ['entity_name' => $name]);
                        $networkResult = $this->tools->northdataNetwork($ndUrl);
                        $registries['northdata_network'] = $networkResult;
                        $northdataNetworkDone = true;

                        $ndSummary = "Network graph loaded";
                        if (str_contains($networkResult, 'OWNED BY')) {
                            $ndSummary = "Ownership structure found — parent entities identified";
                        } elseif (str_contains($networkResult, 'ultimate parent/TopCo')) {
                            $ndSummary = "Entity appears to be TopCo — no parent above it";
                        }
                        $this->log('northdata', "Network: {$ndSummary}", [
                            'entity_name' => $name,
                            'expandable' => true,
                            'sections' => [['label' => 'Full Network', 'content' => $networkResult]],
                        ]);
                    }
                }
            }

            // EDGAR Exhibit 21 parent search (US)
            if ($isUS || $isUnknown) {
                $edgarResult = $this->tools->edgarParentSearch($name);
                $registries["edgar_parent:{$name}"] = $edgarResult;

                $edgarSummary = "No parent found";
                if (str_contains($edgarResult, 'EDGAR PARENT FOUND')) {
                    $parent = '';
                    if (preg_match('/Parent:\s*(.+)/', $edgarResult, $pm)) $parent = trim($pm[1]);
                    $confirmed = str_contains($edgarResult, 'STRONG evidence');
                    $edgarSummary = "subsidiary of {$parent}" . ($confirmed ? " (confirmed)" : "");
                } elseif (str_contains($edgarResult, 'EDGAR MENTION ONLY')) {
                    $filer = '';
                    if (preg_match('/filings of:\s*(.+)/', $edgarResult, $pm)) $filer = trim($pm[1]);
                    $edgarSummary = "mentioned in filings by {$filer} (weak)";
                }
                $this->log('edgar', "Exhibit 21: \"{$name}\" → {$edgarSummary}", [
                    'entity_name' => $name,
                    'expandable' => true,
                    'sections' => [['label' => 'Full Result', 'content' => $edgarResult]],
                ]);
            }
        }

        // ── Trademark search for short_names (US only, additional to registry searches) ──
        if (($isUS || $isUnknown) && !empty($shortNames)) {
            $this->log('entity_header', 'Trademark Searches', ['entity_num' => 'TM', 'entity_total' => count($shortNames) . ' names']);
            foreach ($shortNames as $sn) {
                $tmResult = $this->tools->searchBizapediaTrademark($sn);
                $registries["trademark:{$sn}"] = $tmResult;
                $this->logRegistryResult('bizapedia', 'Bizapedia TM', $sn, $tmResult, $sn);
            }
        }

        // ── Brand name search on Companies House (UK only) ──
        // Search for short/brand names, filter by shared postcode or director with known entities
        if (($isUK || $isUnknown) && !empty($shortNames)) {
            // Collect known company numbers from CH results
            $knownCompanyNumbers = [];
            foreach ($registries as $key => $val) {
                if (preg_match_all('/\/company\/([A-Z0-9]+)/', $val, $m)) {
                    $knownCompanyNumbers = array_merge($knownCompanyNumbers, $m[1]);
                }
            }
            $knownCompanyNumbers = array_unique($knownCompanyNumbers);

            // Fetch address + officers for each known company via CH API
            $knownPostcodes = [];
            $knownOfficers = [];
            $this->log('ch', "Collecting addresses and officers from " . count($knownCompanyNumbers) . " known CH companies for brand search cross-reference...");
            foreach (array_slice($knownCompanyNumbers, 0, 5) as $num) {
                $co = $this->tools->companiesHouseGetCompany($num);
                if ($co && $co['postal_code']) {
                    $knownPostcodes[] = $co['postal_code'];
                    $this->log('ch', "  #{$num} ({$co['company_name']}): {$co['address']}");
                }
                $officers = $this->tools->companiesHouseGetOfficers($num);
                foreach ($officers as $oName) {
                    $surname = trim(explode(',', $oName)[0]);
                    if ($surname) $knownOfficers[] = $surname;
                }
                if (!empty($officers)) {
                    $this->log('ch', "  #{$num} officers: " . implode(', ', array_slice($officers, 0, 5)));
                }
            }
            $knownPostcodes = array_unique($knownPostcodes);
            $knownOfficers = array_unique($knownOfficers);

            if (!empty($knownPostcodes) || !empty($knownOfficers)) {
                foreach ($shortNames as $sn) {
                    if (isset($registries["ch_brand_search:{$sn}"])) continue;
                    $this->log('ch', "Brand search: \"{$sn}\" — matching against " . count($knownPostcodes) . " postcodes, " . count($knownOfficers) . " officer surnames");
                    $matches = $this->tools->companiesHouseBrandSearch($sn, $knownPostcodes, $knownOfficers, $knownCompanyNumbers);
                    if (!empty($matches)) {
                        $lines = ["Companies House companies matching brand name \"{$sn}\" that share an address or director with known entities:"];
                        $lines[] = "";
                        foreach ($matches as $match) {
                            $lines[] = "- {$match['company_name']} (#{$match['company_number']}) — status: {$match['company_status']}, address: {$match['address']}, matched by: {$match['match_reason']}";
                        }
                        $registries["ch_brand_search:{$sn}"] = implode("\n", $lines);
                        $matchNames = array_map(fn($m) => $m['company_name'], $matches);
                        $summary = count($matches) . " related \"$sn\" companies found: " . (count($matchNames) <= 5
                            ? implode(', ', $matchNames)
                            : implode(', ', array_slice($matchNames, 0, 4)) . " + " . (count($matchNames) - 4) . " more");
                        $this->log('ch', $summary, ['expandable' => true, 'sections' => [['label' => 'All Matches', 'content' => $registries["ch_brand_search:{$sn}"]]]]);
                    } else {
                        $this->log('ch', "Brand search: no related companies found for \"{$sn}\"");
                    }
                }
            } else {
                $this->log('ch', "Brand search skipped — no known postcodes or officers to cross-reference");
            }
        }

        // ── Domain-level SEC searches (US only) ──
        if ($isUS || $isUnknown) {
            $this->log('entity_header', "SEC Domain & Filing Searches", ['entity_num' => 'SEC', 'entity_total' => $domain]);

            // SEC fulltext search by domain
            $registries["sec_fulltext:{$domain}"] = $this->tools->searchSecFulltext($domain);
            $this->logRegistryResult('sec', 'SEC Fulltext', $domain, $registries["sec_fulltext:{$domain}"]);

            // Fetch SEC submissions for any CIKs found across all results
            $ciks = [];
            foreach ($registries as $key => $val) {
                if (preg_match_all('/CIK:\s*(\d+)/', $val, $m)) {
                    $ciks = array_merge($ciks, $m[1]);
                }
                if (str_contains($key, 'sec_fulltext') && preg_match_all('/CIK\s*(\d+)/', $val, $m)) {
                    $ciks = array_merge($ciks, $m[1]);
                }
            }
            $ciks = array_unique($ciks);
            $ciks = array_slice($ciks, 0, $this->config['max_ciks']);

            if ($ciks) {
                $this->log('sec', "Fetching submissions for " . count($ciks) . " CIK(s): " . implode(', ', $ciks));
            }

            foreach ($ciks as $cik) {
                $submissions = $this->tools->fetchSecSubmissions($cik);
                $registries["sec_submissions:{$cik}"] = $submissions;
                $subData = json_decode($submissions, true);
                $entityName = $subData['name'] ?? 'unknown';
                $this->log('sec', "CIK {$cik} → {$entityName} ({$subData['total_filings']} filings)", [
                    'expandable' => true,
                    'sections' => [['label' => 'Full Submissions', 'content' => $submissions]],
                ]);

                if ($subData && isset($subData['latest_filings'])) {
                    // Fetch 8-K cover page for structured entity data
                    $eightK = $this->tools->fetchSec8K($cik, $subData);
                    if ($eightK) {
                        $registries["sec_8k:{$cik}"] = json_encode($eightK, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES);
                        $regName = $eightK['registered_name'] ?? 'unknown';
                        $state = $eightK['state_of_incorporation'] ?? '';
                        $ein = $eightK['irs_ein'] ?? '';
                        $this->log('sec', "8-K: {$regName} — incorporated in {$state}, EIN {$ein}", [
                            'expandable' => true,
                            'sections' => [['label' => '8-K Cover Page', 'content' => $registries["sec_8k:{$cik}"]]],
                        ]);
                    }

                    // Look for Form D filings
                    foreach ($subData['latest_filings'] as $filing) {
                        if (($filing['form'] ?? '') === 'D' && !empty($filing['primaryDocument'])) {
                            $accession = str_replace('-', '', $filing['accession']);
                            $doc = $filing['primaryDocument'];
                            $filingUrl = "https://www.sec.gov/Archives/edgar/data/{$cik}/{$accession}/{$doc}";
                            $registries["sec_filing:{$cik}:FormD"] = $this->tools->fetchSecFiling($filingUrl);
                            $this->log('sec', "Form D: CIK {$cik}", [
                                'expandable' => true,
                                'sections' => [['label' => 'Filing Content', 'content' => $registries["sec_filing:{$cik}:FormD"]]],
                            ]);
                            break;
                        }
                    }
                }

                // Fetch XBRL financial data (revenue, assets, etc.)
                $financials = $this->tools->secEdgarFinancials($cik);
                if ($financials) {
                    $registries["sec_financials:{$cik}"] = $financials;
                    $lineCount = substr_count($financials, "\n");
                    $this->log('sec', "XBRL Financials: CIK {$cik} ({$lineCount} lines)", [
                        'expandable' => true,
                        'sections' => [['label' => 'Financial Data', 'content' => $financials]],
                    ]);
                }
            }
        }

        return $registries;
    }

    /**
     * Phase 4: Run regex over all registry results to discover new entity names
     * that weren't in the original search, then search those in registries.
     */
    private function discoverAndSearchNewEntities(array $registries, array $entityInfo, string $domain): array
    {
        // Collect all text from registry results
        $registryText = implode("\n", array_values($registries));

        // Run regex extraction on registry text (higher limit to catch all candidates)
        $newCandidates = $this->extractCandidateNames($registryText, 20);


        // Build set of already-searched names (normalised for comparison)
        $alreadySearched = array_merge(
            $entityInfo['entity_names'] ?? [],
            $entityInfo['short_names'] ?? []
        );
        $alreadyNorm = array_map(fn($n) => strtolower(trim($n, ' .')), $alreadySearched);

        // Filter to genuinely new names
        $newNames = [];
        foreach ($newCandidates as $candidate) {
            $norm = strtolower(trim($candidate, ' .'));
            // Skip if already searched or is a substring/superstring of an existing name
            $dominated = false;
            foreach ($alreadyNorm as $existing) {
                if ($norm === $existing || str_contains($existing, $norm) || str_contains($norm, $existing)) {
                    $dominated = true;
                    break;
                }
            }
            if (!$dominated && !in_array($norm, array_map(fn($n) => strtolower(trim($n, ' .')), $newNames))) {
                $newNames[] = $candidate;
            }
        }

        // Deduplicate the new names
        $newNames = $this->deduplicateNames($newNames);

        // Cap to avoid excessive searches
        $newNames = array_slice($newNames, 0, 5);

        if (empty($newNames)) {
            $this->log('registry', "No new entity names discovered from registry data");
            return $registries;
        }

        $this->log('registry', "Discovered " . count($newNames) . " new entity names from registry data: " . json_encode($newNames));

        // Determine jurisdiction flags (same logic as searchRegistries)
        $jurisdiction = strtolower($entityInfo['jurisdiction'] ?? 'unknown');
        $jurisdictions = [$jurisdiction];
        if (!empty($entityInfo['known_jurisdiction'])) {
            $knownJur = strtolower($entityInfo['known_jurisdiction']);
            if ($knownJur !== $jurisdiction) $jurisdictions[] = $knownJur;
        }
        $isUS = array_intersect($jurisdictions, ['us', 'united states', 'delaware', 'new york', 'california']);
        $isUK = array_intersect($jurisdictions, ['uk', 'england', 'scotland', 'wales', 'united kingdom']);
        $isEU = array_intersect($jurisdictions, ['germany', 'france', 'netherlands', 'austria', 'switzerland', 'europe', 'finland', 'denmark', 'sweden', 'norway', 'poland', 'czech republic', 'belgium', 'luxembourg', 'italy', 'spain', 'ireland']);
        $isUnknown = in_array('unknown', $jurisdictions);

        $bizapediaAll = [];

        foreach ($newNames as $name) {
            $this->log('registry', "Searching new entity: \"{$name}\"");

            if ($isUK || $isUnknown) {
                $registries["companies_house:{$name}"] = $this->tools->searchCompaniesHouse($name);
                $this->logRegistryResult('ch', 'Companies House', $name, $registries["companies_house:{$name}"]);
            }
            if ($isUS || $isUnknown) {
                $registries["sec_company:{$name}"] = $this->tools->searchSecCompany($name);
                $this->logRegistryResult('sec', 'SEC Company', $name, $registries["sec_company:{$name}"]);

                $iapdResult = $this->tools->searchSecIapd($name);
                $registries["sec_iapd:{$name}"] = $iapdResult;
                $this->logRegistryResult('sec_iapd', 'SEC IAPD', $name, $iapdResult);

                $bizResults = $this->tools->searchBizapedia($name);
                $bizapediaAll = array_merge($bizapediaAll, $bizResults);
                $this->logRegistryResult('bizapedia', 'Bizapedia', $name, count($bizResults) . ' results');

                if (!isset($registries["delaware:{$name}"])) {
                    $delawareResult = $this->tools->searchDelaware($name);
                    $registries["delaware:{$name}"] = $delawareResult;
                    $this->logRegistryResult('delaware', 'Delaware', $name, $delawareResult);
                }
            }
            if ($isEU || $isUnknown) {
                $registries["northdata:{$name}"] = $this->tools->searchNorthdata($name);
                $this->logRegistryResult('northdata', 'North Data', $name, $registries["northdata:{$name}"]);
            }
        }

        // Merge new Bizapedia results into the existing deduplicated set
        if (!empty($bizapediaAll)) {
            $existingBizapedia = [];
            if (isset($registries['bizapedia'])) {
                $existingBizapedia = json_decode($registries['bizapedia'], true) ?? [];
            }
            // Re-deduplicate with both old and new raw results combined
            // We need the raw results for dedup, so convert existing compact records aren't raw —
            // just append new raw and re-dedup the whole thing
            $allRaw = $bizapediaAll;
            // Add existing file numbers to seen set via the dedup function
            $newDeduped = LookupTools::deduplicateBizapediaResults($allRaw);
            $newParsed = json_decode($newDeduped, true) ?? [];

            // Merge: existing + new (skip duplicates by file_number+jurisdiction_code)
            $seen = [];
            foreach ($existingBizapedia as $e) {
                $key = ($e['jurisdiction_code'] ?? '') . ':' . ($e['file_number'] ?? '');
                $seen[$key] = true;
            }
            foreach ($newParsed as $n) {
                $key = ($n['jurisdiction_code'] ?? '') . ':' . ($n['file_number'] ?? '');
                if (!isset($seen[$key])) {
                    $existingBizapedia[] = $n;
                    $seen[$key] = true;
                }
            }

            $registries['bizapedia'] = json_encode($existingBizapedia, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);
            $this->log('registry', "Bizapedia updated: now " . count($existingBizapedia) . " unique entities (+" . count($newParsed) . " from discovery)", [
                'expandable' => true,
                'sections' => [['label' => 'Bizapedia Entities', 'content' => $registries['bizapedia']]],
            ]);
        }

        return $registries;
    }

    // ── Entity name normalisation ──────────────────────────────────────────

    private static array $abbreviations = [
        // Legal structure — English
        'inc' => 'incorporated', 'corp' => 'corporation', 'ltd' => 'limited',
        'co' => 'company', 'plc' => 'public limited company',
        'llc' => 'limited liability company', 'l.l.c.' => 'limited liability company',
        'llp' => 'limited liability partnership', 'l.l.p.' => 'limited liability partnership',
        'lp' => 'limited partnership', 'l.p.' => 'limited partnership',
        'pty' => 'proprietary',
        // Legal structure — German
        'ag' => 'aktiengesellschaft',
        'gmbh' => 'gesellschaft mit beschränkter haftung',
        'kg' => 'kommanditgesellschaft',
        'ohg' => 'offene handelsgesellschaft',
        'eg' => 'eingetragene genossenschaft',
        'se' => 'societas europaea',
        // Legal structure — French
        'sa' => 'société anonyme',
        'sas' => 'société par actions simplifiée',
        'sarl' => 'société à responsabilité limitée',
        // Legal structure — Dutch
        'bv' => 'besloten vennootschap',
        'nv' => 'naamloze vennootschap',
        // Legal structure — Nordic
        'ab' => 'aktiebolag',
        'as' => 'aksjeselskap',
        'aps' => 'anpartsselskab',
        'oy' => 'osakeyhtiö',
        // Legal structure — Italian/Spanish
        'srl' => 'società a responsabilità limitata',
        'spa' => 'società per azioni',
        'sl' => 'sociedad limitada',
        // Business terms
        'assoc' => 'association', 'assn' => 'association',
        'bros' => 'brothers',
        'intl' => 'international', "int'l" => 'international',
        'natl' => 'national', "nat'l" => 'national',
        'mgmt' => 'management', 'mgt' => 'management',
        'svcs' => 'services', 'svc' => 'service',
        'grp' => 'group',
        'hldgs' => 'holdings', 'hldg' => 'holding',
        'mfg' => 'manufacturing',
        'dept' => 'department',
        'dist' => 'distribution',
        'tech' => 'technology',
        'fin' => 'financial',
        'dev' => 'development',
        'invt' => 'investment', 'inv' => 'investment',
        'props' => 'properties', 'prop' => 'property',
        'sys' => 'systems',
        'indus' => 'industries', 'ind' => 'industries',
        'engr' => 'engineering', 'eng' => 'engineering',
        'pharm' => 'pharmaceutical', 'pharma' => 'pharmaceutical',
        'chem' => 'chemical',
        'elec' => 'electric', 'electr' => 'electronic',
        'telecom' => 'telecommunications',
        'transp' => 'transportation',
        'ins' => 'insurance',
        'bancorp' => 'banking corporation',
        'mtg' => 'mortgage',
        'realty' => 'realty', 'rlty' => 'realty',
    ];

    /**
     * Generate all normalised variants of an entity name.
     * Applies: punctuation removal, & ↔ and, abbreviation expansion/contraction.
     */
    private function normaliseEntityName(string $name): array
    {
        // Build reverse map (long → short)
        static $reverse = null;
        if ($reverse === null) {
            $reverse = [];
            foreach (self::$abbreviations as $short => $long) {
                $reverse[$long] = $short;
            }
        }

        $base = strtolower(trim($name));
        // Remove periods and extra commas/spaces
        $clean = str_replace('.', '', $base);
        $clean = preg_replace('/\s*,\s*/', ' ', $clean);
        $clean = preg_replace('/\s+/', ' ', trim($clean));

        $variants = [$base, $clean];

        // & ↔ and
        if (str_contains($clean, ' & ')) {
            $variants[] = str_replace(' & ', ' and ', $clean);
        } elseif (str_contains($clean, ' and ')) {
            $variants[] = str_replace(' and ', ' & ', $clean);
        }

        // For each variant so far, expand abbreviations and contract full words
        $expanded = $variants;
        foreach ($expanded as $v) {
            $words = explode(' ', $v);
            $changed = false;

            // Try expanding (short → long)
            $expWords = $words;
            foreach ($expWords as &$w) {
                if (isset(self::$abbreviations[$w])) {
                    $w = self::$abbreviations[$w];
                    $changed = true;
                }
            }
            unset($w);
            if ($changed) $variants[] = implode(' ', $expWords);

            // Try contracting (long → short)
            $conWords = $words;
            $changed = false;
            foreach ($conWords as &$w) {
                if (isset($reverse[$w])) {
                    $w = $reverse[$w];
                    $changed = true;
                }
            }
            unset($w);
            if ($changed) $variants[] = implode(' ', $conWords);
        }

        return array_unique($variants);
    }

    /**
     * Check if any normalised variant of the entity name appears on the website.
     * Returns the matched variant or null.
     */
    private function matchEntityName(string $entityName, string $websiteText): ?string
    {
        $variants = $this->normaliseEntityName($entityName);
        foreach ($variants as $variant) {
            if (strlen($variant) > 3 && str_contains($websiteText, $variant)) {
                return $variant;
            }
        }
        return null;
    }

    // ── Cross-reference SEC data against website ────────────────────────────

    private function crossReferenceSecData(array $websiteData, array $registries, array $entityInfo): string
    {
        // Collect all website text for matching
        $websiteText = strtolower(implode("\n", $websiteData['pages']));
        if (strlen($websiteText) < 100) {
            $this->log('crossref', "Skipping cross-reference — website text too short (" . strlen($websiteText) . " chars)");
            return '';
        }

        // Website addresses from LLM extraction (must be real street addresses)
        $rawAddresses = $entityInfo['addresses'] ?? $entityInfo['address'] ?? [];
        if (is_string($rawAddresses)) $rawAddresses = $rawAddresses ? [$rawAddresses] : [];
        $websiteAddresses = array_filter(array_map('trim', $rawAddresses));

        $this->log('crossref', "Website addresses from LLM: " . ($websiteAddresses ? json_encode($websiteAddresses) : "(none extracted)"));

        // Count how many SEC submissions we have to work with
        $secKeys = array_filter(array_keys($registries), fn($k) => str_starts_with($k, 'sec_submissions:'));
        $this->log('crossref', "Found " . count($secKeys) . " SEC submission record(s) to cross-reference: " . implode(', ', $secKeys));

        $matches = [];

        // Extract SEC submission data for cross-referencing
        foreach ($registries as $key => $val) {
            if (!str_starts_with($key, 'sec_submissions:')) continue;
            $sub = json_decode($val, true);
            if (!$sub || empty($sub['name'])) {
                $this->log('crossref', "  {$key}: skipped — no parseable data");
                continue;
            }

            $cik = $sub['cik'] ?? '';
            $entityName = $sub['name'] ?? '';
            $edgarUrl = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={$cik}";

            $this->log('crossref', "  Checking SEC entity: \"{$entityName}\" (CIK {$cik})");

            // 1. Entity name on website
            $nameMatch = $this->matchEntityName($entityName, $websiteText);
            if ($nameMatch) {
                $label = $nameMatch === $entityName ? '' : " (normalised from \"{$entityName}\")";
                $matches[] = "ENTITY NAME MATCH: \"{$nameMatch}\"{$label} found on website | {$edgarUrl}";
                $this->log('crossref', "    ✓ Name match: \"{$nameMatch}\" found on website");
            } else {
                $this->log('crossref', "    ✗ Name \"{$entityName}\" not found on website");
            }

            // 2. Address matching
            $bizAddr = $sub['addresses']['business'] ?? [];
            $secCity = $bizAddr['city'] ?? '';
            $secState = $bizAddr['stateOrCountryDescription'] ?? $bizAddr['stateOrCountry'] ?? '';
            $secStreet = trim(($bizAddr['street1'] ?? '') . ' ' . ($bizAddr['street2'] ?? ''));
            $secZip = $bizAddr['zipCode'] ?? '';
            $secAddrFull = implode(', ', array_filter([$secStreet, $secCity, $secState, $secZip]));

            $this->log('crossref', "    SEC address: \"{$secAddrFull}\"");

            if ($websiteAddresses) {
                // Extract street name words from SEC address (skip house number)
                $secStreetClean = strtolower(preg_replace('/[.,]/', '', $secStreet));
                $streetWords = preg_split('/\s+/', $secStreetClean);
                $streetName = '';
                foreach ($streetWords as $i => $w) {
                    if ($i === 0 && preg_match('/^\d/', $w)) continue;
                    if (in_array($w, ['c/o', 'suite', 'ste', 'floor', 'flr', 'box', 'apt', 'unit'])) break;
                    if (strlen($w) > 2) { $streetName .= ($streetName ? ' ' : '') . $w; }
                }
                $this->log('crossref', "    Extracted street name: \"{$streetName}\" — comparing against " . count($websiteAddresses) . " website address(es)");

                $matched = false;
                foreach ($websiteAddresses as $wa) {
                    $waClean = strtolower(preg_replace('/[.,]/', '', $wa));

                    if ($streetName && strlen($streetName) > 4 && str_contains($waClean, $streetName)) {
                        $matches[] = "ADDRESS MATCH (street): \"{$secStreet}\" matches website address \"{$wa}\" | SEC: {$secAddrFull} | {$edgarUrl}";
                        $this->log('crossref', "    ✓ Street match: \"{$streetName}\" found in website address \"{$wa}\"");
                        $matched = true; break;
                    } elseif ($secZip && str_contains($waClean, strtolower($secZip))) {
                        $matches[] = "ADDRESS MATCH (zip): ZIP \"{$secZip}\" matches website address \"{$wa}\" | SEC: {$secAddrFull} | {$edgarUrl}";
                        $this->log('crossref', "    ✓ ZIP match: \"{$secZip}\" found in website address \"{$wa}\"");
                        $matched = true; break;
                    }
                }
                if (!$matched && $secCity) {
                    $genericCities = ['new york', 'london', 'los angeles', 'chicago', 'houston', 'phoenix', 'san antonio', 'san diego', 'dallas', 'austin'];
                    if (in_array(strtolower($secCity), $genericCities)) {
                        $this->log('crossref', "    ✗ City \"{$secCity}\" is too generic for city-only match");
                    } else {
                        $cityMatched = false;
                        foreach ($websiteAddresses as $wa) {
                            $waClean = strtolower(preg_replace('/[.,]/', '', $wa));
                            if (str_contains($waClean, strtolower($secCity))) {
                                $matches[] = "ADDRESS MATCH (city): City \"{$secCity}\" in website address \"{$wa}\" | SEC: {$secAddrFull} | {$edgarUrl}";
                                $this->log('crossref', "    ✓ City match: \"{$secCity}\" found in website address \"{$wa}\"");
                                $cityMatched = true; break;
                            }
                        }
                        if (!$cityMatched) {
                            $this->log('crossref', "    ✗ No address match (street/zip/city)");
                        }
                    }
                } elseif (!$matched) {
                    $this->log('crossref', "    ✗ No address match (street/zip)");
                }
            } else {
                $this->log('crossref', "    — No website addresses to compare against");
            }

            // 3. Phone matching
            $secPhone = $sub['phone'] ?? '';
            if ($secPhone) {
                $phoneDigits = preg_replace('/\D/', '', $secPhone);
                if (strlen($phoneDigits) >= 10 && str_contains(preg_replace('/\D/', '', $websiteText), $phoneDigits)) {
                    $matches[] = "PHONE MATCH: {$secPhone} found on website | {$edgarUrl}";
                    $this->log('crossref', "    ✓ Phone match: {$secPhone}");
                } else {
                    $this->log('crossref', "    ✗ Phone \"{$secPhone}\" not found on website");
                }
            } else {
                $this->log('crossref', "    — No phone number in SEC data");
            }

            // 4. Related persons and GP entities from Form D
            $formDKey = "sec_filing:{$cik}:FormD";
            $formDContent = $registries[$formDKey] ?? '';
            if ($formDContent) {
                $formDLines = explode("\n", $formDContent);
                $personNames = [];
                $gpEntities = [];

                for ($i = 0; $i < count($formDLines); $i++) {
                    $line = trim($formDLines[$i]);

                    if ($line === 'Last Name' && isset($formDLines[$i + 2])) {
                        $lastName = '';
                        $firstName = '';
                        for ($j = $i + 1; $j < min($i + 8, count($formDLines)); $j++) {
                            $val = trim($formDLines[$j]);
                            if ($val === 'First Name' || $val === 'Middle Name' || $val === 'Last Name') continue;
                            if (!$lastName) { $lastName = $val; continue; }
                            if (!$firstName) { $firstName = $val; break; }
                        }
                        if ($firstName && $firstName !== 'n/a' && $lastName && $lastName !== 'n/a') {
                            $personNames[] = "{$firstName} {$lastName}";
                        } elseif ($lastName && $firstName === 'n/a') {
                            $gpEntities[] = $lastName;
                        }
                    }

                    if ($line === 'Name of Signer' && isset($formDLines[$i + 1])) {
                        $signer = trim($formDLines[$i + 1]);
                        if (preg_match('/^[A-Z][a-z]+\s+[A-Z][a-z]+$/', $signer)) {
                            $personNames[] = $signer;
                        }
                    }
                }

                $personNames = array_unique($personNames);
                $this->log('crossref', "    Form D persons: " . ($personNames ? json_encode(array_values($personNames)) : "(none)") . " | GP entities: " . ($gpEntities ? json_encode($gpEntities) : "(none)"));

                foreach ($personNames as $person) {
                    if (strlen($person) > 4 && stripos($websiteText, strtolower($person)) !== false) {
                        $matches[] = "PERSON MATCH: \"{$person}\" (Related Person in Form D) found on website | {$edgarUrl}";
                        $this->log('crossref', "    ✓ Person match: \"{$person}\" found on website");
                    } else {
                        $this->log('crossref', "    ✗ Person \"{$person}\" not found on website");
                    }
                }

                foreach ($gpEntities as $gp) {
                    $gpMatch = $this->matchEntityName($gp, $websiteText);
                    if ($gpMatch) {
                        $label = $gpMatch === strtolower($gp) ? '' : " (normalised from \"{$gp}\")";
                        $matches[] = "GP ENTITY MATCH: \"{$gpMatch}\"{$label} (General Partner in Form D) found on website | {$edgarUrl}";
                        $this->log('crossref', "    ✓ GP entity match: \"{$gp}\" found on website");
                    } else {
                        $this->log('crossref', "    ✗ GP entity \"{$gp}\" not found on website");
                    }
                }
            } else {
                $this->log('crossref', "    — No Form D filing found for CIK {$cik}");
            }
        }

        // Deduplicate
        $matches = array_unique($matches);

        if (!$matches) {
            $this->log('crossref', "No SEC-to-website cross-references found");
            return '';
        }

        $result = "=== SEC CROSS-REFERENCE EVIDENCE ===\n";
        if ($websiteAddresses) {
            $result .= "WEBSITE ADDRESSES (from LLM extraction): " . implode(' | ', $websiteAddresses) . "\n";
        }
        $result .= implode("\n", $matches);

        $this->log('crossref', count($matches) . " SEC-to-website cross-reference(s) found", [
            'expandable' => true,
            'sections' => [['label' => 'Cross-reference Detail', 'content' => $result]],
        ]);

        return $result;
    }

    // ── Phase 4: LLM Analysis ────────────────────────────────────────────────

    private function summarizeResult(string $result): string
    {
        $firstLine = strtok($result, "\n");
        if (strlen($firstLine) > 80) {
            $firstLine = substr($firstLine, 0, 80) . '...';
        }
        $lineCount = substr_count($result, "\n") + 1;
        return $lineCount > 1 ? "{$firstLine} (+{$lineCount} lines)" : $firstLine;
    }

    private function logRegistryResult(string $phase, string $source, string $name, string $result, ?string $entityName = null): void
    {
        $summary = $this->summarizeResult($result);
        $lineCount = substr_count($result, "\n") + 1;
        $detail = $lineCount > 1 ? [
            'expandable' => true,
            'sections' => [['label' => "Full Result ({$lineCount} lines)", 'content' => $result]],
        ] : [];
        if ($entityName) {
            $detail['entity_name'] = $entityName;
        }
        $this->log($phase, "{$source}: \"{$name}\" → {$summary}", $detail ?: null);
    }

    private function analyzeEvidence(string $url, string $domain, array $websiteData, array $entityInfo, array $registries): array
    {
        $evidenceText = $this->formatEvidence($url, $domain, $websiteData, $entityInfo, $registries);
        $systemPrompt = $this->analysisPrompt . "\n\n" . $this->jsonSchema;
        $userMessage = "Analyze the following evidence and produce the entity lookup report:\n\n{$evidenceText}";

        // Build expandable sections for the evidence
        $evidenceSections = [
            ['label' => 'System Prompt', 'content' => $systemPrompt],
            ['label' => 'Metadata', 'content' => "TARGET URL: {$url}\nDOMAIN: {$domain}\nCANDIDATE ENTITY NAMES: " . json_encode($entityInfo['entity_names'] ?? []) . "\nJURISDICTION: " . json_encode([$entityInfo['jurisdiction'] ?? 'unknown']) . "\nADDRESSES: " . json_encode($entityInfo['addresses'] ?? $entityInfo['address'] ?? [])],
            ['label' => 'WHOIS', 'content' => $this->scrubBlockedNames($websiteData['whois'] ?? 'Not available')],
        ];
        foreach ($websiteData['pages'] as $pageName => $text) {
            $truncated = substr($text, 0, 4000);
            $evidenceSections[] = ['label' => "Website: " . ucfirst($pageName) . " (" . number_format(strlen($truncated)) . " chars)", 'content' => $truncated];
        }
        foreach ($registries as $key => $result) {
            $truncated = substr($result, 0, 10000);
            $evidenceSections[] = ['label' => "Registry: {$key} (" . number_format(strlen($truncated)) . " chars)", 'content' => $truncated];
        }
        $this->log('llm', "LLM analysis — calling {$this->config['model']} with " . count($registries) . " registry results (" . number_format(strlen($evidenceText)) . " chars)", [
            'expandable' => true,
            'sections' => $evidenceSections,
        ]);

        $responseText = $this->callLLM($systemPrompt, $userMessage, 8192);

        $this->log('llm', "LLM analysis response (" . number_format(strlen($responseText)) . " chars)", [
            'expandable' => true,
            'sections' => [
                ['label' => 'Output', 'content' => $responseText],
            ],
        ]);

        $report = $this->parseJsonResponse($responseText, [
            'input_url' => $url,
            'date' => date('Y-m-d'),
            'report_id' => 'ERROR',
            'recommended_entity' => null,
            'confidence' => 'insufficient',
            'note' => 'LLM returned invalid JSON.',
            'evidence_forward' => [],
            'evidence_reverse' => [],
            'substance_score' => 0,
            'substance_band' => 'insufficient',
            'substance_factors' => [],
            'corporate_structure' => null,
            'key_people' => [],
            'other_entities' => [],
            'sources_used' => [],
        ]);

        return $report;
    }

    /**
     * Post-LLM validation: look up the recommended entity by registry ID
     * and verify the name matches exactly.
     */
    private function validateEntityInRegistry(array $report): array
    {
        $entity = $report['recommended_entity'] ?? null;
        if (!$entity || empty($entity['registry_id'])) return $report;

        $registryId = $entity['registry_id'];
        $country = strtoupper($entity['jurisdiction_country'] ?? '');
        $state = strtoupper($entity['jurisdiction_state'] ?? '');
        $llmName = $entity['legal_entity_name'] ?? '';

        $this->log('validate', "Validating \"{$llmName}\" — registry_id: {$registryId}, country: {$country}, state: {$state}");

        $registryName = null;
        $registryStatus = null;
        $registryData = null;
        $source = null;
        $validationUrl = null;

        // US → Bizapedia by file number + state
        if ($country === 'US' && $state) {
            $this->log('validate', "Looking up Bizapedia: file number {$registryId} in {$state}...");
            $biz = $this->tools->lookupBizapediaByFileNumber($registryId, $state);
            if ($biz) {
                $registryName = $biz['EntityName'] ?? null;
                $registryStatus = $biz['FilingStatus'] ?? null;
                $registryData = $biz;
                $source = 'Bizapedia';
                $validationUrl = '/validate.php?' . http_build_query(['entity_name' => $llmName, 'registry_id' => $registryId, 'country' => 'US', 'state' => $state]);
                $entityType = strtoupper($biz['EntityType'] ?? '');
                $domesticState = $biz['DomesticJurisdictionPostalAbbreviation'] ?? null;
                // Check for branch (Foreign) registration
                if (str_contains($entityType, 'FOREIGN') || str_contains($entityType, 'OUT OF STATE')) {
                    $registryStatus = "Branch ({$state}) — home: {$domesticState}";
                }
                // Check for fictitious name (trade name, not a legal entity)
                if (str_contains($entityType, 'FICTITIOUS')) {
                    $fictitiousOwner = null;
                    foreach ($biz['Principals'] ?? [] as $p) {
                        if (strtolower($p['Titles'] ?? '') === 'owner' && !empty($p['PrincipalName'])) {
                            $fictitiousOwner = $p['PrincipalName'];
                            break;
                        }
                    }
                    $ownerNote = $fictitiousOwner ? " (owner: {$fictitiousOwner})" : '';
                    $registryStatus = "Fictitious name{$ownerNote}";
                }
                $this->log('validate', "Bizapedia returned: \"{$registryName}\" (status: {$registryStatus})", [
                    'expandable' => true,
                    'sections' => [['label' => 'Full Bizapedia Record', 'content' => json_encode($biz, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES)]],
                ]);
            } else {
                $this->log('validate', "Bizapedia: no result for file number {$registryId} in {$state}");

                // Fallback: Delaware Division of Corporations for DE entities
                if ($state === 'DE') {
                    $this->log('validate', "Trying Delaware Div. of Corps: file number {$registryId}...");
                    $de = $this->tools->lookupDelawareByFileNumber($registryId);
                    if ($de) {
                        $registryName = $de['name'];
                        $deStatus = strtolower($de['status'] ?? '');
                        // Map Delaware statuses to our status format
                        $registryStatus = str_contains($deStatus, 'good standing') ? 'Active' : ($de['status'] ?? 'unknown');
                        $registryData = $de;
                        $source = 'Delaware Div. of Corps.';
                        $validationUrl = 'https://icis.corp.delaware.gov/ecorp/entitysearch/namesearch.aspx';
                        $this->log('validate', "Delaware returned: \"{$registryName}\" (status: {$registryStatus})", [
                            'expandable' => true,
                            'sections' => [['label' => 'Delaware Record', 'content' => json_encode($de, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES)]],
                        ]);
                    } else {
                        $this->log('validate', "Delaware: no result for file number {$registryId}");
                    }
                }
            }
        }

        // UK → Companies House by company number
        if ($country === 'GB') {
            $this->log('validate', "Looking up Companies House: company number {$registryId}...");
            $ch = $this->tools->lookupCompaniesHouseByNumber($registryId);
            if ($ch) {
                $registryName = $ch['company_name'] ?? null;
                $registryStatus = $ch['company_status'] ?? null;
                $registryData = $ch;
                $source = 'Companies House';
                $validationUrl = "https://find-and-update.company-information.service.gov.uk/company/{$registryId}";
                $this->log('validate', "Companies House returned: \"{$registryName}\" (status: {$registryStatus})", [
                    'expandable' => true,
                    'sections' => [['label' => 'Full CH Record', 'content' => json_encode($ch, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES)]],
                ]);
            } else {
                $this->log('validate', "Companies House: no result for company number {$registryId}");
            }
        }

        // Europe → NorthData: search by name, verify country + registry ID
        $northdataCountries = ['DE', 'NL', 'FR', 'AT', 'CH', 'BE', 'LU', 'IT', 'ES', 'DK', 'SE', 'NO', 'FI', 'PL', 'CZ', 'IE'];
        if (in_array($country, $northdataCountries) && !$registryName) {
            $this->log('validate', "Searching NorthData for \"{$llmName}\" (country: {$country}, registry ID: {$registryId})...");
            $nd = $this->tools->validateNorthdataEntity($llmName, $registryId, $country);
            if ($nd) {
                $fullNdName = preg_replace('/\s*\([^)]*\)\s*$/', '', $nd['name']); // strip (liq) etc
                $parts = array_map('trim', explode(',', $fullNdName));
                $registryName = count($parts) >= 3 ? implode(', ', array_slice($parts, 0, -2)) : $parts[0];
                $registryStatus = $nd['status'] ?? 'unknown';
                $registryData = $nd;
                $source = 'NorthData';
                $validationUrl = $nd['url'] ?? "https://www.northdata.com/" . urlencode($llmName);
                $countryMatch = $nd['country_match'] ?? false;
                $regIdMatch = $nd['registry_id_match'] ?? false;

                $countryNote = $countryMatch ? "country confirmed" : "country NOT confirmed";
                $regIdNote = $regIdMatch
                    ? "registry ID \"{$registryId}\" found on page"
                    : "registry ID \"{$registryId}\" not found on page";

                $this->log('validate', "NorthData: name: \"{$registryName}\", status: {$registryStatus}, {$countryNote}, {$regIdNote}", [
                    'expandable' => true,
                    'sections' => [['label' => 'NorthData Result', 'content' => json_encode($nd, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES)]],
                ]);

                // Primary: entity must exist with correct country
                if (!$countryMatch) {
                    $registryName = null;
                    $this->log('validate', "Discarding result — country mismatch for \"{$llmName}\"");
                }
            } else {
                $this->log('validate', "NorthData: no results for \"{$llmName}\"");
            }
        }

        // Build validation result
        if (!$registryName) {
            $report['registry_validation'] = [
                'status' => 'not_found',
                'message' => "Registry ID {$registryId} not found in " . ($source ?? 'registry'),
            ];
            $this->log('validate', "VALIDATION FAILED — entity not found in registry");
            return $report;
        }

        // Compare names (case-insensitive, normalise punctuation)
        $normLlm = strtoupper(preg_replace('/[^A-Z0-9 ]/', '', strtoupper($llmName)));
        $normReg = strtoupper(preg_replace('/[^A-Z0-9 ]/', '', strtoupper($registryName)));
        $nameMatch = $normLlm === $normReg;

        // Check status
        $statusLower = strtolower($registryStatus ?? '');
        $statusOk = in_array($statusLower, ['active', 'unknown']);

        if ($nameMatch && $statusOk) {
            $report['registry_validation'] = [
                'status' => 'verified',
                'message' => "Verified: \"{$registryName}\" is {$registryStatus} in {$source}",
                'registry_name' => $registryName,
                'registry_status' => $registryStatus,
                'source' => $source,
                'validation_url' => $validationUrl,
            ];
            $this->log('validate', "VERIFIED — \"{$registryName}\" matches, status: {$registryStatus}");
        } elseif ($nameMatch && !$statusOk) {
            $report['registry_validation'] = [
                'status' => 'name_match_bad_status',
                'message' => "Name matches but status is \"{$registryStatus}\" (not active) in {$source}",
                'registry_name' => $registryName,
                'registry_status' => $registryStatus,
                'source' => $source,
                'validation_url' => $validationUrl,
            ];
            $this->log('validate', "WARNING — name matches but status is \"{$registryStatus}\"");
            // Downgrade confidence if entity is dissolved/canceled
            if (in_array($report['confidence'] ?? '', ['high', 'medium'])) {
                $report['confidence'] = 'low';
                $report['note'] = ($report['note'] ?? '') . " [Registry validation: entity status is \"{$registryStatus}\" — confidence downgraded.]";
                $this->log('validate', "Confidence downgraded to LOW due to inactive status");
            }
        } else {
            $report['registry_validation'] = [
                'status' => 'name_mismatch',
                'message' => "Name mismatch: LLM said \"{$llmName}\" but registry has \"{$registryName}\"",
                'registry_name' => $registryName,
                'registry_status' => $registryStatus,
                'source' => $source,
                'validation_url' => $validationUrl,
            ];
            $this->log('validate', "WARNING — name mismatch: expected \"{$llmName}\", registry has \"{$registryName}\"");
            if (in_array($report['confidence'] ?? '', ['high', 'medium'])) {
                $report['confidence'] = 'low';
                $report['note'] = ($report['note'] ?? '') . " [Registry validation: name mismatch — LLM returned \"{$llmName}\" but registry has \"{$registryName}\".]";
                $this->log('validate', "Confidence downgraded to LOW due to name mismatch");
            }
        }

        return $report;
    }

    /**
     * Validate a single entity against its registry.
     * Returns: 'verified', 'name_mismatch', 'not_found', or 'inactive'.
     */
    private function validateSingleEntity(string $name, string $registryId, string $country, ?string $state): array
    {
        $registryName = null;
        $registryStatus = null;
        $source = null;
        $validationUrl = null;

        // US → Bizapedia, then Delaware fallback
        if ($country === 'US' && $state) {
            $biz = $this->tools->lookupBizapediaByFileNumber($registryId, $state);
            if ($biz) {
                $registryName = $biz['EntityName'] ?? null;
                $registryStatus = $biz['FilingStatus'] ?? null;
                $source = 'Bizapedia';
                $validationUrl = '/validate.php?' . http_build_query(['entity_name' => $name, 'registry_id' => $registryId, 'country' => 'US', 'state' => $state]);
                $entityType = strtoupper($biz['EntityType'] ?? '');
                if (str_contains($entityType, 'FOREIGN') || str_contains($entityType, 'OUT OF STATE')) {
                    $registryStatus = 'Branch';
                }
                if (str_contains($entityType, 'FICTITIOUS')) {
                    $registryStatus = 'Fictitious name';
                }
            } elseif ($state === 'DE') {
                $de = $this->tools->lookupDelawareByFileNumber($registryId);
                if ($de) {
                    $registryName = $de['name'];
                    $registryStatus = 'Active';
                    $source = 'Delaware Div. of Corps.';
                    $validationUrl = 'https://icis.corp.delaware.gov/ecorp/entitysearch/namesearch.aspx';
                }
            }
        }

        // UK → Companies House
        if ($country === 'GB' && !$registryName) {
            $ch = $this->tools->lookupCompaniesHouseByNumber($registryId);
            if ($ch) {
                $registryName = $ch['company_name'] ?? null;
                $registryStatus = $ch['company_status'] ?? null;
                $source = 'Companies House';
                $validationUrl = "https://find-and-update.company-information.service.gov.uk/company/{$registryId}";
            }
        }

        // EU → NorthData
        $ndCountries = ['DE', 'NL', 'FR', 'AT', 'CH', 'BE', 'LU', 'IT', 'ES', 'DK', 'SE', 'NO', 'FI', 'PL', 'CZ', 'IE'];
        if (in_array($country, $ndCountries) && !$registryName) {
            $nd = $this->tools->validateNorthdataEntity($name, $registryId, $country);
            if ($nd && ($nd['country_match'] ?? false)) {
                $fullNdName = preg_replace('/\s*\([^)]*\)\s*$/', '', $nd['name']);
                $parts = array_map('trim', explode(',', $fullNdName));
                $registryName = count($parts) >= 3 ? implode(', ', array_slice($parts, 0, -2)) : $parts[0];
                $registryStatus = $nd['status'] ?? 'unknown';
                $source = 'NorthData';
                $validationUrl = $nd['url'] ?? null;
            }
        }

        if (!$registryName) {
            return ['status' => 'not_found', 'source' => $source];
        }

        $normLlm = strtoupper(preg_replace('/[^A-Z0-9 ]/', '', strtoupper($name)));
        $normReg = strtoupper(preg_replace('/[^A-Z0-9 ]/', '', strtoupper($registryName)));
        $nameMatch = $normLlm === $normReg;
        $statusLower = strtolower($registryStatus ?? '');
        $statusOk = in_array($statusLower, ['active', 'unknown']);

        if ($nameMatch && $statusOk) {
            return ['status' => 'verified', 'registry_name' => $registryName, 'source' => $source, 'validation_url' => $validationUrl];
        } elseif ($nameMatch) {
            return ['status' => 'inactive', 'registry_name' => $registryName, 'registry_status' => $registryStatus, 'source' => $source, 'validation_url' => $validationUrl];
        } else {
            return ['status' => 'name_mismatch', 'registry_name' => $registryName, 'source' => $source, 'validation_url' => $validationUrl];
        }
    }

    /**
     * Phase 8: When registry validation fails, search registries for any new
     * entity name the registry returned, then call the LLM again with the
     * original report + validation details + new registry data.
     */
    private function reanalyzeAfterValidationFailure(
        array $report,
        string $url,
        string $domain,
        array $websiteData,
        array $entityInfo,
        array $registries
    ): array {
        $rv = $report['registry_validation'] ?? [];
        $rvStatus = $rv['status'] ?? '';
        $registryName = $rv['registry_name'] ?? null;
        $llmName = $report['recommended_entity']['legal_entity_name'] ?? null;
        $originalNames = array_map('strtolower', $entityInfo['entity_names'] ?? []);

        // Save original report
        $originalReport = $report;

        // Collect names to search: the LLM's recommended name + the registry-returned name
        $namesToSearch = [];
        if ($llmName) $namesToSearch[] = $llmName;
        if ($registryName) $namesToSearch[] = $registryName;

        // For each name to search: run new searches if not done in Phase 3,
        // otherwise include the existing Phase 3 Bizapedia results for that name
        $supplementaryResults = [];
        $searchedNorms = [];
        foreach ($namesToSearch as $name) {
            $normName = strtolower(trim(preg_replace('/[^a-z0-9 ]/i', '', strtolower($name))));
            if (isset($searchedNorms[$normName])) continue;
            $searchedNorms[$normName] = true;

            $alreadySearched = false;
            foreach ($originalNames as $on) {
                $normOn = strtolower(trim(preg_replace('/[^a-z0-9 ]/i', '', $on)));
                if ($normOn === $normName) {
                    $alreadySearched = true;
                    break;
                }
            }

            if (!$alreadySearched) {
                $this->log('reanalysis', "\"{$name}\" was not searched in Phase 3 — running supplementary searches");
                $newResults = $this->searchRegistriesForName($name, $entityInfo, $registries);
                $supplementaryResults = array_merge($supplementaryResults, $newResults);
            } else {
                $this->log('reanalysis', "\"{$name}\" was already searched in Phase 3 — including existing Bizapedia results");
                $bizKey = "bizapedia:{$name}";
                if (isset($registries[$bizKey])) {
                    $supplementaryResults[$bizKey] = $registries[$bizKey];
                }
            }
        }

        // Build the follow-up prompt
        $originalReportJson = json_encode($report, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES);
        $source = $rv['source'] ?? 'the registry';
        $registryStatus = $rv['registry_status'] ?? null;

        // Lead with "could not be verified", then add context as notes
        $validationSummary = "Your recommended entity \"{$llmName}\" could not be verified in the registry.\n";

        // Supplementary searches headline
        $searchedNames = [];
        foreach ($supplementaryResults as $key => $result) {
            if (preg_match('/^[^:]+:(.+)$/', $key, $m)) {
                $searchedNames[trim($m[1])] = true;
            }
        }
        if (!empty($searchedNames)) {
            $nameList = implode('" and "', array_keys($searchedNames));
            $validationSummary .= "We searched {$source} for \"{$nameList}\" and found the supplementary results below.\n";
        }

        // Context notes (secondary)
        $registryId = $report['recommended_entity']['registry_id'] ?? null;
        if ($rvStatus === 'name_mismatch' && $registryName && $registryId) {
            if ($registryStatus && stripos($registryStatus, 'fictitious') !== false) {
                $validationSummary .= "Note: the registry_id {$registryId} you provided is a fictitious name (trade name) registration for \"{$registryName}\", not a legal entity.\n";
            } else {
                $validationSummary .= "Note: the registry_id {$registryId} you provided belongs to \"{$registryName}\", not \"{$llmName}\".\n";
            }
        } elseif ($rvStatus === 'not_found' && $registryId) {
            $validationSummary .= "Note: registry_id {$registryId} was not found in {$source}.\n";
        } elseif ($rvStatus === 'name_match_bad_status') {
            $validationSummary .= "Note: \"{$registryName}\" was found but its status is \"{$registryStatus}\" (not active).\n";
        } elseif (!$registryId) {
            $validationSummary .= "Note: no registry_id was provided. The supplementary results below may help identify the correct filing.\n";
        }
        if (!empty($rv['is_branch'])) {
            $validationSummary .= "Note: this is a branch (foreign) registration. Domestic jurisdiction: " . ($rv['domestic_state'] ?? 'unknown') . ".\n";
        }

        $supplementaryText = '';
        if (!empty($supplementaryResults)) {
            $supplementaryText = "\n\n=== SUPPLEMENTARY REGISTRY RESULTS ===\n";
            foreach ($supplementaryResults as $key => $result) {
                $truncated = substr($result, 0, 10000);
                $supplementaryText .= "\n--- {$key} ---\n{$truncated}\n";
            }
        }

        $systemPrompt = $this->analysisPrompt . "\n\n" . $this->jsonSchema;
        $userMessage = <<<PROMPT
Your previous analysis for {$url} was checked against the official registry and could not be verified.

=== WHAT WENT WRONG ===
{$validationSummary}
=== YOUR PREVIOUS REPORT ===
{$originalReportJson}
{$supplementaryText}

=== ORIGINAL EVIDENCE ===
{$this->formatEvidence($url, $domain, $websiteData, $entityInfo, $registries)}

Please re-analyze using the new registry data and produce a corrected report in the same JSON format.
PROMPT;

        // Log the re-analysis prompt
        $this->log('reanalysis', "Calling LLM for re-analysis with validation failure context", [
            'expandable' => true,
            'sections' => [
                ['label' => 'Validation Summary', 'content' => $validationSummary],
                ['label' => 'Supplementary Results', 'content' => $supplementaryText ?: '(none — name already searched)'],
            ],
        ]);

        $inputTokensBefore = $this->totalInputTokens;
        $outputTokensBefore = $this->totalOutputTokens;

        $responseText = $this->callLLM($systemPrompt, $userMessage, 8192);

        $reanalysisCost = (($this->totalInputTokens - $inputTokensBefore) * 3.0 / 1_000_000)
                        + (($this->totalOutputTokens - $outputTokensBefore) * 15.0 / 1_000_000);

        $this->log('reanalysis', "Re-analysis response (" . number_format(strlen($responseText)) . " chars, \$" . number_format($reanalysisCost, 4) . ")", [
            'expandable' => true,
            'sections' => [['label' => 'Output', 'content' => $responseText]],
        ]);

        $newReport = $this->parseJsonResponse($responseText, $originalReport);

        // Re-validate the new recommendation
        $newEntity = $newReport['recommended_entity'] ?? null;
        if ($newEntity && !empty($newEntity['registry_id'])) {
            $this->log('reanalysis', "Re-validating new recommendation: \"{$newEntity['legal_entity_name']}\" ({$newEntity['registry_id']})");
            $newReport = $this->validateEntityInRegistry($newReport);
            $newRvStatus = $newReport['registry_validation']['status'] ?? 'unknown';
            $this->log('reanalysis', "Re-validation result: {$newRvStatus}");
        }

        // Store original report for comparison
        $newReport['original_report'] = $originalReport;

        return $newReport;
    }

    /**
     * Run targeted registry searches for a single entity name.
     * Used by Phase 8 when the registry returns a name not previously searched.
     */
    private function searchRegistriesForName(string $name, array $entityInfo, array $existingRegistries): array
    {
        $jurisdiction = strtolower($entityInfo['jurisdiction'] ?? 'unknown');
        $jurisdictions = [$jurisdiction];
        if (!empty($entityInfo['known_jurisdiction'])) {
            $knownJur = strtolower($entityInfo['known_jurisdiction']);
            if ($knownJur !== $jurisdiction) $jurisdictions[] = $knownJur;
        }
        $isUS = array_intersect($jurisdictions, ['us', 'united states', 'delaware', 'new york', 'california']);
        $isUK = array_intersect($jurisdictions, ['uk', 'england', 'scotland', 'wales', 'united kingdom']);
        $isEU = array_intersect($jurisdictions, ['germany', 'france', 'netherlands', 'austria', 'switzerland', 'europe', 'finland', 'denmark', 'sweden', 'norway', 'poland', 'czech republic', 'belgium', 'luxembourg', 'italy', 'spain', 'ireland']);
        $isUnknown = in_array('unknown', $jurisdictions);

        $results = [];

        if ($isUK || $isUnknown) {
            if (!isset($existingRegistries["companies_house:{$name}"])) {
                $results["companies_house:{$name}"] = $this->tools->searchCompaniesHouse($name);
                $this->logRegistryResult('reanalysis', 'Companies House', $name, $results["companies_house:{$name}"]);
            }
        }

        if ($isUS || $isUnknown) {
            if (!isset($existingRegistries["sec_company:{$name}"])) {
                $results["sec_company:{$name}"] = $this->tools->searchSecCompany($name);
                $this->logRegistryResult('reanalysis', 'SEC Company', $name, $results["sec_company:{$name}"]);
            }

            $bizResults = $this->tools->searchBizapedia($name);
            if (!empty($bizResults)) {
                $results["bizapedia:{$name}"] = LookupTools::deduplicateBizapediaResults($bizResults);
                $this->logRegistryResult('reanalysis', 'Bizapedia', $name, count($bizResults) . ' results');
            }

            if (!isset($existingRegistries["delaware:{$name}"])) {
                $delawareResult = $this->tools->searchDelaware($name);
                $results["delaware:{$name}"] = $delawareResult;
                $this->logRegistryResult('reanalysis', 'Delaware', $name, $delawareResult);
            }
        }

        if ($isEU || $isUnknown) {
            if (!isset($existingRegistries["northdata:{$name}"])) {
                $results["northdata:{$name}"] = $this->tools->searchNorthdata($name);
                $this->logRegistryResult('reanalysis', 'North Data', $name, $results["northdata:{$name}"]);
            }
        }

        return $results;
    }

    private function formatEvidence(string $url, string $domain, array $websiteData, array $entityInfo, array $registries): string
    {
        $pageUrls = $websiteData['pageUrls'] ?? [];
        $parts = [];
        $parts[] = "TARGET URL: {$url}";
        $parts[] = "DOMAIN: {$domain}";
        $parts[] = "CANDIDATE ENTITY NAMES: " . json_encode($entityInfo['entity_names'] ?? []);
        $parts[] = "LIKELY JURISDICTIONS: " . json_encode([$entityInfo['jurisdiction'] ?? 'unknown']);

        $whoisSource = "whois lookup for {$domain}";
        $parts[] = "\n=== WHOIS ===";
        $parts[] = "source: {$whoisSource}";
        $parts[] = $this->scrubBlockedNames($websiteData['whois'] ?? 'Not available');

        foreach ($websiteData['pages'] as $pageName => $text) {
            $truncated = substr($text, 0, 4000);
            $sourceUrl = $pageUrls[$pageName] ?? $url;
            $parts[] = "\n=== WEBSITE: " . strtoupper($pageName) . " ===";
            $parts[] = "source: {$sourceUrl}";
            $parts[] = $truncated;
        }

        foreach ($registries as $key => $result) {
            $truncated = substr($result, 0, 10000);
            $source = $this->registryKeyToSource($key);
            $parts[] = "\n=== REGISTRY: {$key} ===";
            $parts[] = "source: {$source}";
            $parts[] = $truncated;
        }

        return implode("\n", $parts);
    }

    private function registryKeyToSource(string $key): string
    {
        if (str_starts_with($key, 'companies_house:')) return 'https://find-and-update.company-information.service.gov.uk/';
        if (str_starts_with($key, 'sec_company:')) return 'https://efts.sec.gov/LATEST/search-index?q=' . urlencode(substr($key, 12));
        if (str_starts_with($key, 'sec_iapd:')) return 'https://adviserinfo.sec.gov/';
        if (str_starts_with($key, 'sec_fulltext:')) return 'https://efts.sec.gov/LATEST/search-index?q=' . urlencode(substr($key, 13));
        if (str_starts_with($key, 'sec_submissions:')) return 'https://data.sec.gov/submissions/CIK' . str_pad(substr($key, 16), 10, '0', STR_PAD_LEFT) . '.json';
        if (str_starts_with($key, 'sec_financials:')) return 'https://data.sec.gov/api/xbrl/companyfacts/CIK' . str_pad(explode(':', $key)[1], 10, '0', STR_PAD_LEFT) . '.json';
        if (str_starts_with($key, 'yahoo_finance:')) return 'https://finance.yahoo.com/quote/' . explode(':', $key)[1] . '/';
        if ($key === 'google_search') return 'https://www.google.com/';
        if ($key === 'linkedin') return 'https://www.linkedin.com/';
        if (str_starts_with($key, 'sec_filing:')) return 'https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=' . explode(':', $key)[1];
        if ($key === 'bizapedia') return 'https://www.bizapedia.com/';
        if (str_starts_with($key, 'trademark:')) return 'https://www.bizapedia.com/ (trademark search)';
        if (str_starts_with($key, 'northdata:')) return 'https://www.northdata.com/';
        if ($key === 'northdata_network') return 'https://www.northdata.com/ (ownership network)';
        if ($key === 'ownership_chain') return 'https://find-and-update.company-information.service.gov.uk/ (PSC chain)';
        if (str_starts_with($key, 'edgar_parent:')) return 'https://efts.sec.gov/ (Exhibit 21 search)';
        if ($key === 'ch_corporate_appointments') return 'https://find-and-update.company-information.service.gov.uk/ (corporate officer appointments)';
        if (str_starts_with($key, 'ch_brand_search:')) return 'https://find-and-update.company-information.service.gov.uk/ (brand name search)';
        if ($key === 'sec_cross_reference') return 'cross-reference of SEC data against website content';
        return $key;
    }

    // ── LLM API ────────────────────────────────────────────────────────────

    private function isOpenAIModel(): bool
    {
        $model = $this->config['model'] ?? '';
        return str_starts_with($model, 'gpt-') || str_starts_with($model, 'o1') || str_starts_with($model, 'o3') || str_starts_with($model, 'o4');
    }

    private function callLLM(string $systemPrompt, string $userMessage, int $maxTokens): string
    {
        if ($this->isOpenAIModel()) {
            return $this->callOpenAI($systemPrompt, $userMessage, $maxTokens);
        }
        return $this->callClaude($systemPrompt, $userMessage, $maxTokens);
    }

    private function callClaude(string $systemPrompt, string $userMessage, int $maxTokens): string
    {
        $this->tools->incrementApiCall('claude');
        $inputChars = strlen($systemPrompt) + strlen($userMessage);
        $this->log('llm', "Calling Claude ({$this->config['model']}) — input: " . number_format($inputChars) . " chars, max_tokens: {$maxTokens}");

        $payload = [
            'model' => $this->config['model'],
            'max_tokens' => $maxTokens,
            'system' => $systemPrompt,
            'messages' => [
                ['role' => 'user', 'content' => $userMessage],
            ],
        ];

        $ch = curl_init('https://api.anthropic.com/v1/messages');
        curl_setopt_array($ch, [
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_POST => true,
            CURLOPT_HTTPHEADER => [
                'Content-Type: application/json',
                'x-api-key: ' . $this->config['anthropic_api_key'],
                'anthropic-version: 2023-06-01',
            ],
            CURLOPT_POSTFIELDS => json_encode($payload, JSON_INVALID_UTF8_SUBSTITUTE),
            CURLOPT_TIMEOUT => 120,
        ]);

        $response = curl_exec($ch);
        $httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);

        if ($httpCode !== 200 || !$response) {
            $errBody = $response ? json_decode($response, true) : null;
            $errMsg = $errBody['error']['message'] ?? ($response ?: 'No response body');
            $this->log('llm', "ERROR: Claude API returned HTTP {$httpCode} — {$errMsg}");
            return json_encode(['error' => "Claude API returned HTTP {$httpCode}"]);
        }

        $data = json_decode($response, true);

        $usage = $data['usage'] ?? [];
        $inputTokens = $usage['input_tokens'] ?? 0;
        $outputTokens = $usage['output_tokens'] ?? 0;
        $this->totalInputTokens += $inputTokens;
        $this->totalOutputTokens += $outputTokens;

        $text = '';
        foreach ($data['content'] ?? [] as $block) {
            if (($block['type'] ?? '') === 'text') {
                $text .= $block['text'];
            }
        }

        $stopReason = $data['stop_reason'] ?? 'unknown';
        $callCost = ($inputTokens * 3.0 / 1_000_000) + ($outputTokens * 15.0 / 1_000_000);
        $this->log('llm', "Response: " . number_format($inputTokens) . " input / " . number_format($outputTokens) . " output tokens — $" . number_format($callCost, 4));

        if ($stopReason === 'max_tokens') {
            $this->log('llm', "WARNING: Response truncated (hit max_tokens={$maxTokens}). Output may be incomplete.");
        }

        return $text;
    }

    private function callOpenAI(string $systemPrompt, string $userMessage, int $maxTokens): string
    {
        $this->tools->incrementApiCall('openai');
        $inputChars = strlen($systemPrompt) + strlen($userMessage);
        $model = $this->config['model'];
        $this->log('llm', "Calling OpenAI ({$model}) — input: " . number_format($inputChars) . " chars, max_tokens: {$maxTokens}");

        $payload = [
            'model' => $model,
            'max_completion_tokens' => $maxTokens,
            'messages' => [
                ['role' => 'system', 'content' => $systemPrompt],
                ['role' => 'user', 'content' => $userMessage],
            ],
        ];

        $ch = curl_init('https://api.openai.com/v1/chat/completions');
        curl_setopt_array($ch, [
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_POST => true,
            CURLOPT_HTTPHEADER => [
                'Content-Type: application/json',
                'Authorization: Bearer ' . $this->config['openai_api_key'],
            ],
            CURLOPT_POSTFIELDS => json_encode($payload, JSON_INVALID_UTF8_SUBSTITUTE),
            CURLOPT_TIMEOUT => 120,
        ]);

        $response = curl_exec($ch);
        $httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);

        if ($httpCode !== 200 || !$response) {
            $errBody = $response ? json_decode($response, true) : null;
            $errMsg = $errBody['error']['message'] ?? ($response ?: 'No response body');
            $this->log('llm', "ERROR: OpenAI API returned HTTP {$httpCode} — {$errMsg}");
            return json_encode(['error' => "OpenAI API returned HTTP {$httpCode}"]);
        }

        $data = json_decode($response, true);

        $usage = $data['usage'] ?? [];
        $inputTokens = $usage['prompt_tokens'] ?? 0;
        $outputTokens = $usage['completion_tokens'] ?? 0;
        $this->totalInputTokens += $inputTokens;
        $this->totalOutputTokens += $outputTokens;

        $text = $data['choices'][0]['message']['content'] ?? '';

        $finishReason = $data['choices'][0]['finish_reason'] ?? 'unknown';
        // GPT-4o pricing: $2.50/$10 per 1M tokens
        $callCost = ($inputTokens * 2.5 / 1_000_000) + ($outputTokens * 10.0 / 1_000_000);
        $this->log('llm', "Response: " . number_format($inputTokens) . " input / " . number_format($outputTokens) . " output tokens — $" . number_format($callCost, 4));

        if ($finishReason === 'length') {
            $this->log('llm', "WARNING: Response truncated (hit max_tokens={$maxTokens}). Output may be incomplete.");
        }

        return $text;
    }

    // ── JSON Parsing ─────────────────────────────────────────────────────────

    private function parseJsonResponse(string $text, array $fallback): array
    {
        $clean = trim($text);

        // Strip markdown fences
        if (preg_match('/```(?:json)?\s*(\{.*?\})\s*```/s', $clean, $m)) {
            $clean = $m[1];
        }

        $result = json_decode($clean, true);
        if (is_array($result)) {
            return $result;
        }

        // Try to find JSON object
        $start = strpos($clean, '{');
        if ($start !== false) {
            $depth = 0;
            for ($i = $start; $i < strlen($clean); $i++) {
                if ($clean[$i] === '{') $depth++;
                elseif ($clean[$i] === '}') {
                    $depth--;
                    if ($depth === 0) {
                        $result = json_decode(substr($clean, $start, $i - $start + 1), true);
                        if (is_array($result)) {
                            return $result;
                        }
                        break;
                    }
                }
            }
        }

        return $fallback;
    }
}
