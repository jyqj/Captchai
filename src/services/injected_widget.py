"""Shared base for injected-widget captcha solvers (hCaptcha + Turnstile).

Both hCaptcha and Cloudflare Turnstile are solved with the same primary
strategy: intercept the top-level document request for ``websiteURL`` and serve
a minimal synthetic page that renders the provider's widget for the sitekey
(``<widget>.render``) so the token is bound to the correct origin without
fighting the real page's interstitial.

The two solvers previously each carried a near-identical *copy* of:

  * ``_build_injected_page`` — inject api.js + a ``render()`` hook wired with a
    ``callback`` (captures the token) and an ``error-callback`` (surfaces widget
    failures);
  * the document route fulfiller; and
  * ``_poll_token`` — the event-driven, budget-bounded token wait.

Two copies is exactly how the ``NameError: name 'asyncio' is not defined``
regression reached production: Turnstile's ``_poll_token`` used ``asyncio`` but
a refactor removed the module's only *other* ``asyncio`` reference, and there
was no shared, tested implementation to inherit. This base collapses those
three into one place; per-provider differences are expressed as small class
attributes plus one render-body hook. Turnstile now inherits the same
``_poll_token`` hCaptcha's integration tests already exercise.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from playwright.async_api import Route

from .browser import set_context_resource_blocking
from .browser_solver import BaseBrowserSolver, SolveStage
from .captcha_errors import classify_widget_error

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PageStrategy:
    """How one solve prepares the page before the shared widget interaction.

    An injected-page solve and a real-page solve differ in exactly three ways,
    which are the whole of this object:

    * ``block_resources`` — resource interception is a safe bandwidth win on a
      synthetic injected page but a bot tell on a real target page (a browser
      that aborts every CSS/font/image is trivially flagged), so it's OFF for
      real-page solves.
    * ``prepare_context`` — how the widget is injected: route-fulfill the
      top-level document with synthetic HTML, or hook ``<widget>.render`` via an
      init script on the real page.
    * ``timeout_message`` — the error raised when no token is obtained.

    Everything else (the acquire → prepare → goto → provider interaction →
    release choreography, plus phase timing and stage tracking) is shared by
    :meth:`InjectedWidgetSolver._run_page_solve`, so hCaptcha and Turnstile
    can't drift into two separately-rotting copies (the root cause of the
    original missing-``import asyncio`` regression).
    """

    block_resources: bool
    timeout_message: str
    prepare_context: Callable[[Any], Awaitable[None]]


class InjectedWidgetSolver(BaseBrowserSolver):
    """Common injected-page rendering + token polling for widget solvers.

    Subclasses set the provider constants (and optionally override
    :meth:`_widget_render_body`) to specialise the injected page for their
    widget SDK, and implement :meth:`_interact` for the post-load choreography.
    Everything else — the document fulfiller, the unified event-driven
    :meth:`_poll_token`, and the :meth:`_run_page_solve` template shared by the
    injected and real-page strategies — lives here.
    """

    #: Human-readable provider name used in logs + ``classify_widget_error``.
    PROVIDER: str = "captcha"
    #: The ``window.<global>`` the widget SDK installs (``turnstile`` /
    #: ``hcaptcha``); probed in the injected page before calling ``render``.
    WIDGET_GLOBAL: str = ""
    #: ``<script src>`` for the widget SDK (explicit-render mode).
    WIDGET_API_JS: str = ""
    #: id of the container div the widget renders into.
    WIDGET_CONTAINER_ID: str = "omc-widget"
    #: JS expression assigned to ``window.__omcError`` inside error-callback.
    #: Turnstile historically set a bare ``true``; hCaptcha stringifies ``e``.
    WIDGET_ERROR_CALLBACK_VALUE: str = "String(e)"
    #: Extractor evaluated by :meth:`_poll_token`. Must return
    #: ``{token, error}`` and mention both ``__omcToken`` and ``__omcError``.
    WIDGET_TOKEN_EXTRACTOR_JS: str = ""

    # ── injected page ──────────────────────────────────────────

    def _widget_render_body(self) -> str:
        """JS that calls the widget's ``render`` (inside the injected page).

        Default form renders into the container by CSS selector; hCaptcha
        overrides this to store the widget id and drive invisible widgets.
        """
        return (
            f"window.{self.WIDGET_GLOBAL}.render("
            f"'#{self.WIDGET_CONTAINER_ID}', opts);"
        )

    def _build_injected_page(self, website_key: str, options: dict[str, Any]) -> str:
        """Minimal HTML that renders the provider widget for ``website_key``.

        ``action`` / ``cData`` / ``rqdata`` / ``enterprise`` and any other
        caller-supplied render options are merged into the ``render`` call so a
        production widget configured with them accepts the resulting token.
        """
        render_opts = {"sitekey": website_key, **options}
        opts_json = json.dumps(render_opts)
        return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>verify</title>
<script src="{self.WIDGET_API_JS}" async defer></script>
</head>
<body>
<div id="{self.WIDGET_CONTAINER_ID}"></div>
<script>
    window.__omcToken = null;
    function omcRender() {{
        if (!window.{self.WIDGET_GLOBAL}) {{ setTimeout(omcRender, 50); return; }}
        const opts = {opts_json};
        opts.callback = function (token) {{ window.__omcToken = token; }};
        opts['error-callback'] = function (e) {{ window.__omcError = {self.WIDGET_ERROR_CALLBACK_VALUE}; }};
        try {{
            {self._widget_render_body()}
        }} catch (e) {{ window.__omcError = String(e); }}
    }}
    omcRender();
</script>
</body>
</html>"""

    def _document_route_handler(
        self, html: str
    ) -> "Callable[[Route], Awaitable[None]]":
        """A route handler that fulfils the top-level document with ``html``.

        Non-document sub-requests (the widget's own api.js / xhr) are passed
        through untouched so the real SDK still loads.
        """

        async def _fulfill_document(route: Route) -> None:
            if route.request.resource_type == "document":
                await route.fulfill(
                    status=200, content_type="text/html", body=html
                )
            else:
                await route.continue_()

        return _fulfill_document

    # ── token polling (unified, event-driven) ──────────────────

    async def _poll_token(
        self, page: Any, budget: "float | None" = None
    ) -> Optional[str]:
        """Check-first token wait bounded by the poll budget.

        Uses ``page.wait_for_function`` so it returns the instant the widget
        callback fires (rather than sleeping a fixed interval), and surfaces a
        widget ``error-callback`` immediately as a classified error so the
        retry loop can react per kind (rate-limit → fail fast, etc.).

        ``budget`` defaults to ``config.poll_budget``; callers pass a shorter
        passive budget or a longer post-challenge budget.
        """
        total = budget if budget is not None else float(self._config.poll_budget)
        deadline = asyncio.get_event_loop().time() + total
        interval_ms = max(50, int(self._config.poll_interval * 1000))
        while asyncio.get_event_loop().time() < deadline:
            result = await page.evaluate(self.WIDGET_TOKEN_EXTRACTOR_JS)
            token = result.get("token") if isinstance(result, dict) else None
            err = result.get("error") if isinstance(result, dict) else None
            if isinstance(token, str) and len(token) > 20:
                log.info("Got %s token (len=%d)", self.PROVIDER, len(token))
                return token
            if err:
                raise classify_widget_error(err, provider=self.PROVIDER)
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
                # Timed out this slice — loop re-checks (also catches a token
                # set via the hidden input rather than the JS callback).
                pass
        return None

    # ── real-page injection ────────────────────────────────────

    def _build_real_page_init_script(self, options: dict[str, Any]) -> str:
        """Init script that hooks the *real* page's ``<widget>.render``.

        Used by the real-page strategy: instead of serving a synthetic page we
        navigate to the real target and wrap its own ``render`` call to merge
        the task's render options and capture the token / error via the same
        ``window.__omc*`` globals the injected page uses. Shared by every
        widget provider (parameterised by :attr:`WIDGET_GLOBAL`).
        """
        opts_json = json.dumps(options)
        g = self.WIDGET_GLOBAL
        return f"""
(function() {{
    window.__omcToken = null;
    window.__omcError = null;
    const __omcRenderOptions = {opts_json};
    function omcHook() {{
        if (window.{g} && window.{g}.render) {{
            const origRender = window.{g}.render.bind(window.{g});
            window.{g}.render = function(container, opts) {{
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
            setTimeout(omcHook, 50);
        }}
    }}
    omcHook();
}})();
"""

    # ── page-preparation strategies ────────────────────────────

    def _injected_page_strategy(
        self, website_url: str, website_key: str, render_options: dict[str, Any]
    ) -> PageStrategy:
        """Synthetic-page strategy: route-fulfil the document with our HTML.

        Resource interception stays ON (a safe bandwidth win — there's no real
        page to make look human).
        """
        html = self._build_injected_page(website_key, render_options)

        async def prepare(context: Any) -> None:
            # Re-assert blocking ON in case a reused warm session had it turned
            # off by a prior real-page solve, then fulfil the document.
            set_context_resource_blocking(context, True)
            await context.route(website_url, self._document_route_handler(html))

        return PageStrategy(
            block_resources=True,
            timeout_message=f"{self.PROVIDER} token not obtained within budget",
            prepare_context=prepare,
        )

    def _real_page_strategy(self, render_options: dict[str, Any]) -> PageStrategy:
        """Real-page strategy: hook ``<widget>.render`` via an init script.

        Resource interception is OFF — a browser that aborts every CSS/font/
        image on a real target page is one of the easiest anti-bot signals.
        """

        async def prepare(context: Any) -> None:
            set_context_resource_blocking(context, False)
            await context.add_init_script(
                self._build_real_page_init_script(render_options)
            )

        return PageStrategy(
            block_resources=False,
            timeout_message=f"{self.PROVIDER} token not obtained (real page mode)",
            prepare_context=prepare,
        )

    # ── shared solve template ──────────────────────────────────

    def _guard(self, solve_context: Any, params: dict[str, Any]) -> None:
        """Optional pre-goto guard; default no-op.

        Overridden by hCaptcha to refuse an enterprise solve that would run on
        a bare server-egress (proxyless) context.
        """
        return

    async def _interact(
        self,
        page: Any,
        website_url: str,
        website_key: str,
        render_options: dict[str, Any],
        params: dict[str, Any],
        client_key: "str | None",
    ) -> Optional[str]:
        """Provider-specific post-load interaction returning a token or ``None``.

        Implemented by each solver (Turnstile: humanised checkbox + poll;
        hCaptcha: passive poll → checkbox → visual-challenge dispatch).
        """
        raise NotImplementedError

    async def _run_page_solve(
        self,
        website_url: str,
        website_key: str,
        render_options: dict[str, Any],
        params: dict[str, Any],
        client_key: "str | None",
        *,
        strategy: PageStrategy,
    ) -> tuple[str, str]:
        """Acquire → prepare → goto → interact → release, shared by all widgets.

        The ``strategy`` supplies the injected-vs-real-page differences; the
        provider supplies :meth:`_interact`. Phase timings and :class:`SolveStage`
        tracking are recorded here so both providers (and both strategies) get
        them uniformly.
        """
        solve_context = await self._acquire_context(params)
        self._stash_fingerprint_geo(solve_context, params)
        context = solve_context.context
        user_agent = solve_context.user_agent

        solved = False
        # Initialised before the try so the finally's challenge-phase timing is
        # always defined even if context setup / goto raises early.
        _challenge_started = time.monotonic()
        try:
            params["_phase"] = SolveStage.ACQUIRE.value
            self._guard(solve_context, params)
            await strategy.prepare_context(context)
            page = await context.new_page()
            params["_phase"] = SolveStage.PAGE_LOAD.value
            timeout_ms = self._config.browser_timeout * 1000
            _page_started = time.monotonic()
            await page.goto(
                website_url, wait_until="domcontentloaded", timeout=timeout_ms
            )
            params["_phase_page_load_ms"] = int(
                (time.monotonic() - _page_started) * 1000
            )
            _challenge_started = time.monotonic()

            token = await self._interact(
                page, website_url, website_key, render_options, params, client_key
            )
            if token:
                solved = True
                return token, user_agent

            raise RuntimeError(strategy.timeout_message)
        finally:
            params["_phase_challenge_ms"] = int(
                (time.monotonic() - _challenge_started) * 1000
            )
            await self._release_context(solve_context, solved, params)
