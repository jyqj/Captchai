"""Shared browser-solver helpers for context acquisition and proxy categories."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import random
import socket
import time
from contextvars import ContextVar
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlsplit, urlunsplit

from playwright.async_api import Browser

from ..assets.proxy_pool import proxy_from_params
from ..assets.session_pool import PROXYLESS_KEY
from ..core.config import Config
from ..models.task import AttemptContext
from .browser import BrowserManager
from .captcha_errors import CaptchaError, NonRetryableWidgetError, TokenRejectedError

# Egress helpers live in ``egress.py`` (pure, no Playwright dependency) and are
# re-exported here so existing ``from .browser_solver import egress_from_params``
# imports keep working.
from .egress import (  # noqa: F401
    ProxyKind,
    SolveIdentity,
    egress_from_params,
    fingerprint_geo_from_params,
    has_task_proxy,
    initial_proxy_kind,
    proxy_ip_from_params,
)

log = logging.getLogger(__name__)

#: Legacy ``params`` keys that belong to ONE solve attempt.
#:
#: .. deprecated::
#:    Attempt-scoped state now lives on a per-attempt
#:    :class:`~src.models.task.AttemptContext`; these keys are only its
#:    end-of-attempt mirror for the non-refactored solvers. Kept as a
#:    read-only compatibility alias for code that imported the old constant.
ATTEMPT_SCOPED_KEYS: tuple[str, ...] = tuple(
    AttemptContext.PARAM_KEY_BY_FIELD.values()
)

#: The AttemptContext of the attempt currently executing in this asyncio task.
#:
#: A :class:`~contextvars.ContextVar` (rather than a solver attribute) is what
#: makes the context concurrency-safe: one solver instance serves many tasks,
#: and each task's retry loop owns its own context. The acquire / release /
#: fingerprint helpers read it so their signatures stay unchanged — tests and
#: the hCaptcha / Turnstile solvers call them with the legacy ``params`` only.
_CURRENT_ATTEMPT: ContextVar[Optional[AttemptContext]] = ContextVar(
    "captchai_attempt_context", default=None
)


def current_attempt() -> Optional[AttemptContext]:
    """The attempt context active in the current asyncio task, if any."""
    return _CURRENT_ATTEMPT.get()


def _bind_attempt(params: dict[str, Any], **fields: Any) -> AttemptContext:
    """Write attempt-scoped fields to the live context AND the legacy mirrors.

    With no active context (a direct helper call / the non-refactored solvers)
    a throwaway context is used purely to translate field names to params keys,
    preserving the pre-refactor ``params``-only behaviour exactly.
    """
    attempt = _CURRENT_ATTEMPT.get()
    if attempt is None:
        attempt = AttemptContext()
    return attempt.bind(params, **fields)


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


def _address_is_blocked(ip: "ipaddress.IPv4Address | ipaddress.IPv6Address") -> bool:
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


async def validate_public_http_url(url: str, *, field: str = "websiteURL") -> str:
    """Reject a caller-supplied URL that could reach an internal address.

    Scheme must be ``http``/``https``; the host is resolved off the event loop
    (``asyncio.to_thread``) and the URL is rejected when ANY resolved address
    is loopback / private / link-local / reserved / multicast. Resolution
    happens before navigation, so a hostname that rebinds to an internal IP
    after the check is still out of reach for this solve (Playwright resolves
    on its own, but the pre-flight check refuses the obvious SSRF targets).

    Raises ``ValueError`` with a caller-readable message; the task manager maps
    any solve exception to ``ERROR_CAPTCHA_UNSOLVABLE`` (never a 500).
    """
    if not isinstance(url, str) or not url.strip():
        raise ValueError(f"{field} must be a non-empty http(s) URL")
    target = url.strip()
    parts = urlsplit(target)
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

    try:
        infos = await asyncio.to_thread(
            socket.getaddrinfo, host, port, type=socket.SOCK_STREAM
        )
    except socket.gaierror as exc:
        raise ValueError(
            f"{field} host {host!r} could not be resolved: {exc}"
        ) from exc
    if not infos:
        raise ValueError(f"{field} host {host!r} did not resolve to any address")
    for info in infos:
        address = info[4][0]
        try:
            ip = ipaddress.ip_address(address.split("%", 1)[0])
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


def _literal_ip(url: str) -> "ipaddress.IPv4Address | ipaddress.IPv6Address | None":
    """Parse a URL host that is itself an IP literal, else ``None``.

    ``ipaddress.ip_address`` rejects the legacy IPv4 spellings Chromium
    canonicalises before connecting (decimal ``2130706433``, octal
    ``0177.0.0.1``, hex ``0x7f000001``), so a blocked target could slip past a
    literal check. ``socket.inet_aton`` accepts all of those; use it as a
    second parser. Hostnames that need DNS return ``None`` (they are covered
    by the resolved-address preflight / post-navigation checks).
    """
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return None
    candidate = host.strip()
    if not candidate:
        return None
    candidate = candidate.split("%", 1)[0]
    try:
        return ipaddress.ip_address(candidate)
    except ValueError:
        pass
    try:
        packed = socket.inet_aton(candidate)
    except OSError:
        return None
    try:
        return ipaddress.ip_address(packed)
    except ValueError:
        return None


async def _ssrf_route_guard(route: Any) -> None:
    """Per-request SSRF guard for the page being solved.

    Synchronous and DNS-free: only a host that parses as an IP literal is
    checked (including Chromium's legacy IPv4 spellings via
    :func:`_literal_ip`). A hostname is passed through; its resolved address
    is validated by the preflight and again on the final landed ``page.url``.
    Aborting here refuses a redirect/subrequest straight at an internal
    literal (``http://169.254.169.254/...``) before the browser opens a
    socket.
    """
    blocked = False
    try:
        ip = _literal_ip(route.request.url)
        blocked = ip is not None and _address_is_blocked(ip)
    except Exception:  # noqa: BLE001 - guard must never break navigation
        blocked = False
    if blocked:
        await route.abort()
        return
    # ``fallback`` (not ``continue_``) hands the request to the next
    # registered handler — the context-level resource-blocking route — so
    # this guard does not silently disable it.
    fallback = getattr(route, "fallback", None)
    if fallback is not None:
        await fallback()
    else:
        await route.continue_()


async def install_ssrf_route_guard(page: Any) -> None:
    """Install the per-request literal-IP guard on a solve page.

    No-op for the lightweight fake pages used in tests (no ``route``). The
    residual DNS-rebinding caveat: a hostname whose resolution changes after
    the preflight is not caught by this sync check — the post-navigation
    revalidation of ``page.url`` is the mitigation for the document target.
    """
    route = getattr(page, "route", None)
    if route is None:
        return
    try:
        await route("**/*", _ssrf_route_guard)
    except Exception as exc:  # noqa: BLE001 - landing check still applies
        log.debug("SSRF route guard install failed: %s", exc)


async def validate_landed_page_url(
    page: Any, config: Any, *, field: str = "websiteURL"
) -> None:
    """Re-validate the FINAL URL after ``page.goto`` (redirects resolved).

    The preflight checks the caller's URL string, but navigation follows
    redirects and the browser performs its own DNS resolution. Validating
    ``page.url`` closes the redirect-to-internal-target gap (and re-resolves
    the hostname for a second, independent check). Raises the classified
    non-retryable error the solvers already use for a bad target.

    Residual caveat: a DNS name that resolves public for this check but
    rebinds to an internal address for a *later* request from the same page
    is only partly covered — the per-request route guard catches literal-IP
    hosts, not a rebinding name. A full fix needs Chromium-level DNS pinning.
    """
    if not getattr(config, "ssrf_protect", True):
        return
    landed = getattr(page, "url", None)
    if not isinstance(landed, str) or not landed:
        return
    try:
        await validate_public_http_url(
            landed, field=f"{field} (after redirects)"
        )
    except ValueError as exc:
        raise NonRetryableWidgetError(str(exc)) from exc


def _credentialed_egress_url(asset: Any) -> Optional[str]:
    """Build a ``scheme://user:pass@host:port`` URL from a proxy asset.

    Uses ``playwright_proxy()`` so a sticky-session ``{session}`` placeholder is
    substituted to the SAME exit IP the solve used. Returns ``None`` when the
    asset has no usable server. Only called when the operator opts in via
    ``POOL_EGRESS_EXPOSE_CREDENTIALS`` — a pool proxy's credentials are a server
    secret, but a caller with an IP-bound token needs them to route their
    downstream submit through the identical egress.
    """
    pw = asset.playwright_proxy() if asset is not None else None
    if not pw or not pw.get("server"):
        return None
    parts = urlsplit(pw["server"])
    if not parts.scheme or not parts.netloc:
        return None
    user = pw.get("username")
    password = pw.get("password")
    netloc = parts.netloc
    if user:
        cred = user if not password else f"{user}:{password}"
        netloc = f"{cred}@{parts.netloc}"
    return urlunsplit(
        (parts.scheme, netloc, parts.path, parts.query, parts.fragment)
    )


class SolveStage(str, Enum):
    """The stage a browser solve has reached, tracked on ``params["_phase"]``.

    Makes the previously-implicit staging first-class: an attempt stamps the
    stage it reaches as it progresses (acquire → page load → passive poll →
    interaction → visual challenge), so the retry loop can report *where* a
    solve failed and drive stage-aware escalation — e.g. only escalate the
    expensive vision tier when the failure actually reached the visual
    challenge, rather than burning a cloud-model retry on a page that never
    loaded.
    """

    ACQUIRE = "acquire"
    PAGE_LOAD = "page_load"
    PASSIVE = "passive"
    INTERACTION = "interaction"
    CHALLENGE = "challenge"
    VERIFY = "verify"


#: Phases whose failure is an egress/transport failure (proxy checkout,
#: context creation, navigation). Only these count against proxy health in the
#: pool; a failure in a later phase is a *solve* failure, not the proxy's fault,
#: and must not push a working proxy toward cooldown.
_TRANSPORT_STAGES = frozenset({SolveStage.ACQUIRE.value, SolveStage.PAGE_LOAD.value})


def _proxy_report_stage(
    params: dict[str, Any],
    solved: bool,
    attempt: Optional[AttemptContext] = None,
) -> str:
    """Map an attempt's phase to the proxy pool's ``stage`` reporting axis.

    ``"transport"`` only for proxy/connect failures (acquisition or page load,
    where the egress itself may be at fault); every other failure is reported
    as ``"solve"`` so it is logged but never penalised against the proxy.
    Success is reported as ``"solve"`` too — the pool counts it regardless of
    stage. The live AttemptContext is preferred; ``params["_phase"]`` remains
    the fallback for the non-refactored solvers.
    """
    if solved:
        return "solve"
    # ``params["_phase"]`` is the common transport: the real-page template
    # mirrors every stage transition onto it and the non-refactored solvers
    # (hCaptcha / Turnstile) stamp it directly. The typed attempt is only a
    # fallback for a context advanced without the mirror.
    phase = (
        params.get("_phase")
        or (attempt.phase if attempt is not None else None)
        or SolveStage.ACQUIRE.value
    )
    return "transport" if phase in _TRANSPORT_STAGES else "solve"


@dataclass
class SolveContext:
    context: Any
    user_agent: str
    proxy_kind: ProxyKind
    session: Any | None = None
    proxy_id: str | None = None
    session_id: str | None = None
    #: Set by ``_release_context`` before it touches the context/session, so a
    #: second release (an exception unwinding through two ``finally`` blocks, or
    #: a cancellation caught and re-run) can never double-close a context or
    #: double-release a warm-session slot.
    released: bool = False


class BaseBrowserSolver:
    """Common lifecycle and context handling for Playwright-backed solvers."""

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
        log.info("%s stopped", self.__class__.__name__)

    async def _acquire_context(self, params: dict[str, Any]) -> SolveContext:
        """Acquire a browser context and stamp the final proxy category.

        The egress mode is selected by ``params["egress"]``:

        * ``"auto"`` (default): caller task proxy → server-side pool proxy →
          server egress IP. Pool-proxy solves reuse a warm session bound to the
          sticky proxy; proxyless solves reuse a warm server-IP session.
        * ``"task"``: require a caller-supplied proxy and bind a fresh context
          to it. Raises if no task proxy was provided.
        * ``"pool"``: require a server-side pool proxy (sticky warm session).
          Raises if the pool is empty.
        * ``"proxyless"``: ignore any caller proxy and any pool proxy; use a
          warm session if available, else a fresh server-IP context.
        """

        egress = params.get("egress") or "auto"

        if egress == "task":
            return await self._acquire_task(params)
        if egress == "pool":
            return await self._acquire_pool(params)
        if egress == "proxyless":
            return await self._acquire_proxyless(params)
        # auto: task proxy → pool proxy → proxyless (server egress).
        if has_task_proxy(params):
            return await self._acquire_task(params)
        if self._pool_has_available():
            try:
                return await self._acquire_pool(params)
            except RuntimeError as exc:
                if "egress=pool" not in str(exc):
                    raise
                # Pool emptied between ``has_available`` and ``checkout``:
                # fall through to the proxyless path rather than failing.
        return await self._acquire_proxyless(params)

    def _pool_has_available(self) -> bool:
        """Sync peek: is there a server-side proxy pool with available proxies?"""
        if self._services is None:
            return False
        proxy_pool = getattr(self._services, "proxy_pool", None)
        return proxy_pool is not None and proxy_pool.has_available()

    def _stash_fingerprint_geo(
        self, solve_context: SolveContext, params: dict[str, Any]
    ) -> None:
        """Stash the solve's fingerprint timezone / languages onto ``params``.

        Warm-session solves carry the fingerprint on ``solve_context.session``
        (``BrowserSession.fingerprint``); fresh-context solves already had it
        stashed by ``resolve_context_options``. This helper unifies the two
        paths so the solver's ``solve()`` can read
        ``params["_used_timezone"]`` / ``params["_used_languages"]`` and
        surface them in the solution (``SolutionObject.timezoneId`` /
        ``acceptLanguage``) for callers to align their submit context.
        """
        session = getattr(solve_context, "session", None)
        fp = getattr(session, "fingerprint", None) if session is not None else None
        if fp is not None:
            _bind_attempt(
                params,
                timezone_id=fp.timezone_id,
                languages=list(fp.languages),
                # Touch modality follows the fingerprint: a mobile session
                # drives touch taps / scroll motion instead of mouse events.
                is_mobile=bool(fp.is_mobile),
            )
        # Fresh-context path: resolve_context_options already stashed them.

    async def _acquire_task(self, params: dict[str, Any]) -> SolveContext:
        """Bind a fresh context to the caller-supplied proxy (no session reuse)."""
        if not has_task_proxy(params):
            raise RuntimeError(
                "egress=task requires a caller-supplied proxy "
                "(proxy / proxyAddress+proxyPort fields)"
            )
        _bind_attempt(params, proxy_kind=ProxyKind.TASK_PROXY.value)
        task_asset = proxy_from_params(params)
        if task_asset is not None:
            # Credential-free gateway (scheme://host:port) surfaced back to the
            # caller so they can align their downstream submit egress.
            _bind_attempt(params, egress_server=task_asset.server)
        context, user_agent = await self._manager.new_context(params)
        return SolveContext(
            context=context,
            user_agent=user_agent,
            proxy_kind=ProxyKind.TASK_PROXY,
        )

    async def _acquire_pool(self, params: dict[str, Any]) -> SolveContext:
        """Check out a sticky pool proxy and reuse (or build) a warm session for it.

        Requires a non-empty server-side proxy pool. Raises if no pool proxy is
        available. When a ``SessionPool`` is wired, the pool proxy is paired
        with a warm session bound to that proxy's bucket (so the same sticky
        proxy keeps a coherent fingerprint + cookie jar across solves).
        Otherwise a fresh context is bound to the proxy via
        ``_proxy_override``.

        The proxy's exit-IP geo (``timezone`` / ``locale`` / ``country``) is
        stashed onto ``params`` as ``_pool_geo`` and its ``id`` as
        ``_proxy_seed`` so a fresh-context build (no session pool) can produce
        a fingerprint aligned with the proxy's egress. Warm sessions seed the
        fingerprint themselves inside ``SessionPool.checkout``.
        """
        proxy_pool = (
            getattr(self._services, "proxy_pool", None)
            if self._services is not None
            else None
        )
        if proxy_pool is None:
            raise RuntimeError(
                "egress=pool requires a server-side proxy but the pool is empty"
            )
        # WP5: a caller may require a specific proxy kind (e.g. enterprise
        # hCaptcha forces residential-or-mobile). ``_required_proxy_kinds`` is
        # the preferred form (a list/tuple of accepted kinds); the legacy
        # ``_required_proxy_kind`` (single str) is wrapped into a 1-tuple for
        # uniform handling. ``None`` falls back to any kind.
        required_kinds = params.get("_required_proxy_kinds")
        if required_kinds is None:
            legacy = params.get("_required_proxy_kind")
            required_kinds = (legacy,) if legacy else None
        pool_proxy = await proxy_pool.checkout(
            kind=required_kinds, sitekey=params.get("websiteKey")
        )
        if pool_proxy is None:
            if required_kinds:
                kinds_str = " or ".join(required_kinds)
                raise RuntimeError(
                    f"egress=pool requires a {kinds_str} pool proxy but "
                    "none is available"
                )
            raise RuntimeError(
                "egress=pool requires a server-side proxy but the pool is empty"
            )
        _bind_attempt(
            params,
            proxy_id=pool_proxy.id,
            proxy_kind=ProxyKind.POOL_PROXY.value,
            # Gateway of the checked-out pool proxy surfaced so the caller can
            # route their downstream (IP-bound) submit through the same egress.
            # Default is credential-free (the pool proxy is a server secret);
            # when the operator opts in via POOL_EGRESS_EXPOSE_CREDENTIALS we
            # surface the full credentialed URL (sticky session substituted) so
            # the caller can actually reach the SAME exit IP — otherwise an
            # enterprise/Stripe token minted here is bound to an IP the caller
            # can never reproduce.
            egress_server=pool_proxy.server,
        )
        if getattr(self._config, "pool_egress_expose_credentials", False):
            full = _credentialed_egress_url(pool_proxy)
            if full:
                _bind_attempt(params, egress_server=full)
        # WP-geo: if the proxy has no geo annotation, probe its exit IP once
        # (through the proxy itself) and cache the derived timezone/locale so a
        # German exit IP presents Europe/Berlin + de-DE instead of a random
        # locale. Runs before the fingerprint is built (fresh context) or the
        # warm session is checked out (which seeds its fingerprint from the
        # proxy geo), so both paths pick up the probed geo.
        await self._maybe_probe_geo(proxy_pool, pool_proxy)
        # WP3: thread the proxy's exit-IP geo + a deterministic seed so a
        # fresh-context build (no session pool) produces a fingerprint
        # aligned with the proxy's egress. Warm sessions read these from the
        # proxy directly inside ``SessionPool.checkout``.
        _bind_attempt(
            params,
            pool_geo={
                "timezone": pool_proxy.timezone,
                "locale": pool_proxy.locale,
                "country": pool_proxy.country,
            },
            proxy_seed=pool_proxy.id,
            # P1-4: a mobile pool proxy drives an Android Chrome fingerprint (a
            # carrier IP with a desktop fingerprint is a contradiction). Read by
            # resolve_context_options for the fresh-context path; the warm-session
            # path reads proxy.kind directly in SessionPool.checkout.
            pool_proxy_mobile=pool_proxy.kind == "mobile",
        )

        session_pool = (
            getattr(self._services, "session_pool", None)
            if self._services is not None
            else None
        )
        # Enterprise solves can force a fresh context per solve (bypassing warm
        # session reuse) so a single sticky proxy's cookie jar / fingerprint
        # isn't reused across different sitekeys — reusing one warm session to
        # hammer the same sitekey builds a suspicious pattern enterprise risk
        # models cluster on. ``_force_fresh_context`` is set by the hCaptcha
        # solver for enterprise variants when ``ENTERPRISE_FRESH_CONTEXT`` is on.
        if session_pool is not None and not params.get("_force_fresh_context"):
            session = await session_pool.checkout(
                key=pool_proxy.id, proxy=pool_proxy, sitekey=params.get("websiteKey")
            )
            _bind_attempt(params, session_id=session.id)
            return SolveContext(
                context=session.context,
                user_agent=session.user_agent,
                proxy_kind=ProxyKind.POOL_PROXY,
                session=session,
                proxy_id=pool_proxy.id,
                session_id=session.id,
            )

        # No session pool: fall back to a fresh context bound to the proxy.
        pw_proxy = pool_proxy.playwright_proxy()
        if pw_proxy:
            _bind_attempt(params, proxy_override=pw_proxy)
        context, user_agent = await self._manager.new_context(params)
        return SolveContext(
            context=context,
            user_agent=user_agent,
            proxy_kind=ProxyKind.POOL_PROXY,
            proxy_id=pool_proxy.id,
        )

    async def _maybe_probe_geo(self, proxy_pool: Any, pool_proxy: Any) -> None:
        """Probe + cache a pool proxy's exit-IP geo when it has none.

        Best-effort and one-shot per proxy (guarded by ``geo_probed``). A
        resolved country derives timezone/locale via the shared table and is
        persisted through ``proxy_pool.set_geo`` so later checkouts (including
        on other workers via Redis) skip the probe. Any failure is swallowed:
        the proxy keeps unset geo and the fingerprint falls back to a random
        coherent identity (pre-existing behaviour).
        """
        if not getattr(self._config, "proxy_geo_probe", False):
            return
        if getattr(pool_proxy, "country", None) or getattr(
            pool_proxy, "geo_probed", False
        ):
            return
        set_geo = getattr(proxy_pool, "set_geo", None)
        if set_geo is None:
            return
        try:
            from ..assets.geo_probe import probe_proxy_geo

            url = getattr(
                self._config, "proxy_geo_probe_url", "http://ip-api.com/json"
            )
            await probe_proxy_geo(pool_proxy, url=url)
            await set_geo(
                pool_proxy.id,
                country=pool_proxy.country,
                timezone=pool_proxy.timezone,
                locale=pool_proxy.locale,
                geo_probed=True,
            )
        except Exception as exc:  # noqa: BLE001 - probe must never fail a solve
            log.debug("proxy geo probe failed for %s: %s", pool_proxy.id, exc)

    async def _acquire_proxyless(
        self, params: dict[str, Any]
    ) -> SolveContext:
        """Use a warm server-IP session if available, else a fresh context."""
        _bind_attempt(params, proxy_kind=ProxyKind.PROXYLESS.value)
        if self._services is not None:
            session_pool = getattr(self._services, "session_pool", None)
            # ``_force_fresh_context`` (enterprise device hygiene, or the
            # injected-widget stale-options guard) bypasses warm-session reuse.
            if session_pool is not None and not params.get("_force_fresh_context"):
                session = await session_pool.checkout(
                    key=PROXYLESS_KEY,
                    proxy=None,
                    sitekey=params.get("websiteKey"),
                )
                _bind_attempt(params, session_id=session.id)
                return SolveContext(
                    context=session.context,
                    user_agent=session.user_agent,
                    proxy_kind=ProxyKind.PROXYLESS,
                    session=session,
                    session_id=session.id,
                )
        context, user_agent = await self._manager.new_context(params)
        return SolveContext(
            context=context,
            user_agent=user_agent,
            proxy_kind=ProxyKind.PROXYLESS,
        )

    async def _release_context(
        self, solve_context: SolveContext, solved: bool, params: dict[str, Any]
    ) -> None:
        """Return or close the browser context and report proxy-pool health.

        For warm sessions (pool or proxyless) the context outlives the solve:
        release it back to the session pool, which handles retirement based on
        reputation / max_solves. Per-solve byte attribution is skipped — a
        warm session accumulates bytes across solves that aren't meaningfully
        attributable to a single task. For pool sessions, the pool proxy is
        reported per solve so the proxy-pool health tracker stays current.

        For fresh contexts (task_proxy or proxyless/pool fallback when no
        session pool is wired) the per-context byte counter (set by
        BrowserManager's response listener) is read out before close so the
        proxy pool and ledger can attribute bandwidth to this solve. For
        task_proxy there is no pool proxy to report; for the pool-fallback
        fresh path the pool proxy is reported with the bytes.

        Bookkeeping is strictly best-effort: this runs from the solver's
        ``finally``, so a failing session/proxy backend must never mask the
        token (or the real solve error) being propagated. ``SolveContext.released``
        is flipped before any await so an unwinding caller can't close a
        context or release a session slot twice.
        """
        if solve_context.released:
            log.debug("solve context already released; skipping duplicate release")
            return
        solve_context.released = True
        attempt = current_attempt()

        bytes_used = 0
        if solve_context.session is None:
            # Fresh context (task_proxy / pool-fallback / proxyless-fallback):
            # read the accumulated byte count before closing.
            bytes_used = int(getattr(solve_context.context, "_omc_bytes_used", 0))
            _bind_attempt(params, proxy_bytes=bytes_used)
            try:
                await solve_context.context.close()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - close must never raise
                log.debug("browser context close failed: %s", exc)
        else:
            # Warm session (pool or proxyless): release back to the session
            # pool; per-solve byte attribution is skipped (the context outlives
            # this solve). Pool-proxy health is still reported per solve below.
            session_pool = (
                getattr(self._services, "session_pool", None)
                if self._services is not None
                else None
            )
            if session_pool is not None:
                try:
                    await session_pool.release(
                        solve_context.session, success=solved, burned=not solved
                    )
                except asyncio.CancelledError:
                    # The released flag above means the caller's finally cannot
                    # release it a second time; the pool owns cancellation
                    # cleanup for a checkout/release it accepted.
                    raise
                except Exception as exc:  # noqa: BLE001 - bookkeeping only
                    log.warning(
                        "session pool release failed for %s: %s",
                        solve_context.session_id,
                        exc,
                    )

        proxy_id = solve_context.proxy_id or params.pop("_pool_proxy_id", None)
        if proxy_id and self._services is not None:
            proxy_pool = getattr(self._services, "proxy_pool", None)
            if proxy_pool is not None:
                try:
                    await proxy_pool.report(
                        proxy_id,
                        success=solved,
                        bytes_used=bytes_used,
                        stage=_proxy_report_stage(params, solved, attempt),
                    )
                    if solved:
                        # Lets the real-outcome closure (siteverify) skip a
                        # second proxy-success report for the same attempt.
                        _bind_attempt(params, proxy_reported=True)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - bookkeeping only
                    log.warning("proxy pool report failed for %s: %s", proxy_id, exc)
                sitekey = params.get("websiteKey")
                if sitekey:
                    try:
                        await proxy_pool.report_sitekey(
                            proxy_id, sitekey, success=solved
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001 - bookkeeping only
                        log.warning(
                            "proxy sitekey report failed for %s: %s", proxy_id, exc
                        )

    # ── real-page solve template (reCAPTCHA v2 / v3) ───────────

    #: Human-readable provider name used in log lines and token errors.
    PROVIDER_LABEL = "browser"

    def _invalid_token_message(self, token: Any) -> str:
        """Message for a structurally invalid token (provider overridable)."""
        return f"Invalid {self.PROVIDER_LABEL} token: {token!r}"

    def _stamp_phase(self, params: dict[str, Any], stage: SolveStage) -> None:
        """Advance the current attempt's stage tracker (typed + mirror)."""
        _bind_attempt(params, phase=stage.value)

    async def _prepare_page(
        self, page: Any, params: dict[str, Any], attempt: AttemptContext
    ) -> None:
        """Hook: provider-specific preparation between page load and extraction.

        Runs after the document has loaded and the landed URL has been
        validated, before :meth:`_extract_token`. Default is a no-op.
        """

    async def _extract_token(
        self, page: Any, params: dict[str, Any], attempt: AttemptContext
    ) -> str | None:
        """Hook: provider-specific token extraction from the loaded page."""
        return None

    async def _on_provider_error(
        self,
        page: Any,
        params: dict[str, Any],
        attempt: AttemptContext,
        exc: Exception,
    ) -> None:
        """Hook: lift a generic failure into a classified provider error.

        Called only for unexpected (non-``CaptchaError``) exceptions raised by
        :meth:`_prepare_page` / :meth:`_extract_token`. Returning leaves the
        original exception in place; raising a ``CaptchaError`` replaces it.
        """

    async def _solve_real_page_once(
        self,
        params: dict[str, Any],
        website_url: str,
    ) -> tuple[str, str]:
        """Template method: one real-page solve attempt.

        Shared choreography for providers that navigate the target page:

        SSRF preflight before any egress is leased -> acquire context ->
        ``new_page`` INSIDE the try (so a failure after page creation still
        releases the warm-session slot / closes the context) -> per-request
        route guard -> stage-timed navigation -> landed-URL revalidation ->
        provider hooks -> token validation -> ``finally``: page close,
        best-effort context/session/proxy bookkeeping, and the
        :class:`AttemptContext` -> ``params`` mirror.

        Provider differences live in the hooks: :meth:`_prepare_page`
        (pre-token interaction), :meth:`_extract_token` (checkbox/audio vs
        ``execute``) and :meth:`_on_provider_error` (site-owner faults).
        """
        attempt = current_attempt()
        owned_token = None
        if attempt is None:
            # Direct call outside the retry loop (one-shot use / tests): seed
            # from the legacy params keys and own the context for the duration.
            attempt = AttemptContext.from_params(params)
            owned_token = _CURRENT_ATTEMPT.set(attempt)
        try:
            # Refuse a non-http(s) or internal-address target BEFORE a
            # proxy/context is checked out. Classified non-retryable so a
            # malformed target does not burn the retry budget. Honours the
            # global SSRF_PROTECT kill-switch.
            if getattr(self._config, "ssrf_protect", True):
                try:
                    website_url = await validate_public_http_url(
                        website_url, field="websiteURL"
                    )
                except ValueError as exc:
                    raise NonRetryableWidgetError(str(exc)) from exc

            solve_context = await self._acquire_context(params)
            self._stash_fingerprint_geo(solve_context, params)
            context = solve_context.context
            user_agent = solve_context.user_agent
            # Once the context is checked out, later failures are solve
            # failures, not proxy/transport ones — the proxy pool reads the
            # phase in ``_release_context`` to attribute health correctly.
            self._stamp_phase(params, SolveStage.PAGE_LOAD)

            solved = False
            page = None
            page_started = time.perf_counter()
            challenge_started = page_started
            try:
                page = await context.new_page()
                # The preflight validated the caller's URL string, but goto
                # follows redirects and the browser resolves DNS independently.
                # Install a per-request literal-IP guard and re-validate the
                # FINAL landed URL, failing non-retryably on a blocked target.
                # Residual DNS-rebinding caveat: see validate_landed_page_url.
                if getattr(self._config, "ssrf_protect", True):
                    await install_ssrf_route_guard(page)
                timeout_ms = self._config.browser_timeout * 1000
                await page.goto(
                    website_url, wait_until="domcontentloaded", timeout=timeout_ms
                )
                attempt.phase_page_load_ms = int(
                    (time.perf_counter() - page_started) * 1000
                )
                await validate_landed_page_url(page, self._config)

                challenge_started = time.perf_counter()
                self._stamp_phase(params, SolveStage.INTERACTION)
                try:
                    await self._prepare_page(page, params, attempt)
                    token = await self._extract_token(page, params, attempt)
                except CaptchaError:
                    # Already classified (rate limit / invalid sitekey / …):
                    # let the retry loop's taxonomy see it unchanged.
                    raise
                except Exception as exc:
                    # One chance to replace a generic failure with the
                    # provider's classified error (e.g. Google's anchor error
                    # slot). A plain return keeps the original exception.
                    await self._on_provider_error(page, params, attempt, exc)
                    raise

                if not isinstance(token, str) or len(token) < 20:
                    raise RuntimeError(self._invalid_token_message(token))
                log.info("Got %s token (len=%d)", self.PROVIDER_LABEL, len(token))
                solved = True
                return token, user_agent
            finally:
                attempt.phase_challenge_ms = int(
                    (time.perf_counter() - challenge_started) * 1000
                )
                # Close the per-solve page on BOTH paths (fresh context and
                # warm session): a pooled context outlives the solve, so an
                # unclosed page would accumulate across solves.
                try:
                    closer = (
                        getattr(page, "close", None) if page is not None else None
                    )
                    if closer is not None:
                        try:
                            await closer()
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:  # noqa: BLE001 - close never fails a solve
                            log.debug(
                                "%s page close failed: %s",
                                self.PROVIDER_LABEL,
                                exc,
                            )
                finally:
                    try:
                        await self._release_context(solve_context, solved, params)
                    finally:
                        # Attempt END: mirror the typed context onto the legacy
                        # params keys exactly as the pre-refactor code left them,
                        # so the shared recorder / verify closure / solution echo
                        # (and the other solvers' params contract) keep working.
                        attempt.sync_to_params(params)
        finally:
            if owned_token is not None:
                _CURRENT_ATTEMPT.reset(owned_token)

    async def _solve_with_retries(
        self,
        params: dict[str, Any],
        *,
        sitekey: str,
        client_key: Optional[str],
        attempt_fn: "Callable[[], Awaitable[tuple[str, str]]]",
        build_solution: "Callable[[str, str], dict[str, Any]]",
        provider: str,
        default_task_type: str,
        default_challenge_shape: str = "widget",
        on_error: "Optional[Callable[[int, Exception, str], None]]" = None,
        verify_provider: Optional[str] = None,
    ) -> dict[str, Any]:
        """Shared attempt loop for every browser solver (WP-pipeline).

        Collapses the four near-identical retry loops (hCaptcha / Turnstile /
        reCAPTCHA v2 / v3) into one template: per-attempt timing + record,
        classified-error fast-fail (a non-retryable :class:`CaptchaError` such
        as a rate limit raises immediately instead of hammering the egress),
        exponential backoff with jitter between retryable attempts, and
        solution assembly (token + UA + fingerprint geo + egress echo). The
        provider-specific bits are injected:

        * ``attempt_fn`` runs one solve attempt → ``(token, user_agent)``.
        * ``build_solution`` maps ``(token, user_agent)`` to the provider's
          solution dict (e.g. ``gRecaptchaResponse`` vs ``token``).
        * ``on_error`` is an optional hook (attempt index, exc, reached stage)
          for per-provider stage-aware escalation (hCaptcha bumps its vision
          tier only when the failure reached the visual challenge).
        """
        last_error: Exception | None = None
        retries = int(self._config.captcha_retries)
        for attempt in range(retries):
            # A FRESH AttemptContext per attempt is the clean slate: attempt 2
            # can never report attempt 1's proxy bytes / model spend, echo a
            # released egress, or inherit stale timings, because nothing
            # attempt-scoped is carried over. ``reset_params`` installs the
            # fresh defaults as the legacy params mirror (hCaptcha / Turnstile
            # still read/write it), replacing the old delete-these-keys pass.
            attempt_ctx = AttemptContext(attempt=attempt)
            attempt_ctx.reset_params(params)
            # Stamp the stage tracker for this attempt; the attempt advances
            # ``phase`` as it progresses so a failure knows where it stopped
            # (see SolveStage / stage-aware on_error below).
            attempt_ctx.phase = SolveStage.ACQUIRE.value
            params["_phase"] = SolveStage.ACQUIRE.value
            attempt_token = _CURRENT_ATTEMPT.set(attempt_ctx)
            started = time.perf_counter()
            try:
                token, user_agent = await attempt_fn()
                await self._record(
                    params,
                    sitekey,
                    client_key,
                    "ready",
                    started,
                    task_type=default_task_type,
                    challenge_shape=default_challenge_shape,
                )
                # WP6 token-trust closure: when siteverify is configured for
                # this sitekey, verify the token now and feed the verdict into
                # the real-outcome accounting (same buckets /reportIncorrect
                # writes). A definitive rejection retries on a fresh egress
                # rather than returning a token the provider already refused.
                verdict = await self._verify_and_close(
                    params, sitekey, client_key, token, verify_provider
                )
                if verdict is False:
                    last_error = TokenRejectedError(
                        f"{provider} token rejected by siteverify"
                    )
                    log.warning(
                        "%s attempt %d/%d: token rejected by siteverify",
                        provider,
                        attempt + 1,
                        retries,
                    )
                    if attempt < retries - 1:
                        await self._sleep_backoff(attempt)
                    continue
                # The solve's resolved identity (egress + fingerprint geo) as one
                # immutable object rather than four loose params reads. Surfaced
                # in the solution so the caller can align their downstream submit
                # egress + browser context with the context that minted the token.
                identity = SolveIdentity.from_params(params)
                solution = build_solution(token, user_agent)
                solution.setdefault("userAgent", user_agent)
                solution.update(identity.solution_fields())
                return solution
            except CaptchaError as exc:
                last_error = exc
                await self._record(
                    params,
                    sitekey,
                    client_key,
                    exc.outcome,
                    started,
                    task_type=default_task_type,
                    challenge_shape=default_challenge_shape,
                )
                stage = params.get("_phase", SolveStage.ACQUIRE.value)
                log.warning(
                    "%s attempt %d/%d: %s (retryable=%s, stage=%s)",
                    provider,
                    attempt + 1,
                    retries,
                    exc,
                    exc.retryable,
                    stage,
                )
                if not exc.retryable:
                    raise
                if on_error is not None:
                    on_error(attempt, exc, stage)
                if attempt < retries - 1:
                    await self._sleep_backoff(attempt)
            except Exception as exc:
                last_error = exc
                await self._record(
                    params,
                    sitekey,
                    client_key,
                    "failed",
                    started,
                    task_type=default_task_type,
                    challenge_shape=default_challenge_shape,
                )
                stage = params.get("_phase", SolveStage.ACQUIRE.value)
                log.warning(
                    "%s attempt %d/%d failed at stage=%s: %s",
                    provider,
                    attempt + 1,
                    retries,
                    stage,
                    exc,
                )
                if on_error is not None:
                    on_error(attempt, exc, stage)
                if attempt < retries - 1:
                    await self._sleep_backoff(attempt)
            finally:
                # The attempt is over on every path (return / continue /
                # classified raise / unexpected raise); detach its context so
                # nothing can read it once the next attempt starts.
                _CURRENT_ATTEMPT.reset(attempt_token)

        raise RuntimeError(
            f"{provider} failed after {retries} attempts: {last_error}"
        )

    async def _sleep_backoff(self, attempt: int) -> None:
        """Exponential backoff with jitter between retryable attempts.

        Replaces the fixed 2s sleep: ``base * 2**attempt`` capped at
        ``retry_backoff_max``, plus up to 25% jitter so concurrent solves that
        all fail at once don't retry in lockstep (thundering herd). Rate-limit
        errors are non-retryable and never reach here — they fail fast.
        """
        base = float(getattr(self._config, "retry_backoff_base", 1.0))
        cap = float(getattr(self._config, "retry_backoff_max", 8.0))
        delay = min(cap, base * (2 ** attempt))
        delay += random.uniform(0.0, delay * 0.25)
        await asyncio.sleep(delay)

    def _backend_kind(self, model: str | None) -> Optional[str]:
        """Best-effort backend kind (``"local"`` / ``"cloud"``) for a served id.

        The vision router reports the concrete served model id, not the
        backend, so infer it: an explicit alias identifies itself, and a
        concrete id equal to a configured backend model identifies that
        backend. Returns ``None`` when the id can't be attributed (pricing then
        falls back to its name heuristics).
        """
        if not model:
            return None
        name = str(model)
        if name in ("local", "cloud"):
            return name
        for kind, attr in (("local", "local_model"), ("cloud", "cloud_model")):
            configured = getattr(self._config, attr, None)
            if configured and name == str(configured):
                return kind
        return None

    async def _record(
        self,
        params: dict[str, Any],
        sitekey: str,
        client_key: Optional[str],
        outcome: str,
        started: float,
        *,
        task_type: str | None = None,
        challenge_shape: str = "widget",
    ) -> None:
        """Append a SolveRecord to the shared ledger and update accounting.

        Reads vision stats from params["_vision"] when present (hCaptcha/Turnstile
        set it after challenge dispatch) and phase timings stashed on params
        (``_phase_page_load_ms`` / ``_phase_challenge_ms``) so the ledger can
        break ``wall_ms`` down into page-load / challenge / vision time. Metering
        failures are swallowed so they never fail a solve.
        """
        if self._services is None:
            return
        from ..consumption.ledger import SolveRecord, estimate_cost

        vision = params.get("_vision")
        # Concrete served model id when the vision plane reports one
        # (``ModelCallResult.model``); older/audio paths may still stamp the
        # backend alias. Either way it is the id pricing should key on, so an
        # alias is resolved against the wired pool when possible.
        model = getattr(vision, "last_model", None)
        actual_model = model
        pool = getattr(self._services, "model_pool", None)
        if pool is not None and model in ("local", "cloud"):
            client = getattr(pool, model, None)
            actual_model = getattr(client, "model", None) or model
        # Backend kind for pricing. The router reports the concrete served id,
        # not the backend, so infer it: an alias is explicit, and a concrete id
        # equal to a configured backend model identifies that backend. Pricing
        # treats a local backend as free even when its id has no local marker.
        backend_kind = self._backend_kind(model)
        in_tok = getattr(vision, "total_input_tokens", 0) or 0
        out_tok = getattr(vision, "total_output_tokens", 0) or 0
        calls = getattr(vision, "total_vision_calls", 0) or 0
        vision_ms = int(getattr(vision, "total_vision_ms", 0) or 0)
        cost = estimate_cost(
            model or "local",
            in_tok,
            out_tok,
            actual_model=actual_model,
            backend=backend_kind,
        )
        # One typed read of the solve's egress/session identity instead of
        # three separate ``params.get("_...")`` lookups spread across the record.
        identity = SolveIdentity.from_params(params)

        try:
            rec = SolveRecord(
                task_id=str(params.get("_taskId") or ""),
                sitekey=sitekey,
                task_type=task_type or params.get("type", "unknown"),
                proxy_id=identity.proxy_id,
                session_id=identity.session_id,
                proxy_kind=identity.proxy_kind,
                model=model,
                actual_model=actual_model,
                # hCaptcha stashes the detected shape; fall back to the
                # provider default (e.g. "widget" / "audio").
                challenge_shape=params.get("_challenge_shape") or challenge_shape,
                vision_calls=calls,
                input_tokens=in_tok,
                output_tokens=out_tok,
                proxy_bytes=int(params.get("_proxy_bytes", 0)),
                wall_ms=int((time.perf_counter() - started) * 1000),
                page_load_ms=int(params.get("_phase_page_load_ms", 0) or 0),
                challenge_ms=int(params.get("_phase_challenge_ms", 0) or 0),
                vision_ms=vision_ms,
                outcome=outcome,
                est_cost_usd=cost,
                client_key=client_key,
            )
            try:
                await self._services.ledger.record(
                    rec, actual_model=actual_model, backend=backend_kind
                )
            except TypeError:
                # Duck-typed ledgers without the Wave-A kwargs: the concrete id
                # still rides on the record's `actual_model` field.
                await self._services.ledger.record(rec)
            await self._services.accounting.record(
                sitekey,
                outcome,
                proxy_kind=identity.proxy_kind,
                model=model,
            )
        except Exception as exc:
            log.debug("ledger record failed: %s", exc)

    async def _verify_and_close(
        self,
        params: dict[str, Any],
        sitekey: str,
        client_key: Optional[str],
        token: str,
        provider_key: Optional[str],
    ) -> Optional[bool]:
        """Verify the token via siteverify and close the real-outcome loop.

        Returns the tri-state verdict: ``True`` accepted, ``False`` rejected,
        ``None`` unknown (no verifier wired, no secret for the sitekey, or a
        transient verify error). A definitive verdict is fed into the same
        real-outcome path ``/reportCorrect`` uses so proxy health / routing
        learn from ground truth automatically. ``None`` leaves the loop
        caller-driven (pre-WP6 behaviour).
        """
        if self._services is None or not provider_key:
            return None
        verifier = getattr(self._services, "token_verifier", None)
        if verifier is None:
            return None
        try:
            verdict = await verifier.verify(
                token,
                provider=provider_key,
                sitekey=sitekey,
                remote_ip=self._proxy_ip(params),
            )
        except Exception as exc:  # noqa: BLE001 - verify never fails a solve
            log.debug("token verification raised: %s", exc)
            return None
        if verdict is None:
            return None
        await self._close_real_outcome(params, sitekey, success=verdict)
        return verdict

    async def _close_real_outcome(
        self, params: dict[str, Any], sitekey: str, *, success: bool
    ) -> None:
        """Feed a real (siteverify) outcome into accounting + proxy + session.

        Delegates to the shared :func:`~src.services.outcome.record_real_outcome`
        module — the same fan-out ``/reportCorrect`` // ``/reportIncorrect`` uses
        — driven from the live solve's :class:`SolveIdentity` instead of a stored
        ledger record. Every downstream call is guarded there so verification can
        never fail a solve.
        """
        if self._services is None:
            return
        from .outcome import record_real_outcome

        identity = SolveIdentity.from_params(params)
        model = getattr(params.get("_vision"), "last_model", None)
        if success and params.get("_proxy_reported"):
            # This attempt's ``_release_context`` already filed a proxy
            # SUCCESS (token obtained). Feeding a second success through the
            # real-outcome fan-out would double-count ``success_count`` and
            # reset the fail streak twice for one solve. Keep the real
            # per-sitekey bucket — it is a distinct signal from the solver's
            # token-obtained bucket — but drop the duplicate proxy-health sink.
            proxy_pool = getattr(self._services, "proxy_pool", None)
            if identity.proxy_id and proxy_pool is not None and sitekey:
                try:
                    await proxy_pool.report_sitekey_real(
                        identity.proxy_id, sitekey, success=True
                    )
                except Exception as exc:  # noqa: BLE001 - feedback never fails a solve
                    log.debug("proxy real-sitekey report failed: %s", exc)
            identity = replace(identity, proxy_id=None)
        await record_real_outcome(
            self._services, identity, sitekey, success=success, model=model
        )

    def _proxy_ip(self, params: dict[str, Any]) -> Optional[str]:
        return proxy_ip_from_params(params)

    async def _human_click_in_frame(
        self,
        page: Any,
        frame_locator: Any,
        selector: str,
        *,
        timeout_ms: int = 10_000,
        touch: bool = False,
    ) -> None:
        """Click an element inside an iframe with a human-like pointer path.

        hCaptcha / Turnstile collect ``motionData`` from the moment the widget
        loads, and the checkbox click dynamics are scored too — a raw
        ``locator.click()`` teleports the pointer with zero travel/dwell, one of
        the strongest automation tells. This reuses :func:`human_click` (the
        same eased/jittered path the tile clicks already use): a
        ``FrameLocator``'s ``bounding_box()`` returns page-relative coordinates,
        so moving ``page.mouse`` along a path to that box lands the click
        correctly. Falls back to ``locator.click()`` when humanisation is
        disabled, the geometry can't be read, or the page has no usable mouse
        (tests / invisible widgets).
        """
        # ``.first`` so a selector matching multiple elements (e.g. Turnstile's
        # ``input[type="checkbox"], label``) resolves to a single element for
        # both bounding_box() and click() (matches the pre-P1-5 ``.first`` call).
        locator = frame_locator.locator(selector).first
        if getattr(self._config, "human_mouse_enabled", True):
            try:
                from ..parsing.shapes.human_cursor import human_click, human_tap_box

                box = await locator.bounding_box()
                if box:
                    jitter = float(
                        getattr(self._config, "human_mouse_jitter_ms", 90)
                    )
                    # Mobile fingerprint → trusted touch tap; else eased mouse
                    # path. A mouse click on a phone context contradicts the
                    # touch-capable fingerprint hCaptcha mobile scores.
                    if touch:
                        tapped = await human_tap_box(page, box)
                        if tapped is not None:
                            return
                    result = await human_click(page, box, jitter_ms=jitter)
                    if result is not None:
                        return
            except Exception as exc:  # noqa: BLE001 - fall back to plain click
                log.debug("human checkbox click failed, falling back: %s", exc)
        await locator.click(timeout=timeout_ms)

    async def _human_mouse(
        self, page: Any, *, seconds: "float | None" = None, touch: bool = False
    ) -> None:
        """Seed a realistic pre-interaction motion timeline (wander + scroll).

        hCaptcha scores a *continuous* ``motionData`` timeline. The previous
        warmup was a single ~0.5s eased move, which leaves an almost-empty
        motion buffer — a strong "automation / invisible-every-time" tell. This
        traces several eased sub-movements to random on-viewport points with
        human dwell between them (see :func:`human_wander`), plus an occasional
        small scroll, so the buffer looks like a human landing on and reading a
        page. Gated by ``human_mouse_enabled`` (off in tests / when a caller
        wants a deterministic no-motion solve).

        ``touch=True`` (a mobile fingerprint) fills the window with scroll
        gestures + dwell instead — a phone has no hovering cursor, so tracing
        ``mouse.move`` on a touch context would itself be a contradiction.

        ``seconds`` overrides the wander duration for the passive window; the
        default is ``config.human_passive_motion_seconds``.
        """
        if not getattr(self._config, "human_mouse_enabled", True):
            return
        from ..parsing.shapes.human_cursor import human_touch_scroll, human_wander

        budget = (
            seconds
            if seconds is not None
            else float(getattr(self._config, "human_passive_motion_seconds", 1.4))
        )
        if touch:
            await human_touch_scroll(page, seconds=budget)
            return
        jitter = float(getattr(self._config, "human_mouse_jitter_ms", 80))
        viewport = self._viewport_size(page)
        cursor = await human_wander(
            page, seconds=budget, viewport=viewport, jitter_ms=jitter
        )
        # An occasional short scroll — a human settling onto a page usually
        # nudges the wheel. Best-effort; a fake page without wheel is a no-op.
        if cursor is not None and random.random() < 0.5:
            try:
                await page.mouse.wheel(0, random.randint(60, 240))
                await asyncio.sleep(random.uniform(0.15, 0.5))
            except Exception:  # noqa: BLE001 - scroll is best-effort
                pass

    @staticmethod
    def _viewport_size(page: Any) -> "tuple[int, int] | None":
        """Best-effort ``(width, height)`` of the page viewport, else ``None``."""
        try:
            vp = getattr(page, "viewport_size", None)
            if isinstance(vp, dict) and vp.get("width") and vp.get("height"):
                return int(vp["width"]), int(vp["height"])
        except Exception:  # noqa: BLE001
            pass
        return None
