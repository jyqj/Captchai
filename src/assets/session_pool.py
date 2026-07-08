"""Warm browser-session pool bucketed by egress identity.

Launching a fresh Playwright context per solve is slow and throws away the
"warmed up" browser state (cookies, JS JIT, TLS session) that makes a session
look human. ``SessionPool`` keeps a bounded set of live sessions, hands out a
warm idle one when available, and only creates a new one — via an injected
async ``context_factory`` — when it must. Sessions accrue a reputation and are
retired (context closed) when their reputation drops below a threshold or they
exceed ``max_solves``; a single solve failure no longer forces retirement.

Idle sessions are bucketed by **egress identity**: pool-proxy-bound sessions
are keyed by ``proxy.id``; server-IP (proxyless) sessions are keyed by the
literal ``"proxyless"``. A session owns its proxy for its whole lifetime —
``checkout`` for a given key only reuses sessions bound to that exact key, so
a pool-proxy session is never reused for a proxyless solve (or vice versa).
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

# Bucket key for proxyless (server-IP) sessions. Pool-proxy sessions use
# ``proxy.id`` as their bucket key.
PROXYLESS_KEY = "proxyless"


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


def _bucket_key(proxy: Optional[ProxyAsset]) -> str:
    return proxy.id if proxy is not None else PROXYLESS_KEY


class SessionPool:
    """Bounded pool of reusable browser sessions, bucketed by egress identity."""

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
        # Idle sessions bucketed by egress key: ``proxy.id`` for pool-proxy
        # sessions, ``"proxyless"`` for server-IP sessions. A checkout for a
        # given key only reuses sessions from that bucket, so a sticky proxy
        # identity is preserved across solves.
        self._idle: Dict[str, List[BrowserSession]] = {}
        self._in_use: Dict[str, BrowserSession] = {}
        self._lock = asyncio.Lock()

    async def checkout(
        self,
        *,
        key: str,
        proxy: Optional[ProxyAsset] = None,
        sitekey: Optional[str] = None,
    ) -> BrowserSession:
        """Return a warm idle session for ``key``, or create one (bounded by ``size``).

        Reuses the most recently idled session in the requested bucket when
        possible. When none is idle it waits for a free slot and builds a new
        session via the factory, so the number of concurrently live sessions
        never exceeds ``size``. The fingerprint is seeded by ``proxy.id`` when
        a proxy is given (so a sticky proxy keeps a stable coherent identity
        across re-warms); proxyless sessions draw from entropy.
        """
        del sitekey  # accepted for call-site compatibility; not used for selection
        # Claim a slot for a NEW session in this bucket. A free slot is used
        # directly; otherwise an idle session from ANOTHER bucket is retired to
        # free its slot (buckets share the global size bound, so warming a new
        # egress identity may evict a stale one). Only when every slot is held
        # by an *in-use* session — nothing is evictable — do we block until one
        # is released.
        while True:
            async with self._lock:
                session = self._pop_idle(key)
                if session is not None:
                    self._in_use[session.id] = session
                    return session
                if not self._slots.locked():
                    # A slot is free (value > 0): acquiring is synchronous and
                    # won't yield, so it's safe under the lock. Build below.
                    await self._slots.acquire()
                    break
                evicted = self._evict_one_idle()
            if evicted is not None:
                # Retire the evicted session and free its slot, then retry. The
                # freed slot may be taken by another coroutine, in which case we
                # evict again or finally block.
                await _maybe_await_close(evicted.context)
                evicted.warm = False
                self._slots.release()
                continue
            # Nothing evictable: all slots are held by in-use sessions. Block
            # until one is released, then re-check idle / build below.
            await self._slots.acquire()
            break

        async with self._lock:
            session = self._pop_idle(key)
            if session is not None:
                self._slots.release()  # reusing instead of creating; give slot back
                self._in_use[session.id] = session
                return session

        try:
            # Seed by proxy id when given so a sticky proxy keeps a stable
            # coherent identity; proxyless sessions draw from entropy. The
            # proxy's exit-IP geo (timezone / locale) drives the fingerprint
            # so a German residential IP presents Europe/Berlin + de-DE
            # rather than en-US/New_York; ``None`` falls back to a random
            # coherent identity (current behavior, no regression).
            fingerprint = generate_fingerprint(
                seed=proxy.id if proxy else None,
                timezone_id=proxy.timezone if proxy else None,
                locale=proxy.locale if proxy else None,
            )
            context, user_agent = await self._factory(fingerprint, proxy)
        except BaseException:
            self._slots.release()
            raise

        session = BrowserSession(
            id=str(uuid.uuid4()),
            context=context,
            fingerprint=fingerprint,
            proxy=proxy,
            user_agent=user_agent,
            created_at=time.monotonic(),
            warm=True,
        )
        async with self._lock:
            self._in_use[session.id] = session
        return session

    async def prewarm(self, *, sitekey: Optional[str] = None) -> int:
        """Create idle proxyless sessions up to the configured pool size.

        Matches the legacy prewarm behaviour: only the ``"proxyless"`` bucket
        is prewarmed, since pool-proxy sessions are created lazily on first
        solve for a given proxy.
        """
        del sitekey  # accepted for call-site compatibility; not used for prewarm
        if self._size <= 0:
            return 0
        sessions = await asyncio.gather(
            *[
                self.checkout(key=PROXYLESS_KEY, proxy=None)
                for _ in range(self._size)
            ]
        )
        for session in sessions:
            await self.release(session, success=True)
        return len(sessions)

    def _pop_idle(self, key: str) -> Optional[BrowserSession]:
        bucket = self._idle.get(key)
        while bucket:
            session = bucket.pop()
            if not bucket:
                del self._idle[key]
            return session
        return None

    def _evict_one_idle(self) -> Optional[BrowserSession]:
        """Pop an idle session from any bucket to free its slot (caller retires it).

        Buckets share the global ``size`` bound, so warming a new egress
        identity when every slot is held may require retiring an idle session
        bound to a different identity. Returns ``None`` when nothing is idle
        (all slots held by in-use sessions). Must be called under ``self._lock``.
        """
        for k, bucket in list(self._idle.items()):
            if bucket:
                session = bucket.pop()
                if not bucket:
                    del self._idle[k]
                return session
        return None

    async def release(
        self, session: BrowserSession, *, success: bool, burned: bool = False
    ) -> None:
        """Return a session to its bucket, updating its reputation.

        Each release counts as one completed solve. A session is retired (its
        context closed and its slot freed so a replacement can be created lazily
        on the next ``checkout``) when it has reached ``max_solves`` OR its
        reputation has dropped below the eviction threshold (0.3). A single
        solve failure no longer forces retirement: a fresh session (rep=1.0)
        survives one failure (1.0→0.6) and is only evicted after a second
        consecutive failure (0.6→0.2). Otherwise the session goes back to its
        egress-keyed warm idle bucket.

        ``burned`` is accepted for backward compat (callers such as
        ``browser_solver`` still pass ``burned=not solved``) but no longer
        forces retirement; reputation decay handles eviction.

        The session keeps its ``ProxyAsset`` reference for its whole lifetime;
        the caller (``browser_solver``) is responsible for ``proxy_pool.report``
        on each solve. This method does NOT release the proxy back to the
        proxy pool — only when the session retires does its slot come back.
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
        key = _bucket_key(session.proxy)
        async with self._lock:
            self._idle.setdefault(key, []).append(session)

    async def close_all(self) -> None:
        """Retire every session (idle and in-use) and free all slots."""
        async with self._lock:
            sessions = list(self._in_use.values())
            for bucket in self._idle.values():
                sessions.extend(bucket)
            self._idle.clear()
            self._in_use.clear()
        for session in sessions:
            await _maybe_await_close(session.context)
            session.warm = False
            self._slots.release()

    async def report_outcome(self, session_id: str, *, success: bool) -> bool:
        """Nudge a session's reputation by id after a real-outcome report.

        Called by /reportCorrect / /reportIncorrect to feed the token-actually-
        accepted signal back into session selection. Searches both the idle
        buckets and the in-use map for the session and applies the same
        reputation delta as ``release`` (``+0.05`` on success, ``-0.4`` on
        failure, clamped to ``[0, 1]``). Does NOT retire or close the session
        — eviction still happens via the normal ``release`` path when the
        reputation drops below ``0.3``.

        Returns ``True`` if the session was found and nudged, ``False`` if it
        was already retired / gone (a non-fatal no-op for the caller).
        """
        async with self._lock:
            target: Optional[BrowserSession] = None
            for bucket in self._idle.values():
                for s in bucket:
                    if s.id == session_id:
                        target = s
                        break
                if target is not None:
                    break
            if target is None:
                for s in self._in_use.values():
                    if s.id == session_id:
                        target = s
                        break
            if target is None:
                return False
            if success:
                target.reputation = min(1.0, target.reputation + 0.05)
            else:
                target.reputation = max(0.0, target.reputation - 0.4)
            return True

    def snapshot(self) -> List[Dict[str, Any]]:
        """Serialisable view of live sessions for an admin endpoint."""
        result: List[Dict[str, Any]] = []
        idle_rows: List[Tuple[BrowserSession, bool]] = []
        for bucket in self._idle.values():
            idle_rows.extend((s, False) for s in bucket)
        in_use_rows = [(s, True) for s in self._in_use.values()]
        for session, in_use in idle_rows + in_use_rows:
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
