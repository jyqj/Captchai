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

        * ``"auto"`` (default): caller task proxy → fresh context; else warm
          session → else server-side pool proxy → else server egress IP.
        * ``"task"``: require a caller-supplied proxy and bind a fresh context
          to it. Raises if no task proxy was provided.
        * ``"pool"``: skip warm session and task proxy; require a server-side
          pool proxy. Raises if the pool is empty.
        * ``"proxyless"``: ignore any caller proxy and any pool proxy; use a
          warm session if available, else server egress IP.
        """

        egress = params.get("egress") or "auto"

        if egress == "task":
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

        if egress == "pool":
            proxy_pool = (
                getattr(self._services, "proxy_pool", None)
                if self._services is not None
                else None
            )
            if proxy_pool is None:
                raise RuntimeError(
                    "egress=pool requires a server-side proxy but the pool is empty"
                )
            pool_proxy = await proxy_pool.checkout(sitekey=params.get("websiteKey"))
            if pool_proxy is None:
                raise RuntimeError(
                    "egress=pool requires a server-side proxy but the pool is empty"
                )
            params["_pool_proxy_id"] = pool_proxy.id
            params["_proxyKind"] = ProxyKind.POOL_PROXY.value
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

        if egress == "proxyless":
            params["_proxyKind"] = ProxyKind.PROXYLESS.value
            if self._services is not None:
                session_pool = getattr(self._services, "session_pool", None)
                if session_pool is not None:
                    session = await session_pool.checkout(
                        sitekey=params.get("websiteKey")
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

        # egress == "auto" (default): task proxy → session → pool → server egress.
        if has_task_proxy(params):
            params["_proxyKind"] = ProxyKind.TASK_PROXY.value
            context, user_agent = await self._manager.new_context(params)
            return SolveContext(
                context=context,
                user_agent=user_agent,
                proxy_kind=ProxyKind.TASK_PROXY,
            )

        if self._services is not None:
            session_pool = getattr(self._services, "session_pool", None)
            if session_pool is not None:
                session = await session_pool.checkout(sitekey=params.get("websiteKey"))
                params["_proxyKind"] = ProxyKind.PROXYLESS.value
                params["_sessionId"] = session.id
                return SolveContext(
                    context=session.context,
                    user_agent=session.user_agent,
                    proxy_kind=ProxyKind.PROXYLESS,
                    session=session,
                    session_id=session.id,
                )

            proxy_pool = getattr(self._services, "proxy_pool", None)
            if proxy_pool is not None:
                pool_proxy = await proxy_pool.checkout(sitekey=params.get("websiteKey"))
                if pool_proxy is not None:
                    params["_pool_proxy_id"] = pool_proxy.id
                    params["_proxyKind"] = ProxyKind.POOL_PROXY.value
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

        context, user_agent = await self._manager.new_context(params)
        params["_proxyKind"] = ProxyKind.PROXYLESS.value
        return SolveContext(
            context=context,
            user_agent=user_agent,
            proxy_kind=ProxyKind.PROXYLESS,
        )

    async def _release_context(
        self, solve_context: SolveContext, solved: bool, params: dict[str, Any]
    ) -> None:
        """Return or close the browser context and report proxy-pool health.

        Reads the per-context byte counter (set by BrowserManager's response
        listener) so the proxy pool and ledger can attribute bandwidth to this
        solve. For warm proxyless sessions the counter is left at 0 — session
        contexts persist across solves so per-solve attribution isn't meaningful
        there, and proxyless solves have no proxy to bill anyway.
        """
        bytes_used = 0
        if solve_context.session is None:
            # Fresh context (task_proxy / pool_proxy / proxyless-fallback):
            # read the accumulated byte count before closing.
            bytes_used = int(getattr(solve_context.context, "_omc_bytes_used", 0))
            params["_proxy_bytes"] = bytes_used
            try:
                await solve_context.context.close()
            except Exception:
                pass
        else:
            # Warm session: release back to the pool; per-solve byte attribution
            # is skipped (the context outlives this solve).
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
