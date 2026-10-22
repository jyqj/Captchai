"""Tests for the pydantic-settings migration of ``src.core.config``.

These pin the contract the rest of the application relies on:

* ``load_config`` keeps returning a frozen ``Config`` with the historical
  attribute names, types, and defaults,
* every legacy environment alias chain still resolves with the old precedence,
* booleans keep the documented word set (and ``StrictBool`` + a targeted
  ``BeforeValidator`` still reject anything else *naming the variable*),
* malformed values raise ``ValueError`` naming every offending env var, and
* the module-level ``config`` object is built at import time.

``tests/test_phase0_regressions.py`` covers the external error contract; this
file covers the settings layer directly.
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    _ = sys.path.insert(0, str(PROJECT_ROOT))

import src.core.config as config_mod  # noqa: E402
from src.core.config import Settings, load_config  # noqa: E402

# Every environment variable the Settings model can read (canonical name plus
# legacy aliases), so each test starts from defaults regardless of the host.
_CONFIG_ENV_NAMES: frozenset[str] = frozenset(
    {name.upper() for name in Settings.model_fields}
    | {
        alias
        for aliases in config_mod._FIELD_ENV_ALIASES.values()
        for alias in aliases
    }
)


@pytest.fixture()
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Remove every config env var for the duration of one test."""
    for name in _CONFIG_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


# ---------------------------------------------------------------------------
# Shape / defaults
# ---------------------------------------------------------------------------


def test_import_time_config_exists() -> None:
    # ``tests/test_report.py`` reloads ``src.core.config``, which rebinds the
    # module's classes; compare against the module's CURRENT class object
    # rather than the one captured by this module's import.
    assert type(config_mod.config) is config_mod.Config


def test_defaults_match_legacy_loader(clean_env: pytest.MonkeyPatch) -> None:
    cfg = load_config()

    # The Settings model and the frozen value object must stay field-for-field
    # identical — all of src/ and tests/ read ``config.<attr>``.
    assert set(config_mod.Config.__dataclass_fields__) == set(
        config_mod.Settings.model_fields
    )

    assert (cfg.server_host, cfg.server_port) == ("0.0.0.0", 8000)
    assert cfg.client_key is None
    assert cfg.allow_anonymous is False
    assert cfg.admin_key is None
    assert cfg.rate_limit_rpm == 60
    assert cfg.ssrf_protect is True
    assert cfg.cloud_base_url == "https://your-openai-compatible-endpoint/v1"
    assert cfg.cloud_api_key == ""
    assert cfg.cloud_model == "gpt-5.4"
    assert cfg.cloud_audio_model == "whisper-1"
    assert cfg.local_base_url == "http://localhost:30000/v1"
    assert cfg.local_api_key == "EMPTY"
    assert cfg.local_model == "Qwen/Qwen3.5-2B"
    assert cfg.cloud_max_concurrency == 4
    assert cfg.local_max_concurrency == 0
    assert cfg.browser_concurrency == 4
    assert cfg.browser_proxyless_concurrency == 4
    assert cfg.browser_proxied_concurrency == 4
    assert cfg.browser_pool_proxy_concurrency == 4
    assert cfg.queue_max_size == 128
    assert cfg.solve_timeout == 180
    assert cfg.vision_tier2_detail == "high"
    assert cfg.browser_runtime == "chromium"
    assert cfg.proxy_max_gb == 0.0
    assert cfg.redis_url is None
    assert cfg.account_balance_usd == 99999.0
    assert cfg.budget_global_cap_usd is None
    assert cfg.budget_per_client_cap_usd is None
    assert cfg.accounting_window == 100
    assert cfg.resource_block_types == "image,media,font,stylesheet"
    assert cfg.resource_allow_hosts == (
        "hcaptcha.com,challenges.cloudflare.com,google.com,recaptcha.net,"
        "gstatic.com,cloudflare.com"
    )


def test_config_is_frozen_and_keeps_legacy_properties(
    clean_env: pytest.MonkeyPatch,
) -> None:
    clean_env.setenv("CAPTCHA_BASE_URL", "https://cloud.example/v1")
    clean_env.setenv("CAPTCHA_MODEL", "model-x")
    clean_env.setenv("CAPTCHA_MULTIMODAL_MODEL", "model-local")
    cfg = load_config()

    assert cfg.captcha_base_url == cfg.cloud_base_url
    assert cfg.captcha_api_key == cfg.cloud_api_key
    assert cfg.captcha_model == cfg.cloud_model
    assert cfg.captcha_multimodal_model == cfg.local_model
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.server_port = 1  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Legacy alias chains
# ---------------------------------------------------------------------------


def test_captcha_aliases_feed_both_backends(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("CAPTCHA_BASE_URL", "https://legacy.example/v1")
    clean_env.setenv("CAPTCHA_API_KEY", "legacy-key")
    clean_env.setenv("CAPTCHA_MODEL", "legacy-model")
    clean_env.setenv("CAPTCHA_MULTIMODAL_MODEL", "legacy-mm")

    cfg = load_config()
    assert cfg.cloud_base_url == "https://legacy.example/v1"
    assert cfg.local_base_url == "https://legacy.example/v1"
    assert cfg.cloud_api_key == "legacy-key"
    assert cfg.local_api_key == "legacy-key"
    assert cfg.cloud_model == "legacy-model"
    assert cfg.local_model == "legacy-mm"


def test_backend_specific_names_win_over_captcha_aliases(
    clean_env: pytest.MonkeyPatch,
) -> None:
    clean_env.setenv("CAPTCHA_BASE_URL", "https://legacy.example/v1")
    clean_env.setenv("CAPTCHA_API_KEY", "legacy-key")
    clean_env.setenv("CLOUD_BASE_URL", "https://cloud.example/v1")
    clean_env.setenv("CLOUD_API_KEY", "cloud-key")
    clean_env.setenv("LOCAL_BASE_URL", "http://local.example/v1")
    clean_env.setenv("LOCAL_API_KEY", "local-key")
    clean_env.setenv("LOCAL_MODEL", "local-model")

    cfg = load_config()
    assert cfg.cloud_base_url == "https://cloud.example/v1"
    assert cfg.cloud_api_key == "cloud-key"
    assert cfg.local_base_url == "http://local.example/v1"
    assert cfg.local_api_key == "local-key"
    assert cfg.local_model == "local-model"


def test_solve_timeout_alias_precedence(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("SOLVE_TIMEOUT", "42")
    assert load_config().solve_timeout == 42

    clean_env.setenv("CAPTCHA_SOLVE_TIMEOUT", "7")
    assert load_config().solve_timeout == 7


def test_browser_concurrency_alias_chain(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("BROWSER_CONCURRENCY", "9")
    cfg = load_config()
    assert cfg.browser_concurrency == 9
    assert cfg.browser_proxyless_concurrency == 9
    assert cfg.browser_proxied_concurrency == 9
    assert cfg.browser_pool_proxy_concurrency == 9

    clean_env.setenv("BROWSER_PROXIED_CONCURRENCY", "7")
    cfg = load_config()
    assert cfg.browser_proxyless_concurrency == 9  # still falls back
    assert cfg.browser_proxied_concurrency == 7
    assert cfg.browser_pool_proxy_concurrency == 7  # proxied wins over global

    clean_env.setenv("BROWSER_POOL_PROXY_CONCURRENCY", "3")
    cfg = load_config()
    assert cfg.browser_pool_proxy_concurrency == 3
    assert cfg.browser_proxied_concurrency == 7


# ---------------------------------------------------------------------------
# Typed parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["true", "1", "yes", "on", " TRUE "])
def test_bool_word_set_accepts_true(
    clean_env: pytest.MonkeyPatch, raw: str
) -> None:
    clean_env.setenv("ALLOW_ANONYMOUS", raw)
    assert load_config().allow_anonymous is True


@pytest.mark.parametrize("raw", ["false", "0", "no", "off", " Off "])
def test_bool_word_set_accepts_false(
    clean_env: pytest.MonkeyPatch, raw: str
) -> None:
    clean_env.setenv("ALLOW_ANONYMOUS", raw)
    assert load_config().allow_anonymous is False


@pytest.mark.parametrize(
    ("env_name", "raw", "attr"),
    [
        ("BROWSER_DISABLE_SANDBOX", "", None),
        ("BROWSER_DISABLE_SANDBOX", "yes", True),
        ("BROWSER_DISABLE_SANDBOX", "0", False),
        ("BUDGET_GLOBAL_CAP_USD", "", None),
        ("BUDGET_GLOBAL_CAP_USD", "12.5", 12.5),
        ("BUDGET_PER_CLIENT_CAP_USD", "3", 3.0),
    ],
)
def test_optional_values(
    clean_env: pytest.MonkeyPatch, env_name: str, raw: str, attr: object
) -> None:
    clean_env.setenv(env_name, raw)
    assert getattr(load_config(), env_name.lower()) == attr


def test_proxy_max_gb_empty_means_zero(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("PROXY_MAX_GB", "")
    assert load_config().proxy_max_gb == 0.0
    clean_env.setenv("PROXY_MAX_GB", "2.5")
    assert load_config().proxy_max_gb == 2.5


def test_string_normalisation(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("BROWSER_RUNTIME", "  CamouFox ")
    clean_env.setenv("VISION_TIER2_DETAIL", "HIGH")
    clean_env.setenv("CAMOUFOX_OS", " windows ")
    clean_env.setenv("CLIENT_KEY", "  secret  ")
    clean_env.setenv("REDIS_URL", "   ")
    cfg = load_config()
    assert cfg.browser_runtime == "camoufox"
    assert cfg.vision_tier2_detail == "high"
    assert cfg.camoufox_os == "windows"
    assert cfg.client_key == "secret"
    assert cfg.redis_url is None


def test_case_insensitive_env(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("server_port", "9001")
    assert load_config().server_port == 9001


# ---------------------------------------------------------------------------
# Errors: every problem names its environment variable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("env_name", "bad_value"),
    [
        ("SERVER_PORT", "abc"),
        ("SERVER_PORT", "0"),
        ("ENTERPRISE_REQUIRE_RESIDENTIAL", "ture"),
        ("BUDGET_GLOBAL_CAP_USD", "oops"),
        ("BUDGET_GLOBAL_CAP_USD", "-1"),
        ("BROWSER_CONCURRENCY", "0"),
        ("QUEUE_MAX_SIZE", "0"),
        ("BROWSER_RUNTIME", "opera"),
        ("VISION_TIER2_DETAIL", "medium"),
        ("PROXY_MAX_GB", "oops"),
        ("RATE_LIMIT_RPM", "-1"),
        ("SESSION_IDLE_TTL_SECONDS", "-1"),
        ("SESSION_MAX_AGE_SECONDS", "-1"),
    ],
)
def test_malformed_value_names_env_var(
    clean_env: pytest.MonkeyPatch, env_name: str, bad_value: str
) -> None:
    clean_env.setenv(env_name, bad_value)
    with pytest.raises(ValueError, match=env_name):
        load_config()


def test_errors_are_aggregated(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("SERVER_PORT", "0")
    clean_env.setenv("BROWSER_CONCURRENCY", "0")
    clean_env.setenv("QUEUE_MAX_SIZE", "0")
    clean_env.setenv("SOLVE_TIMEOUT", "0")

    with pytest.raises(ValueError) as excinfo:
        load_config()

    message = str(excinfo.value)
    assert message.startswith("invalid configuration:")
    for name in (
        "SERVER_PORT",
        "BROWSER_CONCURRENCY",
        "QUEUE_MAX_SIZE",
        "SOLVE_TIMEOUT",
    ):
        assert name in message


def test_solve_timeout_error_names_the_alias_actually_set(
    clean_env: pytest.MonkeyPatch,
) -> None:
    clean_env.setenv("SOLVE_TIMEOUT", "0")
    with pytest.raises(ValueError, match="SOLVE_TIMEOUT: expected > 0"):
        load_config()
