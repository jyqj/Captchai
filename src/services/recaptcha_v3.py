"""reCAPTCHA v3 solver using Playwright browser automation."""

from __future__ import annotations

import logging
from typing import Any

from .browser_solver import BaseBrowserSolver, has_task_proxy

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

class RecaptchaV3Solver(BaseBrowserSolver):
    """Solves RecaptchaV3TaskProxyless tasks via a shared headless Chromium.

    Context acquisition / release and proxy categorisation are handled by
    :class:`BaseBrowserSolver`.
    """

    async def solve(self, params: dict[str, Any]) -> dict[str, Any]:
        website_url = params["websiteURL"]
        website_key = params["websiteKey"]
        page_action = params.get("pageAction") or params.get("action") or "verify"
        client_key = params.get("_clientKey")

        # reCAPTCHA v3 scores behaviour, not the egress IP, so default to a
        # proxyless server-egress session. Respect an explicit caller egress or
        # a caller-supplied task proxy (which implies a chosen egress).
        if not has_task_proxy(params):
            params.setdefault("egress", "proxyless")

        return await self._solve_with_retries(
            params,
            sitekey=website_key,
            client_key=client_key,
            attempt_fn=lambda: self._solve_once(
                website_url, website_key, page_action, params
            ),
            build_solution=lambda token, ua: {
                "gRecaptchaResponse": token,
                "userAgent": ua,
            },
            provider="reCAPTCHA v3",
            default_task_type=params.get("type", "RecaptchaV3TaskProxyless"),
            default_challenge_shape="widget",
        )

    async def _solve_once(
        self, website_url: str, website_key: str, page_action: str, params: dict[str, Any]
    ) -> tuple[str, str]:
        solve_context = await self._acquire_context(params)
        self._stash_fingerprint_geo(solve_context, params)
        context = solve_context.context
        user_agent = solve_context.user_agent
        page = await context.new_page()

        solved = False
        try:
            timeout_ms = self._config.browser_timeout * 1000
            await page.goto(
                website_url, wait_until="domcontentloaded", timeout=timeout_ms
            )

            # A short human-like pre-interaction to warm the behaviour score.
            # Reduced from two fixed sleeps to a single eased mouse move so
            # v3 still gets a real-interaction signal without dead time.
            await self._human_mouse(page)

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
            solved = True
            return token, user_agent
        finally:
            await self._release_context(solve_context, solved, params)
