"""
Mergr.com explorer — login and see what data is available.
Respectful speed: minimum 3s between requests.
"""
import requests
import time
from bs4 import BeautifulSoup

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
})

BASE = "https://mergr.com"
EMAIL = "craig.anderson@hyndlanpartners.com"
PASSWORD = "X9R/N^3RvjtuJ.^"

MIN_DELAY = 3  # seconds between requests


def slow_get(url, **kwargs):
    time.sleep(MIN_DELAY)
    print(f"  GET {url}")
    r = SESSION.get(url, **kwargs)
    print(f"  -> {r.status_code} ({len(r.text)} bytes)")
    return r


def slow_post(url, **kwargs):
    time.sleep(MIN_DELAY)
    print(f"  POST {url}")
    r = SESSION.post(url, **kwargs)
    print(f"  -> {r.status_code} ({len(r.text)} bytes)")
    return r


def login():
    print("Step 1: Load login page...")
    r = slow_get(f"{BASE}/login")

    soup = BeautifulSoup(r.text, "html.parser")
    form = soup.find("form", id="loginform")
    if not form:
        # Try any form on the page
        form = soup.find("form")

    # Look for CSRF token or hidden fields
    hidden_fields = {}
    if form:
        for inp in form.find_all("input", type="hidden"):
            name = inp.get("name")
            val = inp.get("value", "")
            if name:
                hidden_fields[name] = val
                print(f"  Hidden field: {name}={val[:50]}...")

    # Find form action
    action = form.get("action", f"{BASE}/login") if form else f"{BASE}/login"
    if action and not action.startswith("http"):
        action = BASE + action
    print(f"  Form action: {action}")

    print("\nStep 2: Submit login...")
    payload = {
        **hidden_fields,
        "email": EMAIL,
        "password": PASSWORD,
    }
    r = slow_post(action, data=payload, allow_redirects=True)

    # Check if login worked
    if "logout" in r.text.lower() or "dashboard" in r.text.lower() or "account" in r.text.lower():
        print("  LOGIN SUCCESS")
    else:
        # Check for error messages
        soup = BeautifulSoup(r.text, "html.parser")
        errors = soup.find_all(class_=lambda c: c and ("error" in c or "alert" in c))
        for e in errors:
            print(f"  Error: {e.get_text(strip=True)}")
        print(f"  Final URL: {r.url}")
        # Save page for debugging
        with open("/Users/craiganderson/Dropbox/dev/on-testing/data-engine/mergr_debug.html", "w") as f:
            f.write(r.text)
        print("  Saved page to mergr_debug.html for inspection")

    return r


def explore_structure(page_html):
    """After login, look at navigation and available pages."""
    soup = BeautifulSoup(page_html, "html.parser")

    print("\nStep 3: Explore site structure...")

    # Find nav links
    nav_links = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(strip=True)
        if href.startswith("/") and text and len(text) < 60:
            nav_links.add((href, text))

    print(f"\n  Found {len(nav_links)} internal links:")
    for href, text in sorted(nav_links):
        print(f"    {href} — {text}")


def explore_known_pages():
    """Try known Mergr URL patterns to find the data structure."""
    urls_to_try = [
        f"{BASE}/",
        f"{BASE}/dashboard",
        f"{BASE}/search",
        f"{BASE}/account",
        f"{BASE}/api",
        f"{BASE}/companies",
        f"{BASE}/deals",
        f"{BASE}/transactions",
    ]
    for url in urls_to_try:
        r = slow_get(url, allow_redirects=True)
        soup = BeautifulSoup(r.text, "html.parser")
        title = soup.title.get_text(strip=True) if soup.title else "(no title)"
        # Look for any interesting content
        print(f"    Title: {title}")
        print(f"    Final URL: {r.url}")
        # Check for API-like JSON
        ct = r.headers.get("content-type", "")
        if "json" in ct:
            print(f"    JSON response!")
        print()


def check_search(keyword="fire protection"):
    """Try a search to see the data format."""
    print(f"\nStep 4: Try search for '{keyword}'...")

    # Try search endpoint
    r = slow_get(f"{BASE}/search?q={keyword}", allow_redirects=True)
    print(f"  Final URL: {r.url}")
    print(f"  Content-Type: {r.headers.get('content-type', '')}")

    # Save for inspection
    with open("/Users/craiganderson/Dropbox/dev/on-testing/data-engine/mergr_search.html", "w") as f:
        f.write(r.text)
    print(f"  Saved to mergr_search.html ({len(r.text)} bytes)")

    # Look for any data in the page
    soup = BeautifulSoup(r.text, "html.parser")
    scripts = soup.find_all("script")
    for s in scripts:
        text = s.get_text()
        if keyword.lower() in text.lower() or "company" in text.lower() or "result" in text.lower():
            print(f"  Found data in script tag ({len(text)} chars):")
            print(f"    {text[:500]}")
            print()


def search_company(name):
    """Search for a company by name and extract profile data."""
    print(f"\nSearching for: {name}")
    params = {"company[keyword]": name}
    r = slow_get(f"{BASE}/companies", params=params, allow_redirects=True)

    soup = BeautifulSoup(r.text, "html.parser")

    # Find company profile links in results
    results = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(strip=True)
        if href.startswith("https://mergr.com/company/") and text and len(text) > 2:
            if (href, text) not in results:
                results.append((href, text))

    print(f"  Found {len(results)} results:")
    for href, text in results[:10]:
        print(f"    {href} — {text}")

    return results


def get_company_profile(url):
    """Fetch and parse a company profile page."""
    r = slow_get(url, allow_redirects=True)
    soup = BeautifulSoup(r.text, "html.parser")

    profile = {"url": r.url}

    # Company name
    h1 = soup.find("h1")
    if h1:
        profile["name"] = h1.get_text(strip=True).replace("– Company Overview", "").strip()

    # Extract key facts from the summary section
    for h4 in soup.find_all("h4"):
        label = h4.get_text(strip=True).lower()
        # Get the next sibling or parent's text
        parent = h4.find_parent()
        if parent:
            sibling_text = ""
            for sib in h4.find_next_siblings():
                t = sib.get_text(strip=True)
                if t:
                    sibling_text = t
                    break
            if not sibling_text and parent:
                # Try getting all text after the h4 within the parent
                texts = parent.get_text(strip=True)
                sibling_text = texts.replace(h4.get_text(strip=True), "").strip()

            if "sector" in label:
                profile["sector"] = sibling_text
            elif "revenue" in label:
                profile["revenue"] = sibling_text
            elif "employees" in label:
                profile["employees"] = sibling_text
            elif "established" in label or "founded" in label:
                profile["founded"] = sibling_text

    # M&A summary table
    tables = soup.find_all("table")
    for table in tables:
        rows = table.find_all("tr")
        for row in rows:
            cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
            row_text = " | ".join(cells)
            if "buy" in row_text.lower() or "sell" in row_text.lower():
                if "buy" in row_text.lower():
                    profile["ma_buy"] = row_text
                if "sell" in row_text.lower():
                    profile["ma_sell"] = row_text

    # Investors
    for table in tables:
        header = table.find("th")
        if header and "investor" in header.get_text(strip=True).lower():
            profile["investors_table"] = True

    # PE-backed FAQ
    for h4 in soup.find_all("h4"):
        if "pe-backed" in h4.get_text(strip=True).lower():
            answer = h4.find_next("p") or h4.find_next("div")
            if answer:
                profile["pe_backed"] = answer.get_text(strip=True)[:200]
                break

    return profile


if __name__ == "__main__":
    r = login()

    # Test with our 4 known buyers
    test_buyers = ["Waterland", "PTSG", "Franchise Brands", "NexPhase Capital"]

    for buyer_name in test_buyers:
        results = search_company(buyer_name)
        if results:
            # Fetch first result profile
            profile = get_company_profile(results[0][0])
            print(f"\n  PROFILE DATA:")
            for k, v in profile.items():
                print(f"    {k}: {v}")
