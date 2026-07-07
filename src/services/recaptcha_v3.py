"""reCAPTCHA v3 solver using Playwright browser automation."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from playwright.async_api import Browser

from ..core.config import Config
from .browser import BrowserManager

log = logging.getLogger(__name__)

# JS executed inside the browser to obtain a reCAPTCHA v3 token.
# Handles both standard and enterprise reCAPTCHA libraries.
_EXECUTE_JS = """
([key, action]) => new Promise((resolve, reject) => {
    const gr = window.grecaptcha?.enterprise || window.grecaptcha;
    if (gr && typeof gr.execute === 'function') {
        gr.ready(() => {
            gr.execute(key, {action}).then(resolve).catch(reject);
        });
        return;
    }
    // grecaptcha not loaded yet — inject the script ourselves
    const script = document.createElement('script');
    script.src = 'https://www.google.com/recaptcha/api.js?render=' + key;
    script.onerror = () => reject(new Error('Failed to load reCAPTCHA script'));
    script.onload = () => {
        const g = window.grecaptcha;
        if (!g) { reject(new Error('grecaptcha still undefined after script load')); return; }
        g.ready(() => {
            g.execute(key, {action}).then(resolve).catch(reject);
        });
    };
    document.head.appendChild(script);
})
"""

class RecaptchaV3Solver:
    """Solves RecaptchaV3TaskProxyless tasks via a shared headless Chromium."""

    def __init__(
        self,
        config: Config,
        manager: BrowserManager | None = None,
        browser: Browser | None = None,
    ) -> None:
        self._config = config
        self._manager = manager or BrowserManager(config)
        self._owns_manager = manager is None
        if browser is not None:
            self._manager._browser = browser  # type: ignore[attr-defined]

    async def start(self) -> None:
        if self._owns_manager:
            await self._manager.start()

    async def stop(self) -> None:
        if self._owns_manager:
            await self._manager.stop()
        log.info("RecaptchaV3Solver stopped")

    async def solve(self, params: dict[str, Any]) -> dict[str, Any]:
        website_url = params["websiteURL"]
        website_key = params["websiteKey"]
        page_action = params.get("pageAction") or params.get("action") or "verify"

        last_error: Exception | None = None
        for attempt in range(self._config.captcha_retries):
            try:
                token, user_agent = await self._solve_once(
                    website_url, website_key, page_action, params
                )
                return {"gRecaptchaResponse": token, "userAgent": user_agent}
            except Exception as exc:
                last_error = exc
                log.warning(
                    "Attempt %d/%d failed for %s: %s",
                    attempt + 1,
                    self._config.captcha_retries,
                    website_url,
                    exc,
                )
                if attempt < self._config.captcha_retries - 1:
                    await asyncio.sleep(2)

        raise RuntimeError(
            f"Failed after {self._config.captcha_retries} attempts: {last_error}"
        )

    async def _solve_once(
        self, website_url: str, website_key: str, page_action: str, params: dict[str, Any]
    ) -> tuple[str, str]:
        context, user_agent = await self._manager.new_context(params)
        page = await context.new_page()

        try:
            timeout_ms = self._config.browser_timeout * 1000
            await page.goto(
                website_url, wait_until="networkidle", timeout=timeout_ms
            )

            # Simulate minimal human-like behaviour to improve score
            await page.mouse.move(400, 300)
            await asyncio.sleep(1)
            await page.mouse.move(600, 400)
            await asyncio.sleep(0.5)

            # Wait for reCAPTCHA to become available (may already be on page)
            try:
                await page.wait_for_function(
                    "(typeof grecaptcha !== 'undefined' && typeof grecaptcha.execute === 'function') "
                    "|| (typeof grecaptcha !== 'undefined' && typeof grecaptcha?.enterprise?.execute === 'function')",
                    timeout=10_000,
                )
            except Exception:
                log.info(
                    "grecaptcha not detected on page, will attempt script injection"
                )

            token = await page.evaluate(_EXECUTE_JS, [website_key, page_action])

            if not isinstance(token, str) or len(token) < 20:
                raise RuntimeError(f"Invalid token received: {token!r}")

            log.info(
                "Got reCAPTCHA token for %s (len=%d)", website_url, len(token)
            )
            return token, user_agent
        finally:
            await context.close()
