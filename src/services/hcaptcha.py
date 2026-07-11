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
import time
from dataclasses import dataclass
from typing import Any, Optional

from playwright.async_api import FrameLocator

from ..assets.proxy_pool import proxy_from_params
from ..parsing.dispatcher import (
    ChallengeClassifier,
    ChallengeContext,
    ChallengeDispatcher,
    ChallengeShape,
    ClassifierSelectors,
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

# Token extractor. Reads the shared-DOM ``#omc-result`` FIRST so it works under
# Camoufox (whose isolated-world evaluate can't see the page's main-world
# ``window.__omc*`` globals), then falls back to the ``window`` global, the
# provider's own ``h-captcha-response`` textarea, and ``hcaptcha.getResponse()``
# for stock Chromium.
_EXTRACT_HCAPTCHA_TOKEN_JS = (
    "() => {\n"
    "    let token = null;\n"
    "    let error = null;\n"
    "    " + InjectedWidgetSolver._omc_dom_read_js() + ""
    "    if (!token && window.__omcToken) {\n"
    "        token = window.__omcToken;\n"
    "    }\n"
    "    if (!token) {\n"
    "        const textarea = document.querySelector('[name=\"h-captcha-response\"]')\n"
    "            || document.querySelector('[name=\"g-recaptcha-response\"]');\n"
    "        if (textarea && textarea.value && textarea.value.length > 20) {\n"
    "            token = textarea.value;\n"
    "        } else if (window.hcaptcha && typeof window.hcaptcha.getResponse === 'function') {\n"
    "            try {\n"
    "                const resp = window.hcaptcha.getResponse();\n"
    "                if (resp && resp.length > 20) token = resp;\n"
    "            } catch (e) {}\n"
    "        }\n"
    "    }\n"
    "    return {token: token, error: error || window.__omcError || null};\n"
    "}\n"
)

# Localization-independent iframe matchers. The challenge iframe title is
# localized ("Main content of the hCaptcha challenge" in English only), so match
# on the src host as the robust primary signal.
#
# Self-hosted enterprise deployments serve the widget iframes from a customer
# ``assethost`` rather than hcaptcha.com, so the host-pinned matcher alone would
# miss them. Match the path segment (``/checkbox`` / ``/challenge``) and the
# hCaptcha frame name convention (``a-*`` / ``c-*``) too — both are host-
# independent — before falling back to the localized title.
_CHECKBOX_IFRAME = (
    'iframe[src*="hcaptcha.com"][src*="checkbox"], '
    'iframe[src*="/checkbox"], '
    'iframe[title*="checkbox"], '
    'iframe[name^="a-"]'
)
_CHALLENGE_IFRAME = (
    'iframe[src*="hcaptcha.com"][src*="challenge"], '
    'iframe[src*="/challenge"], '
    'iframe[title*="challenge"], '
    'iframe[name^="c-"]'
)

# ``enterprisePayload`` is the 2captcha / YesCaptcha container convention whose
# keys are hcaptcha.render() options. A few solver ecosystems use field names
# that differ from the JS API; normalise the common ones so the widget actually
# receives them. Everything else passes through unchanged.
_ENTERPRISE_FIELD_ALIASES = {
    "apiEndpoint": "endpoint",
}

# hCaptcha-scoped challenge-shape detection. The generic classifier carries
# every provider's class names (GeeTest sliders, reCAPTCHA dynamic markers,
# invented bbox overlays); hCaptcha uses none of those, so pinning it to
# hCaptcha's real challenge DOM (a ``.task-grid`` of ``.task-image`` tiles,
# ``.prompt-text`` prompt, native ``draggable`` for its rare drag challenge,
# ``.challenge-example`` panel for area-select) stops a plain grid from being
# misrouted to a slide / dynamic solver it never matches.
_HCAPTCHA_CLASSIFIER_SELECTORS = ClassifierSelectors(
    grid=(".task-grid", ".task-image"),
    tile=".task-image",
    dynamic_markers=(),
    slider=(),
    drag=("[draggable='true']", ".draggable"),
    bbox=(".challenge-example",),
    prompt=(".prompt-text", ".challenge-prompt"),
)


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

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Opt-in cross-context device-trust store: per egress-identity bucket ->
        # the hCaptcha domain cookies (``hmt`` etc.) captured from a prior solve.
        # Re-seeding these into a fresh enterprise context makes the widget see
        # a returning device instead of a brand-new zero-history one (which
        # enterprise risk models penalise). In-memory only; a restart starts
        # cold. Keyed so one exit IP keeps one coherent device.
        self._device_cookies: dict[str, list] = {}

    PROVIDER = "hCaptcha"
    WIDGET_GLOBAL = "hcaptcha"
    WIDGET_API_JS = "https://js.hcaptcha.com/1/api.js?render=explicit"
    WIDGET_CONTAINER_ID = "omc-hcaptcha"
    WIDGET_ERROR_CALLBACK_VALUE = "String(e)"
    WIDGET_TOKEN_EXTRACTOR_JS = _EXTRACT_HCAPTCHA_TOKEN_JS
    # Invisible hCaptcha scores the browser passively; firing execute() the
    # instant the widget renders ships a near-empty motion buffer (a strong
    # "invisible → challenge every time" tell). Defer it so ``_interact_invisible``
    # can seed motionData first, then trigger the exposed ``window.__omcExecute``.
    DEFER_INVISIBLE_EXECUTE = True

    def _widget_render_body(self) -> str:
        # hCaptcha renders by container *id* (not a CSS selector), stores the
        # widget id, and exposes an explicit execute trigger. For an invisible
        # widget execute() is DEFERRED (``__omcDeferExecute``) so the solver can
        # seed behaviour before the passive request; otherwise it fires inline.
        return (
            "window.__omcWidgetId = window.hcaptcha.render('omc-hcaptcha', opts);\n"
            "            window.__omcExecute = function () {\n"
            "                if (window.__omcExecuted) return;\n"
            "                window.__omcExecuted = true;\n"
            "                try { window.hcaptcha.execute(window.__omcWidgetId); }\n"
            "                catch (e) { window.__omcError = String(e); __omcSet('error', e && e.message ? e.message : String(e)); }\n"
            "            };\n"
            # Bridge the deferred invisible execute() through the shared DOM so a
            # Camoufox isolated-world evaluate can trigger it (it cannot call the
            # main-world __omcExecute function directly).
            "            __omcInstallExecBridge();\n"
            "            if (opts.size === 'invisible' && !window.__omcDeferExecute) {\n"
            "                window.__omcExecute();\n"
            "            }"
        )

    def _widget_api_js(self, options: dict[str, Any]) -> str:
        # Self-hosted enterprise hCaptcha serves its SDK from the customer's
        # ``assethost`` rather than js.hcaptcha.com; loading the public script
        # there contradicts the render config (``endpoint`` / ``assethost`` /
        # ``imghost``) and the solve fails. When ``assethost`` is an explicit
        # https origin, load api.js from it (hCaptcha's self-host convention:
        # ``<assethost>/1/api.js``). Standard enterprise (e.g. Stripe) sets no
        # assethost, so this falls back to the public SDK unchanged.
        assethost = options.get("assethost")
        if isinstance(assethost, str) and assethost.startswith("http"):
            return f"{assethost.rstrip('/')}/1/api.js?render=explicit"
        return self.WIDGET_API_JS

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
        # Refuse (or loudly warn about) an enterprise solve on detectable stock
        # Chromium, whose automation + software-WebGL signals enterprise
        # detectors flag. Runs before any attempt so the refusal is fast.
        self._enforce_enterprise_runtime(profile.variant)
        # Caller-facing caveats the service can't enforce itself (enterprise
        # tokens can't be server-verified without the target's secret; rqdata is
        # single-use / IP-bound). Surfaced in ``solution.warnings``.
        warnings = self._enterprise_warnings(params, profile)

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

        started = time.monotonic()
        solution = await self._solve_with_retries(
            params,
            sitekey=website_key,
            client_key=client_key,
            attempt_fn=_attempt,
            build_solution=lambda token, ua: {
                "gRecaptchaResponse": token,
                "userAgent": ua,
                **({"warnings": warnings} if warnings else {}),
            },
            provider="HCaptcha",
            default_task_type=params.get("type", "HCaptchaTaskProxyless"),
            default_challenge_shape="grid_select",
            on_error=_escalate_tier,
            verify_provider="hcaptcha",
        )
        # rqdata is single-use AND short-lived: an enterprise widget rejects a
        # token minted from a stale nonce even if the grid was answered
        # correctly. We can't read the true server TTL, but a solve that took
        # longer than a conservative budget is the likeliest cause of a
        # structurally-valid-but-rejected enterprise token, so surface it as a
        # warning the caller can act on (capture a fresher rqdata / speed up the
        # egress) rather than silently returning a probably-dead token.
        if profile.variant == "enterprise" and profile.render_options.get("rqdata"):
            elapsed = time.monotonic() - started
            ttl = float(getattr(self._config, "hcaptcha_rqdata_ttl", 30.0))
            if ttl > 0 and elapsed > ttl:
                note = (
                    f"enterprise solve took {elapsed:.0f}s (> rqdata freshness "
                    f"budget {ttl:.0f}s); the rqdata nonce may have expired — if "
                    "the token is rejected, capture a fresh rqdata and/or use a "
                    "faster egress"
                )
                existing = solution.get("warnings") or []
                solution["warnings"] = [*existing, note]
        return solution

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
                name = str(key)
                render_options[_ENTERPRISE_FIELD_ALIASES.get(name, name)] = value

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

    def _enforce_enterprise_runtime(self, variant: str) -> None:
        """Warn (or refuse) an enterprise solve on detectable stock Chromium.

        Stock headless Chromium's automation signals — and its software-WebGL
        renderer, which contradicts the stealth layer's spoofed discrete-GPU
        string — are exactly what enterprise hCaptcha detectors flag. A hardened
        runtime (camoufox / rebrowser) avoids both. This never *changes* the
        runtime; it surfaces the risk, and when
        ``ENTERPRISE_REQUIRE_HARDENED_RUNTIME`` is set it refuses the solve so an
        operator can't unknowingly mint low-trust enterprise tokens on Chromium.
        """
        if variant != "enterprise":
            return
        runtime = getattr(self._manager, "runtime", "chromium")
        if runtime in ("camoufox", "rebrowser"):
            return
        if getattr(self._config, "enterprise_require_hardened_runtime", False):
            raise RuntimeError(
                "Enterprise hCaptcha requires a hardened browser runtime "
                "(BROWSER_RUNTIME=camoufox or rebrowser) but the active runtime "
                f"is {runtime!r} (ENTERPRISE_REQUIRE_HARDENED_RUNTIME=true). "
                "Stock Chromium's automation + software-WebGL signals are "
                "trivially flagged by enterprise detectors."
            )
        log.warning(
            "Enterprise hCaptcha on stock Chromium runtime %r — enterprise "
            "detectors flag its automation / software-WebGL signals. Set "
            "BROWSER_RUNTIME=camoufox (and install it) for reliable solves.",
            runtime,
        )

    def _enterprise_warnings(
        self, params: dict[str, Any], profile: HCaptchaProfile
    ) -> list[str]:
        """Caller-facing caveats for an enterprise solve the service can't enforce.

        Enterprise tokens are minted in a relay model: the service holds no
        siteverify secret for the target sitekey, so it *cannot* confirm the
        token will be accepted, and the token is bound to a single-use ``rqdata``
        nonce plus the egress IP. These are surfaced in ``solution.warnings`` so
        the caller knows to (a) capture a FRESH rqdata per solve and (b) submit
        from the same egress — the two most common reasons a structurally-valid
        enterprise token is still rejected downstream.
        """
        if profile.variant != "enterprise":
            return []
        warnings: list[str] = [
            "enterprise hCaptcha token cannot be server-verified (no siteverify "
            "secret for this sitekey); confirm acceptance via /reportCorrect or "
            "your own downstream check",
            "enterprise token is IP-bound: submit it from the same egress that "
            "minted it (solution.egressServer / proxyKind)",
        ]
        rqdata = profile.render_options.get("rqdata")
        if not rqdata:
            warnings.append(
                "no rqdata supplied for an enterprise solve; most enterprise "
                "widgets reject a token minted without a fresh rqdata nonce"
            )
        elif not isinstance(rqdata, str) or len(rqdata.strip()) < 20:
            warnings.append(
                "rqdata looks malformed (too short); capture a fresh rqdata from "
                "the target's own hCaptcha session"
            )
        return warnings

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

    # ── device-trust persistence (opt-in) ─────────────────────

    _HCAPTCHA_COOKIE_DOMAINS = ("hcaptcha.com", ".hcaptcha.com")

    def _device_key(self, params: dict[str, Any]) -> str:
        """Egress-identity bucket for the device-trust cookie store.

        Tie the persisted device to the exit IP (pool proxy id / seed), so one
        residential IP keeps one coherent returning device across solves; task
        proxies key by their gateway; proxyless keys by a constant.
        """
        return str(
            params.get("_proxy_seed")
            or params.get("_pool_proxy_id")
            or params.get("_egress_server")
            or "proxyless"
        )

    def _device_persistence_on(self) -> bool:
        return bool(getattr(self._config, "hcaptcha_device_persistence", False))

    async def _restore_device(self, context: Any, params: dict[str, Any]) -> None:
        """Re-seed persisted hCaptcha device cookies into a fresh context."""
        if not self._device_persistence_on():
            return
        add_cookies = getattr(context, "add_cookies", None)
        if add_cookies is None:
            return
        cookies = self._device_cookies.get(self._device_key(params))
        if not cookies:
            return
        try:
            await add_cookies(cookies)
        except Exception as exc:  # noqa: BLE001 - device restore is best-effort
            log.debug("hCaptcha device cookie restore failed: %s", exc)

    async def _persist_device(
        self, context: Any, params: dict[str, Any], solved: bool
    ) -> None:
        """Capture the hCaptcha device cookies from this context for reuse.

        Persists the provider's device-trust cookies (domain ``.hcaptcha.com``)
        keyed by egress identity so the next solve on the same IP presents a
        returning device. Best-effort and guarded for fake contexts in tests.
        """
        if not self._device_persistence_on():
            return
        get_cookies = getattr(context, "cookies", None)
        if get_cookies is None:
            return
        try:
            all_cookies = await get_cookies()
        except Exception as exc:  # noqa: BLE001 - device persist is best-effort
            log.debug("hCaptcha device cookie capture failed: %s", exc)
            return
        if not isinstance(all_cookies, list):
            return
        hc = [
            c
            for c in all_cookies
            if isinstance(c, dict)
            and any(
                str(c.get("domain", "")).endswith(d)
                for d in self._HCAPTCHA_COOKIE_DOMAINS
            )
        ]
        if hc:
            self._device_cookies[self._device_key(params)] = hc

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
        """Warmup → passive poll → checkbox → passive poll → challenge dispatch.

        Invisible widgets take a distinct path (:meth:`_interact_invisible`):
        they have no checkbox iframe, so the solver seeds motion, triggers the
        deferred ``execute()``, then waits for a passive token or an escalated
        challenge — instead of burning the checkbox-click timeout on an element
        that never exists.
        """
        invisible = bool(
            params.get("isInvisible") or render_options.get("size") == "invisible"
        )
        passive_budget = getattr(self._config, "poll_budget_passive", 2.0)

        if invisible:
            return await self._interact_invisible(
                page, website_key, params, client_key, passive_budget
            )

        touch = bool(params.get("_is_mobile"))
        params["_phase"] = SolveStage.PASSIVE.value
        await self._human_mouse(page, touch=touch)

        # Passive widgets may resolve without interaction. Kept short: the
        # event-driven wait returns the instant a passive token fires, so a
        # small budget only bounds the "challenge is coming" case rather than
        # adding fixed dead time before dispatch.
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
            await self._human_click_in_frame(
                page, checkbox_frame, "#checkbox", touch=touch
            )
        except Exception as exc:
            log.info("hCaptcha checkbox click skipped: %s", exc)

        token = await self._poll_token(page, budget=passive_budget)
        if token:
            return token

        # Escalated to a visual challenge — dispatch by shape.
        params["_phase"] = SolveStage.CHALLENGE.value
        return await self._solve_challenge(page, website_key, params, client_key)

    async def _interact_invisible(
        self,
        page: Any,
        website_key: str,
        params: dict[str, Any],
        client_key: Optional[str],
        passive_budget: float,
    ) -> Optional[str]:
        """Invisible flow: seed motion → deferred execute() → poll → dispatch.

        An invisible hCaptcha widget has no checkbox and is scored passively, so:

        * We wait for the widget to render (``__omcExecute`` defined) *before*
          moving the pointer, so hCaptcha's motion listeners (attached at widget
          load) actually capture the movement — a page with an empty motion
          buffer is one of the strongest "invisible → challenge every time"
          signals.
        * ``execute()`` is fired explicitly AFTER that motion (it was deferred at
          render) so the passive ``/getcaptcha`` carries real behaviour.
        * There is NO checkbox click. The previous flow waited out the full
          click timeout on a checkbox iframe that never exists for an invisible
          widget — tens of seconds of dead time per attempt.
        """
        params["_phase"] = SolveStage.PASSIVE.value
        touch = bool(params.get("_is_mobile"))
        await self._await_widget_ready(page)
        # Invisible widgets are scored purely on the passive behaviour timeline,
        # so seed a fuller motion history (not the short pre-checkbox warmup)
        # BEFORE firing execute(), so the passive ``/getcaptcha`` carries a real
        # ``motionData`` buffer rather than a near-empty one (the strongest
        # "invisible → challenge every time" tell).
        invisible_seconds = float(
            getattr(self._config, "hcaptcha_invisible_motion_seconds", 3.0)
        )
        await self._human_mouse(page, seconds=invisible_seconds, touch=touch)
        await self._trigger_invisible_execute(page)

        # Passive pass is the entire point of an invisible widget, so give the
        # verdict a longer budget than the checkbox-path passive poll (the
        # /getcaptcha round-trip through a residential proxy commonly exceeds
        # the 2s default before a passive token lands).
        invisible_budget = max(
            passive_budget,
            float(getattr(self._config, "hcaptcha_invisible_passive_budget", 4.0)),
        )
        token = await self._poll_token(page, budget=invisible_budget)
        if token:
            return token

        # Passive scoring rejected the browser → a visual challenge; dispatch it.
        params["_phase"] = SolveStage.CHALLENGE.value
        return await self._solve_challenge(page, website_key, params, client_key)

    async def _await_widget_ready(self, page: Any) -> None:
        """Wait (bounded) for the invisible widget's ``__omcExecute`` to exist.

        execute() is deferred for invisible widgets, so we must wait for the
        widget to actually render before triggering it. Bounded well under the
        page timeout; a widget that never renders falls through and the passive
        poll / challenge dispatch handles the miss.
        """
        timeout_ms = min(
            8000, int(getattr(self._config, "browser_timeout", 30) * 1000)
        )
        try:
            # DOM-first readiness: under Camoufox ``window.__omcExecute`` lives
            # in the main world and is invisible here, so also accept the shared
            # DOM signals (``#omc-result`` marked rendered/done/error) the page
            # sets at render time.
            await page.wait_for_function(
                "() => { const el = document.getElementById('"
                + self.OMC_RESULT_ID
                + "'); const st = el ? el.getAttribute('data-status') : null;"
                " return st === 'rendered' || st === 'done' || st === 'error'"
                " || typeof window.__omcExecute === 'function'; }",
                timeout=timeout_ms,
            )
        except Exception as exc:  # noqa: BLE001 - readiness probe is best-effort
            log.debug("hCaptcha invisible widget not ready in time: %s", exc)

    async def _trigger_invisible_execute(self, page: Any) -> None:
        """Fire the deferred invisible ``execute()`` (no-op if not yet defined).

        Flips ``#omc-exec``'s ``data-exec`` on the shared DOM — the page's
        MutationObserver (main world) then calls ``execute()`` — so the trigger
        crosses Camoufox's isolated-world boundary. Also calls the main-world
        function directly as a stock-Chromium fast path (guarded against a
        double-fire by ``__omcExecuted``).
        """
        try:
            await page.evaluate(
                "() => { const t = document.getElementById('"
                + self.OMC_EXEC_ID
                + "'); if (t) { t.setAttribute('data-exec', '1'); }"
                " if (window.__omcExecute) { window.__omcExecute(); } }"
            )
        except Exception as exc:  # noqa: BLE001 - trigger is best-effort
            log.info("hCaptcha invisible execute trigger failed: %s", exc)

    # ── challenge dispatch ─────────────────────────────────────

    async def _solve_challenge(
        self,
        page: Any,
        website_key: str,
        params: dict[str, Any],
        client_key: Optional[str],
    ) -> Optional[str]:
        # Surface a widget error-callback immediately as a classified error so
        # the solve loop reacts per kind (rate-limit → fail fast, etc.). Reads
        # the shared-DOM ``#omc-result`` first (Camoufox-safe), then the
        # main-world ``window.__omcError`` fallback.
        err = await page.evaluate(
            "() => { const el = document.getElementById('"
            + self.OMC_RESULT_ID
            + "'); if (el && el.getAttribute('data-status') === 'error') {"
            " return el.textContent || 'error'; } return window.__omcError || null; }"
        )
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
                # Mobile fingerprint → shape solvers tap via the touchscreen
                # instead of the mouse, matching the phone context hCaptcha
                # mobile challenges score.
                "touch": bool(params.get("_is_mobile")),
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
        classifier = ChallengeClassifier(
            vision=vision,
            selectors=_HCAPTCHA_CLASSIFIER_SELECTORS,
            # The hCaptcha VisionAdapter classifies tile images, not "what shape
            # is this frame" — an image-less shape hint would be a wasted model
            # call that can't return a shape. Rely on the UNKNOWN→grid fallback.
            vision_hint=False,
        )
        # UNKNOWN → grid_select: hCaptcha's dominant shape. A DOM class-name
        # change (or an unfamiliar layout) then still gets one real grid attempt
        # instead of a hard miss + full retry.
        dispatcher = ChallengeDispatcher(
            classifier, unknown_fallback=ChallengeShape.GRID_SELECT
        )
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

