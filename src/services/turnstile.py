"""Cloudflare Turnstile solver using Playwright browser automation.

Supports TurnstileTaskProxyless and TurnstileTaskProxylessM1 task types.

Solving strategy — sitekey injection (primary):
  Rather than loading the real target URL (which is frequently behind a
  Cloudflare interstitial that blocks headless Chromium before the widget even
  renders), we intercept the top-level document request for ``websiteURL`` and
  serve a minimal synthetic page that renders the Turnstile widget for the given
  sitekey via ``turnstile.render``. Because the request is fulfilled *as* the
  target origin, ``window.location``/document origin match the real host, so the
  token is bound to the correct domain.

Real-page mode (parity with hCaptcha): when ``TURNSTILE_REAL_PAGE`` is set, the
solver instead navigates to the real target URL and hooks the page's own
``turnstile.render`` — for targets whose Cloudflare interstitial the synthetic
page can't satisfy. Both modes share :class:`InjectedWidgetSolver`'s
``_run_page_solve`` template (acquire → prepare → goto → interact → release,
with phase timing + stage tracking) and the single event-driven ``_poll_token``,
so hCaptcha and Turnstile can't drift into two separately-rotting copies — the
class of bug that produced the original missing ``import asyncio``.

Key correctness details for real production widgets:
  * ``action`` / ``cData`` / ``chlPageData`` are forwarded to ``render`` — a
    widget configured with these rejects tokens generated without them.
  * The context is bound to the task proxy and User-Agent; the resolved
    User-Agent is echoed back so the caller submits with a matching UA.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .browser_solver import SolveStage
from .injected_widget import InjectedWidgetSolver

log = logging.getLogger(__name__)

_EXTRACT_TURNSTILE_TOKEN_JS = """
() => {
    let token = null;
    if (window.__omcToken) {
        token = window.__omcToken;
    } else {
        const input = document.querySelector('[name="cf-turnstile-response"]')
            || document.querySelector('input[name*="turnstile"]');
        if (input && input.value && input.value.length > 20) {
            token = input.value;
        } else if (window.turnstile && typeof window.turnstile.getResponse === 'function') {
            try {
                const resp = window.turnstile.getResponse();
                if (resp && resp.length > 20) token = resp;
            } catch (e) {}
        }
    }
    return {token: token, error: window.__omcError || null};
}
"""


class TurnstileSolver(InjectedWidgetSolver):
    """Solves Cloudflare Turnstile tasks via a shared headless Chromium."""

    PROVIDER = "Turnstile"
    WIDGET_GLOBAL = "turnstile"
    WIDGET_API_JS = (
        "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit"
    )
    WIDGET_CONTAINER_ID = "omc-turnstile"
    # Turnstile's error-callback historically took no argument and set a bare
    # ``true``; kept identical so behaviour is unchanged.
    WIDGET_ERROR_CALLBACK_VALUE = "true"
    WIDGET_TOKEN_EXTRACTOR_JS = _EXTRACT_TURNSTILE_TOKEN_JS

    # Selector for the Turnstile widget iframe on the injected page.
    _WIDGET_IFRAME = 'iframe[src*="challenges.cloudflare.com"]'
    _CHECKBOX_SELECTOR = 'input[type="checkbox"], label'

    async def solve(self, params: dict[str, Any]) -> dict[str, Any]:
        website_url = params["websiteURL"]
        website_key = params["websiteKey"]
        client_key = params.get("_clientKey")

        render_options: dict[str, Any] = {}
        if params.get("action"):
            render_options["action"] = params["action"]
        if params.get("cData"):
            render_options["cData"] = params["cData"]
        if params.get("chlPageData"):
            render_options["chlPageData"] = params["chlPageData"]

        # Real-page mode is opt-in (TURNSTILE_REAL_PAGE) — the injected page is
        # the default because it sidesteps the Cloudflare interstitial. A caller
        # can also force it per task via ``params["realPage"]``.
        use_real_page = bool(
            params.get("realPage")
            or getattr(self._config, "turnstile_real_page", False)
        )
        strategy = (
            self._real_page_strategy(render_options)
            if use_real_page
            else self._injected_page_strategy(website_url, website_key, render_options)
        )

        return await self._solve_with_retries(
            params,
            sitekey=website_key,
            client_key=client_key,
            attempt_fn=lambda: self._run_page_solve(
                website_url,
                website_key,
                render_options,
                params,
                client_key,
                strategy=strategy,
            ),
            build_solution=lambda token, ua: {"token": token, "userAgent": ua},
            provider="Turnstile",
            default_task_type=params.get("type", "TurnstileTaskProxyless"),
            default_challenge_shape="widget",
            verify_provider="turnstile",
        )

    async def _interact(
        self,
        page: Any,
        website_url: str,
        website_key: str,
        render_options: dict[str, Any],
        params: dict[str, Any],
        client_key: Optional[str],
    ) -> Optional[str]:
        """Warm the pointer, click the widget checkbox, then poll for a token."""
        params["_phase"] = SolveStage.INTERACTION.value
        await self._human_mouse(page)

        try:
            frame = page.frame_locator(self._WIDGET_IFRAME)
            # Human-like checkbox click (P1-5): Turnstile also scores the
            # pointer dynamics of the checkbox interaction.
            await self._human_click_in_frame(
                page,
                frame,
                self._CHECKBOX_SELECTOR,
                timeout_ms=5_000,
            )
        except Exception as exc:
            log.info("Turnstile checkbox click skipped (managed widget?): %s", exc)

        params["_phase"] = SolveStage.CHALLENGE.value
        return await self._poll_token(page)
