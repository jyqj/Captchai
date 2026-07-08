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
    per-sitekey success accounting.

Token is returned in ``gRecaptchaResponse`` for YesCaptcha compatibility.

hCaptcha tokens are one-time: once submitted to the provider's siteverify
endpoint they're consumed, so we never cache or reuse them across requests.
Enterprise solves additionally refuse a proxyless server-egress fallback
because the token is bound to the generating egress IP.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

from playwright.async_api import FrameLocator, Route

from ..parsing.dispatcher import (
    ChallengeClassifier,
    ChallengeContext,
    ChallengeDispatcher,
    ChallengeShape,
)
from ..parsing.shapes.area_bbox import AreaBBoxSolver
from ..parsing.shapes.drag_drop import DragDropSolver
from ..parsing.shapes.dynamic_grid import DynamicGridSolver
from ..parsing.shapes.grid_select import GridSelectSolver
from ..parsing.shapes.slide import CanvasSlideSolver
from ..parsing.vision_adapter import VisionAdapter
from .browser_solver import BaseBrowserSolver, ProxyKind, has_task_proxy

log = logging.getLogger(__name__)

_EXTRACT_HCAPTCHA_TOKEN_JS = """
() => {
    let token = null;
    if (window.__omcToken) {
        token = window.__omcToken;
    } else {
        const textarea = document.querySelector('[name="h-captcha-response"]')
            || document.querySelector('[name="g-recaptcha-response"]');
        if (textarea && textarea.value && textarea.value.length > 20) {
            token = textarea.value;
        } else if (window.hcaptcha && typeof window.hcaptcha.getResponse === 'function') {
            try {
                const resp = window.hcaptcha.getResponse();
                if (resp && resp.length > 20) token = resp;
            } catch (e) {}
        }
    }
    return {token: token, error: window.__omcError || null};
}
"""

def _build_real_page_init_script(options: dict[str, Any]) -> str:
    """Hook real-page hcaptcha.render and merge task-level enterprise options."""

    opts_json = json.dumps(options)
    return f"""
(function() {{
    window.__omcToken = null;
    window.__omcError = null;
    const __omcRenderOptions = {opts_json};
    function hookHcaptcha() {{
        if (window.hcaptcha && window.hcaptcha.render) {{
            const origRender = window.hcaptcha.render.bind(window.hcaptcha);
            window.hcaptcha.render = function(container, opts) {{
                opts = Object.assign({{}}, opts || {{}}, __omcRenderOptions);
                const origCb = opts.callback;
                opts.callback = function(token) {{
                    window.__omcToken = token;
                    if (origCb) origCb(token);
                }};
                const origErr = opts['error-callback'];
                opts['error-callback'] = function(e) {{
                    window.__omcError = String(e);
                    if (origErr) origErr(e);
                }};
                return origRender(container, opts);
            }};
        }} else {{
            setTimeout(hookHcaptcha, 50);
        }}
    }}
    hookHcaptcha();
}})();
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


@dataclass(frozen=True)
class HCaptchaProfile:
    """Resolved hCaptcha variant policy for a single solve."""

    variant: str
    render_options: dict[str, Any]
    cache_tokens: bool
    use_real_page: bool
    vision_tier: int


class HCaptchaSolver(BaseBrowserSolver):
    """Solves HCaptchaTaskProxyless via the shared browser + vision/parsing layers."""

    # ── public solve ───────────────────────────────────────────

    async def solve(self, params: dict[str, Any]) -> dict[str, Any]:
        website_url = params["websiteURL"]
        website_key = params["websiteKey"]
        client_key = params.get("_clientKey")
        profile = self._profile(params)
        params["_hcaptcha_variant"] = profile.variant
        params["_hcaptcha_vision_tier"] = profile.vision_tier

        last_error: Exception | None = None
        for attempt in range(self._config.captcha_retries):
            started = time.monotonic()
            try:
                if profile.use_real_page:
                    token, user_agent = await self._solve_once_real_page(
                        website_url,
                        website_key,
                        profile.render_options,
                        params,
                        client_key,
                    )
                else:
                    token, user_agent = await self._solve_once(
                        website_url,
                        website_key,
                        profile.render_options,
                        params,
                        client_key,
                    )
                await self._record(
                    params, website_key, client_key, "ready", started
                )
                return {"gRecaptchaResponse": token, "userAgent": user_agent}
            except Exception as exc:
                last_error = exc
                await self._record(
                    params, website_key, client_key, "failed", started
                )
                # Escalate vision tier for the next attempt: the first attempt
                # used the cheaper local model (tier=1). A failure suggests the
                # challenge is hard, so bump to tier=2 to let VisionRouter's
                # cloud + self-consistency voting path engage.
                params["_hcaptcha_vision_tier"] = 2
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

    def _profile(self, params: dict[str, Any]) -> HCaptchaProfile:
        """Resolve regular vs enterprise behavior in one place."""

        render_options: dict[str, Any] = {}
        if params.get("rqdata"):
            render_options["rqdata"] = params["rqdata"]
        if params.get("enterprisePayload"):
            render_options["enterprise"] = params["enterprisePayload"]
        if params.get("isInvisible"):
            render_options["size"] = "invisible"

        is_enterprise = bool(params.get("rqdata") or params.get("enterprisePayload"))
        return HCaptchaProfile(
            variant="enterprise" if is_enterprise else "regular",
            render_options=render_options,
            cache_tokens=False,
            use_real_page=getattr(self._config, "hcaptcha_real_page", False)
            or is_enterprise,
            vision_tier=1,
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
        solve_context = await self._acquire_context(params)
        context = solve_context.context
        user_agent = solve_context.user_agent
        html = _build_injected_page(website_key, render_options)

        async def _fulfill_document(route: Route) -> None:
            if route.request.resource_type == "document":
                await route.fulfill(status=200, content_type="text/html", body=html)
            else:
                await route.continue_()

        solved = False
        try:
            if (
                params.get("_hcaptcha_variant") == "enterprise"
                and solve_context.proxy_kind == ProxyKind.PROXYLESS
                and not has_task_proxy(params)
            ):
                raise RuntimeError(
                    "Enterprise hCaptcha requires a task or pool proxy; "
                    "server egress IP would not match the submission IP"
                )
            page = await context.new_page()
            await context.route(website_url, _fulfill_document)
            timeout_ms = self._config.browser_timeout * 1000
            await page.goto(website_url, wait_until="domcontentloaded", timeout=timeout_ms)

            if not params.get("isInvisible") and render_options.get("size") != "invisible":
                await self._human_mouse(page)

            passive_budget = getattr(self._config, "poll_budget_passive", 2.0)

            # Passive / invisible widgets may resolve without interaction. Kept
            # short: the event-driven wait returns the instant a passive token
            # fires, so a small budget only bounds the "challenge is coming" case
            # rather than adding fixed dead time before dispatch.
            token = await self._poll_token(page, budget=passive_budget)
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

            token = await self._poll_token(page, budget=passive_budget)
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
            await self._release_context(solve_context, solved, params)

    # ── real-page mode ─────────────────────────────────────────

    async def _solve_once_real_page(
        self,
        website_url: str,
        website_key: str,
        render_options: dict[str, Any],
        params: dict[str, Any],
        client_key: Optional[str],
    ) -> tuple[str, str]:
        """Navigate to the real target page and hook hcaptcha callbacks."""
        solve_context = await self._acquire_context(params)
        context = solve_context.context
        user_agent = solve_context.user_agent
        solved = False
        try:
            if (
                params.get("_hcaptcha_variant") == "enterprise"
                and solve_context.proxy_kind == ProxyKind.PROXYLESS
                and not has_task_proxy(params)
            ):
                raise RuntimeError(
                    "Enterprise hCaptcha requires a task or pool proxy; "
                    "server egress IP would not match the submission IP"
                )
            await context.add_init_script(_build_real_page_init_script(render_options))
            page = await context.new_page()
            timeout_ms = self._config.browser_timeout * 1000
            await page.goto(website_url, wait_until="domcontentloaded", timeout=timeout_ms)

            if not params.get("isInvisible") and render_options.get("size") != "invisible":
                await self._human_mouse(page)

            passive_budget = getattr(self._config, "poll_budget_passive", 2.0)

            token = await self._poll_token(page, budget=passive_budget)
            if token:
                solved = True
                return token, user_agent

            try:
                checkbox_frame = page.frame_locator(_CHECKBOX_IFRAME)
                await checkbox_frame.locator("#checkbox").click(timeout=10_000)
            except Exception as exc:
                log.info("hCaptcha checkbox click skipped (real page): %s", exc)

            token = await self._poll_token(page, budget=passive_budget)
            if token:
                solved = True
                return token, user_agent

            token = await self._solve_challenge(
                page, website_key, params, client_key
            )
            if token:
                solved = True
                return token, user_agent

            raise RuntimeError("hCaptcha token not obtained (real page mode)")
        finally:
            await self._release_context(solve_context, solved, params)

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

        challenge_budget = getattr(self._config, "poll_budget_challenge", 10.0)

        async def _token_poll() -> Optional[str]:
            return await self._poll_token(page, budget=challenge_budget)

        vision = self._build_vision_adapter(website_key, client_key, params)
        dispatcher = self._build_dispatcher(vision, _token_poll)

        ctx = ChallengeContext(
            prompt="",
            task_id=params.get("_taskId"),
            sitekey=website_key,
            extra={"page": page},
        )

        original_solve = dispatcher.solve

        async def _solve_and_record_shape(frame, solve_ctx):
            shape = await dispatcher._classifier.detect(frame, solve_ctx)
            params["_challenge_shape"] = shape.value
            return await original_solve(frame, solve_ctx)

        token = await _solve_and_record_shape(challenge_frame, ctx)
        params["_vision"] = vision
        return token

    def _build_vision_adapter(
        self, website_key: str, client_key: Optional[str], params: dict[str, Any]
    ) -> Optional[VisionAdapter]:
        if self._services is None:
            return None
        return VisionAdapter(
            self._services.vision_router,
            task_tier=int(params.get("_hcaptcha_vision_tier", 2)),
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
        dispatcher.register(
            ChallengeShape.AREA_BBOX,
            AreaBBoxSolver(vision=vision, token_poll=token_poll),
        )
        dispatcher.register(
            ChallengeShape.CANVAS_SLIDE,
            CanvasSlideSolver(vision=vision, token_poll=token_poll),
        )
        dispatcher.register(
            ChallengeShape.DRAG_DROP,
            DragDropSolver(vision=vision, token_poll=token_poll),
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
            result = await page.evaluate(_EXTRACT_HCAPTCHA_TOKEN_JS)
            token = result.get("token") if isinstance(result, dict) else None
            err = result.get("error") if isinstance(result, dict) else None
            if isinstance(token, str) and len(token) > 20:
                log.info("Got hCaptcha token (len=%d)", len(token))
                return token
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
                    proxy_id=params.get("_pool_proxy_id"),
                    session_id=params.get("_sessionId"),
                    proxy_kind=params.get("_proxyKind"),
                    model=model,
                    challenge_shape=params.get("_challenge_shape", "grid_select"),
                    vision_calls=calls,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    proxy_bytes=int(params.get("_proxy_bytes", 0)),
                    wall_ms=int((time.monotonic() - started) * 1000),
                    outcome=outcome,
                    est_cost_usd=cost,
                    client_key=client_key,
                )
            )
            await self._services.accounting.record(
                sitekey,
                outcome,
                proxy_kind=params.get("_proxyKind"),
                model=model,
            )
        except Exception as exc:  # metering must never fail a solve
            log.debug("ledger record failed: %s", exc)
