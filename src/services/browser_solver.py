"""Shared browser-solver helpers for context acquisition and proxy categories."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from playwright.async_api import Browser

from ..assets.proxy_pool import proxy_from_params
from ..assets.session_pool import PROXYLESS_KEY
from ..core.config import Config
from .browser import BrowserManager

log = logging.getLogger(__name__)


class ProxyKind(str, Enum):
    """Explicit egress category used for scheduling and accounting."""

    PROXYLESS = "proxyless"
    TASK_PROXY = "task_proxy"
    POOL_PROXY = "pool_proxy"


@dataclass
class SolveContext:
    context: Any
    user_agent: str
    proxy_kind: ProxyKind
    session: Any | None = None
    proxy_id: str | None = None
    session_id: str | None = None


def has_task_proxy(params: dict[str, Any]) -> bool:
    """Return true when the caller supplied explicit proxy fields."""

    return proxy_from_params(params) is not None


def initial_proxy_kind(params: dict[str, Any]) -> ProxyKind:
    """Classify the request before server-side pools are consulted."""

    return ProxyKind.TASK_PROXY if has_task_proxy(params) else ProxyKind.PROXYLESS


def proxy_ip_from_params(params: dict[str, Any]) -> Optional[str]:
    """Extract a stable proxy host for token-cache bucketing."""

    address = params.get("proxyAddress")
    if address:
        return str(address)
    single = params.get("proxy")
    if single and "://" in str(single):
        rest = str(single).split("://", 1)[1]
        hostport = rest.rsplit("@", 1)[-1]
        return hostport.split(":", 1)[0]
    return None


def fingerprint_geo_from_params(params: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    """Return ``(timezone_id, accept_language)`` stashed on ``params``.

    ``_stash_fingerprint_geo`` (or ``resolve_context_options`` for fresh
    contexts) writes ``_used_timezone`` and ``_used_languages`` onto params
    so any solver can surface them in the solution without re-reading the
    fingerprint. Returns ``(None, None)`` when no fingerprint geo was
    stashed (e.g. tests that mock ``_acquire_context``).
    """
    tz = params.get("_used_timezone")
    langs = params.get("_used_languages") or []
    accept = ", ".join(langs) if langs else None
    return tz, accept


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
            params["_used_timezone"] = fp.timezone_id
            params["_used_languages"] = list(fp.languages)
        # Fresh-context path: resolve_context_options already stashed them.

    async def _acquire_task(self, params: dict[str, Any]) -> SolveContext:
        """Bind a fresh context to the caller-supplied proxy (no session reuse)."""
        if not has_task_proxy(params):
            raise RuntimeError(
                "egress=task requires a caller-supplied proxy "
                "(proxy / proxyAddress+proxyPort fields)"
            )
        params["_proxyKind"] = ProxyKind.TASK_PROXY.value
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
        params["_pool_proxy_id"] = pool_proxy.id
        params["_proxyKind"] = ProxyKind.POOL_PROXY.value
        # WP3: thread the proxy's exit-IP geo + a deterministic seed so a
        # fresh-context build (no session pool) produces a fingerprint
        # aligned with the proxy's egress. Warm sessions read these from the
        # proxy directly inside ``SessionPool.checkout``.
        params["_pool_geo"] = {
            "timezone": pool_proxy.timezone,
            "locale": pool_proxy.locale,
            "country": pool_proxy.country,
        }
        params["_proxy_seed"] = pool_proxy.id

        session_pool = (
            getattr(self._services, "session_pool", None)
            if self._services is not None
            else None
        )
        if session_pool is not None:
            session = await session_pool.checkout(
                key=pool_proxy.id, proxy=pool_proxy, sitekey=params.get("websiteKey")
            )
            params["_sessionId"] = session.id
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
            params["_proxy_override"] = pw_proxy
        context, user_agent = await self._manager.new_context(params)
        return SolveContext(
            context=context,
            user_agent=user_agent,
            proxy_kind=ProxyKind.POOL_PROXY,
            proxy_id=pool_proxy.id,
        )

    async def _acquire_proxyless(
        self, params: dict[str, Any]
    ) -> SolveContext:
        """Use a warm server-IP session if available, else a fresh context."""
        params["_proxyKind"] = ProxyKind.PROXYLESS.value
        if self._services is not None:
            session_pool = getattr(self._services, "session_pool", None)
            if session_pool is not None:
                session = await session_pool.checkout(
                    key=PROXYLESS_KEY,
                    proxy=None,
                    sitekey=params.get("websiteKey"),
                )
                params["_sessionId"] = session.id
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
        """
        bytes_used = 0
        if solve_context.session is None:
            # Fresh context (task_proxy / pool-fallback / proxyless-fallback):
            # read the accumulated byte count before closing.
            bytes_used = int(getattr(solve_context.context, "_omc_bytes_used", 0))
            params["_proxy_bytes"] = bytes_used
            try:
                await solve_context.context.close()
            except Exception:
                pass
        else:
            # Warm session (pool or proxyless): release back to the session
            # pool; per-solve byte attribution is skipped (the context outlives
            # this solve). Pool-proxy health is still reported per solve below.
            if self._services is not None:
                await self._services.session_pool.release(
                    solve_context.session, success=solved, burned=not solved
                )

        proxy_id = solve_context.proxy_id or params.pop("_pool_proxy_id", None)
        if proxy_id and self._services is not None:
            proxy_pool = getattr(self._services, "proxy_pool", None)
            if proxy_pool is not None:
                await proxy_pool.report(proxy_id, success=solved, bytes_used=bytes_used)
                sitekey = params.get("websiteKey")
                if sitekey:
                    await proxy_pool.report_sitekey(proxy_id, sitekey, success=solved)

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
        set it after challenge dispatch). Metering failures are swallowed so they
        never fail a solve.
        """
        if self._services is None:
            return
        from ..consumption.ledger import SolveRecord, estimate_cost

        vision = params.get("_vision")
        model = getattr(vision, "last_model", None)
        in_tok = getattr(vision, "total_input_tokens", 0) or 0
        out_tok = getattr(vision, "total_output_tokens", 0) or 0
        calls = getattr(vision, "total_vision_calls", 0) or 0
        cost = estimate_cost(model or "local", in_tok, out_tok)

        try:
            await self._services.ledger.record(
                SolveRecord(
                    task_id=str(params.get("_taskId") or ""),
                    sitekey=sitekey,
                    task_type=task_type or params.get("type", "unknown"),
                    proxy_id=params.get("_pool_proxy_id"),
                    session_id=params.get("_sessionId"),
                    proxy_kind=params.get("_proxyKind"),
                    model=model,
                    challenge_shape=challenge_shape,
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
        except Exception as exc:
            log.debug("ledger record failed: %s", exc)

    def _proxy_ip(self, params: dict[str, Any]) -> Optional[str]:
        return proxy_ip_from_params(params)

    async def _human_mouse(self, page: Any) -> None:
        """Simulate a small human-like pre-interaction mouse path."""

        if not getattr(self._config, "human_mouse_enabled", True):
            return
        mouse = page.mouse
        start_x = random.randint(100, 300)
        start_y = random.randint(80, 200)
        await mouse.move(start_x, start_y)
        await asyncio.sleep(random.uniform(0.1, 0.3))
        target_x = random.randint(300, 500)
        target_y = random.randint(250, 400)
        steps = random.randint(4, 8)
        for i in range(1, steps + 1):
            t = i / steps
            eased = t * t * (3 - 2 * t)
            x = start_x + (target_x - start_x) * eased + random.uniform(-4, 4)
            y = start_y + (target_y - start_y) * eased + random.uniform(-3, 3)
            await mouse.move(x, y)
            jitter_s = getattr(self._config, "human_mouse_jitter_ms", 80) / 1000.0
            await asyncio.sleep(random.uniform(0.01, jitter_s))
        await asyncio.sleep(random.uniform(0.2, 0.5))
