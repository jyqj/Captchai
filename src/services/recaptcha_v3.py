"""reCAPTCHA v3 solver using Playwright browser automation."""

from __future__ import annotations

import logging
from typing import Any

from ..models.task import AttemptContext
from .browser_solver import BaseBrowserSolver, SolveStage, has_task_proxy

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
            verify_provider="recaptcha",
        )

    #: Provider label used by the shared real-page template in logs/errors.
    PROVIDER_LABEL = "reCAPTCHA v3"

    async def _solve_once(
        self, website_url: str, website_key: str, page_action: str, params: dict[str, Any]
    ) -> tuple[str, str]:
        """Thin wrapper over the shared real-page template.

        ``page_action`` / ``website_key`` are retained for API compatibility;
        the hook re-derives the exact same values from ``params`` (the source
        ``solve`` read them from).
        """
        return await self._solve_real_page_once(params, website_url)

    def _invalid_token_message(self, token: Any) -> str:
        # Preserve the pre-refactor wording tests assert on.
        return f"Invalid token received: {token!r}"

    async def _prepare_page(
        self, page: Any, params: dict[str, Any], attempt: AttemptContext
    ) -> None:
        """Warm the behaviour score and wait (briefly) for the SDK."""
        # A short human-like pre-interaction to warm the behaviour score.
        # Reduced from two fixed sleeps to a single eased mouse move so
        # v3 still gets a real-interaction signal without dead time.
        await self._human_mouse(page)
        # Wait for reCAPTCHA to become available (may already be on page).
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

    async def _extract_token(
        self, page: Any, params: dict[str, Any], attempt: AttemptContext
    ) -> str | None:
        """Execute the action and return the v3 token."""
        self._stamp_phase(params, SolveStage.CHALLENGE)
        action = params.get("pageAction") or params.get("action") or "verify"
        return await page.evaluate(
            _EXECUTE_JS, [params["websiteKey"], action]
        )
