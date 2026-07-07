"""hCaptcha solver using Playwright + the shared asset / vision / parsing layers.

Supports ``HCaptchaTaskProxyless`` (the type Stripe Radar / checkout presents).

Unlike the original "screenshot .task-image, ask the local model, click" path,
this solver:

  * **Owns no model client and no browser lifecycle.** It requests a browser
    context (task-proxy-bound, or a warm pooled session for proxyless solves)
    and delegates vision to the shared :class:`VisionRouter`, which routes hard
    grids to the cloud model with self-consistency voting + a confidence gate.
  * **Dispatches by challenge shape.** A :class:`ChallengeDispatcher` detects the
    challenge shape (grid select, dynamic grid, area bbox, slide, drag) from
    cheap DOM signals and runs the matching shape solver, instead of assuming a
    single ``.task-image`` grid.
  * **Threads enterprise fields.** ``rqdata`` and ``enterprisePayload`` are
    forwarded to ``hcaptcha.render``; a widget ``error-callback`` is surfaced as
    a distinguishable error instead of being silently swallowed.
  * **Meters consumption.** Every solve appends a :class:`SolveRecord` (model,
    tokens, rounds, outcome, wall time) to the shared cost ledger and updates
    per-sitekey success accounting; reusable tokens are served from a TTL cache.

Token is returned in ``gRecaptchaResponse`` for YesCaptcha compatibility.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Optional

from playwright.async_api import Browser, FrameLocator, Route

from ..core.config import Config
from ..parsing.dispatcher import (
    ChallengeClassifier,
    ChallengeContext,
    ChallengeDispatcher,
    ChallengeShape,
)
from ..parsing.shapes.dynamic_grid import DynamicGridSolver
from ..parsing.shapes.grid_select import GridSelectSolver
from ..parsing.vision_adapter import VisionAdapter
from .browser import BrowserManager

log = logging.getLogger(__name__)

_EXTRACT_HCAPTCHA_TOKEN_JS = """
() => {
    if (window.__omcToken) return window.__omcToken;
    const textarea = document.querySelector('[name="h-captcha-response"]')
        || document.querySelector('[name="g-recaptcha-response"]');
    if (textarea && textarea.value && textarea.value.length > 20) {
        return textarea.value;
    }
    if (window.hcaptcha && typeof window.hcaptcha.getResponse === 'function') {
        try {
            const resp = window.hcaptcha.getResponse();
            if (resp && resp.length > 20) return resp;
        } catch (e) {}
    }
    return null;
}
"""

# Localization-independent iframe matchers. The challenge iframe title is
# localized ("Main content of the hCaptcha challenge" in English only), so match
# on the src host as the robust primary signal.
_CHECKBOX_IFRAME = 'iframe[src*="hcaptcha.com"][src*="checkbox"], iframe[title*="checkbox"]'
_CHALLENGE_IFRAME = 'iframe[src*="hcaptcha.com"][src*="challenge"], iframe[title*="challenge"]'


def _build_injected_page(website_key: str, options: dict[str, Any]) -> str:
    render_opts = {"sitekey": website_key, **options}
    opts_json = json.dumps(render_opts)
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>verify</title>
<script src="https://js.hcaptcha.com/1/api.js?render=explicit" async defer></script>
</head>
<body>
<div id="omc-hcaptcha"></div>
<script>
    window.__omcToken = null;
    function omcRender() {{
        if (!window.hcaptcha) {{ setTimeout(omcRender, 50); return; }}
        const opts = {opts_json};
        opts.callback = function (token) {{ window.__omcToken = token; }};
        opts['error-callback'] = function (e) {{ window.__omcError = String(e); }};
        try {{
            window.__omcWidgetId = window.hcaptcha.render('omc-hcaptcha', opts);
            if (opts.size === 'invisible') {{
                window.hcaptcha.execute(window.__omcWidgetId);
            }}
        }} catch (e) {{ window.__omcError = String(e); }}
    }}
    omcRender();
</script>
</body>
</html>"""


class HCaptchaSolver:
    """Solves HCaptchaTaskProxyless via the shared browser + vision/parsing layers."""

    def __init__(
        self,
        config: Config,
        manager: BrowserManager | None = None,
        browser: Browser | None = None,
        services: Any | None = None,
    ) -> None:
        self._config = config
        self._manager = manager or BrowserManager(config)
        self._owns_manager = manager is None
        if browser is not None:
            self._manager._browser = browser  # type: ignore[attr-defined]
        self._services = services

    async def start(self) -> None:
        if self._owns_manager:
            await self._manager.start()

    async def stop(self) -> None:
        if self._owns_manager:
            await self._manager.stop()
        log.info("HCaptchaSolver stopped")

    # ── public solve ───────────────────────────────────────────

    async def solve(self, params: dict[str, Any]) -> dict[str, Any]:
        website_url = params["websiteURL"]
        website_key = params["websiteKey"]
        client_key = params.get("_clientKey")

        render_options: dict[str, Any] = {}
        if params.get("rqdata"):
            render_options["rqdata"] = params["rqdata"]
        # Enterprise widgets frequently require an enterprise payload; forward it
        # verbatim. Its absence on a widget that needs it produces a widget
        # error-callback, which we surface as a first-class error below.
        if params.get("enterprisePayload"):
            render_options["enterprise"] = params["enterprisePayload"]
        if params.get("isInvisible"):
            render_options["size"] = "invisible"

        # Token cache: a burst of identical createTask calls for the same
        # (sitekey, proxy-IP, UA) can reuse a still-valid token.
        proxy_ip = self._proxy_ip(params)
        cached_ua = params.get("userAgent") or ""
        if self._services is not None and cached_ua:
            cached = await self._services.token_cache.get(
                website_key, proxy_ip, cached_ua
            )
            if cached:
                log.info("Serving cached hCaptcha token for sitekey %s", website_key)
                return {"gRecaptchaResponse": cached, "userAgent": cached_ua}

        last_error: Exception | None = None
        for attempt in range(self._config.captcha_retries):
            started = time.monotonic()
            try:
                token, user_agent = await self._solve_once(
                    website_url, website_key, render_options, params, client_key
                )
                await self._record(
                    params, website_key, client_key, "ready", started
                )
                if self._services is not None:
                    await self._services.token_cache.put(
                        website_key, proxy_ip, user_agent, token
                    )
                return {"gRecaptchaResponse": token, "userAgent": user_agent}
            except Exception as exc:
                last_error = exc
                await self._record(
                    params, website_key, client_key, "failed", started
                )
                log.warning(
                    "HCaptcha attempt %d/%d failed: %s",
                    attempt + 1,
                    self._config.captcha_retries,
                    exc,
                )
                if attempt < self._config.captcha_retries - 1:
                    await asyncio.sleep(2)

        raise RuntimeError(
            f"HCaptcha failed after {self._config.captcha_retries} attempts: {last_error}"
        )

    # ── one attempt ────────────────────────────────────────────

    async def _solve_once(
        self,
        website_url: str,
        website_key: str,
        render_options: dict[str, Any],
        params: dict[str, Any],
        client_key: Optional[str],
    ) -> tuple[str, str]:
        context, user_agent, session = await self._acquire_context(params)
        html = _build_injected_page(website_key, render_options)

        async def _fulfill_document(route: Route) -> None:
            if route.request.resource_type == "document":
                await route.fulfill(status=200, content_type="text/html", body=html)
            else:
                await route.continue_()

        page = await context.new_page()
        solved = False
        try:
            await context.route(website_url, _fulfill_document)
            timeout_ms = self._config.browser_timeout * 1000
            await page.goto(website_url, wait_until="domcontentloaded", timeout=timeout_ms)

            await page.mouse.move(400, 300)
            await asyncio.sleep(0.5)

            # Passive / invisible widgets may resolve without interaction. Kept
            # short: the event-driven wait returns the instant a passive token
            # fires, so a small budget only bounds the "challenge is coming" case
            # rather than adding fixed dead time before dispatch.
            token = await self._poll_token(page, budget=1.5)
            if token:
                solved = True
                return token, user_agent

            # Click the checkbox to trigger the challenge. A failure here is a
            # signal (managed widget / detached frame), logged rather than
            # silently swallowed.
            try:
                checkbox_frame = page.frame_locator(_CHECKBOX_IFRAME)
                await checkbox_frame.locator("#checkbox").click(timeout=10_000)
            except Exception as exc:
                log.info("hCaptcha checkbox click skipped: %s", exc)

            token = await self._poll_token(page, budget=1.5)
            if token:
                solved = True
                return token, user_agent

            # Escalated to a visual challenge — dispatch by shape.
            token = await self._solve_challenge(
                page, website_key, params, client_key
            )
            if token:
                solved = True
                return token, user_agent

            raise RuntimeError("hCaptcha token not obtained within budget")
        finally:
            await self._release_context(context, session, solved, params)

    # ── context acquisition ────────────────────────────────────

    async def _acquire_context(self, params: dict[str, Any]):
        """Return (context, user_agent, session|None).

        A task-supplied proxy binds the token to a specific egress, so those
        solves get a fresh task-bound context. Proxyless solves may reuse a warm
        pooled session (better hCaptcha scoring + lower latency).
        """
        has_proxy = bool(
            params.get("proxy") or (params.get("proxyAddress") and params.get("proxyPort"))
        )
        if (
            not has_proxy
            and self._services is not None
            and self._services.session_pool is not None
        ):
            session = await self._services.session_pool.checkout(
                sitekey=params.get("websiteKey")
            )
            return session.context, session.user_agent, session

        context, user_agent = await self._manager.new_context(params)
        return context, user_agent, None

    async def _release_context(
        self, context: Any, session: Any, solved: bool, params: dict[str, Any]
    ) -> None:
        if session is not None and self._services is not None:
            # Return the warm session to the pool; burn it after a failure so a
            # replacement is created lazily.
            await self._services.session_pool.release(
                session, success=solved, burned=not solved
            )
        else:
            try:
                await context.close()
            except Exception:
                pass

    def _proxy_ip(self, params: dict[str, Any]) -> Optional[str]:
        address = params.get("proxyAddress")
        if address:
            return str(address)
        single = params.get("proxy")
        if single and "://" in str(single):
            rest = str(single).split("://", 1)[1]
            hostport = rest.rsplit("@", 1)[-1]
            return hostport.split(":", 1)[0]
        return None

    # ── challenge dispatch ─────────────────────────────────────

    async def _solve_challenge(
        self,
        page: Any,
        website_key: str,
        params: dict[str, Any],
        client_key: Optional[str],
    ) -> Optional[str]:
        # Surface a widget error-callback immediately as a distinguishable error.
        err = await page.evaluate("() => window.__omcError || null")
        if err:
            raise RuntimeError(f"hCaptcha widget error: {err}")

        challenge_frame: FrameLocator = page.frame_locator(_CHALLENGE_IFRAME)

        async def _token_poll() -> Optional[str]:
            return await self._poll_token(page, budget=2.0)

        vision = self._build_vision_adapter(website_key, client_key)
        dispatcher = self._build_dispatcher(vision, _token_poll)

        ctx = ChallengeContext(
            prompt="",
            task_id=params.get("_taskId"),
            sitekey=website_key,
        )
        token = await dispatcher.solve(challenge_frame, ctx)
        # Stash consumption from this attempt for the ledger.
        params["_vision"] = vision
        return token

    def _build_vision_adapter(
        self, website_key: str, client_key: Optional[str]
    ) -> Optional[VisionAdapter]:
        if self._services is None:
            return None
        return VisionAdapter(
            self._services.vision_router,
            task_tier=2,  # Stripe-grade hCaptcha grids route to the cloud model
            sitekey=website_key,
            client_key=client_key,
        )

    def _build_dispatcher(
        self, vision: Optional[VisionAdapter], token_poll
    ) -> ChallengeDispatcher:
        classifier = ChallengeClassifier(vision=vision)
        dispatcher = ChallengeDispatcher(classifier)
        dispatcher.register(
            ChallengeShape.GRID_SELECT,
            GridSelectSolver(vision=vision, token_poll=token_poll),
        )
        dispatcher.register(
            ChallengeShape.RECAPTCHA_DYNAMIC,
            DynamicGridSolver(vision=vision, token_poll=token_poll),
        )
        return dispatcher

    # ── token polling (unified, event-driven) ──────────────────

    async def _poll_token(self, page: Any, budget: float | None = None) -> Optional[str]:
        """Check-first token wait bounded by the unified poll budget.

        Uses ``page.wait_for_function`` so we return the instant the widget
        callback fires, and surfaces a widget ``error-callback`` as an error.
        """
        total = budget if budget is not None else float(self._config.poll_budget)
        deadline = asyncio.get_event_loop().time() + total
        interval_ms = max(50, int(self._config.poll_interval * 1000))
        while asyncio.get_event_loop().time() < deadline:
            token = await page.evaluate(_EXTRACT_HCAPTCHA_TOKEN_JS)
            if isinstance(token, str) and len(token) > 20:
                log.info("Got hCaptcha token (len=%d)", len(token))
                return token
            err = await page.evaluate("() => window.__omcError || null")
            if err:
                raise RuntimeError(f"hCaptcha widget error: {err}")
            remaining_ms = int((deadline - asyncio.get_event_loop().time()) * 1000)
            if remaining_ms <= 0:
                break
            try:
                await page.wait_for_function(
                    "() => window.__omcToken || window.__omcError",
                    timeout=min(interval_ms * 4, remaining_ms),
                )
            except Exception:
                pass
        return None

    # ── consumption metering ───────────────────────────────────

    async def _record(
        self,
        params: dict[str, Any],
        sitekey: str,
        client_key: Optional[str],
        outcome: str,
        started: float,
    ) -> None:
        if self._services is None:
            return
        from ..consumption.ledger import SolveRecord, estimate_cost

        vision = params.get("_vision")
        model = getattr(vision, "last_model", None)
        in_tok = getattr(vision, "total_input_tokens", 0)
        out_tok = getattr(vision, "total_output_tokens", 0)
        calls = getattr(vision, "total_vision_calls", 0)
        cost = estimate_cost(model or "local", in_tok, out_tok)

        try:
            await self._services.ledger.record(
                SolveRecord(
                    task_id=str(params.get("_taskId") or ""),
                    sitekey=sitekey,
                    task_type=params.get("type", "HCaptchaTaskProxyless"),
                    model=model,
                    challenge_shape="grid_select",
                    vision_calls=calls,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    wall_ms=int((time.monotonic() - started) * 1000),
                    outcome=outcome,
                    est_cost_usd=cost,
                    client_key=client_key,
                )
            )
            await self._services.accounting.record(
                sitekey, outcome, model=model
            )
        except Exception as exc:  # metering must never fail a solve
            log.debug("ledger record failed: %s", exc)
