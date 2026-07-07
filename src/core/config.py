"""Environment-driven application configuration.

Two model backends are supported:

  Cloud model  — a remote OpenAI-compatible API (e.g. gpt-5.4 via a hosted
                 endpoint).  Used as the powerful multimodal backbone for
                 tasks like audio transcription.

  Local model  — a self-hosted model served via SGLang, vLLM, or any
                 OpenAI-compatible server (e.g. Qwen3.5-2B on localhost).
                 Used for high-throughput image recognition / classification.

Both backends expose ``/v1/chat/completions``; the only difference is the
base URL, API key, and model name.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    server_host: str
    server_port: int

    # Auth: YesCaptcha clientKey
    client_key: str | None

    # ── Cloud model (remote API) ──
    cloud_base_url: str
    cloud_api_key: str
    cloud_model: str

    # ── Local model (self-hosted via SGLang / vLLM) ──
    local_base_url: str
    local_api_key: str
    local_model: str

    captcha_retries: int
    captcha_timeout: int

    # Playwright browser
    browser_headless: bool
    browser_timeout: int  # seconds

    # ── Runtime / concurrency (orchestration plane) ──
    # Browser solves and pure-vision (classification/recognition) calls draw from
    # separate concurrency pools so a burst of image tasks can't starve browser
    # solvers. See services/task_manager.py.
    browser_concurrency: int
    vision_concurrency: int
    # Bounded task queue admission control; 0 disables the cap.
    queue_max_size: int
    # Per-task wall-clock budget (seconds) enforced by the scheduler.
    solve_timeout: int

    # ── Token polling (unified budget) ──
    # Single poll budget shared by hCaptcha / Turnstile widget-token extraction.
    poll_budget: int  # seconds
    poll_interval: float  # seconds between checks

    # ── Vision routing (parsing plane) ──
    # When true, hard grid challenges may escalate to the powerful cloud model.
    vision_cloud_enabled: bool
    # Self-consistency: sample the model this many times and majority-vote per
    # tile when confidence is low. 1 disables voting.
    vision_vote_samples: int
    # Escalate / re-vote when reported confidence is below this threshold.
    vision_confidence_threshold: float
    # OpenAI image `detail` for tier-2 (hard) grids.
    vision_tier2_detail: str

    # ── Asset pools ──
    session_pool_size: int
    session_max_solves: int
    session_prewarm: bool
    proxy_cooldown: int  # seconds
    proxy_max_consecutive_fails: int
    token_cache_ttl: int  # seconds
    # Playwright runtime flavour: "chromium" (stock) | "rebrowser" | "camoufox".
    browser_runtime: str

    # ── State backend ──
    # Redis URL for the persistent task store; None => in-memory fallback.
    redis_url: str | None

    # ── Billing / budget (consumption plane) ──
    # Starting credit surfaced by getBalance; the ledger's spend is subtracted.
    account_balance_usd: float
    # Optional hard spend caps enforced by BudgetGuard before a cloud call.
    budget_global_cap_usd: float | None
    budget_per_client_cap_usd: float | None

    # ── Convenience aliases (backward-compat) ──

    @property
    def captcha_base_url(self) -> str:
        return self.cloud_base_url

    @property
    def captcha_api_key(self) -> str:
        return self.cloud_api_key

    @property
    def captcha_model(self) -> str:
        return self.cloud_model

    @property
    def captcha_multimodal_model(self) -> str:
        return self.local_model


def load_config() -> Config:
    return Config(
        server_host=os.environ.get("SERVER_HOST", "0.0.0.0"),
        server_port=int(os.environ.get("SERVER_PORT", "8000")),
        client_key=os.environ.get("CLIENT_KEY", "").strip() or None,
        # Cloud model
        cloud_base_url=os.environ.get(
            "CLOUD_BASE_URL",
            os.environ.get("CAPTCHA_BASE_URL", "https://your-openai-compatible-endpoint/v1"),
        ),
        cloud_api_key=os.environ.get(
            "CLOUD_API_KEY",
            os.environ.get("CAPTCHA_API_KEY", ""),
        ),
        cloud_model=os.environ.get(
            "CLOUD_MODEL",
            os.environ.get("CAPTCHA_MODEL", "gpt-5.4"),
        ),
        # Local model
        local_base_url=os.environ.get(
            "LOCAL_BASE_URL",
            os.environ.get("CAPTCHA_BASE_URL", "http://localhost:30000/v1"),
        ),
        local_api_key=os.environ.get(
            "LOCAL_API_KEY",
            os.environ.get("CAPTCHA_API_KEY", "EMPTY"),
        ),
        local_model=os.environ.get(
            "LOCAL_MODEL",
            os.environ.get("CAPTCHA_MULTIMODAL_MODEL", "Qwen/Qwen3.5-2B"),
        ),
        captcha_retries=int(os.environ.get("CAPTCHA_RETRIES", "3")),
        captcha_timeout=int(os.environ.get("CAPTCHA_TIMEOUT", "30")),
        browser_headless=os.environ.get("BROWSER_HEADLESS", "true").strip().lower()
        in {"1", "true", "yes"},
        browser_timeout=int(os.environ.get("BROWSER_TIMEOUT", "30")),
        # Runtime / concurrency
        browser_concurrency=int(os.environ.get("BROWSER_CONCURRENCY", "4")),
        vision_concurrency=int(os.environ.get("VISION_CONCURRENCY", "8")),
        queue_max_size=int(os.environ.get("QUEUE_MAX_SIZE", "128")),
        solve_timeout=int(
            os.environ.get("CAPTCHA_SOLVE_TIMEOUT", os.environ.get("SOLVE_TIMEOUT", "180"))
        ),
        # Token polling
        poll_budget=int(os.environ.get("POLL_BUDGET", "30")),
        poll_interval=float(os.environ.get("POLL_INTERVAL", "0.5")),
        # Vision routing
        vision_cloud_enabled=os.environ.get("VISION_CLOUD_ENABLED", "true")
        .strip()
        .lower()
        in {"1", "true", "yes"},
        vision_vote_samples=int(os.environ.get("VISION_VOTE_SAMPLES", "3")),
        vision_confidence_threshold=float(
            os.environ.get("VISION_CONFIDENCE_THRESHOLD", "0.6")
        ),
        vision_tier2_detail=os.environ.get("VISION_TIER2_DETAIL", "high"),
        # Asset pools
        session_pool_size=int(os.environ.get("SESSION_POOL_SIZE", "4")),
        session_max_solves=int(os.environ.get("SESSION_MAX_SOLVES", "8")),
        session_prewarm=os.environ.get("SESSION_PREWARM", "false").strip().lower()
        in {"1", "true", "yes"},
        proxy_cooldown=int(os.environ.get("PROXY_COOLDOWN", "120")),
        proxy_max_consecutive_fails=int(
            os.environ.get("PROXY_MAX_CONSECUTIVE_FAILS", "3")
        ),
        token_cache_ttl=int(os.environ.get("TOKEN_CACHE_TTL", "110")),
        browser_runtime=os.environ.get("BROWSER_RUNTIME", "chromium").strip().lower(),
        # State backend
        redis_url=os.environ.get("REDIS_URL", "").strip() or None,
        # Billing / budget
        account_balance_usd=float(os.environ.get("ACCOUNT_BALANCE_USD", "99999.0")),
        budget_global_cap_usd=_optional_float(os.environ.get("BUDGET_GLOBAL_CAP_USD")),
        budget_per_client_cap_usd=_optional_float(
            os.environ.get("BUDGET_PER_CLIENT_CAP_USD")
        ),
    )


def _optional_float(value: str | None) -> float | None:
    if value is None or not value.strip():
        return None
    try:
        return float(value)
    except ValueError:
        return None


config = load_config()
