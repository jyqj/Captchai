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


def resolve_context_options(config: Config, params: dict[str, Any]) -> ContextOptions:
    """Build a coherent fingerprint + proxy for a task.

    A caller-supplied ``userAgent`` still wins (so the token binds to the UA the
    caller will submit with); otherwise the fingerprint's UA is used. The
    fingerprint is seeded by sitekey so repeat solves of the same target reuse a
    stable, coherent identity.
    """
    del config  # reserved for future per-config fingerprint policy
    seed = params.get("websiteKey") or params.get("proxy")
    fingerprint = generate_fingerprint(seed=seed)
    user_agent = params.get("userAgent") or fingerprint.user_agent
    return ContextOptions(
        user_agent=user_agent,
        proxy=_proxy_dict_from_params(params),
        fingerprint=fingerprint,
    )


class BrowserManager:
    """Owns one shared browser process for all solvers."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._runtime = getattr(config, "browser_runtime", "chromium")

    async def start(self) -> None:
        if self._browser is not None:
            return
        self._playwright = await async_playwright().start()
        self._browser = await self._launch()
        log.info(
            "Shared browser started (runtime=%s, headless=%s)",
            self._runtime,
            self._config.browser_headless,
        )

    async def _launch(self) -> Browser:
        assert self._playwright is not None
        launcher = self._playwright.chromium
        # rebrowser-playwright exposes the same API surface via the standard
        # `chromium` object once its patched driver is installed; camoufox ships
        # a Firefox build. We attempt the requested channel and degrade to stock
        # Chromium if the runtime isn't available in this environment.
        try:
            if self._runtime == "camoufox":
                return await self._playwright.firefox.launch(
                    headless=self._config.browser_headless
                )
            return await launcher.launch(
                headless=self._config.browser_headless,
                args=_LAUNCH_ARGS,
            )
        except Exception as exc:  # pragma: no cover - depends on host runtimes
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
        context = await self._build_context(fingerprint, opts.proxy)
        return context, opts.user_agent

    async def context_factory(
        self, fingerprint: FingerprintProfile, proxy: Optional[ProxyAsset]
    ) -> Tuple[BrowserContext, str]:
        """SessionPool-compatible factory: build a warm context from a fingerprint."""
        proxy_dict = proxy.playwright_proxy() if proxy is not None else None
        context = await self._build_context(fingerprint, proxy_dict)
        return context, fingerprint.user_agent

    async def _build_context(
        self, fingerprint: FingerprintProfile, proxy: Optional[dict]
    ) -> BrowserContext:
        assert self._browser is not None, "BrowserManager not started"
        kwargs = context_kwargs(fingerprint, proxy)
        context = await self._browser.new_context(**kwargs)  # type: ignore[arg-type]
        await context.add_init_script(build_stealth_js(fingerprint))
        return context
