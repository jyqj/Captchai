"""Typed test doubles shared across the suite.

The suite drives production code (``BrowserManager``, ``VisionRouter``,
``resolve_context_options``, the widget solvers) that all take a
:class:`src.core.config.Config`. Historically each test built its own
``SimpleNamespace`` stand-in, which pyright can only accept because the tests
``executionEnvironment`` relaxes ``reportArgumentType``. :func:`fake_config`
centralises that into one ``cast``-typed builder so call sites are typed as
``Config`` and the relaxed rule can be tightened incrementally.

The defaults mirror :func:`src.core.config.load_config` with an empty
environment, so a fake config behaves exactly like the production default; pass
keyword overrides for the fields a given test cares about.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

from src.assets.model_pool import ModelPool
from src.core.config import (
    _PLACEHOLDER_CLOUD_BASE_URL,
    _PLACEHOLDER_CLOUD_MODEL,
    _PLACEHOLDER_LOCAL_MODEL,
    Config,
)
from src.services.browser import BrowserManager

# Faithful copy of the production defaults (load_config with no env set). Kept
# here rather than calling load_config() so a test never depends on the ambient
# process environment.
_CONFIG_DEFAULTS: dict[str, Any] = {
    "server_host": "0.0.0.0",
    "server_port": 8000,
    "client_key": None,
    "admin_key": None,
    # Cloud model
    "cloud_base_url": _PLACEHOLDER_CLOUD_BASE_URL,
    "cloud_api_key": "",
    "cloud_model": _PLACEHOLDER_CLOUD_MODEL,
    "cloud_audio_model": "whisper-1",
    # Local model
    "local_base_url": "http://localhost:30000/v1",
    "local_api_key": "EMPTY",
    "local_model": _PLACEHOLDER_LOCAL_MODEL,
    "cloud_max_concurrency": 4,
    "local_max_concurrency": 0,
    "model_connection_fallback": True,
    "captcha_retries": 3,
    "captcha_timeout": 30,
    "retry_backoff_base": 1.0,
    "retry_backoff_max": 8.0,
    # Browser
    "browser_headless": True,
    "browser_timeout": 30,
    "browser_concurrency": 4,
    "browser_proxyless_concurrency": 4,
    "browser_proxied_concurrency": 4,
    "browser_pool_proxy_concurrency": 4,
    "vision_concurrency": 8,
    "queue_max_size": 128,
    "solve_timeout": 180,
    # Token polling
    "poll_budget": 30,
    "poll_interval": 0.5,
    "poll_budget_passive": 2.0,
    "poll_budget_challenge": 10.0,
    "poll_budget_challenge_ready": 4.0,
    # Vision routing
    "vision_cloud_enabled": True,
    "vision_vote_samples": 3,
    "vision_confidence_threshold": 0.6,
    "vision_tier2_detail": "high",
    "vision_vote_concurrent": True,
    "vision_inline_escalate": True,
    "vision_stitch_grid": True,
    "vision_trust_self_confidence": False,
    # Resource interception
    "resource_block_enabled": True,
    "resource_block_types": "image,media,font,stylesheet",
    "resource_allow_hosts": (
        "hcaptcha.com,challenges.cloudflare.com,google.com,"
        "recaptcha.net,gstatic.com,cloudflare.com"
    ),
    "resource_block_hosts": "",
    # Asset pools
    "session_pool_size": 4,
    "session_max_solves": 8,
    "session_prewarm": False,
    "proxy_cooldown": 120,
    "proxy_max_consecutive_fails": 3,
    "proxy_max_gb": 0.0,
    "proxy_geo_probe": True,
    "proxy_geo_probe_url": "http://ip-api.com/json",
    "browser_runtime": "chromium",
    "browser_runtime_strict": False,
    "camoufox_humanize": True,
    "camoufox_block_webrtc": True,
    "camoufox_os": "",
    # Enterprise hCaptcha
    "enterprise_require_residential": True,
    "enterprise_fresh_context": True,
    "enterprise_require_hardened_runtime": False,
    "enterprise_require_residential_on_task": False,
    "pool_egress_expose_credentials": False,
    # Token-trust verification
    "token_verify_enabled": False,
    "token_verify_secrets": "",
    "token_verify_timeout": 10.0,
    # Human behavior / real-page mode
    "hcaptcha_real_page": False,
    "turnstile_real_page": False,
    "human_mouse_enabled": True,
    "human_mouse_jitter_ms": 80,
    "human_passive_motion_seconds": 1.4,
    "hcaptcha_invisible_motion_seconds": 3.0,
    "hcaptcha_invisible_passive_budget": 4.0,
    "hcaptcha_rqdata_ttl": 30.0,
    "hcaptcha_device_persistence": False,
    # State backend
    "redis_url": None,
    # Billing / budget
    "account_balance_usd": 99999.0,
    "budget_global_cap_usd": None,
    "budget_per_client_cap_usd": None,
    "accounting_window": 100,
}


def fake_config(**overrides: Any) -> Config:
    """Return a ``Config``-typed test double with production defaults.

    ``fake_config(vision_vote_samples=1)`` yields the default config with just
    that field changed. The result is a ``SimpleNamespace`` cast to ``Config``:
    every production consumer reads config via ``getattr(cfg, name, default)``
    or plain attribute access, so a namespace with the same attributes is a
    faithful stand-in without constructing the frozen dataclass.
    """
    fields = {**_CONFIG_DEFAULTS, **overrides}
    return cast(Config, SimpleNamespace(**fields))


def as_browser_manager(double: Any) -> BrowserManager:
    """Type a browser-manager test double as ``BrowserManager`` for call sites.

    The solvers take ``manager: BrowserManager | None`` and only call
    ``await manager.new_context(params)`` (or, for the manager's own unit tests,
    poke ``manager._browser``). A test ``FakeManager`` with that surface is a
    faithful stand-in; this ``cast`` lets the injection site stay typed as
    ``BrowserManager`` so the relaxed ``reportArgumentType`` rule can be dropped.
    """
    return cast(BrowserManager, double)


def as_model_pool(double: Any) -> ModelPool:
    """Type a model-pool test double as ``ModelPool`` for call sites.

    ``ModelInvoker`` and ``VisionRouter`` take ``model_pool: ModelPool`` and only
    call ``pool.get(name)`` (plus ``pool.local`` / ``pool.cloud`` in some paths).
    A test ``FakePool`` exposing that surface is a faithful stand-in; this
    ``cast`` keeps the call site typed as ``ModelPool``.
    """
    return cast(ModelPool, double)
