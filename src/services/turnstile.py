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

Key correctness details for real production widgets:
  * ``action`` / ``cData`` / ``chlPageData`` are forwarded to ``render`` — a
    widget configured with these rejects tokens generated without them.
  * The context is bound to the task proxy and User-Agent; the resolved
    User-Agent is echoed back so the caller submits with a matching UA.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from playwright.async_api import Route

from .browser_solver import BaseBrowserSolver
from .captcha_errors import classify_widget_error

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


def _build_injected_page(website_key: str, options: dict[str, Any]) -> str:
    """Minimal HTML that renders a Turnstile widget for the given sitekey."""
    render_opts = {"sitekey": website_key, **options}
    opts_json = json.dumps(render_opts)
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>verify</title>
<script src="https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit" async defer></script>
</head>
<body>
<div id="omc-turnstile"></div>
<script>
    window.__omcToken = null;
    function omcRender() {{
        if (!window.turnstile) {{ setTimeout(omcRender, 50); return; }}
        const opts = {opts_json};
        opts.callback = function (token) {{ window.__omcToken = token; }};
        opts['error-callback'] = function () {{ window.__omcError = true; }};
        try {{ window.turnstile.render('#omc-turnstile', opts); }}
        catch (e) {{ window.__omcError = String(e); }}
    }}
    omcRender();
</script>
</body>
</html>"""


class TurnstileSolver(BaseBrowserSolver):
    """Solves Cloudflare Turnstile tasks via a shared headless Chromium."""

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

        return await self._solve_with_retries(
            params,
            sitekey=website_key,
            client_key=client_key,
            attempt_fn=lambda: self._solve_once(
                website_url, website_key, render_options, params
            ),
            build_solution=lambda token, ua: {"token": token, "userAgent": ua},
            provider="Turnstile",
            default_task_type=params.get("type", "TurnstileTaskProxyless"),
            default_challenge_shape="widget",
        )

    async def _solve_once(
        self,
        website_url: str,
        website_key: str,
        render_options: dict[str, Any],
        params: dict[str, Any],
    ) -> tuple[str, str]:
        solve_context = await self._acquire_context(params)
        self._stash_fingerprint_geo(solve_context, params)
        context = solve_context.context
        user_agent = solve_context.user_agent
        html = _build_injected_page(website_key, render_options)
        solved = False

        async def _fulfill_document(route: Route) -> None:
            request = route.request
            if request.resource_type == "document":
                await route.fulfill(status=200, content_type="text/html", body=html)
            else:
                await route.continue_()

        page = await context.new_page()
        try:
            await context.route(website_url, _fulfill_document)

            timeout_ms = self._config.browser_timeout * 1000
            await page.goto(website_url, wait_until="domcontentloaded", timeout=timeout_ms)

            await self._human_mouse(page)

            try:
                frame = page.frame_locator(
                    'iframe[src*="challenges.cloudflare.com"]'
                )
                # Human-like checkbox click (P1-5): Turnstile also scores the
                # pointer dynamics of the checkbox interaction.
                await self._human_click_in_frame(
                    page,
                    frame,
                    'input[type="checkbox"], label',
                    timeout_ms=5_000,
                )
            except Exception as exc:
                log.info("Turnstile checkbox click skipped (managed widget?): %s", exc)

            token = await self._poll_token(page)
            if token:
                log.info("Got Turnstile token (len=%d)", len(token))
                solved = True
                return token, user_agent

            raise RuntimeError("Turnstile token not obtained within timeout")
        finally:
            await self._release_context(solve_context, solved, params)

    async def _poll_token(self, page: Any) -> "str | None":
        """Check-first, event-driven token wait bounded by the unified budget.

        Returns the instant the widget callback fires (``page.wait_for_function``
        on the extractor) rather than wasting a fixed 1s per loop; surfaces a
        widget ``error-callback`` immediately as a distinguishable failure.
        """
        deadline = asyncio.get_event_loop().time() + self._config.poll_budget
        interval_ms = max(50, int(self._config.poll_interval * 1000))
        while asyncio.get_event_loop().time() < deadline:
            result = await page.evaluate(_EXTRACT_TURNSTILE_TOKEN_JS)
            token = result.get("token") if isinstance(result, dict) else None
            err = result.get("error") if isinstance(result, dict) else None
            if isinstance(token, str) and len(token) > 20:
                return token
            if err:
                raise classify_widget_error(err, provider="Turnstile")

            remaining_ms = int(
                (deadline - asyncio.get_event_loop().time()) * 1000
            )
            if remaining_ms <= 0:
                break
            try:
                await page.wait_for_function(
                    "() => window.__omcToken || window.__omcError",
                    timeout=min(interval_ms * 4, remaining_ms),
                )
            except Exception:
                # Timed out this slice — loop re-checks (also catches token set via
                # the hidden input rather than the JS callback).
                pass
        return None
