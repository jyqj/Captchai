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
import html
import ipaddress
import json
import logging
import socket
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlsplit

from playwright.async_api import Route

from .browser import set_context_resource_blocking
from .browser_solver import BaseBrowserSolver, SolveStage
from .captcha_errors import (
    CaptchaError,
    NonRetryableWidgetError,
    classify_widget_error,
)

log = logging.getLogger(__name__)


def _js_literal(value: Any) -> str:
    """Serialise ``value`` for embedding inside an inline ``<script>``.

    ``json.dumps`` gives correct JS literals for every JSON type, but a string
    containing ``</script>`` would terminate the script element early and
    ``U+2028`` / ``U+2029`` are line terminators in JS. Escaping the three
    closes the inline-injection hole for caller-supplied render options.
    """
    return (
        json.dumps(value)
        .replace("</", "<\\/")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


#: SSRF-relevant IPv4 ranges the stdlib predicates do not flag on every
#: Python version: "this network" (0.0.0.0/8), CGNAT shared space
#: (100.64.0.0/10), IETF protocol assignments (192.0.0.0/24) and the
#: benchmarking range (198.18.0.0/15).
_BLOCKED_NETWORKS: tuple[Any, ...] = tuple(
    ipaddress.ip_network(network)
    for network in (
        "0.0.0.0/8",
        "100.64.0.0/10",
        "192.0.0.0/24",
        "198.18.0.0/15",
    )
)


def _address_is_blocked(
    ip: "ipaddress.IPv4Address | ipaddress.IPv6Address",
) -> bool:
    """True when ``ip`` must never be reached from a caller-supplied URL.

    Covers the SSRF-relevant categories: loopback, RFC1918 / ULA private,
    link-local (incl. cloud metadata ``169.254.169.254``), reserved,
    multicast and the unspecified address, plus the extra explicit ranges in
    :data:`_BLOCKED_NETWORKS` (CGNAT ``100.64.0.0/10`` in particular is not
    ``is_private`` on every supported Python). An IPv4-mapped IPv6 address is
    checked as its embedded IPv4 address too (``::ffff:127.0.0.1`` is not
    flagged by ``IPv6Address.is_loopback``).
    """
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if isinstance(ip, ipaddress.IPv4Address) and any(
        ip in network for network in _BLOCKED_NETWORKS
    ):
        return True
    return bool(
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


async def _validate_public_http_url(
    url: str, *, field: str = "websiteURL", strict: bool = False
) -> str:
    """Reject a caller-supplied navigation URL that could reach an internal host.

    Mirrors the reCAPTCHA solvers' SSRF pre-flight, with one deliberate policy
    difference: a DNS *resolution failure* (``socket.gaierror``) is fail-OPEN —
    it is logged and the solve proceeds. An offline/blocked resolver is an
    environment fault, not evidence of an internal target, and failing closed
    there would break every solve (including offline tests) for a hostname the
    navigation itself would immediately report as unreachable. ``strict``
    (driven by ``config.ssrf_strict``) turns that fail-open into a hard
    ``ValueError`` for operators who prefer fail-closed DNS. Once the host
    *does* resolve, the check is fail-CLOSED: ANY resolved address in a
    loopback / private / link-local / reserved / multicast / unspecified range
    rejects the URL before a proxy or browser context is checked out.

    Before resolution, a host that parses as an IP literal is checked
    directly. ``ipaddress`` rejects the legacy IPv4 spellings Chromium
    canonicalises (decimal ``2130706433``, octal ``0177.0.0.1``, hex
    ``0x7f000001``), so ``socket.inet_aton`` is used as a second parser —
    otherwise a literal loopback spelling would slip past the address check
    and be resolved/canonicalised to an internal address by the browser.

    Raises ``ValueError`` with a caller-readable message; the caller converts
    it to :class:`NonRetryableWidgetError` so the retry loop fails fast and the
    task manager reports ``ERROR_CAPTCHA_UNSOLVABLE`` instead of a 500.
    """
    if not isinstance(url, str) or not url.strip():
        raise ValueError(f"{field} must be a non-empty http(s) URL")
    target = url.strip()
    try:
        parts = urlsplit(target)
    except ValueError as exc:
        raise ValueError(f"{field} is not a valid URL: {exc}") from exc
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(
            f"{field} must use http or https (got {parts.scheme or 'no scheme'!r})"
        )
    host = parts.hostname
    if not host:
        raise ValueError(f"{field} has no host: {target!r}")
    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise ValueError(f"{field} has an invalid port: {exc}") from exc

    # Chromium canonicalises legacy IPv4 spellings that ``ipaddress`` rejects,
    # so normalise with ``socket.inet_aton`` (which accepts decimal / octal /
    # hex forms) BEFORE resolution: a literal that parses is checked directly
    # and a blocked one is refused even if the resolver never sees it.
    try:
        packed = socket.inet_aton(host.split("%", 1)[0])
    except OSError:
        packed = None
    if packed is not None:
        try:
            literal = ipaddress.ip_address(packed)
        except ValueError:  # pragma: no cover - inet_aton always yields 4 bytes
            literal = None
        if literal is not None and _address_is_blocked(literal):
            raise ValueError(
                f"{field} host {host!r} is a blocked address {literal} "
                "(loopback/private/link-local/reserved/multicast)"
            )

    try:
        infos = await asyncio.to_thread(
            socket.getaddrinfo, host, port, type=socket.SOCK_STREAM
        )
    except socket.gaierror as exc:
        if strict:
            raise ValueError(
                f"{field} host {host!r} could not be resolved: {exc}"
            ) from exc
        # Fail-open on a resolver error (offline env / transient DNS failure):
        # warn loudly but let navigation surface the real network outcome.
        log.warning(
            "%s SSRF preflight could not resolve host %r (%s); proceeding "
            "without the address check",
            field,
            host,
            exc,
        )
        return target
    for info in infos or ():
        address = info[4][0]
        try:
            ip = ipaddress.ip_address(str(address).split("%", 1)[0])
        except ValueError:
            raise ValueError(
                f"{field} host {host!r} resolved to an invalid address {address!r}"
            )
        if _address_is_blocked(ip):
            raise ValueError(
                f"{field} host {host!r} resolves to a blocked address {ip} "
                "(loopback/private/link-local/reserved/multicast)"
            )
    return target


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

    ``key`` participates in the per-task signature that guards warm-session
    reuse, and ``cleanup_context`` reverses whatever ``prepare_context``
    installed on the (possibly pooled) context so one solve cannot leak a stale
    document route into the next.

    Everything else (the acquire → prepare → goto → provider interaction →
    release choreography, plus phase timing and stage tracking) is shared by
    :meth:`InjectedWidgetSolver._run_page_solve`, so hCaptcha and Turnstile
    can't drift into two separately-rotting copies (the root cause of the
    original missing-``import asyncio`` regression).
    """

    block_resources: bool
    timeout_message: str
    prepare_context: Callable[[Any], Awaitable[None]]
    key: str = "page"
    cleanup_context: Optional[Callable[[Any], Awaitable[None]]] = None


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
    #: When True, an invisible widget's ``execute()`` is NOT auto-fired at
    #: render. Instead the render hook exposes ``window.__omcExecute`` and the
    #: solver drives it explicitly (after seeding behaviour / motionData), so
    #: the passive ``/getcaptcha`` doesn't fire with an empty motion buffer.
    #: hCaptcha opts in (True); Turnstile keeps the immediate fire (False).
    DEFER_INVISIBLE_EXECUTE: bool = False
    #: Init script that flags the deferral to the render hook. Injected before
    #: the widget renders when ``DEFER_INVISIBLE_EXECUTE`` is set for an
    #: invisible solve.
    _DEFER_EXECUTE_JS: str = "window.__omcDeferExecute = true;"

    # ── Camoufox-safe DOM bridge ───────────────────────────────
    #: Camoufox (a Firefox fork) runs ``page.evaluate`` in an ISOLATED content
    #: world for stealth, so a ``window.__omc*`` global set by the page's own
    #: main-world ``<script>`` is invisible to the solver's evaluate calls (it
    #: returns ``undefined``). The shared DOM, however, is the SAME across
    #: worlds. So the token/status handoff (page → solver) and the invisible
    #: ``execute()`` trigger (solver → page) both go through hidden DOM elements:
    #: the page writes the token/status onto ``#omc-result`` and observes
    #: ``#omc-exec`` for an execute signal, while the solver reads ``#omc-result``
    #: and writes ``#omc-exec`` — all DOM ops that cross the world boundary. The
    #: ``window.__omc*`` globals are kept as a fast path for stock Chromium and a
    #: fallback everywhere.
    OMC_RESULT_ID: str = "omc-result"
    OMC_EXEC_ID: str = "omc-exec"

    @classmethod
    def _omc_bridge_js(cls) -> str:
        """Main-world helpers the injected/real page uses for the DOM bridge.

        Defines ``__omcSet`` (write token/status onto ``#omc-result``),
        ``__omcMarkReady`` (mark the widget rendered), and
        ``__omcInstallExecBridge`` (observe ``#omc-exec`` and fire the deferred
        ``window.__omcExecute`` when the solver flips its ``data-exec`` attribute
        — the cross-world trigger for an invisible widget). All create their
        elements lazily so the real-page path (which has no synthetic HTML)
        works too.
        """
        return (
            "function __omcResultEl(){var el=document.getElementById('%(r)s');"
            "if(!el){el=document.createElement('div');el.id='%(r)s';"
            "el.setAttribute('data-status','');el.style.display='none';"
            "(document.body||document.documentElement).appendChild(el);}return el;}"
            "function __omcSet(status,value){var el=__omcResultEl();"
            "el.textContent=(value==null?'':String(value));"
            "el.setAttribute('data-status',status);}"
            "function __omcMarkReady(){var el=__omcResultEl();"
            "if(!el.getAttribute('data-status')){el.setAttribute('data-status','rendered');}}"
            "function __omcInstallExecBridge(){var t=document.getElementById('%(e)s');"
            "if(!t){t=document.createElement('div');t.id='%(e)s';"
            "t.setAttribute('data-exec','0');t.style.display='none';"
            "(document.body||document.documentElement).appendChild(t);}"
            "try{var obs=new MutationObserver(function(){"
            "if(t.getAttribute('data-exec')==='1'&&window.__omcExecute){"
            "window.__omcExecute();}});"
            "obs.observe(t,{attributes:true,attributeFilter:['data-exec']});}catch(e){}}"
        ) % {"r": cls.OMC_RESULT_ID, "e": cls.OMC_EXEC_ID}

    @classmethod
    def _omc_dom_read_js(cls) -> str:
        """JS fragment (statements) that reads a token/error off ``#omc-result``.

        Expects ``token`` and ``error`` to be declared by the caller; sets them
        from the DOM when present. Shared verbatim by the provider token
        extractors so the DOM path is identical for hCaptcha and Turnstile.
        """
        return (
            "const __omcEl = document.getElementById('%(r)s');\n"
            "    if (__omcEl) {\n"
            "        const __st = __omcEl.getAttribute('data-status');\n"
            "        if (__st === 'done') token = __omcEl.textContent;\n"
            "        else if (__st === 'error') error = __omcEl.textContent;\n"
            "    }\n"
        ) % {"r": cls.OMC_RESULT_ID}

    def _classify_error(self, raw: object) -> CaptchaError:
        """Map a provider ``error-callback`` payload to the shared taxonomy.

        Providers with structured error codes override this (e.g. Turnstile's
        numeric codes); the default keeps the substring classifier so behaviour
        for free-text errors is unchanged.
        """
        return classify_widget_error(raw, provider=self.PROVIDER)

    # ── injected page ──────────────────────────────────────────

    def _widget_api_js(self, options: dict[str, Any]) -> str:
        """Resolve the widget SDK script URL for this solve.

        Default is the class-level public SDK (:attr:`WIDGET_API_JS`).
        Providers override this to honour a self-hosted deployment (e.g.
        enterprise hCaptcha served from a custom ``assethost``) so the injected
        page doesn't contradict the render config by pulling the public script.
        """
        return self.WIDGET_API_JS

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
        # ``opts`` is caller-derived: serialise it as a JSON literal (escaped
        # for inline <script>) rather than interpolating raw values into JS.
        opts_json = _js_literal(render_opts)
        # ``api_js`` may come from a caller-supplied assethost: escape it for
        # the HTML attribute so it cannot break out of the src="" context.
        api_js = html.escape(self._widget_api_js(options), quote=True)
        bridge = self._omc_bridge_js()
        # The token/status is written onto ``#omc-result`` (and the legacy
        # ``window.__omc*`` globals) so a Camoufox isolated-world evaluate can
        # still read it off the shared DOM. ``#omc-exec`` is the solver→page
        # trigger for a deferred invisible ``execute()``.
        return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>verify</title>
<script src="{api_js}" async defer></script>
</head>
<body>
<div id="{html.escape(self.WIDGET_CONTAINER_ID, quote=True)}"></div>
<div id="{html.escape(self.OMC_RESULT_ID, quote=True)}" data-status="" style="display:none"></div>
<div id="{html.escape(self.OMC_EXEC_ID, quote=True)}" data-exec="0" style="display:none"></div>
<script>
    window.__omcToken = null;
    {bridge}
    function omcRender() {{
        if (!window.{self.WIDGET_GLOBAL}) {{ setTimeout(omcRender, 50); return; }}
        const opts = {opts_json};
        opts.callback = function (token) {{ window.__omcToken = token; __omcSet('done', token); }};
        opts['error-callback'] = function (e) {{ window.__omcError = {self.WIDGET_ERROR_CALLBACK_VALUE}; __omcSet('error', {self.WIDGET_ERROR_CALLBACK_VALUE}); }};
        try {{
            {self._widget_render_body()}
            __omcMarkReady();
        }} catch (e) {{ window.__omcError = String(e); __omcSet('error', e && e.message ? e.message : String(e)); }}
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
                raise self._classify_error(err)
            remaining_ms = int(
                (deadline - asyncio.get_event_loop().time()) * 1000
            )
            if remaining_ms <= 0:
                break
            try:
                # DOM-first predicate: under Camoufox the ``window.__omc*``
                # globals live in the page's main world and are invisible to
                # this isolated-world wait, but ``#omc-result``'s status is on
                # the shared DOM. Keep the window check as a stock-Chromium fast
                # path / fallback.
                await page.wait_for_function(
                    "() => { const el = document.getElementById('"
                    + self.OMC_RESULT_ID
                    + "'); const st = el ? el.getAttribute('data-status') : null;"
                    " return st === 'done' || st === 'error'"
                    " || window.__omcToken || window.__omcError; }",
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
        # Render options are caller-derived; embed them as an escaped JSON
        # literal so a value like ``</script><script>…`` cannot break out of
        # the init script.
        opts_json = _js_literal(options)
        g = self.WIDGET_GLOBAL
        bridge = self._omc_bridge_js()
        return f"""
(function() {{
    window.__omcToken = null;
    window.__omcError = null;
    window.__omcExecuted = false;
    {bridge}
    const __omcRenderOptions = {opts_json};
    function omcHook() {{
        if (window.{g} && window.{g}.render) {{
            const origRender = window.{g}.render.bind(window.{g});
            window.{g}.render = function(container, opts) {{
                opts = Object.assign({{}}, opts || {{}}, __omcRenderOptions);
                const origCb = opts.callback;
                opts.callback = function(token) {{
                    window.__omcToken = token;
                    __omcSet('done', token);
                    if (origCb) origCb(token);
                }};
                const origErr = opts['error-callback'];
                opts['error-callback'] = function(e) {{
                    window.__omcError = String(e);
                    __omcSet('error', String(e));
                    if (origErr) origErr(e);
                }};
                const wid = origRender(container, opts);
                window.__omcWidgetId = wid;
                // Expose an explicit execute trigger so a solver can drive an
                // invisible widget AFTER seeding behaviour (motionData) instead
                // of firing the passive request with an empty motion buffer.
                // Guarded (``__omcExecuted``) so it can't double-trigger.
                window.__omcExecute = function () {{
                    if (window.__omcExecuted) return;
                    window.__omcExecuted = true;
                    try {{ window.{g}.execute(wid); }} catch (e) {{ window.__omcError = String(e); __omcSet('error', e && e.message ? e.message : String(e)); }}
                }};
                // Bridge a solver→page execute signal through the shared DOM so
                // a Camoufox isolated-world evaluate can trigger the deferred
                // execute() (it can't call the main-world function directly).
                __omcInstallExecBridge();
                // Auto-fire invisible unless the solver deferred it
                // (``__omcDeferExecute``). hCaptcha defers; Turnstile keeps the
                // immediate fire so its behaviour is unchanged.
                if (opts.size === 'invisible' && !window.__omcDeferExecute
                        && window.{g} && typeof window.{g}.execute === 'function') {{
                    window.__omcExecute();
                }}
                return wid;
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
        handler = self._document_route_handler(html)
        defer_execute = (
            self.DEFER_INVISIBLE_EXECUTE
            and render_options.get("size") == "invisible"
        )

        async def prepare(context: Any) -> None:
            # Remember the shared context's current interception state. A warm
            # session may have been prepared by a real-page solve with blocking
            # OFF; cleanup must put that state back for whichever solve gets
            # the context next.
            try:
                context._omc_prior_block_resources = getattr(
                    context, "_omc_block_resources", None
                )
            except Exception:  # noqa: BLE001 - fake contexts may reject attrs
                pass
            # Flag the deferral BEFORE the page's inline render script runs
            # (init scripts execute before any page script) so the render hook
            # sees it and skips the automatic invisible execute().
            if defer_execute:
                await context.add_init_script(self._DEFER_EXECUTE_JS)
            await context.route(website_url, handler)
            # Re-assert blocking ON — LAST, after the per-solve route is
            # installed, so a partial prepare can never flip the shared
            # context's state without cleanup running.
            set_context_resource_blocking(context, True)

        async def cleanup(context: Any) -> None:
            # Restore the blocking state prepare found. Leaving interception
            # ON would make every later real-page solve on this pooled context
            # abort its CSS/fonts/images — a strong bot tell.
            prior = getattr(context, "_omc_prior_block_resources", None)
            if prior is not None:
                set_context_resource_blocking(context, prior)
            try:
                context._omc_prior_block_resources = None
            except Exception:  # noqa: BLE001 - bookkeeping only
                pass
            # A warm pooled context outlives the solve: an un-routed per-solve
            # document handler would keep serving THIS task's HTML (and
            # sitekey) to the next solve that navigates the same URL.
            unroute = getattr(context, "unroute", None)
            if unroute is None:
                return
            try:
                await unroute(website_url, handler)
            except Exception as exc:  # noqa: BLE001 - cleanup never fails a solve
                log.debug("document route cleanup failed: %s", exc)

        return PageStrategy(
            block_resources=True,
            timeout_message=f"{self.PROVIDER} token not obtained within budget",
            prepare_context=prepare,
            key="injected",
            cleanup_context=cleanup,
        )

    def _real_page_strategy(self, render_options: dict[str, Any]) -> PageStrategy:
        """Real-page strategy: hook ``<widget>.render`` via an init script.

        Resource interception is OFF — a browser that aborts every CSS/font/
        image on a real target page is one of the easiest anti-bot signals.
        """

        defer_execute = (
            self.DEFER_INVISIBLE_EXECUTE
            and render_options.get("size") == "invisible"
        )

        async def prepare(context: Any) -> None:
            set_context_resource_blocking(context, False)
            if defer_execute:
                await context.add_init_script(self._DEFER_EXECUTE_JS)
            await context.add_init_script(
                self._build_real_page_init_script(render_options)
            )

        # Playwright exposes no ``remove_init_script``; the per-task signature
        # check in ``_run_page_solve`` is what keeps a pooled context from
        # applying stale render options (a differing URL/options forces a
        # fresh-context acquire). Same-signature reuse is harmless (identical
        # options are merged), so no cleanup callback is needed here.
        return PageStrategy(
            block_resources=False,
            timeout_message=f"{self.PROVIDER} token not obtained (real page mode)",
            prepare_context=prepare,
            key="real_page",
        )

    # ── shared solve template ──────────────────────────────────

    def _guard(self, solve_context: Any, params: dict[str, Any]) -> None:
        """Optional pre-goto guard; default no-op.

        Overridden by hCaptcha to refuse an enterprise solve that would run on
        a bare server-egress (proxyless) context.
        """
        return

    async def _restore_device(self, context: Any, params: dict[str, Any]) -> None:
        """Optional pre-goto device-state restore; default no-op.

        Overridden by hCaptcha to re-seed a provider device-trust cookie (the
        ``hmt`` accessibility cookie) into a fresh context so enterprise risk
        models see a returning device instead of a zero-history one.
        """
        return

    async def _persist_device(
        self, context: Any, params: dict[str, Any], solved: bool
    ) -> None:
        """Optional post-solve device-state persist; default no-op."""
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

        Warm pooled contexts are treated as reusable only for the SAME task:
        ``prepare_context`` installs per-solve routes/init scripts on the
        context, which outlive the solve. A context whose recorded
        URL/options/render signature differs from this task is handed back
        untouched and a fresh context is acquired instead, so a later solve can
        never inherit another sitekey's injected page or render options.
        """
        # SSRF guard at the navigation point, shared by every injected/real-page
        # provider (hCaptcha + Turnstile). Runs BEFORE a proxy or browser
        # context is checked out. A rejected URL is a non-retryable request
        # fault: retrying the same malformed/internal target would only burn
        # the retry budget and egrees, so it raises the classified error the
        # task manager maps to ERROR_CAPTCHA_UNSOLVABLE (never a 500).
        # ``ssrf_protect=False`` disables the pre-flight entirely.
        if getattr(self._config, "ssrf_protect", True):
            try:
                website_url = await _validate_public_http_url(
                    website_url,
                    field="websiteURL",
                    # Opt-in fail-closed DNS (default keeps the offline-test
                    # fail-open policy documented on the helper).
                    strict=bool(getattr(self._config, "ssrf_strict", False)),
                )
            except ValueError as exc:
                raise NonRetryableWidgetError(str(exc)) from exc

        signature = self._render_signature(strategy, website_url, render_options)
        solve_context = await self._acquire_context(params)
        stored = getattr(solve_context.context, "_omc_render_signature", None)
        if (
            solve_context.session is not None
            and stored is not None
            and stored != signature
        ):
            log.info(
                "%s warm session %s was prepared for a different task "
                "(url/options mismatch); requesting a fresh context",
                self.PROVIDER,
                solve_context.session_id,
            )
            await self._return_warm_session(solve_context)
            # Drop the discarded session/proxy identity before re-acquiring so
            # nothing from the rejected context is reported against this solve.
            for key in (
                "_proxy_override",
                "_egress_server",
                "_pool_proxy_id",
                "_pool_geo",
                "_proxy_seed",
                "_pool_proxy_mobile",
                "_sessionId",
            ):
                params.pop(key, None)
            prior_force = params.get("_force_fresh_context")
            params["_force_fresh_context"] = True
            try:
                solve_context = await self._acquire_context(params)
            finally:
                if prior_force is None:
                    params.pop("_force_fresh_context", None)
                else:
                    params["_force_fresh_context"] = prior_force

        self._stash_fingerprint_geo(solve_context, params)
        context = solve_context.context
        user_agent = solve_context.user_agent

        solved = False
        # ``solve_context`` is duck-typed, so ``new_page()`` returns ``Any``;
        # keep the sentinel ``None`` before the page exists.
        page: Any = None
        prepared = False
        # Initialised before the try so the finally's challenge-phase timing is
        # always defined even if context setup / goto raises early.
        _challenge_started = time.perf_counter()
        try:
            params["_phase"] = SolveStage.ACQUIRE.value
            self._guard(solve_context, params)
            await strategy.prepare_context(context)
            prepared = True
            setattr(context, "_omc_render_signature", signature)
            # Re-seed a persisted provider device-trust cookie (hCaptcha ``hmt``)
            # into this (possibly fresh) context so the widget sees a returning
            # device rather than a zero-history one. No-op unless the provider
            # overrides it and the operator opted in.
            await self._restore_device(context, params)
            # ``new_page`` is inside the try: a failure after page creation must
            # still run the finally that closes the page and releases the warm
            # session slot.
            page = await context.new_page()
            params["_phase"] = SolveStage.PAGE_LOAD.value
            timeout_ms = self._config.browser_timeout * 1000
            _page_started = time.perf_counter()
            await page.goto(
                website_url, wait_until="domcontentloaded", timeout=timeout_ms
            )
            params["_phase_page_load_ms"] = int(
                (time.perf_counter() - _page_started) * 1000
            )
            _challenge_started = time.perf_counter()

            token = await self._interact(
                page, website_url, website_key, render_options, params, client_key
            )
            if token:
                solved = True
                return token, user_agent

            raise RuntimeError(strategy.timeout_message)
        finally:
            params["_phase_challenge_ms"] = int(
                (time.perf_counter() - _challenge_started) * 1000
            )
            try:
                # Capture the provider device-trust cookie BEFORE the context is
                # released/closed so the next solve on this egress can present a
                # returning device. Guarded + no-op unless opted in.
                try:
                    await self._persist_device(context, params, solved)
                finally:
                    # Close the per-solve page on BOTH paths — a warm session's
                    # context outlives the solve, so an unclosed page would
                    # accumulate (and its listeners/route stay live) until the
                    # session retires.
                    if page is not None:
                        closer = getattr(page, "close", None)
                        if closer is not None:
                            try:
                                await closer()
                            except asyncio.CancelledError:
                                raise
                            except Exception as exc:  # noqa: BLE001
                                log.debug("page close failed: %s", exc)
            finally:
                try:
                    # Undo this solve's context-level route (warm session) so it
                    # cannot fulfil the next task's document with stale HTML.
                    if prepared and strategy.cleanup_context is not None:
                        await strategy.cleanup_context(context)
                finally:
                    await self._release_context(solve_context, solved, params)

    def _render_signature(
        self, strategy: PageStrategy, website_url: str, render_options: dict[str, Any]
    ) -> str:
        """Identity of the per-solve page preparation (provider + URL + options).

        The provider is part of the key: a warm context prepared by a
        different widget solver already carries that provider's init script,
        so a same-URL/same-options signature without the provider would let
        the other provider reuse it (cross-provider init-script
        contamination).
        """
        try:
            options_key = json.dumps(
                render_options, sort_keys=True, default=str
            )
        except (TypeError, ValueError):  # pragma: no cover - defensive
            options_key = repr(render_options)
        return f"{self.PROVIDER}|{strategy.key}|{website_url}|{options_key}"

    async def _return_warm_session(self, solve_context: Any) -> None:
        """Hand a warm session back untouched when its preparation mismatches.

        Best-effort: a pool failure must not abort the solve (the fresh-context
        path can still run); ``released`` is set so the discarded context can
        never be released twice.
        """
        session = solve_context.session
        session_pool = (
            getattr(self._services, "session_pool", None)
            if self._services is not None
            else None
        )
        if session is None or session_pool is None:
            return
        solve_context.released = True
        try:
            await session_pool.release(session, success=True, burned=False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - bookkeeping only
            log.warning(
                "warm session release on mismatch failed for %s: %s",
                solve_context.session_id,
                exc,
            )
