"""OpenCorporates scraper. Covers 140+ jurisdictions. Needs Browserbase for CAPTCHA."""

from __future__ import annotations
import re
from playwright.async_api import async_playwright, Browser, Page
from .base import BaseScraper, RegistryResult


class OpenCorporatesScraper(BaseScraper):
    registry_name = "OpenCorporates"
    jurisdiction = "GLOBAL"
    needs_browser = True

    SEARCH_URL = "https://opencorporates.com/companies"
    COMPANY_URL = "https://opencorporates.com/companies"

    async def search(
        self,
        entity_name: str,
        connect_url: str | None = None,
        browser: Browser | None = None,
        jurisdiction_code: str | None = None,
    ) -> list[RegistryResult]:
        if browser:
            return await self._search_with_browser(browser, entity_name, jurisdiction_code)

        if not connect_url:
            raise ValueError("Either connect_url or browser must be provided")

        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(connect_url)
            try:
                return await self._search_with_browser(browser, entity_name, jurisdiction_code)
            finally:
                await browser.close()

    async def _search_with_browser(
        self,
        browser: Browser,
        entity_name: str,
        jurisdiction_code: str | None = None,
    ) -> list[RegistryResult]:
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else await context.new_page()

        params = f"?q={entity_name.replace(' ', '+')}&type=companies"
        if jurisdiction_code:
            params += f"&jurisdiction_code={jurisdiction_code}"

        await page.goto(f"{self.SEARCH_URL}{params}", timeout=30000)

        # Wait for CAPTCHA to be solved or results to load
        await self._wait_for_results(page)

        return await self._parse_search_results(page)

    async def _wait_for_results(self, page: Page, timeout_ms: int = 30000) -> None:
        """Wait for search results, handling potential CAPTCHA."""
        try:
            # First check if there's a CAPTCHA
            captcha = await page.query_selector("#captcha_frame, .h-captcha, iframe[src*='hcaptcha']")
            if captcha:
                # Wait for CAPTCHA to be resolved (Browserbase may auto-solve)
                await page.wait_for_selector(
                    "#results, .companies, ul.companies",
                    timeout=timeout_ms,
                )
            else:
                await page.wait_for_selector(
                    "#results, .companies, ul.companies, .search-results",
                    timeout=10000,
                )
        except Exception:
            pass  # Proceed and try to parse whatever is on the page

    async def _parse_search_results(self, page: Page) -> list[RegistryResult]:
        """Parse company search results from the page."""
        results = []
        seen = set()

        # OpenCorporates uses <li> elements within a company list, or <tr> rows
        # Each result has a link to the company page with structured data
        rows = await page.query_selector_all("li.company, tr.company, .search-result")

        if not rows:
            # Fallback: try to find company links directly
            rows = await page.query_selector_all("a[href*='/companies/']")
            for link in rows:
                href = await link.get_attribute("href") or ""
                text = (await link.text_content() or "").strip()
                if not text or len(text) < 3:
                    continue
                # Parse jurisdiction and company number from URL
                # Format: /companies/JURISDICTION/COMPANY_NUMBER
                match = re.search(r'/companies/([a-z_]+)/([^/?#]+)', href)
                if not match:
                    continue
                jurisdiction = match.group(1)
                company_number = match.group(2)
                key = f"{jurisdiction}:{company_number}"
                if key in seen:
                    continue
                seen.add(key)
                results.append(RegistryResult(
                    entity_name=text,
                    registry_id=company_number,
                    jurisdiction=jurisdiction,
                    raw_data={
                        "opencorporates_url": f"https://opencorporates.com{href}" if href.startswith("/") else href,
                    },
                ))
            return results

        for row in rows:
            result = await self._parse_result_row(row)
            if result:
                key = f"{result.jurisdiction}:{result.registry_id}"
                if key not in seen:
                    seen.add(key)
                    results.append(result)

        return results

    async def _parse_result_row(self, row) -> RegistryResult | None:
        """Parse a single search result row."""
        # Find the main company link
        link = await row.query_selector("a[href*='/companies/']")
        if not link:
            return None

        href = await link.get_attribute("href") or ""
        name = (await link.text_content() or "").strip()
        if not name:
            return None

        # Parse jurisdiction and company number from URL
        match = re.search(r'/companies/([a-z_]+)/([^/?#]+)', href)
        if not match:
            return None

        jurisdiction = match.group(1)
        company_number = match.group(2)

        # Extract additional info from the row text
        row_text = (await row.text_content() or "").strip()

        status = self._extract_field(row_text, r'(?:Status|status)[:\s]+([^\n,]+)')
        incorporation_date = self._extract_field(row_text, r'(?:Incorporated|incorporated|Incorporation Date)[:\s]+(\d{1,2}\s+\w+\s+\d{4}|\d{4}-\d{2}-\d{2})')
        address = self._extract_field(row_text, r'(?:Registered Address|registered address|Address)[:\s]+([^\n]+)')
        entity_type = self._extract_field(row_text, r'(?:Company Type|company type|Type)[:\s]+([^\n,]+)')

        oc_url = f"https://opencorporates.com{href}" if href.startswith("/") else href

        return RegistryResult(
            entity_name=name,
            registry_id=company_number,
            status=status,
            jurisdiction=jurisdiction,
            formation_date=incorporation_date,
            entity_type=entity_type,
            address=address,
            raw_data={
                "opencorporates_url": oc_url,
                "row_text": row_text[:500],
            },
        )

    async def get_details(
        self,
        jurisdiction: str,
        company_number: str,
        connect_url: str | None = None,
        browser: Browser | None = None,
    ) -> RegistryResult | None:
        """Get full company details from OpenCorporates."""
        if browser:
            return await self._get_details_with_browser(browser, jurisdiction, company_number)

        if not connect_url:
            raise ValueError("Either connect_url or browser must be provided")

        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(connect_url)
            try:
                return await self._get_details_with_browser(browser, jurisdiction, company_number)
            finally:
                await browser.close()

    async def _get_details_with_browser(
        self,
        browser: Browser,
        jurisdiction: str,
        company_number: str,
    ) -> RegistryResult | None:
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else await context.new_page()

        url = f"{self.COMPANY_URL}/{jurisdiction}/{company_number}"
        await page.goto(url, timeout=30000)
        await self._wait_for_results(page)

        # Extract structured data from the detail page
        body_text = await page.inner_text("body")
        if not body_text or len(body_text) < 50:
            return None

        # Company name from heading
        name = ""
        h1 = await page.query_selector("h1, .company-name, #company-name")
        if h1:
            name = (await h1.text_content() or "").strip()

        # Parse key fields
        status = self._extract_field(body_text, r'(?:Status|Current Status)[:\s]+([^\n]+)')
        incorporation_date = self._extract_field(body_text, r'(?:Incorporation Date|Date of Incorporation|Incorporated)[:\s]+(\d{1,2}\s+\w+\s+\d{4}|\d{4}-\d{2}-\d{2})')
        entity_type = self._extract_field(body_text, r'(?:Company Type|Type)[:\s]+([^\n]+)')
        address = self._extract_field(body_text, r'(?:Registered Address|Registered Office)[:\s]+([^\n]+(?:\n[^\n]+){0,3})')
        registry_url = self._extract_field(body_text, r'(?:Registry Page|Source|Home Company Page)[:\s]+(https?://[^\s\n]+)')
        agent = self._extract_field(body_text, r'(?:Agent Name|Registered Agent)[:\s]+([^\n]+)')

        # Extract officers
        officers = []
        officer_section = await page.query_selector_all(".officer, .officers li, [data-officer]")
        for off in officer_section[:10]:
            off_text = (await off.text_content() or "").strip()
            if off_text:
                officers.append(off_text)

        # Extract previous names
        previous_names = []
        prev_section = await page.query_selector_all(".previous-name, .previous_names li")
        for prev in prev_section[:5]:
            prev_text = (await prev.text_content() or "").strip()
            if prev_text:
                previous_names.append(prev_text)

        return RegistryResult(
            entity_name=name or f"Company {company_number}",
            registry_id=company_number,
            status=status,
            jurisdiction=jurisdiction,
            formation_date=incorporation_date,
            entity_type=entity_type,
            address=address,
            additional_names=previous_names,
            raw_data={
                "opencorporates_url": f"https://opencorporates.com/companies/{jurisdiction}/{company_number}",
                "registry_url": registry_url,
                "agent": agent,
                "officers": officers,
                "page_text": body_text[:3000],
            },
        )

    def _extract_field(self, text: str, pattern: str) -> str | None:
        """Extract a field from text using a regex pattern."""
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).strip()
        return None
