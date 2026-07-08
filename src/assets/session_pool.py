"""Warm browser-session pool with reputation-based retirement.

Launching a fresh Playwright context per solve is slow and throws away the
"warmed up" browser state (cookies, JS JIT, TLS session) that makes a session
look human. ``SessionPool`` keeps a bounded set of live sessions, hands out a
warm idle one when available, and only creates a new one — via an injected
async ``context_factory`` — when it must. Sessions accrue a reputation and are
retired (context closed) when their reputation drops below a threshold or they
exceed ``max_solves``; a single solve failure no longer forces retirement.

The factory is injected rather than importing ``browser.py`` directly so the
pool is fully testable with fakes and has no hard Playwright dependency.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from .fingerprint import FingerprintProfile, generate_fingerprint
from .proxy_pool import ProxyAsset

# async (fingerprint, proxy) -> (context, user_agent)
ContextFactory = Callable[
    [FingerprintProfile, Optional[ProxyAsset]],
    Awaitable[Tuple[Any, str]],
]


@dataclass
class BrowserSession:
    id: str
    context: Any
    fingerprint: FingerprintProfile
    proxy: Optional[ProxyAsset]
    user_agent: str
    created_at: float
    solves: int = 0
    reputation: float = 1.0
    warm: bool = False


async def _maybe_await_close(context: Any) -> None:
    """Call ``context.close()`` tolerating sync or async fakes, never raising."""
    try:
        closer = getattr(context, "close", None)
        if closer is None:
            return
        result = closer()
        if asyncio.iscoroutine(result):
            await result
    except Exception:  # noqa: BLE001 - retirement must never propagate
        pass


class SessionPool:
    """Bounded pool of reusable browser sessions."""

    def __init__(
        self,
        context_factory: ContextFactory,
        *,
        size: int = 4,
        max_solves: int = 8,
    ) -> None:
        self._factory = context_factory
        self._size = size
        self._max_solves = max_solves
        # Semaphore permits == number of live sessions allowed at once. A permit
        # is held for a session's whole lifetime (in-use *and* idle) and only
        # returned on retirement.
        self._slots = asyncio.Semaphore(size)
        self._idle: List[BrowserSession] = []
        self._in_use: Dict[str, BrowserSession] = {}
        self._lock = asyncio.Lock()

    async def checkout(self, *, sitekey: Optional[str] = None) -> BrowserSession:
        """Return a warm idle session, or create one (bounded by ``size``).

        Reuses the most recently idled session when possible. When none is idle
        it waits for a free slot and builds a new session via the factory, so
        the number of concurrently live sessions never exceeds ``size``.
        """
        async with self._lock:
            session = self._pop_idle()
            if session is not None:
                self._in_use[session.id] = session
                return session

        # No idle session: claim a slot (may block until one is retired), then
        # re-check idle in case another release happened while we waited.
        await self._slots.acquire()
        async with self._lock:
            session = self._pop_idle()
            if session is not None:
                self._slots.release()  # reusing instead of creating; give slot back
                self._in_use[session.id] = session
                return session

        try:
            # Random seed so each session gets a unique coherent identity. A
            # sitekey-seeded fingerprint would make every session for the same
            # target identical, which hCaptcha clusters and flags.
            fingerprint = generate_fingerprint(seed=None)
            context, user_agent = await self._factory(fingerprint, None)
        except BaseException:
            self._slots.release()
            raise

        session = BrowserSession(
            id=str(uuid.uuid4()),
            context=context,
            fingerprint=fingerprint,
            proxy=None,
            user_agent=user_agent,
            created_at=time.monotonic(),
            warm=True,
        )
        async with self._lock:
            self._in_use[session.id] = session
        return session

    async def prewarm(self, *, sitekey: Optional[str] = None) -> int:
        """Create idle sessions up to the configured pool size."""

        if self._size <= 0:
            return 0
        sessions = await asyncio.gather(
            *[self.checkout(sitekey=sitekey) for _ in range(self._size)]
        )
        for session in sessions:
            await self.release(session, success=True)
        return len(sessions)

    def _pop_idle(self) -> Optional[BrowserSession]:
        while self._idle:
            session = self._idle.pop()
            return session
        return None

    async def release(
        self, session: BrowserSession, *, success: bool, burned: bool = False
    ) -> None:
        """Return a session to the pool, updating its reputation.

        Each release counts as one completed solve. A session is retired (its
        context closed and its slot freed so a replacement can be created lazily
        on the next ``checkout``) when it has reached ``max_solves`` OR its
        reputation has dropped below the eviction threshold (0.3). A single
        solve failure no longer forces retirement: a fresh session (rep=1.0)
        survives one failure (1.0→0.6) and is only evicted after a second
        consecutive failure (0.6→0.2). Otherwise the session goes back to the
        warm idle set.

        ``burned`` is accepted for backward compat (callers such as
        ``browser_solver`` still pass ``burned=not solved``) but no longer
        forces retirement; reputation decay handles eviction.
        """
        async with self._lock:
            self._in_use.pop(session.id, None)

        session.solves += 1
        if success:
            session.reputation = min(1.0, session.reputation + 0.05)
        else:
            session.reputation = max(0.0, session.reputation - 0.4)

        retire = session.solves >= self._max_solves or session.reputation < 0.3
        if retire:
            await _maybe_await_close(session.context)
            session.warm = False
            self._slots.release()
            return

        session.warm = True
        async with self._lock:
            self._idle.append(session)

    async def close_all(self) -> None:
        """Retire every session (idle and in-use) and free all slots."""
        async with self._lock:
            sessions = list(self._idle) + list(self._in_use.values())
            self._idle.clear()
            self._in_use.clear()
        for session in sessions:
            await _maybe_await_close(session.context)
            session.warm = False
            self._slots.release()

    def snapshot(self) -> List[Dict[str, Any]]:
        """Serialisable view of live sessions for an admin endpoint."""
        result: List[Dict[str, Any]] = []
        for session, in_use in (
            [(s, False) for s in self._idle]
            + [(s, True) for s in self._in_use.values()]
        ):
            result.append(
                {
                    "id": session.id,
                    "in_use": in_use,
                    "warm": session.warm,
                    "solves": session.solves,
                    "reputation": round(session.reputation, 4),
                    "user_agent": session.user_agent,
                    "proxy_id": session.proxy.id if session.proxy else None,
                    "created_at": session.created_at,
                }
            )
        return result
