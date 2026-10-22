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

The environment layer is a :class:`pydantic_settings.BaseSettings` subclass
(:class:`Settings`): every knob is a typed, range-checked field, and the legacy
``CAPTCHA_*`` / ``SOLVE_TIMEOUT`` / ``BROWSER_*`` alias chains are declared with
``AliasChoices`` so the original precedence is preserved.  :class:`Config` stays
the frozen value object the rest of the application consumes; :func:`load_config`
builds it from a validated :class:`Settings` and converts pydantic's aggregated
``ValidationError`` back into the legacy ``ValueError`` whose lines name the
offending environment variable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Mapping

from pydantic import (
    AliasChoices,
    BeforeValidator,
    Field,
    StrictBool,
    ValidationError,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict


@dataclass(frozen=True)
class Config:
    server_host: str
    server_port: int

    # Auth: YesCaptcha clientKey
    client_key: str | None
    # When true, a keyless server may bind a non-loopback address (the startup
    # gate in src/main.py refuses otherwise). Does NOT relax clientKey checks
    # when client_key IS set.
    allow_anonymous: bool
    # Optional bearer key for privileged admin endpoints. None disables them.
    admin_key: str | None

    # ── Server request controls ──
    # Per-client-IP request budget (requests/minute) enforced by the in-process
    # token-bucket middleware. 0 disables the limiter.
    rate_limit_rpm: int
    # SSRF guard for caller-supplied URLs (websiteURL / proxy endpoints).
    # Consumers read this through ``getattr``; on by default.
    ssrf_protect: bool

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
    # Chromium sandbox toggle: None = auto (container-aware default), True /
    # False force ``--no-sandbox`` on / off.
    browser_disable_sandbox: bool | None
    # When true, launch Chromium with ``--disable-site-isolation-trials``.
    browser_disable_site_isolation: bool

    # ── Runtime / concurrency (orchestration plane) ──
    # Browser solves and pure-vision (classification/recognition) calls draw from
    # separate concurrency pools so a burst of image tasks can't starve browser
    # solvers. See services/task_manager.py.
    browser_concurrency: int
    browser_proxyless_concurrency: int
    browser_proxied_concurrency: int
    browser_pool_proxy_concurrency: int
    vision_concurrency: int
    # Bounded task queue admission control (validated to be >= 1).
    queue_max_size: int
    # Per-task wall-clock budget (seconds) enforced by the scheduler.
    solve_timeout: int

    # ── Token polling (unified budget) ──
    # Single poll budget shared by hCaptcha / Turnstile widget-token extraction.
    poll_budget: int  # seconds
    poll_interval: float  # seconds between checks
    poll_budget_passive: float  # seconds – short budget for passive/checkbox polls
    poll_budget_challenge: float  # seconds – longer budget after challenge dispatch
    # Bounded wait for the challenge iframe DOM to render *before* the first
    # classify/dispatch. Without it a slow (1–3s) iframe is classified with an
    # empty DOM → UNKNOWN / zero tiles → the whole attempt is wasted and retried.
    poll_budget_challenge_ready: float  # seconds

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
    # When false (the default), the model's SELF-REPORTED confidence is NOT
    # trusted to decide a grid answer (a small model is frequently confidently
    # wrong): grid challenges instead derive confidence from cross-sample
    # agreement (self-consistency voting) and escalate on low agreement. Set
    # true to restore the legacy behaviour of gating on the model's own number.
    vision_trust_self_confidence: bool

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
    # Warm-session hygiene (seconds; 0 disables the respective bound). An idle
    # session older than the TTL, or a session older than the max age, is
    # retired on its next checkout instead of being handed out.
    session_idle_ttl_seconds: int
    session_max_age_seconds: int
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
    # When true (the default), enterprise hCaptcha solves use a fresh browser
    # context per solve instead of a reused warm session, so a single sticky
    # proxy's cookie jar / fingerprint isn't reused to repeatedly hit the same
    # sitekey (a pattern enterprise risk models cluster on). Costs a little
    # per-solve setup; set false to trade that safety for warm-session speed.
    enterprise_fresh_context: bool
    # When true, an enterprise hCaptcha solve on stock (non-hardened) Chromium
    # is REFUSED rather than attempted. Stock Chromium's automation +
    # software-WebGL signals are trivially flagged by enterprise detectors, so
    # this lets an operator guarantee enterprise solves only run on a hardened
    # runtime (BROWSER_RUNTIME=camoufox/rebrowser). Off by default (warns only).
    enterprise_require_hardened_runtime: bool

    # WP6: enforce the residential requirement even for a caller-supplied task
    # proxy on enterprise hCaptcha. Off by default — a caller task proxy is
    # normally the caller's responsibility (we only warn). When on, an
    # enterprise egress=task solve is refused unless the task proxy is annotated
    # residential/mobile (``|kind=residential``), closing the "enterprise token
    # minted through an unverified datacenter task proxy" gap.
    enterprise_require_residential_on_task: bool

    # Expose the server-side POOL proxy's credentials in the solution's
    # ``egressServer`` (scheme://user:pass@host:port, sticky session
    # substituted). Off by default — pool proxies are a server secret. But an
    # IP-bound token minted through a pool proxy is worthless to a caller who
    # can't reproduce that exit IP for their downstream submit (enterprise
    # hCaptcha / Stripe Radar score IP consistency). Turn this on when clients
    # are trusted and need to route their submit through the same pool egress.
    pool_egress_expose_credentials: bool

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
    # Seconds of continuous pre-interaction wander/scroll seeded into the
    # motionData buffer before the checkbox click (a single short burst reads as
    # "empty motion buffer" to hCaptcha's passive scoring).
    human_passive_motion_seconds: float
    # Invisible hCaptcha is scored purely on the passive behaviour timeline, so
    # it gets a longer motion seed before execute() and a longer passive-token
    # budget than the checkbox path (the /getcaptcha round-trip through a
    # residential proxy commonly exceeds the 2s checkbox-path budget).
    hcaptcha_invisible_motion_seconds: float
    hcaptcha_invisible_passive_budget: float
    # Conservative freshness budget (seconds) for a single-use enterprise rqdata
    # nonce. A solve that exceeds it appends a caller-facing warning that the
    # token may be rejected because the nonce expired. 0 disables the warning.
    hcaptcha_rqdata_ttl: float
    # Opt-in: persist the hCaptcha device-trust cookie (``hmt``) per egress
    # identity and re-seed it into fresh contexts, so an enterprise solve
    # presents a RETURNING device instead of a zero-history one (which
    # enterprise risk models penalise). Off by default — in-memory only; pairs
    # with ENTERPRISE_FRESH_CONTEXT to get fresh-context isolation *and* a
    # stable device identity per exit IP.
    hcaptcha_device_persistence: bool

    # ── State backend ──
    # Redis URL for the persistent task store; None => in-memory fallback.
    redis_url: str | None

    # ── Billing / budget (consumption plane) ──
    # Starting credit surfaced by getBalance; the ledger's spend is subtracted.
    account_balance_usd: float
    # Optional hard spend caps enforced by BudgetGuard before a cloud call.
    budget_global_cap_usd: float | None
    budget_per_client_cap_usd: float | None
    # Rolling window (per (sitekey, proxy_kind, model) bucket) the success
    # accounting keeps for its routing/selection success rates.
    accounting_window: int

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


# ── Environment parsing helpers ──
# Booleans are normalised here before ``StrictBool`` sees them so the accepted
# word set stays exactly the legacy one (pydantic's own bool parser also accepts
# "y"/"n"/"t"/"f", which the project never documented). A malformed value names
# the variable through load_config's ValidationError formatter.
_TRUE_VALUES = frozenset({"true", "1", "yes", "on"})
_FALSE_VALUES = frozenset({"false", "0", "no", "off"})
_SUPPORTED_BROWSER_RUNTIMES = frozenset({"chromium", "camoufox", "rebrowser"})


def _parse_bool_value(value: Any) -> Any:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _TRUE_VALUES:
            return True
        if normalized in _FALSE_VALUES:
            return False
        raise ValueError(
            f"expected bool (true/false/1/0/yes/no/on/off), got {value!r}"
        )
    return value


def _parse_optional_bool_value(value: Any) -> Any:
    """Unset / empty => None (auto); otherwise the same word set as ``bool``."""
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return _parse_bool_value(value)


def _empty_string_to_none(value: Any) -> Any:
    """Optional numerics: an empty env value means 'not set'."""
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _empty_string_to_zero(value: Any) -> Any:
    """PROXY_MAX_GB: empty / unset means 0.0 (quota disabled)."""
    if value is None:
        return 0.0
    if isinstance(value, str) and not value.strip():
        return 0.0
    return value


def _strip_or_none(value: Any) -> Any:
    """Optional strings: strip and collapse an empty value to None."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _strip(value: Any) -> Any:
    return value.strip() if isinstance(value, str) else value


def _lower_strip(value: Any) -> Any:
    return value.strip().lower() if isinstance(value, str) else value


# Reusable annotated field types. ``BeforeValidator`` applies before the strict
# inner type, so env strings keep the legacy word set while the model itself
# still holds genuine bools / floats.
_EnvBool = Annotated[StrictBool, BeforeValidator(_parse_bool_value)]
_EnvOptionalBool = Annotated[
    StrictBool | None, BeforeValidator(_parse_optional_bool_value)
]
_EnvOptionalFloat = Annotated[float | None, BeforeValidator(_empty_string_to_none)]
_EnvProxyQuota = Annotated[float, BeforeValidator(_empty_string_to_zero)]
_EnvOptionalStr = Annotated[str | None, BeforeValidator(_strip_or_none)]
_EnvStrippedStr = Annotated[str, BeforeValidator(_strip)]
_EnvBrowserRuntime = Annotated[
    Literal["chromium", "camoufox", "rebrowser"], BeforeValidator(_lower_strip)
]
_EnvTier2Detail = Annotated[
    Literal["low", "high", "auto"], BeforeValidator(_lower_strip)
]


class Settings(BaseSettings):
    """Typed, validated environment settings.

    Field defaults are the historical defaults; every field reads the same
    environment variable it read in the hand-rolled loader (case-insensitively).
    Fields whose legacy implementation accepted fallback names declare the
    exact chain, primary first, via ``AliasChoices`` — pydantic-settings reads
    the first variable that is present, matching ``_resolve``'s old behaviour.
    """

    model_config = SettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        env_file=None,
    )

    server_host: str = "0.0.0.0"
    server_port: int = Field(8000, ge=1, le=65535)

    client_key: _EnvOptionalStr = None
    allow_anonymous: _EnvBool = False
    admin_key: _EnvOptionalStr = None

    # Server request controls
    rate_limit_rpm: int = Field(60, ge=0)
    ssrf_protect: _EnvBool = True

    # Cloud model (CLOUD_* wins over the legacy CAPTCHA_* names)
    cloud_base_url: str = Field(
        "https://your-openai-compatible-endpoint/v1",
        validation_alias=AliasChoices("CLOUD_BASE_URL", "CAPTCHA_BASE_URL"),
    )
    cloud_api_key: str = Field(
        "",
        validation_alias=AliasChoices("CLOUD_API_KEY", "CAPTCHA_API_KEY"),
    )
    cloud_model: str = Field(
        "gpt-5.4",
        validation_alias=AliasChoices("CLOUD_MODEL", "CAPTCHA_MODEL"),
    )
    cloud_audio_model: str = "whisper-1"

    # Local model (LOCAL_* wins over the legacy CAPTCHA_* names — the same
    # CAPTCHA_BASE_URL / CAPTCHA_API_KEY therefore feed both backends)
    local_base_url: str = Field(
        "http://localhost:30000/v1",
        validation_alias=AliasChoices("LOCAL_BASE_URL", "CAPTCHA_BASE_URL"),
    )
    local_api_key: str = Field(
        "EMPTY",
        validation_alias=AliasChoices("LOCAL_API_KEY", "CAPTCHA_API_KEY"),
    )
    local_model: str = Field(
        "Qwen/Qwen3.5-2B",
        validation_alias=AliasChoices("LOCAL_MODEL", "CAPTCHA_MULTIMODAL_MODEL"),
    )

    cloud_max_concurrency: int = 4
    local_max_concurrency: int = 0
    model_connection_fallback: _EnvBool = True

    captcha_retries: int = 3
    captcha_timeout: int = 30
    retry_backoff_base: float = 1.0
    retry_backoff_max: float = 8.0

    # Playwright browser
    browser_headless: _EnvBool = True
    browser_timeout: int = 30
    browser_disable_sandbox: _EnvOptionalBool = None
    browser_disable_site_isolation: _EnvBool = False

    # Runtime / concurrency
    browser_concurrency: int = Field(4, ge=1)
    browser_proxyless_concurrency: int = Field(
        4,
        validation_alias=AliasChoices(
            "BROWSER_PROXYLESS_CONCURRENCY", "BROWSER_CONCURRENCY"
        ),
    )
    browser_proxied_concurrency: int = Field(
        4,
        validation_alias=AliasChoices(
            "BROWSER_PROXIED_CONCURRENCY", "BROWSER_CONCURRENCY"
        ),
    )
    browser_pool_proxy_concurrency: int = Field(
        4,
        validation_alias=AliasChoices(
            "BROWSER_POOL_PROXY_CONCURRENCY",
            "BROWSER_PROXIED_CONCURRENCY",
            "BROWSER_CONCURRENCY",
        ),
    )
    vision_concurrency: int = 8
    queue_max_size: int = Field(128, ge=1)
    solve_timeout: int = Field(
        180,
        gt=0,
        validation_alias=AliasChoices("CAPTCHA_SOLVE_TIMEOUT", "SOLVE_TIMEOUT"),
    )

    # Token polling
    poll_budget: int = 30
    poll_interval: float = 0.5
    poll_budget_passive: float = 2.0
    poll_budget_challenge: float = 10.0
    poll_budget_challenge_ready: float = 4.0

    # Vision routing
    vision_cloud_enabled: _EnvBool = True
    vision_vote_samples: int = 3
    vision_confidence_threshold: float = 0.6
    vision_tier2_detail: _EnvTier2Detail = "high"
    vision_vote_concurrent: _EnvBool = True
    vision_inline_escalate: _EnvBool = True
    vision_stitch_grid: _EnvBool = True
    vision_trust_self_confidence: _EnvBool = False

    # Resource interception
    resource_block_enabled: _EnvBool = True
    resource_block_types: str = "image,media,font,stylesheet"
    resource_allow_hosts: str = (
        "hcaptcha.com,challenges.cloudflare.com,google.com,recaptcha.net,"
        "gstatic.com,cloudflare.com"
    )
    resource_block_hosts: str = ""

    # Asset pools
    session_pool_size: int = 4
    session_max_solves: int = 8
    session_prewarm: _EnvBool = False
    session_idle_ttl_seconds: int = Field(300, ge=0)
    session_max_age_seconds: int = Field(1800, ge=0)
    proxy_cooldown: int = 120
    proxy_max_consecutive_fails: int = 3
    proxy_max_gb: _EnvProxyQuota = Field(0.0, ge=0)
    proxy_geo_probe: _EnvBool = True
    proxy_geo_probe_url: str = "http://ip-api.com/json"
    browser_runtime: _EnvBrowserRuntime = "chromium"
    browser_runtime_strict: _EnvBool = False
    camoufox_humanize: _EnvBool = True
    camoufox_block_webrtc: _EnvBool = True
    camoufox_os: _EnvStrippedStr = ""

    # Enterprise hCaptcha enforcement (WP5 / WP6)
    enterprise_require_residential: _EnvBool = True
    enterprise_fresh_context: _EnvBool = True
    enterprise_require_hardened_runtime: _EnvBool = False
    enterprise_require_residential_on_task: _EnvBool = False
    pool_egress_expose_credentials: _EnvBool = False

    # Token-trust verification
    token_verify_enabled: _EnvBool = False
    token_verify_secrets: str = ""
    token_verify_timeout: float = 10.0

    # Human behavior / real-page mode
    hcaptcha_real_page: _EnvBool = False
    turnstile_real_page: _EnvBool = False
    human_mouse_enabled: _EnvBool = True
    human_mouse_jitter_ms: int = 80
    human_passive_motion_seconds: float = 1.4
    hcaptcha_invisible_motion_seconds: float = 3.0
    hcaptcha_invisible_passive_budget: float = 4.0
    hcaptcha_rqdata_ttl: float = 30.0
    hcaptcha_device_persistence: _EnvBool = False

    # State backend
    redis_url: _EnvOptionalStr = None

    # Billing / budget
    account_balance_usd: float = 99999.0
    budget_global_cap_usd: _EnvOptionalFloat = Field(None, ge=0)
    budget_per_client_cap_usd: _EnvOptionalFloat = Field(None, ge=0)
    accounting_window: int = 100

    @model_validator(mode="after")
    def _validate_ranges(self) -> "Settings":
        """Aggregate range problems the way the old ``_validate`` pass did.

        The per-field ``Field(ge=/le=/gt=)`` constraints already reject these
        values individually; this model-level pass is the backstop that keeps
        the historical operator-facing wording (and the one-message-per-run
        aggregation) if a constraint is ever relaxed or a field is populated
        outside normal validation.
        """
        problems: list[str] = []

        if not 1 <= self.server_port <= 65535:
            problems.append(
                f"SERVER_PORT: expected 1..65535, got {self.server_port}"
            )
        if self.browser_concurrency < 1:
            problems.append(
                f"BROWSER_CONCURRENCY: expected >= 1, got "
                f"{self.browser_concurrency}"
            )
        if self.queue_max_size < 1:
            problems.append(
                f"QUEUE_MAX_SIZE: expected >= 1, got {self.queue_max_size}"
            )
        if self.solve_timeout <= 0:
            problems.append(
                "CAPTCHA_SOLVE_TIMEOUT (or SOLVE_TIMEOUT): expected > 0, "
                f"got {self.solve_timeout}"
            )
        if (
            self.budget_global_cap_usd is not None
            and self.budget_global_cap_usd < 0
        ):
            problems.append(
                "BUDGET_GLOBAL_CAP_USD: expected >= 0, "
                f"got {self.budget_global_cap_usd}"
            )
        if (
            self.budget_per_client_cap_usd is not None
            and self.budget_per_client_cap_usd < 0
        ):
            problems.append(
                "BUDGET_PER_CLIENT_CAP_USD: expected >= 0, "
                f"got {self.budget_per_client_cap_usd}"
            )
        if self.proxy_max_gb < 0:
            problems.append(
                f"PROXY_MAX_GB: expected >= 0, got {self.proxy_max_gb}"
            )
        if self.rate_limit_rpm < 0:
            problems.append(
                f"RATE_LIMIT_RPM: expected >= 0 (0 disables the limiter), "
                f"got {self.rate_limit_rpm}"
            )
        if self.session_idle_ttl_seconds < 0:
            problems.append(
                "SESSION_IDLE_TTL_SECONDS: expected >= 0, "
                f"got {self.session_idle_ttl_seconds}"
            )
        if self.session_max_age_seconds < 0:
            problems.append(
                "SESSION_MAX_AGE_SECONDS: expected >= 0, "
                f"got {self.session_max_age_seconds}"
            )
        if self.browser_runtime not in _SUPPORTED_BROWSER_RUNTIMES:
            supported = ", ".join(sorted(_SUPPORTED_BROWSER_RUNTIMES))
            problems.append(
                f"BROWSER_RUNTIME: expected one of {supported}, "
                f"got {self.browser_runtime!r}"
            )

        if problems:
            raise ValueError("\n".join(problems))
        return self


# ── Legacy alias chains (primary name first) ──
# Only fields that accept more than their canonical ``FIELD.upper()`` name need
# an entry; used both to name the offending variable in error messages and to
# mirror ``_resolve``'s "which variable was actually read" reporting.
_FIELD_ENV_ALIASES: dict[str, tuple[str, ...]] = {
    "cloud_base_url": ("CLOUD_BASE_URL", "CAPTCHA_BASE_URL"),
    "cloud_api_key": ("CLOUD_API_KEY", "CAPTCHA_API_KEY"),
    "cloud_model": ("CLOUD_MODEL", "CAPTCHA_MODEL"),
    "local_base_url": ("LOCAL_BASE_URL", "CAPTCHA_BASE_URL"),
    "local_api_key": ("LOCAL_API_KEY", "CAPTCHA_API_KEY"),
    "local_model": ("LOCAL_MODEL", "CAPTCHA_MULTIMODAL_MODEL"),
    "solve_timeout": ("CAPTCHA_SOLVE_TIMEOUT", "SOLVE_TIMEOUT"),
    "browser_proxyless_concurrency": (
        "BROWSER_PROXYLESS_CONCURRENCY",
        "BROWSER_CONCURRENCY",
    ),
    "browser_proxied_concurrency": (
        "BROWSER_PROXIED_CONCURRENCY",
        "BROWSER_CONCURRENCY",
    ),
    "browser_pool_proxy_concurrency": (
        "BROWSER_POOL_PROXY_CONCURRENCY",
        "BROWSER_PROXIED_CONCURRENCY",
        "BROWSER_CONCURRENCY",
    ),
}

def _build_loc_map() -> dict[str, str]:
    """Map every field name / alias (lower-cased) back to its field name."""
    mapping: dict[str, str] = {
        field_name.lower(): field_name for field_name in Settings.model_fields
    }
    for field_name, env_names in _FIELD_ENV_ALIASES.items():
        for env_name in env_names:
            mapping[env_name.lower()] = field_name
    return mapping


# pydantic reports the field name (or the first validation alias) in ``loc``;
# either spelling resolves to the field so the message can name the env var.
_LOC_TO_FIELD = _build_loc_map()

# Numeric bound errors: pydantic type -> (ctx key, human symbol).
_RANGE_KINDS: dict[str, tuple[str, str]] = {
    "greater_than_equal": ("ge", ">="),
    "less_than_equal": ("le", "<="),
    "greater_than": ("gt", ">"),
    "less_than": ("lt", "<"),
}


def _env_name_for(field_name: str) -> str:
    """Name the environment variable actually responsible for a field error."""
    names = _FIELD_ENV_ALIASES.get(field_name)
    if not names:
        return field_name.upper()
    for name in names:
        if name in os.environ:
            return name
    return names[0]


def _display_value(value: Any) -> str:
    """Render a raw env string the way the old helpers did (bare numbers)."""
    if isinstance(value, str):
        text = value.strip()
        try:
            return str(int(text))
        except ValueError:
            try:
                return str(float(text))
            except ValueError:
                return repr(value)
    return repr(value)


def _describe_error(error: Mapping[str, Any]) -> str:
    # ``Mapping`` because pydantic hands us its ``ErrorDetails`` TypedDict,
    # which is not assignable to the mutable ``dict[str, Any]``.
    """Translate a pydantic error item into the legacy operator wording."""
    kind = str(error.get("type", ""))
    raw = error.get("input")
    ctx = error.get("ctx") or {}

    if kind in {"int_parsing", "int_type"}:
        return f"expected int, got {raw!r}"
    if kind in {"float_parsing", "float_type"}:
        return f"expected float, got {raw!r}"
    if kind == "literal_error":
        expected = ctx.get("expected")
        if expected:
            return f"expected one of {expected}, got {raw!r}"
        return f"invalid value {raw!r}"
    if kind in _RANGE_KINDS:
        key, symbol = _RANGE_KINDS[kind]
        if key in ctx:
            return f"expected {symbol} {ctx[key]}, got {_display_value(raw)}"
    if kind == "missing":
        return "required"
    if kind in {"value_error", "bool_parsing", "bool_type"}:
        message = str(error.get("msg", "invalid value"))
        if message.startswith("Value error, "):
            message = message[len("Value error, ") :]
        if kind in {"bool_parsing", "bool_type"} and "expected bool" not in message:
            return (
                "expected bool (true/false/1/0/yes/no/on/off), "
                f"got {raw!r}"
            )
        return message

    message = str(error.get("msg", "invalid value"))
    if message.startswith("Value error, "):
        message = message[len("Value error, ") :]
    return message


def _format_validation_error(exc: ValidationError) -> str:
    """Render every pydantic error as ``  - ENV_VAR: problem`` lines.

    pydantic already aggregates all field failures; this maps each error's
    location back to the environment variable name (including the legacy alias
    that was actually set) and keeps the old ``invalid configuration:`` header.
    """
    problems: list[str] = []
    for error in exc.errors():
        loc = error.get("loc") or ()
        field_name = _LOC_TO_FIELD.get(str(loc[0]).lower()) if loc else None
        if field_name is None:
            # Model-level (``loc == ()``) or unknown location: the message is
            # already a full problem line (possibly several).
            message = str(error.get("msg", "invalid value"))
            if message.startswith("Value error, "):
                message = message[len("Value error, ") :]
            for line in message.splitlines():
                line = line.strip()
                if line.startswith("- "):
                    line = line[2:].strip()
                if line:
                    problems.append(line)
            continue
        problems.append(f"{_env_name_for(field_name)}: {_describe_error(error)}")

    if not problems:
        problems.append(str(exc))
    detail = "\n".join(f"  - {problem}" for problem in problems)
    return f"invalid configuration:\n{detail}"


def load_config() -> Config:
    """Build the frozen :class:`Config` from validated environment settings.

    Raises ``ValueError`` naming every offending environment variable when a
    value is malformed or out of range — the same contract the hand-rolled
    loader had.
    """
    try:
        # pyright's dataclass_transform view of pydantic-settings treats
        # ``Field(<default>, ...)`` fields as required kwargs, but every
        # Settings field supplies a runtime default (or reads the env).
        settings = Settings()  # type: ignore[reportCallIssue]
    except ValidationError as exc:
        raise ValueError(_format_validation_error(exc)) from None
    return Config(**settings.model_dump())


config = load_config()
