"""Entity Lookup v3b (Python) — configuration.

Faithful port of php/config.php. Loads from settings.json but env vars win (so the
recognised secrets can be injected via entity.secrets.env, exactly like the PHP path).
"""
import json
import os
import pathlib

_SETTINGS = pathlib.Path(__file__).parent / "settings.json"


def load_config() -> dict:
    settings = {}
    if _SETTINGS.exists():
        try:
            settings = json.loads(_SETTINGS.read_text())
        except Exception:
            settings = {}

    def g(key, env):
        return os.environ.get(env) or settings.get(key, "")

    return {
        "anthropic_api_key": g("anthropic_api_key", "ANTHROPIC_API_KEY"),
        "openai_api_key": g("openai_api_key", "OPENAI_API_KEY"),
        "browserbase_api_key": g("browserbase_api_key", "BROWSERBASE_API_KEY"),
        "browserbase_project_id": g("browserbase_project_id", "BROWSERBASE_PROJECT_ID"),
        "brightdata_api_key": g("brightdata_api_key", "BRIGHTDATA_API_KEY"),
        "brightdata_zone": settings.get("brightdata_zone", "web_unlocker1"),
        "brightdata_scraping_browser_ws": g("brightdata_scraping_browser_ws", "BRIGHTDATA_SCRAPING_BROWSER_WS"),
        "twocaptcha_api_key": g("twocaptcha_api_key", "TWOCAPTCHA_API_KEY"),
        "companies_house_api_key": g("companies_house_api_key", "COMPANIES_HOUSE_API_KEY"),
        "northdata_email": g("northdata_email", "NORTHDATA_EMAIL"),
        "northdata_password": g("northdata_password", "NORTHDATA_PASSWORD"),
        "model": settings.get("model", "claude-sonnet-4-6"),
        "sec_user_agent": settings.get("sec_user_agent", "EntityLookup/1.0 craig@example.com"),
        "max_page_chars": int(settings.get("max_page_chars", 8000)),
        "max_entity_names": int(settings.get("max_entity_names", 5)),
        "max_ciks": int(settings.get("max_ciks", 5)),
        "max_ownership_levels": int(settings.get("max_ownership_levels", 10)),
        "blocked_entity_names": settings.get("blocked_entity_names", []),
    }
