"""
Test: fetch the transaction search listing page and save HTML for inspection.
"""
import asyncio
from playwright.async_api import async_playwright

EMAIL = "craig.anderson@hyndlandpartners.com"
PASSWORD = "X9R/N^3RvjtuJ.^"


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        )
        page = await context.new_page()

        await page.goto("https://mergr.com/login", wait_until="networkidle")
        await page.fill('input[name="username"]', EMAIL)
        await page.fill('input[name="password"]', PASSWORD)
        await page.click('button[type="submit"]')
        await page.wait_for_url("**/dashboard**", timeout=30000)
        print("Logged in OK")
        await asyncio.sleep(5)

        # Fetch transaction listing page
        await page.goto("https://mergr.com/transactions/search", wait_until="networkidle", timeout=30000)

        for _ in range(10):
            html = await page.content()
            if "awswaf" not in html and len(html) > 5000:
                break
            await asyncio.sleep(3)

        html = await page.content()
        print(f"Page length: {len(html)}")

        with open("/tmp/mergr_txn_list.html", "w") as f:
            f.write(html)
        print("Saved to /tmp/mergr_txn_list.html")

        # Also fetch page 2 to see pagination
        await page.goto("https://mergr.com/transactions/search?page=2", wait_until="networkidle", timeout=30000)
        for _ in range(10):
            html = await page.content()
            if "awswaf" not in html and len(html) > 5000:
                break
            await asyncio.sleep(3)
        html = await page.content()
        with open("/tmp/mergr_txn_list_p2.html", "w") as f:
            f.write(html)
        print(f"Page 2 saved ({len(html)} bytes)")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
