"""Ontario Business Registry scraper. Needs Browserbase."""

from __future__ import annotations
from playwright.async_api import async_playwright, Browser
from .base import BaseScraper, RegistryResult


class OntarioOBRScraper(BaseScraper):
    registry_name = "Ontario Business Registry"
    jurisdiction = "CA-ON"
    needs_browser = True

    SEARCH_URL = (
        "https://www.appmybizaccount.gov.on.ca/onbis/master/entry.pub"
        "?applicationCode=onbis-master&businessService=registerItemSearch"
    )
    SEARCH_INPUT = "#QueryString"

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

        await page.goto(self.SEARCH_URL, timeout=60000)
        await page.wait_for_timeout(5000)

        await page.fill(self.SEARCH_INPUT, entity_name)
        await page.wait_for_timeout(1000)
        await page.press(self.SEARCH_INPUT, "Enter")
        await page.wait_for_timeout(8000)

        body = await page.inner_text("body")
        return self._parse_results(body)

    def _parse_results(self, text: str) -> list[RegistryResult]:
        results = []
        lines = text.split("\n")
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            # Look for entity name pattern: "NAME (NUMBER)"
            if "(" in line and ")" in line and any(c.isdigit() for c in line):
                # Check if it looks like an entity entry (preceded by "Corporations" or "Business Names")
                name_part = line.rsplit("(", 1)
                if len(name_part) == 2:
                    entity_name = name_part[0].strip()
                    registry_id = name_part[1].rstrip(")").strip()

                    if not entity_name or not registry_id.isdigit():
                        i += 1
                        continue

                    # Scan ahead for status, date, type
                    status = None
                    formation_date = None
                    entity_type = None
                    address = None
                    additional_names = []

                    for j in range(i + 1, min(i + 15, len(lines))):
                        scan_line = lines[j].strip()
                        if scan_line == "Status":
                            if j + 1 < len(lines):
                                status = lines[j + 1].strip()
                        elif "Incorporation Date" in scan_line or "Registration Date" in scan_line:
                            if j + 1 < len(lines):
                                formation_date = lines[j + 1].strip()
                        elif "Business Type" in scan_line:
                            if j + 1 < len(lines):
                                entity_type = lines[j + 1].strip()
                        elif "Previously known as" in scan_line:
                            if j + 1 < len(lines):
                                additional_names.append(lines[j + 1].strip())
                        elif scan_line.endswith(", Canada"):
                            address = scan_line

                        # Stop if we hit another entity
                        if "(" in scan_line and ")" in scan_line and scan_line != line and any(c.isdigit() for c in scan_line):
                            break

                    results.append(RegistryResult(
                        entity_name=entity_name,
                        registry_id=registry_id,
                        status=status,
                        jurisdiction="CA-ON",
                        formation_date=formation_date,
                        entity_type=entity_type,
                        address=address,
                        additional_names=additional_names,
                    ))
            i += 1

        return results
