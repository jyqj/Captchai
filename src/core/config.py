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
    # Speech-to-text model for the reCAPTCHA v2 audio-challenge path. A chat
    # model can't transcribe audio, so this points at a Whisper-family model on
    # the same OpenAI-compatible endpoint (``audio.transcriptions``).
    cloud_audio_model: str

    # ── Local model (self-hosted via SGLang / vLLM) ──
    local_base_url: str
    local_api_key: str
    local_model: str

    # Per-backend max concurrent model calls (0 = unlimited). Browser solves and
    # pure-vision tasks both call the model layer, and self-consistency voting
    # fans out ``vision_vote_samples`` concurrent calls per solve — so without a
    # bound the peak is ``browser_concurrency × vote_samples`` simultaneous cloud
    # calls, which trips cloud-provider rate limits. The cloud backend gets a
    # tight default; the self-hosted local backend defaults to unlimited.
    cloud_max_concurrency: int
    local_max_concurrency: int
    # When a routed backend call fails with a *connection* error (service down,
    # timeout, refused), retry once on the other backend (local↔cloud) instead
    # of bubbling the failure up as a solve failure.
    model_connection_fallback: bool

    captcha_retries: int
    captcha_timeout: int
    # Retry backoff between retryable attempts: ``base * 2**attempt`` seconds,
    # capped at ``max``, plus up to 25% jitter. Replaces the fixed 2s sleep.
    retry_backoff_base: float
    retry_backoff_max: float

    # Playwright browser
    browser_headless: bool
    browser_timeout: int  # seconds

    # ── Runtime / concurrency (orchestration plane) ──
    # Browser solves and pure-vision (classification/recognition) calls draw from
    # separate concurrency pools so a burst of image tasks can't starve browser
    # solvers. See services/task_manager.py.
    browser_concurrency: int
    browser_proxyless_concurrency: int
    browser_proxied_concurrency: int
    browser_pool_proxy_concurrency: int
    vision_concurrency: int
    # Bounded task queue admission control; 0 disables the cap.
    queue_max_size: int
    # Per-task wall-clock budget (seconds) enforced by the scheduler.
    solve_timeout: int

    # ── Token polling (unified budget) ──
    # Single poll budget shared by hCaptcha / Turnstile widget-token extraction.
    poll_budget: int  # seconds
    poll_interval: float  # seconds between checks
    poll_budget_passive: float  # seconds – short budget for passive/checkbox polls
    poll_budget_challenge: float  # seconds – longer budget after challenge dispatch

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
    # WP4: run vote samples concurrently via asyncio.gather (escape hatch for
    # backends that rate-limit concurrent calls — set false to fall back serial).
    vision_vote_concurrent: bool
    # WP4: when a tier-1 (local) call returns confidence below threshold, retry
    # inline on the cloud model before falling back to a full browser redo.
    vision_inline_escalate: bool
    # When true, multi-tile grid challenges are composed into a single montage
    # image before the model call (1 image instead of N) — ~N× cheaper on input
    # tokens and decode latency, with voting multiplying the saving. Tile order
    # is preserved so the index contract is unchanged. Set false to send each
    # tile as its own image (max per-tile resolution, higher cost).
    vision_stitch_grid: bool

    # ── Resource interception (WP4 performance) ──
    # When true, BrowserManager._build_context registers a per-context route
    # handler that aborts bandwidth-heavy resource types (image/media/font/
    # stylesheet) unless the host is on the allowlist. Challenge hosts
    # (hcaptcha / cloudflare / google) are always allowlisted so the vision
    # layer can screenshot challenge tiles.
    resource_block_enabled: bool
    # Comma-separated Playwright resource types to abort (image, media, font,
    # stylesheet by default). document/script/xhr/fetch are NOT aborted.
    resource_block_types: str
    # Comma-separated host suffixes always allowed through (challenge hosts).
    resource_allow_hosts: str
    # Comma-separated host suffixes always aborted (trackers/ads). Empty disables.
    resource_block_hosts: str

    # ── Asset pools ──
    session_pool_size: int
    session_max_solves: int
    session_prewarm: bool
    proxy_cooldown: int  # seconds
    proxy_max_consecutive_fails: int
    # Per-proxy bandwidth quota in GB; 0 disables. A proxy that exceeds it is
    # burned (removed from rotation) to cap metered residential/mobile spend.
    proxy_max_gb: float
    # WP-geo: probe a pool proxy's exit-IP country on its first checkout (through
    # the proxy itself) and cache the derived timezone/locale, so geo alignment
    # no longer depends on a manual |country= annotation. Best-effort; a probe
    # failure leaves the proxy's geo unset (random coherent fingerprint, as
    # before). ``proxy_geo_probe_url`` is the IP-geo endpoint to hit.
    proxy_geo_probe: bool
    proxy_geo_probe_url: str
    token_cache_ttl: int  # seconds
    # Playwright runtime flavour: "chromium" (stock) | "rebrowser" | "camoufox".
    # Enterprise hCaptcha deployments should set BROWSER_RUNTIME=camoufox (a
    # hardened patched Firefox build) — stock Chromium's automation signals
    # are trivially flagged by enterprise detectors. Per-variant runtime
    # switching (running multiple browser processes side-by-side) is out of
    # scope for the current batch; the runtime is process-wide.
    browser_runtime: str
    # When true, a requested hardened runtime (camoufox / rebrowser) that is
    # unavailable or fails to launch is a FATAL startup error instead of a
    # silent degrade to stock Chromium. Recommended for enterprise hCaptcha
    # deployments so an operator never unknowingly runs detectable Chromium.
    browser_runtime_strict: bool
    # ── Camoufox runtime knobs (only used when BROWSER_RUNTIME=camoufox) ──
    # Camoufox owns the fingerprint at the engine level; these tune its launch.
    # ``humanize`` adds camoufox's built-in human-like cursor motion; block
    # WebRTC to avoid an IP leak past the proxy; ``os`` optionally pins the
    # spoofed OS family (comma list of windows/macos/linux; empty = randomise).
    camoufox_humanize: bool
    camoufox_block_webrtc: bool
    camoufox_os: str

    # WP5: enterprise hCaptcha residential-proxy enforcement. When true (the
    # default), enterprise tasks must use a residential or mobile pool proxy
    # — a datacenter IP is rejected because enterprise detectors flag them.
    # Set to false in tests / dev to relax the requirement (any pool proxy
    # is accepted, or a task proxy if explicitly supplied).
    enterprise_require_residential: bool
    # When true, enterprise hCaptcha solves use a fresh browser context per
    # solve instead of a reused warm session, so a single sticky proxy's cookie
    # jar / fingerprint isn't reused to repeatedly hit the same sitekey (a
    # pattern enterprise risk models cluster on). Off by default (warm-session
    # reuse is faster); turn on for the hardest enterprise targets.
    enterprise_fresh_context: bool

    # WP6: enforce the residential requirement even for a caller-supplied task
    # proxy on enterprise hCaptcha. Off by default — a caller task proxy is
    # normally the caller's responsibility (we only warn). When on, an
    # enterprise egress=task solve is refused unless the task proxy is annotated
    # residential/mobile (``|kind=residential``), closing the "enterprise token
    # minted through an unverified datacenter task proxy" gap.
    enterprise_require_residential_on_task: bool

    # ── Token-trust verification (opt-in siteverify closure) ──
    # When enabled AND a sitekey:secret pair is configured, a minted token is
    # verified against the provider's siteverify endpoint and the real-outcome
    # loop (proxy health / accounting) is closed automatically. Off by default.
    token_verify_enabled: bool
    token_verify_secrets: str  # "sitekey1:secret1,sitekey2:secret2"
    token_verify_timeout: float

    # ── Human behavior / real-page mode ──
    hcaptcha_real_page: bool
    # Turnstile real-page mode (parity with hCaptcha): navigate the real target
    # and hook turnstile.render instead of serving the synthetic injected page.
    # Off by default (injected page sidesteps the Cloudflare interstitial).
    turnstile_real_page: bool
    human_mouse_enabled: bool
    human_mouse_jitter_ms: int

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
        cloud_audio_model=os.environ.get("CLOUD_AUDIO_MODEL", "whisper-1"),
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
        cloud_max_concurrency=int(os.environ.get("CLOUD_MAX_CONCURRENCY", "4")),
        local_max_concurrency=int(os.environ.get("LOCAL_MAX_CONCURRENCY", "0")),
        model_connection_fallback=os.environ.get(
            "MODEL_CONNECTION_FALLBACK", "true"
        )
        .strip()
        .lower()
        in {"1", "true", "yes"},
        captcha_retries=int(os.environ.get("CAPTCHA_RETRIES", "3")),
        captcha_timeout=int(os.environ.get("CAPTCHA_TIMEOUT", "30")),
        retry_backoff_base=float(os.environ.get("RETRY_BACKOFF_BASE", "1.0")),
        retry_backoff_max=float(os.environ.get("RETRY_BACKOFF_MAX", "8.0")),
        browser_headless=os.environ.get("BROWSER_HEADLESS", "true").strip().lower()
        in {"1", "true", "yes"},
        browser_timeout=int(os.environ.get("BROWSER_TIMEOUT", "30")),
        # Runtime / concurrency
        browser_concurrency=int(os.environ.get("BROWSER_CONCURRENCY", "4")),
        browser_proxyless_concurrency=int(
            os.environ.get(
                "BROWSER_PROXYLESS_CONCURRENCY",
                os.environ.get("BROWSER_CONCURRENCY", "4"),
            )
        ),
        browser_proxied_concurrency=int(
            os.environ.get(
                "BROWSER_PROXIED_CONCURRENCY",
                os.environ.get("BROWSER_CONCURRENCY", "4"),
            )
        ),
        browser_pool_proxy_concurrency=int(
            os.environ.get(
                "BROWSER_POOL_PROXY_CONCURRENCY",
                os.environ.get("BROWSER_PROXIED_CONCURRENCY", os.environ.get("BROWSER_CONCURRENCY", "4")),
            )
        ),
        vision_concurrency=int(os.environ.get("VISION_CONCURRENCY", "8")),
        queue_max_size=int(os.environ.get("QUEUE_MAX_SIZE", "128")),
        solve_timeout=int(
            os.environ.get("CAPTCHA_SOLVE_TIMEOUT", os.environ.get("SOLVE_TIMEOUT", "180"))
        ),
        # Token polling
        poll_budget=int(os.environ.get("POLL_BUDGET", "30")),
        poll_interval=float(os.environ.get("POLL_INTERVAL", "0.5")),
        poll_budget_passive=float(os.environ.get("POLL_BUDGET_PASSIVE", "2.0")),
        poll_budget_challenge=float(os.environ.get("POLL_BUDGET_CHALLENGE", "10.0")),
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
        # WP4: vision performance — concurrent voting + inline tier-1→tier-2 escalation.
        vision_vote_concurrent=os.environ.get("VISION_VOTE_CONCURRENT", "true")
        .strip()
        .lower()
        in {"1", "true", "yes"},
        vision_inline_escalate=os.environ.get("VISION_INLINE_ESCALATE", "true")
        .strip()
        .lower()
        in {"1", "true", "yes"},
        vision_stitch_grid=os.environ.get("VISION_STITCH_GRID", "true")
        .strip()
        .lower()
        in {"1", "true", "yes"},
        # WP4: resource interception — per-context bandwidth shaping.
        resource_block_enabled=os.environ.get("RESOURCE_BLOCK_ENABLED", "true")
        .strip()
        .lower()
        in {"1", "true", "yes"},
        resource_block_types=os.environ.get(
            "RESOURCE_BLOCK_TYPES", "image,media,font,stylesheet"
        ),
        resource_allow_hosts=os.environ.get(
            "RESOURCE_ALLOW_HOSTS",
            "hcaptcha.com,challenges.cloudflare.com,google.com,recaptcha.net,gstatic.com,cloudflare.com",
        ),
        resource_block_hosts=os.environ.get("RESOURCE_BLOCK_HOSTS", ""),
        # Asset pools
        session_pool_size=int(os.environ.get("SESSION_POOL_SIZE", "4")),
        session_max_solves=int(os.environ.get("SESSION_MAX_SOLVES", "8")),
        session_prewarm=os.environ.get("SESSION_PREWARM", "false").strip().lower()
        in {"1", "true", "yes"},
        proxy_cooldown=int(os.environ.get("PROXY_COOLDOWN", "120")),
        proxy_max_consecutive_fails=int(
            os.environ.get("PROXY_MAX_CONSECUTIVE_FAILS", "3")
        ),
        proxy_max_gb=_optional_float(os.environ.get("PROXY_MAX_GB")) or 0.0,
        proxy_geo_probe=os.environ.get("PROXY_GEO_PROBE", "true").strip().lower()
        in {"1", "true", "yes"},
        proxy_geo_probe_url=os.environ.get(
            "PROXY_GEO_PROBE_URL", "http://ip-api.com/json"
        ),
        token_cache_ttl=int(os.environ.get("TOKEN_CACHE_TTL", "110")),
        browser_runtime=os.environ.get("BROWSER_RUNTIME", "chromium").strip().lower(),
        browser_runtime_strict=os.environ.get("BROWSER_RUNTIME_STRICT", "false")
        .strip()
        .lower()
        in {"1", "true", "yes"},
        camoufox_humanize=os.environ.get("CAMOUFOX_HUMANIZE", "true").strip().lower()
        in {"1", "true", "yes"},
        camoufox_block_webrtc=os.environ.get("CAMOUFOX_BLOCK_WEBRTC", "true")
        .strip()
        .lower()
        in {"1", "true", "yes"},
        camoufox_os=os.environ.get("CAMOUFOX_OS", "").strip(),
        # WP5: enterprise residential-proxy enforcement (default on; set to
        # "false"/"0" to relax for tests / dev).
        enterprise_require_residential=os.environ.get(
            "ENTERPRISE_REQUIRE_RESIDENTIAL", "true"
        )
        .strip()
        .lower()
        in {"1", "true", "yes"},
        enterprise_fresh_context=os.environ.get(
            "ENTERPRISE_FRESH_CONTEXT", "false"
        )
        .strip()
        .lower()
        in {"1", "true", "yes"},
        enterprise_require_residential_on_task=os.environ.get(
            "ENTERPRISE_REQUIRE_RESIDENTIAL_ON_TASK", "false"
        )
        .strip()
        .lower()
        in {"1", "true", "yes"},
        # Token-trust verification (opt-in)
        token_verify_enabled=os.environ.get("TOKEN_VERIFY_ENABLED", "false")
        .strip()
        .lower()
        in {"1", "true", "yes"},
        token_verify_secrets=os.environ.get("TOKEN_VERIFY_SECRETS", ""),
        token_verify_timeout=float(os.environ.get("TOKEN_VERIFY_TIMEOUT", "10.0")),
        # Human behavior / real-page mode
        hcaptcha_real_page=os.environ.get("HCAPTCHA_REAL_PAGE", "false").strip().lower()
        in {"1", "true", "yes"},
        turnstile_real_page=os.environ.get("TURNSTILE_REAL_PAGE", "false")
        .strip()
        .lower()
        in {"1", "true", "yes"},
        human_mouse_enabled=os.environ.get("HUMAN_MOUSE_ENABLED", "true").strip().lower()
        in {"1", "true", "yes"},
        human_mouse_jitter_ms=int(os.environ.get("HUMAN_MOUSE_JITTER_MS", "80")),
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
