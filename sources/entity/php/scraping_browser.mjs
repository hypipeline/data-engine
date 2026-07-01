import { chromium } from 'playwright-core';

const url = process.argv[2];
const wantJson = process.argv[3] === '--json';

if (!url) {
    console.error('Usage: node scraping_browser.mjs <url> [--json]');
    process.exit(1);
}

const SBR_WS = process.env.SBR_WS;
if (!SBR_WS) {
    console.error('Error: SBR_WS environment variable not set');
    process.exit(1);
}

try {
    const browser = await chromium.connectOverCDP(SBR_WS);
    try {
        const page = await browser.newPage();
        await page.goto(url, { waitUntil: 'load', timeout: 60000 });
        await page.waitForLoadState('networkidle', { timeout: 15000 }).catch(() => {});
        await page.waitForTimeout(2000);

        // Check for CAPTCHA and wait for it to be solved (up to 90s)
        for (let i = 0; i < 45; i++) {
            const hasCaptcha = await page.evaluate(() => {
                const html = document.documentElement.innerHTML;
                return html.includes('captcha_frame') ||
                       html.includes('h-captcha') ||
                       html.includes('hcaptcha') ||
                       html.includes('g-recaptcha') ||
                       html.includes('cf-turnstile');
            });
            if (!hasCaptcha) break;
            await page.waitForTimeout(2000);
        }

        const text = await page.evaluate(() => {
            if (document.body) return document.body.innerText;
            return document.documentElement?.textContent || '';
        });
        if (wantJson) {
            const html = await page.content();
            console.log(JSON.stringify({ text, html }));
        } else {
            console.log(text);
        }
    } finally {
        await browser.close();
    }
} catch (err) {
    console.error(`Error: ${err.message}`);
    process.exit(1);
}
