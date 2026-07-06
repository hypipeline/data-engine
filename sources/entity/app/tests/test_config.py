"""
Faithful Python port of php/tests/test_config.php.

Verifies that config.load_config() passes through all required settings keys
(Section 1 + 2), and that the model-dispatch predicate (_is_openai_model, the
Python equivalent of EntityLookup::isOpenAIModel) classifies models correctly
(Section 3). Pure/offline — no network.

conft.py puts the port dir on sys.path.
"""
import json
import os

import pytest

import config as config_mod
from agent import EntityLookup

# settings.json lives next to config.py in the port dir
_SETTINGS_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "settings.json")

with open(_SETTINGS_PATH) as _f:
    SETTINGS = json.load(_f)

CONFIG = config_mod.load_config()

# Exact same required-keys list as test_config.php.
REQUIRED_KEYS = [
    "anthropic_api_key",
    "openai_api_key",
    "browserbase_api_key",
    "browserbase_project_id",
    "model",
    "sec_user_agent",
    "max_page_chars",
    "max_entity_names",
    "max_ciks",
    "max_ownership_levels",
    "brightdata_api_key",
    "brightdata_zone",
    "companies_house_api_key",
    "northdata_email",
    "northdata_password",
    "blocked_entity_names",
]


# ── Section 1: all required keys present in config output ────────────────────
@pytest.mark.parametrize("key", REQUIRED_KEYS)
def test_required_key_present(key):
    assert key in CONFIG, f"Key '{key}' must exist in config output"


# ── Section 2: config values match settings.json ────────────────────────────
# Mirror the PHP: for numeric values PHP settings.json holds strings but
# config.php casts to int, so int-cast the settings value when config is int.
def _settings_val_for(key):
    settings_val = SETTINGS[key]
    config_val = CONFIG.get(key)
    if isinstance(config_val, int) and not isinstance(config_val, bool) and isinstance(settings_val, str):
        settings_val = int(settings_val)
    return settings_val


@pytest.mark.parametrize("key", [k for k in REQUIRED_KEYS if k in SETTINGS])
def test_config_value_matches_settings(key):
    assert CONFIG.get(key) == _settings_val_for(key), (
        f"Key '{key}' value must match settings.json"
    )


# ── Section 3: model dispatch (isOpenAIModel) ───────────────────────────────
def _lookup_with_model(model):
    cfg = dict(CONFIG)
    cfg["model"] = model
    return EntityLookup(cfg)


def test_claude_model_is_not_openai():
    assert _lookup_with_model("claude-sonnet-4-6")._is_openai_model() is False


def test_gpt4o_is_openai():
    assert _lookup_with_model("gpt-4o")._is_openai_model() is True


def test_o3_is_openai():
    assert _lookup_with_model("o3")._is_openai_model() is True


def test_o4_mini_is_openai():
    assert _lookup_with_model("o4-mini")._is_openai_model() is True
