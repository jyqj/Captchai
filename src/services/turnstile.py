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

import asyncio
import json
import logging
from typing import Any

from playwright.async_api import Browser, Route

from ..core.config import Config
from .browser import BrowserManager

log = logging.getLogger(__name__)

_EXTRACT_TURNSTILE_TOKEN_JS = """
() => {
    if (window.__omcToken) return window.__omcToken;
    const input = document.querySelector('[name="cf-turnstile-response"]')
        || document.querySelector('input[name*="turnstile"]');
    if (input && input.value && input.value.length > 20) {
        return input.value;
    }
    if (window.turnstile && typeof window.turnstile.getResponse === 'function') {
        try {
            const resp = window.turnstile.getResponse();
            if (resp && resp.length > 20) return resp;
        } catch (e) {}
    }
    return null;
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


class TurnstileSolver:
    """Solves Cloudflare Turnstile tasks via a shared headless Chromium."""

    def __init__(
        self,
        config: Config,
        manager: BrowserManager | None = None,
        browser: Browser | None = None,
    ) -> None:
        self._config = config
        self._manager = manager or BrowserManager(config)
        self._owns_manager = manager is None
        if browser is not None:  # backwards-compat for tests passing a browser
            self._manager._browser = browser  # type: ignore[attr-defined]

    async def start(self) -> None:
        if self._owns_manager:
            await self._manager.start()

    async def stop(self) -> None:
        if self._owns_manager:
            await self._manager.stop()
        log.info("TurnstileSolver stopped")

    async def solve(self, params: dict[str, Any]) -> dict[str, Any]:
        website_url = params["websiteURL"]
        website_key = params["websiteKey"]

        render_options: dict[str, Any] = {}
        if params.get("action"):
            render_options["action"] = params["action"]
        if params.get("cData"):
            render_options["cData"] = params["cData"]
        if params.get("chlPageData"):
            render_options["chlPageData"] = params["chlPageData"]

        last_error: Exception | None = None
        for attempt in range(self._config.captcha_retries):
            try:
                token, user_agent = await self._solve_once(
                    website_url, website_key, render_options, params
                )
                return {"token": token, "userAgent": user_agent}
            except Exception as exc:
                last_error = exc
                log.warning(
                    "Turnstile attempt %d/%d failed: %s",
                    attempt + 1,
                    self._config.captcha_retries,
                    exc,
                )
                if attempt < self._config.captcha_retries - 1:
                    await asyncio.sleep(2)

        raise RuntimeError(
            f"Turnstile failed after {self._config.captcha_retries} attempts: {last_error}"
        )

    async def _solve_once(
        self,
        website_url: str,
        website_key: str,
        render_options: dict[str, Any],
        params: dict[str, Any],
    ) -> tuple[str, str]:
        context, user_agent = await self._manager.new_context(params)
        html = _build_injected_page(website_key, render_options)

        async def _fulfill_document(route: Route) -> None:
            request = route.request
            if request.resource_type == "document":
                await route.fulfill(status=200, content_type="text/html", body=html)
            else:
                await route.continue_()

        page = await context.new_page()
        try:
            # Intercept the top-level navigation so the synthetic widget page is
            # served *as* the target origin (token is bound to this host).
            await context.route(website_url, _fulfill_document)

            timeout_ms = self._config.browser_timeout * 1000
            await page.goto(website_url, wait_until="domcontentloaded", timeout=timeout_ms)

            await page.mouse.move(400, 300)

            # Try to click the interactive checkbox when the widget renders one.
            # A failure here is a *signal* (managed / non-interactive widget, or a
            # detached frame), not a no-op — log it distinctly so it shows up in
            # traces instead of being silently swallowed.
            try:
                frame = page.frame_locator(
                    'iframe[src*="challenges.cloudflare.com"]'
                )
                await frame.locator(
                    'input[type="checkbox"], label'
                ).first.click(timeout=5_000)
            except Exception as exc:
                log.info("Turnstile checkbox click skipped (managed widget?): %s", exc)

            token = await self._poll_token(page)
            if token:
                log.info("Got Turnstile token (len=%d)", len(token))
                return token, user_agent

            raise RuntimeError("Turnstile token not obtained within timeout")
        finally:
            await context.close()

    async def _poll_token(self, page: Any) -> str | None:
        """Check-first, event-driven token wait bounded by the unified budget.

        Returns the instant the widget callback fires (``page.wait_for_function``
        on the extractor) rather than wasting a fixed 1s per loop; surfaces a
        widget ``error-callback`` immediately as a distinguishable failure.
        """
        deadline = asyncio.get_event_loop().time() + self._config.poll_budget
        interval_ms = max(50, int(self._config.poll_interval * 1000))
        while asyncio.get_event_loop().time() < deadline:
            token = await page.evaluate(_EXTRACT_TURNSTILE_TOKEN_JS)
            if isinstance(token, str) and len(token) > 20:
                return token

            err = await page.evaluate("() => window.__omcError || null")
            if err:
                raise RuntimeError(f"Turnstile widget error: {err}")

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
