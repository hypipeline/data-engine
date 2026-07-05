"""
Entity Lookup v3b (Python) — Phase 2: Website fetch phase.

Faithful port of the WEBSITE-FETCH methods of php/lookup.php (class EntityLookup):
    - fetchWebsiteData        (~line 375)
    - discoverLinksFromHomepage (~line 269)
    - discoverAndMerge        (~line 333)
    - isDuplicateOfHomepage   (~line 365)
    - fetchNewsArticles       (~line 640)

Combined into EntityLookup via multiple inheritance. On ``self`` this mixin uses:
    - self.config['max_page_chars']
    - self.log(phase, message, detail)
    - self.tools.curl_fetch_multi(urls) -> {url: {'text','http_code','html'}}
    - self.tools.whois_lookup(domain) -> str
    - self.tools.scraping_browser_fetch_parallel(urls) -> {url: {'text','html'}}
      (snake_case of PHP scrapingBrowserFetchParallel; provided by a tools mixin)

Logging matches the PHP exactly: log(phase, message, detail) where detail may carry
{'expandable': True, 'sections': [{'label','content'}]}.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse


def _host(u: str):
    """PHP parse_url($u, PHP_URL_HOST) — host or None."""
    return urlparse(u).hostname


def _path(u: str) -> str:
    """PHP parse_url($u, PHP_URL_PATH) ?? '' — path or ''."""
    return urlparse(u).path


class WebsiteFetchMixin:

    # ── Phase 1: Fetch Website ───────────────────────────────────────────────

    def discover_links_from_homepage(self, html: str, base: str) -> dict:
        """
        Extract links from raw homepage HTML and categorise by page type.
        Returns ['terms' => [...], 'privacy' => [...], 'about' => [...], 'contact' => [...]]
        """
        skip_extensions = re.compile(
            r'\.(?:pdf|doc|docx|xls|xlsx|ppt|pptx|zip|png|jpg|jpeg|gif|svg|mp4|mp3|csv)$', re.I)

        patterns = {
            'terms':       re.compile(r'\b(terms|conditions|tos)\b', re.I),
            'privacy':     re.compile(r'\b(privacy|datenschutz|gdpr)\b', re.I),
            'about':       re.compile(r'\b(about|company|who.we.are|ueber.uns)\b', re.I),
            'contact':     re.compile(r'\b(contact|kontakt)\b', re.I),
            'leadership':  re.compile(r'\b(leadership|management|team|executive|board|directors|officers)\b', re.I),
            'disclosures': re.compile(r'\b(disclosures?|regulatory|form.adv|form.crs)\b', re.I),
            'news':        re.compile(r'\b(news|press|media|announcements?|blog)\b', re.I),
            'impressum':   re.compile(r'\b(impressum|imprint)\b', re.I),
        }

        discovered = {'terms': [], 'privacy': [], 'about': [], 'contact': [],
                      'leadership': [], 'disclosures': [], 'news': [], 'impressum': []}

        # Extract all href values from <a> tags
        matches = re.findall(r'<a\s[^>]*href=["\']([^"\'#]+)', html, re.I)
        if not matches:
            return discovered

        seen = {}
        for href in matches:
            # Resolve relative URLs
            if href.startswith('/'):
                href = base + href
            elif not href.startswith('http'):
                continue  # skip mailto:, javascript:, etc.

            # Only keep URLs on the same domain
            href_host = _host(href)
            base_host = _host(base)
            if href_host and base_host and not href_host.endswith(re.sub(r'^www\.', '', base_host)):
                continue

            # Skip non-HTML file extensions
            path = _path(href) or ''
            if skip_extensions.search(path):
                continue

            # Deduplicate
            normalised = href.rstrip('/')
            if normalised in seen:
                continue
            seen[normalised] = True

            # Categorise by URL path + link text (we match on the href itself)
            for cat, regex in patterns.items():
                if regex.search(path):
                    discovered[cat].append(normalised)

        return discovered

    def discover_and_merge(self, html: str, base: str, pending: dict, pages: dict, log_phase: str) -> list:
        """
        Discover links from homepage HTML and merge into pending categories.
        Returns newly-discovered URLs (not already in pending). ``pending`` is mutated in place.
        """
        discovered = self.discover_links_from_homepage(html, base)
        total_found = sum(len(v) for v in discovered.values())
        if total_found == 0:
            return []

        summary = []
        for cat, d_urls in discovered.items():
            for u in d_urls:
                summary.append(f"{cat}: {u}")
        self.log(log_phase, "  Discovered links from homepage:\n    " + "\n    ".join(summary))

        new_urls = []
        for cat, d_urls in discovered.items():
            if cat in pages:
                continue  # already resolved
            if cat not in pending:
                pending[cat] = []
            for d_url in d_urls:
                if d_url not in pending[cat]:
                    pending[cat].insert(0, d_url)
                    new_urls.append(d_url)
        return new_urls

    def is_duplicate_of_homepage(self, text: str, pages: dict) -> bool:
        """
        Check if sub-page text is just the homepage repeated (redirect/bot block).
        Compares first 500 chars after stripping whitespace.
        """
        if 'homepage' not in pages:
            return False
        normalize = lambda s: re.sub(r'\s+', '', s[:500])
        sub_norm = normalize(text)
        home_norm = normalize(pages['homepage'])
        if len(sub_norm) < 100:
            return False
        return sub_norm == home_norm

    def fetch_website_data(self, url: str, domain: str) -> dict:
        base = url.rstrip('/')
        max_chars = self.config['max_page_chars']

        # ── WHOIS (runs independently) ──
        self.log('fetch', f"WHOIS lookup: {domain}")
        whois = self.tools.whois_lookup(domain)
        self.log('fetch', f"WHOIS: {len(whois)} chars", {
            'expandable': True,
            'sections': [{'label': 'WHOIS Data', 'content': whois}],
        })

        # ── Fallback URL guesses (only used for categories NOT discovered from homepage) ──
        fallback_urls = {
            'terms':       [f"{base}/terms-of-use", f"{base}/terms-of-service", f"{base}/terms"],
            'privacy':     [f"{base}/privacy-policy", f"{base}/privacy"],
            'about':       [f"{base}/about", f"{base}/about-us", f"{base}/company"],
            'contact':     [f"{base}/contact", f"{base}/contact-us"],
            'leadership':  [f"{base}/leadership", f"{base}/team", f"{base}/about/team"],
            'disclosures': [f"{base}/disclosures", f"{base}/regulatory-disclosures"],
            'news':        [f"{base}/news", f"{base}/press"],
            'impressum':   [f"{base}/impressum", f"{base}/imprint"],
        }

        # ── LEVEL 1a: Fetch homepage first ──
        self.log('fetch', "Level 1a: Fetching homepage...")
        curl_results = self.tools.curl_fetch_multi([url])

        # ── Discover links from homepage HTML ──
        discovered = {}
        homepage_html = (curl_results.get(url) or {}).get('html')
        if homepage_html:
            discovered = self.discover_links_from_homepage(homepage_html, base)
            total_found = sum(len(v) for v in discovered.values())
            if total_found > 0:
                summary = []
                for cat, d_urls in discovered.items():
                    for u in d_urls:
                        summary.append(f"{cat}: {u}")
                self.log('fetch', "  Discovered links from homepage:\n    " + "\n    ".join(summary))

        # ── Build URL list: discovered links take priority, fallback guesses fill gaps ──
        categories = {}
        for cat in fallback_urls:
            if discovered.get(cat):
                # Homepage had links for this category — use only those
                categories[cat] = discovered[cat]
            else:
                # No links discovered — use fallback guesses
                categories[cat] = fallback_urls[cat]

        # ── LEVEL 1b: Parallel curl for all sub-page URLs ──
        all_urls = []
        for cat, cat_urls in categories.items():
            for cat_url in cat_urls:
                if cat_url not in curl_results:
                    all_urls.append(cat_url)

        self.log('fetch', f"Level 1b: Fetching {len(all_urls)} sub-page URLs...")
        sub_results = self.tools.curl_fetch_multi(all_urls)
        curl_results.update(sub_results)

        pages = {}        # cat => text (resolved pages)
        page_sources = {}  # cat => source label
        page_urls = {}     # cat => resolved URL
        pending = {}       # cat => [url, url, ...] (still need fetching)
        is404 = {}         # cat => true (skip further attempts)

        # Process homepage
        hr = curl_results[url]
        if hr['text'] is not None:
            pages['homepage'] = hr['text'][:max_chars]
            page_sources['homepage'] = 'curl'
            page_urls['homepage'] = url
            self.log('fetch', f"  homepage: / → HTTP {hr['http_code']} ✓ {len(pages['homepage'])} chars", {
                'expandable': True,
                'sections': [{'label': 'Page Text', 'content': pages['homepage'][:5000]}],
            })
        else:
            self.log('fetch', f"  homepage: / → HTTP {hr['http_code']}")
            if hr['http_code'] == 404:
                is404['homepage'] = True
            else:
                pending['homepage'] = [url]

        # Process sub-pages: log every URL with its status, find first success per category
        for cat, cat_urls in categories.items():
            if cat in pages:
                continue
            found = False
            sbr_candidates = []
            for cat_url in cat_urls:
                cr = curl_results.get(cat_url, {'text': None, 'http_code': 0, 'html': None})
                path = _path(cat_url) or cat_url
                if not found and cr['text'] is not None:
                    candidate = cr['text'][:max_chars]
                    if self.is_duplicate_of_homepage(candidate, pages):
                        self.log('fetch', f"  {cat}: {path} → HTTP {cr['http_code']} — skipped (duplicate of homepage)")
                        sbr_candidates.append(cat_url)
                        continue
                    pages[cat] = candidate
                    page_sources[cat] = 'curl'
                    page_urls[cat] = cat_url
                    self.log('fetch', f"  {cat}: {path} → HTTP {cr['http_code']} ✓ {len(pages[cat])} chars", {
                        'expandable': True,
                        'sections': [{'label': 'Page Text', 'content': pages[cat][:5000]}],
                    })
                    found = True
                else:
                    self.log('fetch', f"  {cat}: {path} → HTTP {cr['http_code']}")
                    if cr['http_code'] != 404:
                        sbr_candidates.append(cat_url)
            if not found:
                if sbr_candidates:
                    pending[cat] = sbr_candidates
                else:
                    is404[cat] = True

        # Summary with remaining candidates
        resolved = list(pages.keys())
        skipped = list(is404.keys())
        summary_parts = []
        summary_parts.append("Resolved: " + ('none' if not resolved else ', '.join(resolved)))
        if skipped:
            summary_parts.append("Skipped (all 404): " + ', '.join(skipped))
        if pending:
            pending_lines = []
            for cat, cat_urls in pending.items():
                paths = [(_path(u) or u) for u in cat_urls]
                pending_lines.append(f"    {cat}: " + ', '.join(paths))
            summary_parts.append("Remaining candidates:\n" + "\n".join(pending_lines))
        self.log('fetch', "Curl summary:\n  " + "\n  ".join(summary_parts))

        # If everything resolved with curl, skip fallbacks
        if not pending:
            pages = self.fetch_news_articles(pages, page_urls, curl_results, categories, base, max_chars)
            return {'url': url, 'domain': domain, 'pages': pages, 'pageUrls': page_urls, 'whois': whois}

        # ── LEVEL 2: Bright Data Scraping Browser (JS rendering + bot bypass, parallel) ──
        sbr_urls = {}
        for cat, cat_urls in pending.items():
            sbr_urls[(url if cat == 'homepage' else cat_urls[0])] = cat
        sbr_url_list = list(sbr_urls.keys())
        sbr_paths = [(_path(u) or '/') for u in sbr_url_list]
        self.log('scraping_browser', f"Level 2: Trying Bright Data Scraping Browser for {len(sbr_urls)} categories in parallel (" + ', '.join(sbr_paths) + ")...")

        sbr_results = self.tools.scraping_browser_fetch_parallel(sbr_url_list)

        # Process homepage first (for link discovery)
        if 'homepage' in pending:
            hr = sbr_results.get(url)
            if hr and len(hr['text']) >= 200:
                pages['homepage'] = hr['text'][:max_chars]
                page_sources['homepage'] = 'scraping_browser'
                page_urls['homepage'] = url
                self.log('scraping_browser', f"  Found homepage ({len(pages['homepage'])} chars)", {
                    'expandable': True,
                    'sections': [{'label': 'Page Text', 'content': pages['homepage'][:5000]}],
                })
                del pending['homepage']

                if hr.get('html'):
                    self.discover_and_merge(hr['html'], base, pending, pages, 'scraping_browser')
            else:
                self.log('scraping_browser', "  Miss homepage — Bright Data Scraping Browser returned no usable content")

        # Fetch any newly-discovered URLs not in the first batch
        new_sbr_urls = {}
        for cat, cat_urls in pending.items():
            if cat == 'homepage' or cat in pages or not cat_urls:
                continue
            cat_url = cat_urls[0]
            if cat_url not in sbr_results:
                new_sbr_urls[cat_url] = cat
        if new_sbr_urls:
            new_paths = [(_path(u) or '/') for u in new_sbr_urls.keys()]
            self.log('scraping_browser', f"  Fetching {len(new_sbr_urls)} discovered URLs in parallel (" + ', '.join(new_paths) + ")...")
            extra_results = self.tools.scraping_browser_fetch_parallel(list(new_sbr_urls.keys()))
            sbr_results.update(extra_results)

        # Process sub-pages
        still_pending = {}
        for cat, cat_urls in pending.items():
            if cat == 'homepage':
                still_pending[cat] = cat_urls
                continue
            if cat in pages or not cat_urls:
                continue
            cat_url = cat_urls[0]
            path = _path(cat_url) or cat_url
            sr = sbr_results.get(cat_url)
            if sr and len(sr['text']) >= 200:
                candidate = sr['text'][:max_chars]
                if self.is_duplicate_of_homepage(candidate, pages):
                    self.log('scraping_browser', f"  Miss {cat}: {path} — duplicate of homepage (likely redirect)")
                    still_pending[cat] = cat_urls
                else:
                    pages[cat] = candidate
                    page_sources[cat] = 'scraping_browser'
                    page_urls[cat] = cat_url
                    self.log('scraping_browser', f"  Found {cat}: {path} ({len(pages[cat])} chars)", {
                        'expandable': True,
                        'sections': [{'label': 'Page Text', 'content': pages[cat][:5000]}],
                    })
            else:
                self.log('scraping_browser', f"  Miss {cat}: {path} — Bright Data Scraping Browser returned no usable content")
                still_pending[cat] = cat_urls

        if not still_pending:
            return {'url': url, 'domain': domain, 'pages': pages, 'pageUrls': page_urls, 'whois': whois}

        # ── LEVELS 3-5 DISABLED ──
        #  Level 3: Bright Data Web Unlocker
        #  Level 4: Browserbase
        #  Level 5: Wayback Machine
        #  Currently only using Level 1 (Curl) and Level 2 (Bright Data Scraping Browser).

        # Summary
        sources = []
        for cat, src in page_sources.items():
            if src != 'curl':
                sources.append(f"{cat}={src}")
        source_note = (' (' + ', '.join(sources) + ')') if sources else ''
        self.log('fetch', f"Fetch complete: {len(pages)} pages" + source_note)

        # ── News articles: if we got a news index page, extract and fetch up to 3 articles ──
        pages = self.fetch_news_articles(pages, page_urls, curl_results, categories, base, max_chars)

        return {'url': url, 'domain': domain, 'pages': pages, 'pageUrls': page_urls, 'whois': whois}

    def fetch_news_articles(self, pages: dict, page_urls: dict, curl_results: dict,
                            categories: dict, base: str, max_chars: int) -> dict:
        """
        Extract article links from the news index page HTML and fetch up to 3.
        ``page_urls`` is mutated in place.
        """
        if 'news' not in pages:
            return pages

        # Find the news index URL that succeeded — we need its HTML
        news_html = None
        for news_url in (categories.get('news') or []):
            if news_url in curl_results and curl_results[news_url]['html']:
                news_html = curl_results[news_url]['html']
                break
        if not news_html:
            self.log('fetch', "News: index page text found but raw HTML not available (fetched via Scraping Browser?) — cannot extract article links")
            return pages
        self.log('fetch', f"News: scanning index page for article links ({len(news_html)} chars of HTML)")

        # Extract article links: look for <a> tags with paths that look like articles
        # (contain dates, slugs, or are deeper than the news index)
        article_urls = []
        matches = re.findall(r'<a\s[^>]*href=["\']([^"\'#]+)', news_html, re.I)
        if matches:
            news_path = None
            for nu in categories['news']:
                if nu in curl_results and curl_results[nu]['html']:
                    news_path = _path(nu)
                    break

            seen = []
            for href in matches:
                if href.startswith('/'):
                    href = base.rstrip('/') + href
                elif not href.startswith('http'):
                    continue

                parsed_host = _host(href)
                base_host = _host(base)
                if parsed_host and parsed_host != base_host:
                    continue

                path = _path(href) or ''

                # Skip non-article links (assets, anchors, index itself)
                if re.search(r'\.(?:pdf|png|jpg|jpeg|gif|svg|css|js|xml|zip)$', path, re.I):
                    continue
                if news_path and path == news_path:
                    continue
                if href in seen:
                    continue

                # Article heuristic: path is deeper than news index, or contains date-like segments
                if news_path and path.startswith(news_path) and path != news_path and path.count('/') > news_path.count('/'):
                    seen.append(href)
                    article_urls.append(href)
                elif re.search(r'/\d{4}/', path) or re.search(r'/\d{4}-\d{2}', path):
                    seen.append(href)
                    article_urls.append(href)

                if len(article_urls) >= 3:
                    break

        if not article_urls:
            total_links = len(matches)
            self.log('fetch', f"News: scanned {total_links} links on index page — no article links matched (need sub-paths of news index or date-patterned URLs)")
            return pages

        article_paths = [(_path(u) or u) for u in article_urls]
        self.log('fetch', f"News: found {len(article_urls)} article links on index page:\n    " + "\n    ".join(article_paths))
        article_results = self.tools.curl_fetch_multi(article_urls)

        i = 0
        for a_url in article_urls:
            ar = article_results.get(a_url)
            if ar and ar['text'] is not None and len(ar['text']) >= 200:
                i += 1
                pages[f"news_{i}"] = ar['text'][:max_chars]
                page_urls[f"news_{i}"] = a_url
                path = _path(a_url) or a_url
                self.log('fetch', f"  news_{i}: {path} ✓ {len(pages[f'news_{i}'])} chars", {
                    'expandable': True,
                    'sections': [{'label': 'Article Text', 'content': pages[f"news_{i}"][:5000]}],
                })

        if i == 0:
            self.log('fetch', "News: articles could not be fetched")

        return pages
