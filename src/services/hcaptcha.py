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
import logging
from dataclasses import dataclass
from typing import Any, Optional

from playwright.async_api import FrameLocator

from ..assets.proxy_pool import proxy_from_params
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
from .injected_widget import InjectedWidgetSolver
from .browser_solver import (
    ProxyKind,
    SolveStage,
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

# Localization-independent iframe matchers. The challenge iframe title is
# localized ("Main content of the hCaptcha challenge" in English only), so match
# on the src host as the robust primary signal.
_CHECKBOX_IFRAME = 'iframe[src*="hcaptcha.com"][src*="checkbox"], iframe[title*="checkbox"]'
_CHALLENGE_IFRAME = 'iframe[src*="hcaptcha.com"][src*="challenge"], iframe[title*="challenge"]'

# ``enterprisePayload`` is the 2captcha / YesCaptcha container convention whose
# keys are hcaptcha.render() options. A few solver ecosystems use field names
# that differ from the JS API; normalise the common ones so the widget actually
# receives them. Everything else passes through unchanged.
_ENTERPRISE_FIELD_ALIASES = {
    "apiEndpoint": "endpoint",
}


@dataclass(frozen=True)
class HCaptchaProfile:
    """Resolved hCaptcha variant policy for a single solve."""

    variant: str
    render_options: dict[str, Any]
    cache_tokens: bool
    use_real_page: bool
    vision_tier: int


class HCaptchaSolver(InjectedWidgetSolver):
    """Solves HCaptchaTaskProxyless via the shared browser + vision/parsing layers."""

    PROVIDER = "hCaptcha"
    WIDGET_GLOBAL = "hcaptcha"
    WIDGET_API_JS = "https://js.hcaptcha.com/1/api.js?render=explicit"
    WIDGET_CONTAINER_ID = "omc-hcaptcha"
    WIDGET_ERROR_CALLBACK_VALUE = "String(e)"
    WIDGET_TOKEN_EXTRACTOR_JS = _EXTRACT_HCAPTCHA_TOKEN_JS

    def _widget_render_body(self) -> str:
        # hCaptcha renders by container *id* (not a CSS selector), stores the
        # widget id, and drives invisible widgets with an explicit execute().
        return (
            "window.__omcWidgetId = window.hcaptcha.render('omc-hcaptcha', opts);\n"
            "            if (opts.size === 'invisible') {\n"
            "                window.hcaptcha.execute(window.__omcWidgetId);\n"
            "            }"
        )

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

        def _escalate_tier(attempt: int, exc: Exception, stage: str) -> None:
            # Stage-aware escalation: only bump the vision tier when the failure
            # actually reached the visual challenge. A failure at an earlier
            # stage (context acquire / page load / passive poll) is not a "hard
            # challenge" signal — engaging the expensive cloud + self-consistency
            # tier on the retry would waste it, so those retries stay at tier 1
            # and simply re-attempt. attempt 1 uses the cheaper local model.
            if stage == SolveStage.CHALLENGE.value:
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
            verify_provider="hcaptcha",
        )

    def _profile(self, params: dict[str, Any]) -> HCaptchaProfile:
        """Resolve regular vs enterprise behavior in one place."""

        render_options: dict[str, Any] = {}

        # enterprisePayload is a *container* of hcaptcha.render() options
        # (rqdata, sentry, endpoint, reportapi, assethost, imghost, custom, …).
        # ``hcaptcha.render`` takes those FLAT — there is no nested ``enterprise``
        # key — so spread the payload onto the render options. Nesting it (the
        # previous behaviour) meant the widget silently ignored it, dropping the
        # rqdata a YesCaptcha client sends inside enterprisePayload and minting a
        # token enterprise siteverify rejects.
        payload = params.get("enterprisePayload")
        if isinstance(payload, dict):
            for key, value in payload.items():
                render_options[_ENTERPRISE_FIELD_ALIASES.get(key, key)] = value

        # A top-level ``rqdata`` wins over one nested in enterprisePayload; both
        # forms are accepted (YesCaptcha clients send it inside the payload,
        # others send it top-level).
        if params.get("rqdata"):
            render_options["rqdata"] = params["rqdata"]
        if params.get("isInvisible"):
            render_options["size"] = "invisible"

        is_enterprise = bool(params.get("rqdata") or payload)
        return HCaptchaProfile(
            variant="enterprise" if is_enterprise else "regular",
            render_options=render_options,
            cache_tokens=False,
            use_real_page=self._resolve_real_page(params),
            vision_tier=1,
        )

    def _resolve_real_page(self, params: dict[str, Any]) -> bool:
        """Decide injected-page vs real-page navigation for this solve.

        Precedence: an explicit per-task ``realPage`` flag wins, else the
        process default ``HCAPTCHA_REAL_PAGE``.

        Enterprise no longer *forces* real-page. In the token-relay model the
        caller captures a fresh ``rqdata`` from the target's own session and we
        mint a token bound to ``sitekey + rqdata + hostname + egress IP``. An
        injected page served at the correct hostname satisfies that binding
        without depending on reproducing the target's session — which a fresh
        ``goto`` of a session-bound URL (e.g. a Stripe checkout) usually can't
        do. Real-page mode remains available per task/config for sites whose
        own anti-bot JS must run.
        """
        task_flag = params.get("realPage")
        if task_flag is not None:
            return bool(task_flag)
        return bool(getattr(self._config, "hcaptcha_real_page", False))

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
          proxies unless ``ENTERPRISE_REQUIRE_RESIDENTIAL_ON_TASK`` is on, in
          which case a non-residential/mobile task proxy is refused).
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
            # WP6: optionally *enforce* the residential requirement on the
            # caller's task proxy too. A caller task proxy is normally the
            # caller's responsibility (we only warn) — but enterprise tokens
            # minted through an unannotated datacenter task proxy are exactly
            # what enterprise detectors reject, so an operator can flip
            # ENTERPRISE_REQUIRE_RESIDENTIAL_ON_TASK on to refuse them. The
            # proxy declares its kind via a ``|kind=residential`` annotation;
            # an unannotated proxy defaults to "datacenter" and is refused.
            if getattr(self._config, "enterprise_require_residential_on_task", False):
                task_asset = proxy_from_params(params)
                kind = getattr(task_asset, "kind", None)
                if kind not in ("residential", "mobile"):
                    raise RuntimeError(
                        "Enterprise hCaptcha with egress=task requires a "
                        "residential or mobile task proxy (annotate it "
                        "'|kind=residential'); got "
                        f"{kind or 'datacenter'}"
                    )
                return
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

        # IP-binding footgun: an enterprise token minted through a server-side
        # pool proxy is bound to that proxy's exit IP, but the caller only gets
        # a credential-free ``egressServer`` and can't route their downstream
        # submit through it. Enterprise/Stripe Radar score IP consistency, so
        # the submit is likely rejected. Warn unless the operator opted to
        # expose reusable pool credentials. (egress=task with the caller's own
        # residential proxy sidesteps this entirely.)
        if not getattr(self._config, "pool_egress_expose_credentials", False):
            log.warning(
                "Enterprise hCaptcha on egress=pool: token is minted through a "
                "server-side proxy IP the caller cannot reuse. Enterprise/Stripe "
                "score IP consistency, so the downstream submit may be rejected. "
                "Use egress=task with your own residential proxy, or set "
                "POOL_EGRESS_EXPOSE_CREDENTIALS=true to receive a reusable egress."
            )

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
        """Solve on the synthetic injected page (regular hCaptcha)."""
        return await self._run_page_solve(
            website_url,
            website_key,
            render_options,
            params,
            client_key,
            strategy=self._injected_page_strategy(
                website_url, website_key, render_options
            ),
        )

    async def _solve_once_real_page(
        self,
        website_url: str,
        website_key: str,
        render_options: dict[str, Any],
        params: dict[str, Any],
        client_key: Optional[str],
    ) -> tuple[str, str]:
        """Solve by navigating to the real target page and hooking render."""
        return await self._run_page_solve(
            website_url,
            website_key,
            render_options,
            params,
            client_key,
            strategy=self._real_page_strategy(render_options),
        )

    def _guard(self, solve_context: Any, params: dict[str, Any]) -> None:
        """WP5 defensive backstop: refuse an enterprise solve on server egress.

        With ``_enforce_enterprise_egress`` run in ``solve()`` an enterprise
        task should never reach a proxyless context (egress is forced to
        pool/task, which raises before returning PROXYLESS). Kept for the mocked
        path and any future code that bypasses the enforcement helper. Runs from
        the shared :meth:`InjectedWidgetSolver._run_page_solve` guard hook.
        """
        if (
            params.get("_hcaptcha_variant") == "enterprise"
            and solve_context.proxy_kind == ProxyKind.PROXYLESS
            and not has_task_proxy(params)
        ):
            raise RuntimeError(
                "Enterprise hCaptcha requires a residential pool proxy; "
                "server egress IP would not match the submission IP"
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
        """Warmup → passive poll → checkbox → passive poll → challenge dispatch."""
        params["_phase"] = SolveStage.PASSIVE.value
        if not params.get("isInvisible") and render_options.get("size") != "invisible":
            await self._human_mouse(page)

        passive_budget = getattr(self._config, "poll_budget_passive", 2.0)

        # Passive / invisible widgets may resolve without interaction. Kept
        # short: the event-driven wait returns the instant a passive token
        # fires, so a small budget only bounds the "challenge is coming" case
        # rather than adding fixed dead time before dispatch.
        token = await self._poll_token(page, budget=passive_budget)
        if token:
            return token

        # Click the checkbox to trigger the challenge with a human-like pointer
        # path (P1-5): hCaptcha scores checkbox click dynamics as part of
        # motionData, so a raw teleport-click is a bot tell — reuse the same
        # humanised path the tile clicks use. A failure here is a signal
        # (managed widget / detached frame), logged not swallowed.
        params["_phase"] = SolveStage.INTERACTION.value
        try:
            checkbox_frame = page.frame_locator(_CHECKBOX_IFRAME)
            await self._human_click_in_frame(page, checkbox_frame, "#checkbox")
        except Exception as exc:
            log.info("hCaptcha checkbox click skipped: %s", exc)

        token = await self._poll_token(page, budget=passive_budget)
        if token:
            return token

        # Escalated to a visual challenge — dispatch by shape.
        params["_phase"] = SolveStage.CHALLENGE.value
        return await self._solve_challenge(page, website_key, params, client_key)

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

        # Give the challenge iframe a bounded moment to render its DOM before we
        # classify. hCaptcha frequently escalates to a visual challenge whose
        # iframe DOM lands 1–3s after the checkbox resolves; classifying an
        # empty iframe yields UNKNOWN / zero tiles and the grid solver bails the
        # whole attempt. If the widget instead *passes* while we wait, that
        # token is returned directly.
        ready_token = await self._await_challenge_ready(
            page, dispatcher.classifier, challenge_frame
        )
        if ready_token:
            params["_vision"] = vision
            return ready_token

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

        def _record_shape(shape: ChallengeShape) -> None:
            # Stash the detected shape for the ledger. Uses the dispatcher's
            # single detect() (via on_detected) rather than a second
            # classify pass — a duplicate detect() doubled the vision cost on
            # the UNKNOWN → vision-fallback path.
            params["_challenge_shape"] = shape.value

        token = await dispatcher.solve(
            challenge_frame, ctx, on_detected=_record_shape
        )
        params["_vision"] = vision
        return token

    async def _await_challenge_ready(
        self, page: Any, classifier: Any, challenge_frame: Any
    ) -> Optional[str]:
        """Wait (bounded) for the challenge DOM to render before classifying.

        Each iteration cheaply checks, in order: an already-minted token (the
        widget passed while we waited — return it), a widget ``error-callback``
        (surface it as a classified error so the retry loop reacts per kind),
        and whether the challenge iframe shows any recognizable shape/prompt
        signal (``classifier.ready`` — stop waiting, let dispatch classify a
        populated DOM). Returns the token if one appeared, else ``None`` once
        the DOM is ready or the budget elapses (dispatch then proceeds
        best-effort, exactly as before this wait existed).
        """
        budget = float(getattr(self._config, "poll_budget_challenge_ready", 4.0))
        interval = max(0.02, float(getattr(self._config, "poll_interval", 0.25)))
        loop = asyncio.get_event_loop()
        deadline = loop.time() + budget
        while True:
            result = await page.evaluate(self.WIDGET_TOKEN_EXTRACTOR_JS)
            token = result.get("token") if isinstance(result, dict) else None
            err = result.get("error") if isinstance(result, dict) else None
            if isinstance(token, str) and len(token) > 20:
                return token
            if err:
                raise classify_widget_error(err, provider=self.PROVIDER)
            try:
                if await classifier.ready(challenge_frame):
                    return None
            except Exception:  # noqa: BLE001 - readiness probe never fails a solve
                return None
            if loop.time() >= deadline:
                return None
            await asyncio.sleep(interval)

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

