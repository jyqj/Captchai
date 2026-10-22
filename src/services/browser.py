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
  installed. ``rebrowser`` launches through ``rebrowser_playwright``'s patched
  driver; the manager records the *actually* started runtime + driver on
  ``effective_runtime`` / ``driver`` so a silent stock launch is observable.

``context_factory`` implements the signature the warm :class:`SessionPool`
expects (``async (fingerprint, proxy) -> (context, user_agent)``), so the pool
can create sessions without importing Playwright itself.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
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
    chrome_major,
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
    "Chrome/149.0.0.0 Safari/537.36"
)

# NOTE: ``--disable-gpu`` is deliberately NOT set. Disabling the GPU forces
# Chromium onto the SwiftShader software rasteriser, whose WebGL parameters
# (extensions, precision, un-spoofed capabilities) read as software rendering
# and CONTRADICT the discrete-GPU vendor/renderer string the stealth layer
# spoofs — a coherent-fingerprint mismatch enterprise hCaptcha detectors flag.
# Leaving the GPU enabled lets a GPU-equipped host (or headful runtime) present
# real hardware WebGL consistent with the spoofed GPU; on a GPU-less headless
# host Chromium still falls back to ANGLE/SwiftShader (no worse than before).
#
# NOTE: ``--no-sandbox`` and the site-isolation-disable flags are deliberately
# NOT part of the default set. ``--no-sandbox`` removes Chromium's renderer
# sandbox (a security downgrade and an automation tell), so it is added only
# when the operator opts in via ``BROWSER_DISABLE_SANDBOX=true`` or, in auto
# mode (``None``), when running as root on POSIX — the container case where
# the setuid sandbox cannot start (never on Windows). Disabling site isolation
# is likewise opt-in via ``BROWSER_DISABLE_SITE_ISOLATION=true``: a browser
# with ``IsolateOrigins,site-per-process`` off behaves (and is detectable)
# differently across origins. See ``BrowserManager._launch_args``.
_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    # WebRTC egress leak guard: a captcha token is IP-bound, but WebRTC can
    # surface the host's real (pre-proxy) local/public IP via ICE candidates,
    # contradicting the proxy egress and handing enterprise risk models a
    # datacenter-IP tell. ``disable_non_proxied_udp`` forces WebRTC through the
    # same proxy as the rest of the context (or not at all), so no unproxied
    # candidate leaks. Camoufox handles this via CAMOUFOX_BLOCK_WEBRTC instead.
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
]
_DISABLE_SITE_ISOLATION_ARGS = [
    "--disable-features=IsolateOrigins,site-per-process",
    "--disable-site-isolation-trials",
]

# Chrome version token (``Chrome/149.0.7827.55`` → ``149.0.7827.55``). Used to
# keep a caller-forced UA coherent with the spoofed client hints.
_CHROME_UA_RE = re.compile(r"Chrome/(\d+(?:\.\d+)*)")


def _parse_csv(value: str) -> set[str]:
    """Split a comma-separated config string into a lowercased set of tokens."""
    return {token.strip().lower() for token in (value or "").split(",") if token.strip()}


def _chrome_ua_version(user_agent: str) -> Optional[Tuple[str, str]]:
    """Return ``(major, full_version)`` from a Chrome UA, or ``None``.

    ``full_version`` is the version token exactly as it appears in the UA
    (``"149.0.0.0"`` for the reduced form, ``"149.0.7827.55"`` for a full
    one), so the spoofed ``uaFullVersion`` never contradicts the UA string.
    """
    match = _CHROME_UA_RE.search(user_agent or "")
    if match is None:
        return None
    version = match.group(1)
    return version.split(".", 1)[0], version


def _ua_client_hint_align_js(user_agent: str) -> Optional[str]:
    """JS init script aligning ``navigator.userAgentData`` with ``user_agent``.

    ``build_stealth_js`` derives the low-entropy brands from the fingerprint's
    UA major, but pins ``fullVersionList`` / ``uaFullVersion`` to the bundled
    engine's full version. When a caller forces an older/newer Chrome UA (so
    the token binds to the UA they will submit with), a low-entropy brand of
    ``131`` next to ``uaFullVersion`` ``149.x`` is a contradiction detectors
    flag. Registered AFTER the stealth script (init scripts run in order), so
    it only rewrites the two high-entropy values, preserving the GREASE brand.
    Returns ``None`` when the UA has no parseable Chrome version.
    """
    parsed = _chrome_ua_version(user_agent)
    if parsed is None:
        return None
    major, full_version = parsed
    return f"""
(() => {{
  try {{
    const uaData = navigator.userAgentData;
    if (!uaData || typeof uaData.getHighEntropyValues !== 'function') return;
    const major = {json.dumps(major)};
    const fullVersion = {json.dumps(full_version)};
    const align = (brands, version) => (brands || []).map((b) => (
      (b && (b.brand === 'Google Chrome' || b.brand === 'Chromium'))
        ? {{ brand: b.brand, version: version }}
        : b
    ));
    const orig = uaData.getHighEntropyValues.bind(uaData);
    Object.defineProperty(uaData, 'getHighEntropyValues', {{
      configurable: true,
      enumerable: true,
      writable: true,
      value: (hints) => orig(hints).then((values) => Object.assign({{}}, values, {{
        brands: align(values.brands || uaData.brands, major),
        fullVersionList: align(values.fullVersionList, fullVersion),
        uaFullVersion: fullVersion,
      }})),
    }});
    try {{
      Object.defineProperty(uaData, 'brands', {{
        configurable: true,
        enumerable: true,
        writable: true,
        value: align(uaData.brands, major),
      }});
    }} catch (e) {{}}
  }} catch (e) {{}}
}})();
"""


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


def set_context_resource_blocking(context: Any, enabled: bool) -> None:
    """Toggle per-context resource interception at solve time.

    Resource interception (aborting image/media/font/stylesheet) is a pure
    bandwidth optimisation that is safe on the *synthetic injected pages* used
    for regular hCaptcha / Turnstile, but a strong bot signal on a *real
    target page*: a browser that never requests any CSS/font/image produces a
    network waterfall no human browser would. Enterprise real-page solves
    therefore disable blocking so the page loads like a real browser.

    The flag is read by :meth:`BrowserManager._resource_handler` on every
    request; setting it ``False`` makes the handler pass everything through.
    Works on both fresh contexts and reused warm-session contexts (the flag is
    re-asserted at the start of every solve), and degrades to a no-op on fake
    contexts used in tests.
    """
    try:
        context._omc_block_resources = bool(enabled)  # type: ignore[attr-defined]
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
    # P1-4: draw a mobile (Android Chrome) fingerprint when the egress is a
    # mobile proxy. The pool path stashes ``_pool_proxy_mobile``; a caller
    # task proxy carries ``kind=mobile`` via its |kind= annotation.
    mobile = bool(params.get("_pool_proxy_mobile"))
    if (
        not mobile
        and not params.get("_proxy_override")
        and params.get("_proxyKind") != "proxyless"
    ):
        task_asset = proxy_from_params(params)
        if task_asset is not None and task_asset.kind == "mobile":
            mobile = True
    fingerprint = generate_fingerprint(
        seed=params.get("_proxy_seed") or None,
        timezone_id=pool_geo.get("timezone") or None,
        locale=pool_geo.get("locale") or None,
        mobile=mobile,
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
    # Touch modality follows the fingerprint so the solver drives touch taps /
    # scroll motion (not mouse events) on a mobile context.
    params["_is_mobile"] = bool(fingerprint.is_mobile)

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
        # What ACTUALLY launched (runtime + Playwright driver package), as
        # opposed to what was requested. ``not-started`` until ``start()``
        # succeeds; a rebrowser request that degrades records
        # ``chromium(rebrowser-unavailable)`` so health/telemetry can tell it
        # apart from a deliberate stock launch.
        self.effective_runtime: str = "not-started"
        self.driver: str = "not-started"
        self._driver = "not-started"
        self._effective_runtime_override: Optional[str] = None
        # Supervisor state: flipped by the browser's ``disconnected`` event so
        # ``is_ready()`` reports a dead process even if the Playwright
        # connection check is unavailable. A future supervisor loop can watch
        # this flag to relaunch.
        self._disconnected = False
        # Launch-arg gating. ``browser_disable_sandbox`` is tri-state
        # (None = auto: root on POSIX only); site-isolation disabling is
        # strictly opt-in. Read defensively so test configs built from a bare
        # ``SimpleNamespace`` keep the safe defaults.
        self._disable_sandbox = getattr(config, "browser_disable_sandbox", None)
        self._disable_site_isolation = bool(
            getattr(config, "browser_disable_site_isolation", False)
        )
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

    def is_ready(self) -> bool:
        """True when the shared browser process is connected and alive.

        Combines the supervisor's ``disconnected`` flag (which catches crashes
        even when the Playwright connection check is unavailable) with a
        guarded ``browser.is_connected()`` call. Never raises: fake browsers
        and partially initialised Playwright objects return False.
        """
        if self._browser is None or self._disconnected:
            return False
        try:
            is_connected = getattr(self._browser, "is_connected", None)
            if not callable(is_connected):
                return False
            return bool(is_connected())
        except Exception:  # noqa: BLE001 - readiness probe is best-effort
            return False

    def _sandbox_disabled(self) -> bool:
        """Whether Chromium should be launched with ``--no-sandbox``.

        Explicit ``BROWSER_DISABLE_SANDBOX`` wins. Auto (``None``) disables
        the sandbox only when running as root on POSIX, where Chromium's
        setuid sandbox cannot start — the container case. Never on Windows.
        """
        if self._disable_sandbox is True:
            return True
        if self._disable_sandbox is False:
            return False
        if sys.platform == "win32":
            return False
        try:
            return os.geteuid() == 0
        except (AttributeError, OSError):
            return False

    def _launch_args(self) -> list[str]:
        """Resolve Chromium launch args, applying the config-gated flags.

        The baseline (``_LAUNCH_ARGS``) is always applied; ``--no-sandbox``
        and the site-isolation-disable flags are opt-in (see the notes on
        ``_LAUNCH_ARGS``) so the default launch is sandboxed and presents the
        standard cross-origin isolation behaviour a real Chrome does.
        """
        args = list(_LAUNCH_ARGS)
        if self._sandbox_disabled():
            args.append("--no-sandbox")
        if self._disable_site_isolation:
            args.extend(_DISABLE_SITE_ISOLATION_ARGS)
        return args

    def _prepare_runtime(self) -> None:
        """Resolve the requested runtime against what is actually installed.

        Strict mode makes an unavailable hardened runtime fatal; lenient mode
        degrades to stock Chromium and records the divergence (loudly logged
        by ``start``). Also invoked from ``_launch`` so direct callers get the
        same guard.
        """
        if self._runtime not in {"camoufox", "rebrowser"}:
            return
        if self._hardened_runtime_available(self._runtime):
            return
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
        if self._runtime == "rebrowser":
            # Distinguish "asked for rebrowser, ran stock Playwright" from a
            # deliberate chromium launch in health/telemetry.
            self._effective_runtime_override = "chromium(rebrowser-unavailable)"
            self._driver = "playwright"
        self._runtime = "chromium"

    async def _start_playwright(self) -> Playwright:
        """Start the Playwright driver matching the resolved runtime.

        ``rebrowser`` must start ``rebrowser_playwright``'s patched driver —
        the stock ``playwright`` package would silently launch an unpatched
        browser (the audit's runtime-truth bug). If that import fails even
        though ``find_spec`` saw the package, strict mode raises (the operator
        asked for a runtime that cannot start); lenient mode degrades to stock
        and records ``chromium(rebrowser-unavailable)``.
        """
        if self._runtime == "rebrowser":
            try:
                from rebrowser_playwright.async_api import (  # type: ignore[reportMissingImports]
                    async_playwright as rebrowser_async_playwright,
                )
            except Exception as exc:  # noqa: BLE001 - broken/partial install
                # ``_prepare_runtime`` only proved the package is *findable*;
                # a broken/partial install can still fail the real import.
                # Strict mode must be honoured here too — otherwise the
                # BROWSER_RUNTIME_STRICT contract is bypassed by the very
                # failure it exists to catch.
                if self._runtime_strict:
                    raise RuntimeError(
                        "BROWSER_RUNTIME=rebrowser was requested but the "
                        "rebrowser_playwright driver could not be imported "
                        f"({exc}) and BROWSER_RUNTIME_STRICT=true forbids the "
                        "stock-Playwright fallback. Reinstall "
                        "rebrowser-playwright or change BROWSER_RUNTIME."
                    ) from exc
                log.warning(
                    "BROWSER_RUNTIME=rebrowser but the rebrowser_playwright "
                    "driver could not be imported (%s); falling back to stock "
                    "Playwright. Effective runtime recorded as "
                    "'chromium(rebrowser-unavailable)' — enterprise/anti-bot "
                    "targets may flag this browser.",
                    exc,
                )
                self._runtime = "chromium"
                self._driver = "playwright"
                self._effective_runtime_override = "chromium(rebrowser-unavailable)"
                return await async_playwright().start()
            self._driver = "rebrowser-playwright"
            return await rebrowser_async_playwright().start()
        self._driver = "playwright"
        return await async_playwright().start()

    def _set_effective_runtime(self) -> None:
        """Record what actually launched (runtime + driver package)."""
        if self._effective_runtime_override is not None:
            self.effective_runtime = self._effective_runtime_override
        elif self._runtime == "camoufox":
            self.effective_runtime = "camoufox"
        elif self._runtime == "rebrowser":
            self.effective_runtime = "rebrowser"
        else:
            self.effective_runtime = "chromium"
        self.driver = self._driver

    def _attach_supervisor(self, browser: Browser) -> None:
        """Wire the disconnect supervisor onto the launched browser.

        Phase 0 only records state and logs; a later supervisor can watch
        ``self._disconnected`` (and add a relaunch loop) without changing the
        hook points.
        """
        try:
            browser.on("disconnected", self._on_browser_disconnected)
        except Exception as exc:  # noqa: BLE001 - fake/partial browsers
            log.warning("Could not attach browser disconnect supervisor: %s", exc)

    def _on_browser_disconnected(self, *_args: Any) -> None:
        """Mark the manager not-ready when the browser process dies."""
        self._disconnected = True
        log.warning(
            "Shared browser process disconnected (effective_runtime=%s); "
            "manager is no longer ready. No auto-relaunch is configured in "
            "this phase.",
            self.effective_runtime,
        )

    async def start(self) -> None:
        if self._browser is not None:
            return
        # Resolve availability BEFORE starting a driver: strict mode must fail
        # before any stock Playwright process is spawned, and rebrowser needs
        # its own patched driver started below.
        self._prepare_runtime()
        self._playwright = await self._start_playwright()
        self._browser = await self._launch()
        self._disconnected = False
        self._attach_supervisor(self._browser)
        if self._runtime == "camoufox":
            await self._cache_camoufox_user_agent()
        else:
            await self._validate_engine_version()
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
            "Shared browser started (runtime=%s, effective_runtime=%s, "
            "driver=%s, requested=%s, headless=%s)",
            self._runtime,
            self.effective_runtime,
            self.driver,
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
        # Already-imported packages are available by definition; this also
        # keeps test-injected fake modules (which often have no ``__spec__``)
        # usable without find_spec raising ValueError.
        if module in sys.modules:
            return True
        try:
            return importlib.util.find_spec(module) is not None
        except (ImportError, AttributeError, ValueError):
            # A partially installed package must not crash startup; treat it
            # as unavailable and let the import in ``_start_playwright``
            # decide (it degrades with the rebrowser-unavailable marker).
            return False

    async def _launch(self) -> Browser:
        assert self._playwright is not None
        # Guard hardened runtimes before we attempt a launch so an operator who
        # asked for camoufox/rebrowser but never installed it gets a clear
        # signal instead of a silent degrade. In strict mode this is fatal; in
        # lenient mode we degrade to stock Chromium (and start() warns loudly).
        self._prepare_runtime()
        launcher = self._playwright.chromium

        try:
            if self._runtime == "camoufox":
                browser = await self._launch_camoufox()
            else:
                # rebrowser-playwright exposes the same API surface via the
                # standard `chromium` object once its patched driver is started
                # (see ``_start_playwright``).
                browser = await launcher.launch(
                    headless=self._config.browser_headless,
                    args=self._launch_args(),
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
            if self._runtime == "rebrowser":
                # ``launcher`` belongs to the rebrowser driver that just
                # failed; retrying through it would replay the same failure.
                # Start the stock Playwright driver and launch there.
                browser = await self._relaunch_on_stock_playwright()
            else:
                self._runtime = "chromium"
                browser = await launcher.launch(
                    headless=self._config.browser_headless,
                    args=self._launch_args(),
                )
        # Record the actual runtime + driver only after the launch succeeded.
        self._set_effective_runtime()
        return browser

    async def _relaunch_on_stock_playwright(self) -> Browser:
        """Relaunch Chromium through the stock Playwright driver.

        Called when the ``rebrowser`` (patched) driver started but failed to
        launch a browser. Retrying through the same Playwright instance would
        just replay the patched driver's failure, so the patched driver is
        stopped and a fresh stock ``playwright`` driver is started. The
        divergence is recorded as ``chromium(rebrowser-launch-failed)`` on
        ``effective_runtime`` so health/telemetry cannot mistake this for a
        deliberate stock launch.
        """
        log.warning(
            "rebrowser driver launch failed; restarting the browser on stock "
            "Playwright. Effective runtime will be recorded as "
            "'chromium(rebrowser-launch-failed)' — enterprise/anti-bot "
            "targets may flag this browser."
        )
        try:
            if self._playwright is not None:
                await self._playwright.stop()
        except Exception:  # noqa: BLE001 - a broken driver may not stop cleanly
            log.debug("stopping the failed rebrowser driver raised", exc_info=True)
        self._playwright = await async_playwright().start()
        self._runtime = "chromium"
        self._driver = "playwright"
        self._effective_runtime_override = "chromium(rebrowser-launch-failed)"
        return await self._playwright.chromium.launch(
            headless=self._config.browser_headless,
            args=self._launch_args(),
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
        # Optional runtime dependency: absent from a stock install, imported
        # only when BROWSER_RUNTIME=camoufox selected it.
        from camoufox import AsyncNewBrowser  # type: ignore[reportMissingImports]  # noqa: WPS433
        from camoufox.utils import launch_options as camoufox_launch_options  # type: ignore[reportMissingImports]

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

    async def _validate_engine_version(self) -> None:
        """Check the live Chromium major matches the pinned fingerprint major.

        The fingerprint pool presents ``Chrome/<_CHROME_MAJOR>`` in the UA and
        client hints; if the actually-launched engine reports a *different*
        major (a stale ``_CHROME_MAJOR`` pin, or a Playwright up/downgrade that
        moved the bundled Chromium), every solve ships a UA/CH version that
        contradicts the real engine — exactly the kind of drift that rots into
        a detection signal over time (a "Chrome/131 in 2026" tell).

        Best-effort: reads ``browser.version`` (e.g. ``"149.0.7827.55"``),
        compares the major to ``chrome_major()``. On mismatch it warns loudly
        (or raises when ``BROWSER_RUNTIME_STRICT`` is set) so an operator finds
        out at startup instead of via a slow decline in enterprise pass rate.
        A version that can't be read is skipped silently (fake browsers in
        tests, odd runtimes).
        """
        try:
            version = getattr(self._browser, "version", None)
            if callable(version):
                version = version()
            if not isinstance(version, str) or not version:
                return
        except Exception:  # noqa: BLE001 - version read is best-effort
            return
        engine_major = version.split(".", 1)[0]
        pinned = chrome_major()
        if engine_major == pinned:
            log.info(
                "Browser engine version %s matches pinned fingerprint major %s",
                version,
                pinned,
            )
            return
        msg = (
            f"Browser engine major {engine_major!r} (full {version!r}) does not "
            f"match the pinned fingerprint Chrome major {pinned!r}. Update "
            f"_CHROME_MAJOR/_CHROME_FULL_VERSION in src/assets/fingerprint.py "
            f"to track the Playwright-bundled Chromium, or pin Playwright to the "
            f"version that ships Chrome {pinned}."
        )
        if self._runtime_strict:
            raise RuntimeError(msg + " (BROWSER_RUNTIME_STRICT=true)")
        log.warning("%s Fingerprints will present a contradictory version.", msg)

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
        # A stopped manager has no launched runtime; reset so is_ready() and
        # health can't report a stale browser.
        self._disconnected = False
        self.effective_runtime = "not-started"
        self.driver = "not-started"
        self._driver = "not-started"
        self._effective_runtime_override = None
        log.info("Shared browser stopped")

    async def new_context(self, params: dict[str, Any]) -> Tuple[BrowserContext, str]:
        """Create an isolated context bound to the task's proxy/UA + fingerprint.

        Returns the context and the resolved User-Agent so the solver can echo it
        back to the caller for token/UA binding.
        """
        opts = resolve_context_options(self._config, params)
        # Honour a caller-forced UA by projecting it onto the fingerprint, but
        # only when it is reconcilable with the Chromium client hints. A UA
        # with no parseable Chrome major would leave ``navigator.userAgentData``
        # / ``Sec-CH-UA`` claiming one browser while the UA claims another — a
        # detection contradiction — so drop the override (and log) instead.
        fingerprint = opts.fingerprint
        forced_ua = params.get("userAgent")
        if forced_ua and self._runtime != "camoufox":
            if _chrome_ua_version(forced_ua) is None:
                log.warning(
                    "Caller-supplied userAgent %r has no parseable Chrome "
                    "major version; dropping the UA override and using the "
                    "coherent fingerprint UA %r.",
                    forced_ua,
                    fingerprint.user_agent,
                )
                forced_ua = None
        if forced_ua and forced_ua != fingerprint.user_agent:
            fingerprint = FingerprintProfile(
                user_agent=forced_ua,
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
                is_mobile=fingerprint.is_mobile,
                device_scale_factor=fingerprint.device_scale_factor,
            )
        # Under camoufox the engine owns the UA; only a *caller-forced* UA
        # (params["userAgent"]) overrides it. Otherwise echo camoufox's real
        # Firefox UA so the caller submits with a matching one.
        context = await self._build_context(
            fingerprint,
            opts.proxy,
            forced_ua=forced_ua,
            # These contexts navigate REAL target pages (recaptcha v2/v3 and
            # every fresh-context solve): aborting CSS/fonts/images on a real
            # page is a bot tell, so default to passthrough. The synthetic
            # widget solvers re-enable blocking in their own prepare step.
            block_resources=False,
        )
        return context, self._effective_user_agent(fingerprint.user_agent, forced_ua)

    async def context_factory(
        self, fingerprint: FingerprintProfile, proxy: Optional[ProxyAsset]
    ) -> Tuple[BrowserContext, str]:
        """SessionPool-compatible factory: build a warm context from a fingerprint."""
        proxy_dict = proxy.playwright_proxy() if proxy is not None else None
        # Warm sessions back real-page solves too (recaptcha v2/v3 use the
        # pool path); widget solvers that inject a synthetic page explicitly
        # re-enable blocking before their navigation.
        context = await self._build_context(
            fingerprint, proxy_dict, block_resources=False
        )
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
        block_resources: Optional[bool] = None,
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
            if forced_ua:
                # Keep navigator.userAgentData's high-entropy fullVersionList /
                # uaFullVersion aligned with the caller-forced UA's Chrome
                # major (the stealth script pins them to the bundled engine
                # version). Registered after the stealth script so it patches
                # the object that script installed.
                align_js = _ua_client_hint_align_js(forced_ua)
                if align_js:
                    await context.add_init_script(align_js)
        # Per-context response byte counter. Accumulated from Content-Length
        # headers (chunked transfers undercount, but the bulk of captcha-solving
        # traffic — images, scripts, stylesheets — carries Content-Length).
        # Read by BaseBrowserSolver._release_context to report proxy bandwidth
        # and populate SolveRecord.proxy_bytes.
        context._omc_bytes_used = 0  # type: ignore[attr-defined]
        # Per-context resource-blocking switch. ``None`` keeps the historical
        # direct-call default (on) for backward compatibility; the production
        # entry points (``new_context`` / ``context_factory``) pass False
        # because their callers navigate real target pages, where aborting
        # CSS/fonts/images is a bot tell. Synthetic widget solvers re-enable
        # blocking in their prepare step via ``set_context_resource_blocking``.
        context._omc_block_resources = (  # type: ignore[reportAttributeAccessIssue]
            True if block_resources is None else bool(block_resources)
        )
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

        The per-context ``_omc_block_resources`` switch (default on) is checked
        first: enterprise real-page solves turn it off so the real target page
        loads all its resources like a human browser (see
        ``set_context_resource_blocking``).
        """
        # Per-context kill switch: when a solve disabled blocking (real-page /
        # enterprise), pass everything through so the page's own CSS/fonts/
        # images load like a real browser.
        try:
            ctx = route.request.frame.page.context
            if getattr(ctx, "_omc_block_resources", True) is False:
                await route.continue_()
                return
        except Exception:
            pass

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
