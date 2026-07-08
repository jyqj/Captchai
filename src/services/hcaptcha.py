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

from .browser import set_context_resource_blocking
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
from .captcha_errors import classify_widget_error
from .browser_solver import (
    BaseBrowserSolver,
    ProxyKind,
    has_task_proxy,
)

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
        # WP5: enterprise residential-proxy enforcement. Forces enterprise
        # tasks onto a pool (residential) proxy — or a caller-supplied task
        # proxy — and refuses proxyless server egress. Must run before
        # _acquire_context so the egress / kind requirements are honoured.
        self._enforce_enterprise_egress(params, profile.variant)

        async def _attempt() -> tuple[str, str]:
            if profile.use_real_page:
                return await self._solve_once_real_page(
                    website_url,
                    website_key,
                    profile.render_options,
                    params,
                    client_key,
                )
            return await self._solve_once(
                website_url,
                website_key,
                profile.render_options,
                params,
                client_key,
            )

        def _escalate_tier(attempt: int, exc: Exception) -> None:
            # Escalate the vision tier for the next attempt: attempt 1 uses the
            # cheaper local model (tier=1); a failure suggests a hard challenge,
            # so bump to tier=2 to engage VisionRouter's cloud + self-consistency
            # voting path.
            params["_hcaptcha_vision_tier"] = 2

        return await self._solve_with_retries(
            params,
            sitekey=website_key,
            client_key=client_key,
            attempt_fn=_attempt,
            build_solution=lambda token, ua: {
                "gRecaptchaResponse": token,
                "userAgent": ua,
            },
            provider="HCaptcha",
            default_task_type=params.get("type", "HCaptchaTaskProxyless"),
            default_challenge_shape="grid_select",
            on_error=_escalate_tier,
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

    def _enforce_enterprise_egress(
        self, params: dict[str, Any], variant: str
    ) -> None:
        """WP5: force enterprise hCaptcha onto a residential pool proxy.

        Enterprise tokens are IP-bound and enterprise detectors flag
        datacenter / server egress, so:

        * ``egress`` None / "auto" + a caller task proxy → ``"task"`` (the
          caller took responsibility for the proxy's egress).
        * ``egress`` None / "auto" + no task proxy → ``"pool"`` (force the
          server-side residential pool).
        * ``egress="proxyless"`` → raise (enterprise can't use server IP).
        * ``egress="task"`` + task proxy → allowed (warn: caller's
          responsibility; residential requirement is NOT enforced on task
          proxies).
        * ``egress="task"`` + no task proxy → raise.
        * ``egress="pool"`` → fine; when ``ENTERPRISE_REQUIRE_RESIDENTIAL``
          is true, set ``_required_proxy_kinds=("residential","mobile")`` so
          the pool checkout in ``_acquire_pool`` filters by kind (mobile is
          accepted too — it's a stricter residential-equivalent for hCaptcha).

        Regular (non-enterprise) tasks are a no-op.
        """
        if variant != "enterprise":
            return

        egress = params.get("egress")
        if egress is None or egress == "auto":
            if has_task_proxy(params):
                # Caller supplied a proxy: honour their intent (egress=task)
                # rather than forcing the pool. The task proxy is the
                # caller's responsibility.
                params["egress"] = "task"
                egress = "task"
            else:
                params["egress"] = "pool"
                egress = "pool"

        if egress == "proxyless":
            raise RuntimeError(
                "Enterprise hCaptcha requires a residential pool proxy; "
                "egress=proxyless is not allowed"
            )

        if egress == "task":
            if not has_task_proxy(params):
                raise RuntimeError(
                    "Enterprise hCaptcha with egress=task requires a "
                    "caller-supplied proxy"
                )
            log.warning(
                "Enterprise hCaptcha with egress=task — caller proxy is the "
                "caller's responsibility; residential requirement not enforced"
            )
            return

        # egress == "pool": require a residential OR mobile pool proxy when
        # ENTERPRISE_REQUIRE_RESIDENTIAL is true. ``_acquire_pool`` reads
        # ``_required_proxy_kinds`` and raises a specific error when no proxy
        # of any requested kind is available. Mobile is accepted alongside
        # residential because it's a stricter, carrier-IP egress that hCaptcha
        # treats as residential-equivalent for enterprise risk scoring.
        if getattr(self._config, "enterprise_require_residential", True):
            params["_required_proxy_kinds"] = ("residential", "mobile")

        # Optionally force a fresh context per enterprise solve so one sticky
        # proxy's warm session (cookie jar + fingerprint) isn't reused to
        # repeatedly hit the same sitekey — a pattern enterprise risk models
        # cluster on. Read by ``_acquire_pool``.
        if getattr(self._config, "enterprise_fresh_context", False):
            params["_force_fresh_context"] = True

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
        self._stash_fingerprint_geo(solve_context, params)
        context = solve_context.context
        user_agent = solve_context.user_agent
        # Synthetic injected page: resource interception is a safe bandwidth
        # win here (no real page to make look human). Re-assert ON in case a
        # reused warm session had it turned off by a prior real-page solve.
        set_context_resource_blocking(context, True)
        html = _build_injected_page(website_key, render_options)

        async def _fulfill_document(route: Route) -> None:
            if route.request.resource_type == "document":
                await route.fulfill(status=200, content_type="text/html", body=html)
            else:
                await route.continue_()

        solved = False
        # Initialised before the try so the finally's challenge-phase timing is
        # always defined even if context setup / goto raises early.
        _challenge_started = time.monotonic()
        try:
            # WP5: defensive guard — with ``_enforce_enterprise_egress`` run
            # in ``solve()``, an enterprise task should never reach a
            # proxyless context (egress is forced to pool/task, which raises
            # before returning PROXYLESS). Kept as a backstop for the mocked
            # path and any future code that bypasses the enforcement helper.
            if (
                params.get("_hcaptcha_variant") == "enterprise"
                and solve_context.proxy_kind == ProxyKind.PROXYLESS
                and not has_task_proxy(params)
            ):
                raise RuntimeError(
                    "Enterprise hCaptcha requires a residential pool proxy; "
                    "server egress IP would not match the submission IP"
                )
            page = await context.new_page()
            await context.route(website_url, _fulfill_document)
            timeout_ms = self._config.browser_timeout * 1000
            _page_started = time.monotonic()
            await page.goto(website_url, wait_until="domcontentloaded", timeout=timeout_ms)
            params["_phase_page_load_ms"] = int(
                (time.monotonic() - _page_started) * 1000
            )
            _challenge_started = time.monotonic()

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

            # Click the checkbox to trigger the challenge with a human-like
            # pointer path (P1-5): hCaptcha scores checkbox click dynamics as
            # part of motionData, so a raw teleport-click is a bot tell — reuse
            # the same humanised path the tile clicks use. A failure here is a
            # signal (managed widget / detached frame), logged not swallowed.
            try:
                checkbox_frame = page.frame_locator(_CHECKBOX_IFRAME)
                await self._human_click_in_frame(page, checkbox_frame, "#checkbox")
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
            params["_phase_challenge_ms"] = int(
                (time.monotonic() - _challenge_started) * 1000
            )
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
        self._stash_fingerprint_geo(solve_context, params)
        context = solve_context.context
        user_agent = solve_context.user_agent
        # P0-1: real-page solves navigate to the *real* merchant/Stripe page,
        # so resource interception must be OFF — a browser that aborts every
        # CSS/font/image on a real page is one of the easiest enterprise-bot
        # signals. Interception stays on for the synthetic injected page
        # (_solve_once) where it's a harmless bandwidth win.
        set_context_resource_blocking(context, False)
        solved = False
        _challenge_started = time.monotonic()
        try:
            # WP5: defensive guard — see _solve_once for rationale.
            if (
                params.get("_hcaptcha_variant") == "enterprise"
                and solve_context.proxy_kind == ProxyKind.PROXYLESS
                and not has_task_proxy(params)
            ):
                raise RuntimeError(
                    "Enterprise hCaptcha requires a residential pool proxy; "
                    "server egress IP would not match the submission IP"
                )
            await context.add_init_script(_build_real_page_init_script(render_options))
            page = await context.new_page()
            timeout_ms = self._config.browser_timeout * 1000
            _page_started = time.monotonic()
            await page.goto(website_url, wait_until="domcontentloaded", timeout=timeout_ms)
            params["_phase_page_load_ms"] = int(
                (time.monotonic() - _page_started) * 1000
            )
            _challenge_started = time.monotonic()

            if not params.get("isInvisible") and render_options.get("size") != "invisible":
                await self._human_mouse(page)

            passive_budget = getattr(self._config, "poll_budget_passive", 2.0)

            token = await self._poll_token(page, budget=passive_budget)
            if token:
                solved = True
                return token, user_agent

            try:
                checkbox_frame = page.frame_locator(_CHECKBOX_IFRAME)
                await self._human_click_in_frame(page, checkbox_frame, "#checkbox")
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
            params["_phase_challenge_ms"] = int(
                (time.monotonic() - _challenge_started) * 1000
            )
            await self._release_context(solve_context, solved, params)

    # ── challenge dispatch ─────────────────────────────────────

    async def _solve_challenge(
        self,
        page: Any,
        website_key: str,
        params: dict[str, Any],
        client_key: Optional[str],
    ) -> Optional[str]:
        # Surface a widget error-callback immediately as a classified error so
        # the solve loop reacts per kind (rate-limit → fail fast, etc.).
        err = await page.evaluate("() => window.__omcError || null")
        if err:
            raise classify_widget_error(err, provider="hCaptcha")

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
            extra={
                "page": page,
                # Shape solvers move page.mouse along a human-like path for tile
                # / submit clicks when this is set; gated by the same flag that
                # controls the pre-checkbox mouse warmup so tests / invisible
                # widgets can disable it.
                "humanize": getattr(self._config, "human_mouse_enabled", True),
                "humanize_jitter_ms": getattr(
                    self._config, "human_mouse_jitter_ms", 90
                ),
            },
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
                raise classify_widget_error(err, provider="hCaptcha")
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

