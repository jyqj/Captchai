"""Shared browser lifecycle, per-task contexts, proxy, and coherent stealth.

All Playwright-based solvers share one browser process (launched once) and
create an isolated ``BrowserContext`` per solve. Context creation centralises
the things that matter for real-world captcha solving:

* **Proxy binding** — Turnstile / reCAPTCHA / hCaptcha tokens are IP-bound, so
  the proxy is applied at the context level and the token is minted through the
  same egress the caller will submit from.
* **Coherent per-context fingerprint** — instead of injecting one hard-coded
  stealth script with identical navigator/WebGL values into every context (a
  detection signal in itself), each context gets a *coherent* fingerprint from
  :mod:`src.assets.fingerprint`: the User-Agent, ``navigator.platform`` and
  WebGL vendor/renderer are drawn from the same profile, and locale/timezone are
  applied at the context level.
* **Runtime selection** — ``BROWSER_RUNTIME`` chooses stock ``chromium`` (default)
  or a hardened patched runtime (``rebrowser`` / ``camoufox``) for the toughest
  targets, falling back to stock Chromium when the patched runtime isn't
  installed.

``context_factory`` implements the signature the warm :class:`SessionPool`
expects (``async (fingerprint, proxy) -> (context, user_agent)``), so the pool
can create sessions without importing Playwright itself.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional, Tuple
from urllib.parse import urlparse

from playwright.async_api import (
    Browser,
    BrowserContext,
    Playwright,
    async_playwright,
)

from ..assets.fingerprint import (
    FingerprintProfile,
    build_stealth_js,
    context_kwargs,
    generate_fingerprint,
)
from ..assets.proxy_pool import ProxyAsset, proxy_from_params
from ..core.config import Config

log = logging.getLogger(__name__)

# Kept for backward compatibility with callers/tests that import it. New code
# should rely on per-context fingerprints from the asset layer.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-features=IsolateOrigins,site-per-process",
    "--disable-site-isolation-trials",
]


def _parse_csv(value: str) -> set[str]:
    """Split a comma-separated config string into a lowercased set of tokens."""
    return {token.strip().lower() for token in (value or "").split(",") if token.strip()}


@dataclass
class ContextOptions:
    """Resolved per-task browser context settings."""

    user_agent: str
    proxy: Optional[dict]
    fingerprint: FingerprintProfile


def _proxy_dict_from_params(params: dict[str, Any]) -> Optional[dict]:
    """Playwright proxy dict from YesCaptcha-style task fields (or None)."""
    asset = proxy_from_params(params)
    return asset.playwright_proxy() if asset is not None else None


def _count_response_bytes(response: Any) -> None:
    """Accumulate Content-Length onto the owning context's byte counter.

    Registered as a ``context.on("response", ...)`` listener so every response
    in the context contributes to a running total. Chunked responses without a
    Content-Length header undercount (acceptable for v1 — the dominant byte
    sources for captcha solving all carry Content-Length).
    """
    try:
        ctx = response.frame.page.context
    except Exception:
        return
    try:
        cl = response.headers.get("content-length") if response.headers else None
        if cl:
            ctx._omc_bytes_used += int(cl)  # type: ignore[attr-defined]
    except Exception:
        pass


async def _safe_continue(route: Any) -> None:
    """Best-effort ``route.continue_()`` — never raises into the route pipeline."""
    try:
        await route.continue_()
    except Exception:
        pass


def resolve_context_options(config: Config, params: dict[str, Any]) -> ContextOptions:
    """Build a coherent fingerprint + proxy for a task.

    A caller-supplied ``userAgent`` still wins (so the token binds to the UA the
    caller will submit with); otherwise the fingerprint's UA is used. The
    fingerprint uses a random seed so each context gets a unique coherent
    identity; seeding by sitekey would let detectors cluster every solve of a
    given target as the same browser.

    WP3 — pool-proxy geo alignment: when the solver stashes the pool proxy's
    exit-IP geo (``_pool_geo``) and a deterministic seed (``_proxy_seed``)
    onto ``params``, the fingerprint is drawn with that timezone/locale so a
    German residential IP presents Europe/Berlin + de-DE rather than
    en-US/New_York. Server-IP / proxyless solves keep the random coherent
    identity (current behavior). The actually-used ``timezone_id`` and
    ``languages`` are stashed back onto ``params`` (``_used_timezone``,
    ``_used_languages``) so the solver can surface them in the solution for
    callers to align their submit context with the solve context.
    """
    del config  # reserved for future per-config fingerprint policy
    pool_geo = params.get("_pool_geo") or {}
    fingerprint = generate_fingerprint(
        seed=params.get("_proxy_seed") or None,
        timezone_id=pool_geo.get("timezone") or None,
        locale=pool_geo.get("locale") or None,
    )
    user_agent = params.get("userAgent") or fingerprint.user_agent

    if params.get("_proxy_override"):
        proxy_dict = params["_proxy_override"]
    elif params.get("_proxyKind") == "proxyless":
        # egress=proxyless: a caller may have supplied task-proxy fields, but
        # the solver has already classified this as a proxyless solve. Honour
        # that intent and use the server egress IP instead of the task proxy.
        proxy_dict = None
    else:
        proxy_dict = _proxy_dict_from_params(params)

    # Surface the actually-used fingerprint geo so solvers can echo it back
    # in the solution (SolutionObject.timezoneId / acceptLanguage). Stashing
    # onto params avoids changing ``new_context``'s return signature.
    params["_used_timezone"] = fingerprint.timezone_id
    params["_used_languages"] = list(fingerprint.languages)

    return ContextOptions(
        user_agent=user_agent,
        proxy=proxy_dict,
        fingerprint=fingerprint,
    )


class BrowserManager:
    """Owns one shared browser process for all solvers."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        # ``_requested_runtime`` is what the operator asked for; ``_runtime`` is
        # what actually launched (they diverge only when a hardened runtime was
        # requested but unavailable and strict mode is off). Surfaced via the
        # ``runtime``/``requested_runtime`` properties and /api/v1/health so a
        # silent degrade to detectable stock Chromium can't go unnoticed.
        self._requested_runtime = getattr(config, "browser_runtime", "chromium")
        self._runtime = self._requested_runtime
        self._runtime_strict = bool(getattr(config, "browser_runtime_strict", False))
        # Camoufox is a Firefox fork that owns its fingerprint at the engine
        # level, so its contexts are built WITHOUT our Chromium stealth JS /
        # Chrome client hints / forced UA (those would contradict a Firefox
        # engine). Its real UA is browser-level and fixed per launch, so we
        # read it once after launch and echo it back to callers.
        self._camoufox_user_agent: Optional[str] = None
        self._camoufox_humanize = getattr(config, "camoufox_humanize", True)
        self._camoufox_block_webrtc = getattr(config, "camoufox_block_webrtc", True)
        # Optional OS pin (e.g. "windows,macos"); empty lets camoufox randomise
        # a coherent OS fingerprint per launch.
        self._camoufox_os = sorted(_parse_csv(getattr(config, "camoufox_os", ""))) or None
        # WP4: pre-parse the resource interception config so the per-request
        # route handler does no string parsing on the hot path. Defaults match
        # ``Config`` so a test ``SimpleNamespace`` without these attributes
        # still behaves like production.
        self._resource_block_enabled = bool(
            getattr(config, "resource_block_enabled", True)
        )
        self._resource_block_types = _parse_csv(
            getattr(config, "resource_block_types", "image,media,font,stylesheet")
        )
        self._resource_allow_hosts = _parse_csv(
            getattr(
                config,
                "resource_allow_hosts",
                "hcaptcha.com,challenges.cloudflare.com,google.com,recaptcha.net,gstatic.com,cloudflare.com",
            )
        )
        self._resource_block_hosts = _parse_csv(
            getattr(config, "resource_block_hosts", "")
        )

    @property
    def runtime(self) -> str:
        """The runtime that actually launched (post-fallback)."""
        return self._runtime

    @property
    def requested_runtime(self) -> str:
        """The runtime the operator requested via ``BROWSER_RUNTIME``."""
        return self._requested_runtime

    async def start(self) -> None:
        if self._browser is not None:
            return
        self._playwright = await async_playwright().start()
        self._browser = await self._launch()
        if self._runtime == "camoufox":
            await self._cache_camoufox_user_agent()
        if self._runtime != self._requested_runtime:
            # Loud, not silent: an operator who set a hardened runtime must know
            # they're actually on detectable stock Chromium (enterprise hCaptcha
            # / Cloudflare flag it). Strict mode turns this into a hard failure
            # in ``_launch`` before we get here.
            log.warning(
                "Requested browser runtime %r is unavailable; running on %r. "
                "Enterprise/anti-bot targets may flag stock Chromium. Install "
                "the runtime or set BROWSER_RUNTIME_STRICT=true to fail fast.",
                self._requested_runtime,
                self._runtime,
            )
        log.info(
            "Shared browser started (runtime=%s, requested=%s, headless=%s)",
            self._runtime,
            self._requested_runtime,
            self._config.browser_headless,
        )

    @staticmethod
    def _hardened_runtime_available(runtime: str) -> bool:
        """Best-effort check that a hardened runtime is genuinely installed.

        * ``camoufox`` → the ``camoufox`` package must be importable.
        * ``rebrowser`` → the ``rebrowser_playwright`` package must be
          importable (it replaces the ``playwright`` driver with a patched one).

        Any other value is treated as "not a hardened runtime" (True so the
        caller proceeds with the stock chromium path).
        """
        import importlib.util

        module = {"camoufox": "camoufox", "rebrowser": "rebrowser_playwright"}.get(
            runtime
        )
        if module is None:
            return True
        return importlib.util.find_spec(module) is not None

    async def _launch(self) -> Browser:
        assert self._playwright is not None
        launcher = self._playwright.chromium

        # Guard hardened runtimes before we attempt a launch so an operator who
        # asked for camoufox/rebrowser but never installed it gets a clear
        # signal instead of a silent degrade. In strict mode this is fatal; in
        # lenient mode we degrade to stock Chromium (and start() warns loudly).
        if self._runtime in {"camoufox", "rebrowser"} and not self._hardened_runtime_available(
            self._runtime
        ):
            msg = (
                f"BROWSER_RUNTIME={self._runtime!r} was requested but the runtime "
                f"is not installed in this environment"
            )
            if self._runtime_strict:
                raise RuntimeError(
                    msg + " (BROWSER_RUNTIME_STRICT=true). Install it or change "
                    "BROWSER_RUNTIME."
                )
            log.warning("%s; degrading to stock Chromium", msg)
            self._runtime = "chromium"

        try:
            if self._runtime == "camoufox":
                return await self._launch_camoufox()
            # rebrowser-playwright exposes the same API surface via the standard
            # `chromium` object once its patched driver is installed.
            return await launcher.launch(
                headless=self._config.browser_headless,
                args=_LAUNCH_ARGS,
            )
        except Exception as exc:  # pragma: no cover - depends on host runtimes
            if self._runtime_strict:
                raise RuntimeError(
                    f"Browser runtime {self._runtime!r} failed to launch and "
                    "BROWSER_RUNTIME_STRICT=true forbids the stock-Chromium "
                    f"fallback: {exc}"
                ) from exc
            log.warning(
                "Browser runtime %r launch failed (%s); falling back to stock Chromium",
                self._runtime,
                exc,
            )
            self._runtime = "chromium"
            return await launcher.launch(
                headless=self._config.browser_headless,
                args=_LAUNCH_ARGS,
            )

    async def _launch_camoufox(self) -> Browser:
        """Launch the patched Camoufox (Firefox) build via the real driver.

        Camoufox spoofs the fingerprint at the engine level, so we let it own
        navigator/WebGL/canvas/screen and only pass egress-relevant knobs:
        headless, humanized input, WebRTC blocking, and an optional OS pin.
        ``launch_options`` resolves the downloaded Camoufox binary + its 50+
        env vars + firefox prefs; ``AsyncNewBrowser`` connects Playwright to it.
        The proxy is applied per-context (Firefox supports context proxies) so
        each solve still egresses through its own pool/task proxy.
        """
        from camoufox import AsyncNewBrowser  # noqa: WPS433 - optional runtime dep
        from camoufox.utils import launch_options as camoufox_launch_options

        opts = camoufox_launch_options(
            headless=self._config.browser_headless,
            humanize=self._camoufox_humanize,
            block_webrtc=self._camoufox_block_webrtc,
            os=self._camoufox_os,
            # geoip needs a proxy at launch to resolve; ours are per-context, so
            # leave it off and align geo per-context via locale/timezone instead.
            geoip=False,
        )
        assert self._playwright is not None
        browser = await AsyncNewBrowser(
            self._playwright, from_options=opts, headless=self._config.browser_headless
        )
        return browser  # type: ignore[return-value]

    async def _cache_camoufox_user_agent(self) -> None:
        """Read Camoufox's engine-level UA once so solvers can echo it back."""
        try:
            assert self._browser is not None
            ctx = await self._browser.new_context()
            page = await ctx.new_page()
            ua = await page.evaluate("() => navigator.userAgent")
            await ctx.close()
            if isinstance(ua, str) and ua:
                self._camoufox_user_agent = ua
                log.info("Camoufox engine User-Agent: %s", ua)
        except Exception as exc:  # noqa: BLE001 - non-fatal; UA echo is best-effort
            log.warning("Could not read Camoufox User-Agent: %s", exc)

    async def stop(self) -> None:
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        log.info("Shared browser stopped")

    async def new_context(self, params: dict[str, Any]) -> Tuple[BrowserContext, str]:
        """Create an isolated context bound to the task's proxy/UA + fingerprint.

        Returns the context and the resolved User-Agent so the solver can echo it
        back to the caller for token/UA binding.
        """
        opts = resolve_context_options(self._config, params)
        # Honour a caller-forced UA by projecting it onto the fingerprint.
        fingerprint = opts.fingerprint
        if opts.user_agent != fingerprint.user_agent:
            fingerprint = FingerprintProfile(
                user_agent=opts.user_agent,
                platform=fingerprint.platform,
                languages=fingerprint.languages,
                hardware_concurrency=fingerprint.hardware_concurrency,
                device_memory=fingerprint.device_memory,
                screen_width=fingerprint.screen_width,
                screen_height=fingerprint.screen_height,
                webgl_vendor=fingerprint.webgl_vendor,
                webgl_renderer=fingerprint.webgl_renderer,
                timezone_id=fingerprint.timezone_id,
                locale=fingerprint.locale,
            )
        # Under camoufox the engine owns the UA; only a *caller-forced* UA
        # (params["userAgent"]) overrides it. Otherwise echo camoufox's real
        # Firefox UA so the caller submits with a matching one.
        forced_ua = params.get("userAgent")
        context = await self._build_context(fingerprint, opts.proxy, forced_ua=forced_ua)
        return context, self._effective_user_agent(opts.user_agent, forced_ua)

    async def context_factory(
        self, fingerprint: FingerprintProfile, proxy: Optional[ProxyAsset]
    ) -> Tuple[BrowserContext, str]:
        """SessionPool-compatible factory: build a warm context from a fingerprint."""
        proxy_dict = proxy.playwright_proxy() if proxy is not None else None
        context = await self._build_context(fingerprint, proxy_dict)
        return context, self._effective_user_agent(fingerprint.user_agent, None)

    def _effective_user_agent(
        self, chromium_ua: str, forced_ua: Optional[str]
    ) -> str:
        """Resolve the UA to echo back to the caller.

        For camoufox, a caller-forced UA wins, else camoufox's engine UA (read
        at launch), else the Chromium fingerprint UA as a last resort. For any
        other runtime the Chromium fingerprint UA is authoritative.
        """
        if self._runtime == "camoufox":
            return forced_ua or self._camoufox_user_agent or chromium_ua
        return chromium_ua

    async def _build_context(
        self,
        fingerprint: FingerprintProfile,
        proxy: Optional[dict],
        *,
        forced_ua: Optional[str] = None,
    ) -> BrowserContext:
        assert self._browser is not None, "BrowserManager not started"
        if self._runtime == "camoufox":
            context = await self._build_camoufox_context(fingerprint, proxy, forced_ua)
        else:
            kwargs = context_kwargs(fingerprint, proxy)
            context = await self._browser.new_context(**kwargs)  # type: ignore[arg-type]
            # Chromium stealth JS only — camoufox spoofs at the engine level, so
            # injecting Chromium-shaped patches there would create contradictions.
            await context.add_init_script(build_stealth_js(fingerprint))
        # Per-context response byte counter. Accumulated from Content-Length
        # headers (chunked transfers undercount, but the bulk of captcha-solving
        # traffic — images, scripts, stylesheets — carries Content-Length).
        # Read by BaseBrowserSolver._release_context to report proxy bandwidth
        # and populate SolveRecord.proxy_bytes.
        context._omc_bytes_used = 0  # type: ignore[attr-defined]
        context.on("response", _count_response_bytes)
        # WP4: per-context resource interception. Registered BEFORE the solver
        # fulfill routes (hcaptcha/turnstile register ``context.route(website_url,
        # _fulfill_document)`` after _acquire_context returns). Playwright runs
        # route handlers in registration order, so this handler runs first and
        # either ``continue_()``s the request (passing it to the next handler)
        # or ``abort()``s it. The synthetic document request has resource_type
        # "document" (not in the block types), so it continues through to the
        # solver's fulfill handler; challenge asset hosts (hcaptcha.com etc.)
        # are on the allowlist and always continue.
        if self._resource_block_enabled:
            await context.route("**/*", self._resource_handler)
        return context

    async def _build_camoufox_context(
        self,
        fingerprint: FingerprintProfile,
        proxy: Optional[dict],
        forced_ua: Optional[str],
    ) -> BrowserContext:
        """Build a camoufox (Firefox) context: geo + proxy only, no Chrome spoof.

        Camoufox owns the navigator / WebGL / canvas / screen fingerprint, so we
        pass ONLY the egress-relevant options: the proxy (per-context egress),
        the geo-aligned locale/timezone (from the proxy-seeded fingerprint), and
        — only when the caller forced one — a UA override. No Chromium stealth
        script and no ``Sec-CH-UA`` headers (Firefox doesn't send client hints;
        emitting them would be a contradiction). ``no_viewport`` lets camoufox's
        own window/screen sizing stand instead of a Chromium-shaped viewport.
        """
        assert self._browser is not None
        kwargs: dict[str, Any] = {
            "locale": fingerprint.locale,
            "timezone_id": fingerprint.timezone_id,
            "no_viewport": True,
        }
        if proxy:
            kwargs["proxy"] = proxy
        if forced_ua:
            kwargs["user_agent"] = forced_ua
        return await self._browser.new_context(**kwargs)  # type: ignore[arg-type]

    async def _resource_handler(self, route: Any) -> None:
        """Per-context route handler that aborts bandwidth-heavy resources.

        Policy (in order, first match wins):
          1. Allowlist host suffix (challenge hosts) → ``continue_``.
          2. Blocklist host suffix (trackers) → ``abort``.
          3. Resource type in the configured block set → ``abort``.
          4. Otherwise → ``continue_``.

        Every step is guarded so a malformed URL / missing attribute never
        breaks solving — the handler falls through to ``continue_`` rather
        than raising. Challenge resources (hCaptcha / Turnstile / reCAPTCHA
        tile images, scripts) are on the allowlist and always pass through,
        which is the highest-risk regression surface for this feature.
        """
        try:
            url = route.request.url
        except Exception:
            await _safe_continue(route)
            return

        try:
            host = (urlparse(url).hostname or "").lower()
        except Exception:
            host = ""

        try:
            if host and any(
                host == suffix or host.endswith("." + suffix)
                for suffix in self._resource_allow_hosts
            ):
                await route.continue_()
                return
        except Exception:
            pass

        try:
            if host and self._resource_block_hosts and any(
                host == suffix or host.endswith("." + suffix)
                for suffix in self._resource_block_hosts
            ):
                await route.abort()
                return
        except Exception:
            pass

        try:
            if route.request.resource_type in self._resource_block_types:
                await route.abort()
                return
        except Exception:
            pass

        await _safe_continue(route)
