"""Delaware Division of Corporations scraper. Needs Browserbase for ASP.NET form."""

from __future__ import annotations
from playwright.async_api import async_playwright, Browser
from .base import BaseScraper, RegistryResult


class DelawareDOSScraper(BaseScraper):
    registry_name = "Delaware Division of Corporations"
    jurisdiction = "US-DE"
    needs_browser = True

    SEARCH_URL = "https://icis.corp.delaware.gov/ecorp/entitysearch/namesearch.aspx"
    NAME_FIELD = "#ctl00_ContentPlaceHolder1_frmEntityName"
    FILE_FIELD = "#ctl00_ContentPlaceHolder1_frmFileNumber"
    SUBMIT_BTN = "#ctl00_ContentPlaceHolder1_btnSubmit"

    async def search(
        self,
        entity_name: str,
        connect_url: str | None = None,
        browser: Browser | None = None,
    ) -> list[RegistryResult]:
        if browser:
            return await self._search_with_browser(browser, entity_name)

        if not connect_url:
            raise ValueError("Either connect_url or browser must be provided")

        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(connect_url)
            try:
                return await self._search_with_browser(browser, entity_name)
            finally:
                await browser.close()

    async def _search_with_browser(self, browser: Browser, entity_name: str) -> list[RegistryResult]:
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else await context.new_page()

        await page.goto(self.SEARCH_URL, timeout=30000)
        await page.wait_for_timeout(3000)

        await page.fill(self.NAME_FIELD, entity_name)
        await page.wait_for_timeout(500)
        await page.click(self.SUBMIT_BTN)
        await page.wait_for_timeout(5000)

        # Parse results — only rows where first cell is a numeric file number
        results = []
        rows = await page.query_selector_all("table tr")
        for row in rows:
            cells = await row.query_selector_all("td")
            if len(cells) >= 2:
                file_number = (await cells[0].text_content() or "").strip()
                if not file_number.isdigit():
                    continue

                name_el = await cells[1].query_selector("a")
                if name_el:
                    name = (await name_el.text_content() or "").strip()
                else:
                    name = (await cells[1].text_content() or "").strip()

                if name:
                    results.append(RegistryResult(
                        entity_name=name,
                        registry_id=file_number,
                        jurisdiction="US-DE",
                        raw_data={"file_number": file_number},
                    ))

        return results

    async def get_details(
        self,
        entity_id: str,
        connect_url: str | None = None,
        browser: Browser | None = None,
    ) -> RegistryResult | None:
        """Get details by file number. Requires browser."""
        if not connect_url and not browser:
            raise ValueError("Either connect_url or browser must be provided")

        if browser:
            return await self._get_details_with_browser(browser, entity_id)

        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(connect_url)
            try:
                return await self._get_details_with_browser(browser, entity_id)
            finally:
                await browser.close()

    async def _get_details_with_browser(self, browser: Browser, file_number: str) -> RegistryResult | None:
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else await context.new_page()

        # Search by name first, then click through (file number field had issues)
        await page.goto(self.SEARCH_URL, timeout=30000)
        await page.wait_for_timeout(3000)

        await page.fill(self.FILE_FIELD, file_number)
        await page.wait_for_timeout(500)
        await page.click(self.SUBMIT_BTN)
        await page.wait_for_timeout(5000)

        # Click the entity link
        entity_link = await page.query_selector("table td a")
        if not entity_link:
            return None

        entity_name = (await entity_link.text_content() or "").strip()
        await entity_link.click()
        await page.wait_for_timeout(5000)

        # Parse detail page
        body = await page.inner_text("body")
        return RegistryResult(
            entity_name=entity_name,
            registry_id=file_number,
            jurisdiction="US-DE",
            raw_data={"detail_text": body[:3000]},
        )
