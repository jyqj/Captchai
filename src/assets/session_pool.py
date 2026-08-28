"""Warm browser-session pool bucketed by egress identity.

``SessionPool`` owns a bounded number of live browser contexts and reuses them
across solves. Sessions are isolated by egress identity: proxyless sessions use
one bucket and pool-proxy sessions use the proxy id, so a context never changes
its network identity during its lifetime.

The pool deliberately uses an :class:`asyncio.Condition` rather than a
``Semaphore``. A semaphore is a poor fit when permits represent *live assets*
(in-use plus idle): returning a healthy session to the idle set must wake a
checkout waiter without releasing the live-asset permit. The condition makes
that hand-off explicit and prevents a full pool from deadlocking when every
session is temporarily in use.
"""

from __future__ import annotations

import asyncio
import inspect
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
    # The checkout bucket is stored on the lease itself. This avoids rebuilding
    # it from mutable proxy metadata during release and makes mismatched caller
    # keys harmless: the session always returns to the bucket it came from.
    bucket_key: str = PROXYLESS_KEY


async def _maybe_await_close(context: Any) -> None:
    """Call ``context.close()`` tolerating sync or async fakes, never raising."""
    try:
        closer = getattr(context, "close", None)
        if closer is None:
            return
        result = closer()
        if inspect.isawaitable(result):
            await result
    except Exception:  # noqa: BLE001 - retirement must never propagate
        pass


async def _run_cleanup_uninterruptibly(cleanup: Awaitable[Any]) -> Any:
    """Finish ownership cleanup before propagating caller cancellation.

    Browser-context close operations update pool capacity only after the real
    resource is gone. Shielding the cleanup task prevents a cancelled request
    or shutdown from leaking a context or leaving lifecycle counters stuck.
    Cancellation is still propagated once cleanup has completed.
    """
    task = asyncio.ensure_future(cleanup)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


class SessionPool:
    """Bounded, cancellation-safe pool of reusable browser sessions.

    ``_live`` counts all capacity already reserved by the pool: idle sessions,
    checked-out sessions, contexts currently being constructed, and contexts
    still closing. A waiter is notified whenever an idle session appears or
    capacity is truly freed. This is the key invariant that the previous
    semaphore-only implementation could not express: returning a reusable
    session does not reduce ``_live``, but it must still wake a waiter so that
    waiter can claim the newly-idle asset.
    """

    def __init__(
        self,
        context_factory: ContextFactory,
        *,
        size: int = 4,
        max_solves: int = 8,
    ) -> None:
        if size <= 0:
            raise ValueError("session pool size must be greater than zero")
        if max_solves <= 0:
            raise ValueError("session max_solves must be greater than zero")

        self._factory = context_factory
        self._size = size
        self._max_solves = max_solves
        self._idle: Dict[str, List[BrowserSession]] = {}
        self._in_use: Dict[str, BrowserSession] = {}

        # Condition protects every mutable pool-state field. It is also the
        # hand-off mechanism between release/retirement and blocked checkouts.
        self._condition = asyncio.Condition()
        self._live = 0
        self._creating = 0
        self._closing = 0
        self._waiters = 0
        self._closed = False

    async def checkout(
        self,
        *,
        key: str,
        proxy: Optional[ProxyAsset] = None,
        sitekey: Optional[str] = None,
    ) -> BrowserSession:
        """Return an idle session for ``key`` or reserve/build a new one.

        When the pool is full and every asset is in use, the caller waits on the
        condition. A normal release wakes it and it reuses the returned session;
        no live-capacity permit needs to be released. If only another bucket has
        an idle session, the least-valuable idle asset is retired and replaced
        in-place, preserving the global size bound.

        Context construction and publication are cancellation-safe: capacity is
        reserved before leaving the lock, and a context that cannot be published
        to ``_in_use`` is closed before its reservation is returned.
        """
        del sitekey  # accepted for call-site compatibility; not used for selection
        bucket_key = key or (proxy.id if proxy is not None else PROXYLESS_KEY)

        while True:
            evicted: Optional[BrowserSession] = None
            async with self._condition:
                self._ensure_open_locked()

                session = self._pop_idle_locked(bucket_key)
                if session is not None:
                    session.warm = True
                    self._in_use[session.id] = session
                    return session

                if self._live < self._size:
                    # Reserve capacity before releasing the condition so no two
                    # builders can oversubscribe the pool.
                    self._live += 1
                    self._creating += 1
                    break

                evicted = self._evict_one_idle_locked()
                if evicted is not None:
                    # Replacement reuses the evicted session's live slot. The
                    # count stays unchanged while construction is in progress.
                    self._creating += 1
                    break

                # Full and all assets are checked out or closing. Wait for an
                # idle hand-off, genuinely-freed capacity, or shutdown.
                self._waiters += 1
                try:
                    await self._condition.wait()
                finally:
                    self._waiters -= 1

        try:
            if evicted is not None:
                await _run_cleanup_uninterruptibly(
                    _maybe_await_close(evicted.context)
                )
                evicted.warm = False

            fingerprint = generate_fingerprint(
                seed=proxy.id if proxy else None,
                timezone_id=proxy.timezone if proxy else None,
                locale=proxy.locale if proxy else None,
                mobile=bool(proxy and getattr(proxy, "kind", None) == "mobile"),
            )
            context, user_agent = await self._factory(fingerprint, proxy)
            session = BrowserSession(
                id=str(uuid.uuid4()),
                context=context,
                fingerprint=fingerprint,
                proxy=proxy,
                user_agent=user_agent,
                created_at=time.monotonic(),
                warm=True,
                bucket_key=bucket_key,
            )
        except BaseException:
            await _run_cleanup_uninterruptibly(self._rollback_creation())
            raise

        return await self._publish_created_session(session)

    async def _publish_created_session(
        self, session: BrowserSession
    ) -> BrowserSession:
        """Atomically publish a built context or dispose of it on cancellation.

        The condition is acquired manually rather than with ``async with`` so
        there is no cancellation point after the session is inserted into
        ``_in_use`` and before it is returned to the caller.
        """
        try:
            await self._condition.acquire()
        except BaseException:
            session.warm = False
            await _run_cleanup_uninterruptibly(
                self._rollback_creation(session.context)
            )
            raise

        try:
            if not self._closed:
                self._creating -= 1
                self._in_use[session.id] = session
                self._condition.notify_all()
                return session
        finally:
            self._condition.release()

        # Shutdown raced with publication. Dispose of the unpublished context
        # and return its live reservation before reporting the terminal state.
        session.warm = False
        await _run_cleanup_uninterruptibly(
            self._rollback_creation(session.context)
        )
        raise RuntimeError("session pool is closed")

    async def _rollback_creation(self, context: Any = None) -> None:
        """Close an unpublished context and return its creation reservation."""
        if context is not None:
            await _maybe_await_close(context)
        async with self._condition:
            self._creating -= 1
            self._live -= 1
            self._condition.notify_all()

    async def prewarm(self, *, sitekey: Optional[str] = None) -> int:
        """Create idle proxyless sessions without consuming solve lifetime."""
        del sitekey  # accepted for call-site compatibility
        tasks = [
            asyncio.create_task(self.checkout(key=PROXYLESS_KEY, proxy=None))
            for _ in range(self._size)
        ]
        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
        except BaseException:
            # ``gather`` cancellation can leave some children already completed
            # with live leases. Reclaim every successful result before
            # propagating the startup cancellation/failure.
            await _run_cleanup_uninterruptibly(
                self._cancel_and_reclaim_prewarm(tasks)
            )
            raise

        sessions = [r for r in results if isinstance(r, BrowserSession)]
        await asyncio.gather(
            *(
                self._release(
                    session,
                    success=True,
                    count_solve=False,
                )
                for session in sessions
            )
        )

        failures = [r for r in results if isinstance(r, BaseException)]
        if failures:
            raise failures[0]
        return len(sessions)

    async def _cancel_and_reclaim_prewarm(
        self, tasks: List["asyncio.Task[BrowserSession]"]
    ) -> None:
        for task in tasks:
            if not task.done():
                task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        sessions = [r for r in results if isinstance(r, BrowserSession)]
        if sessions:
            await asyncio.gather(
                *(
                    self._release(
                        session,
                        success=True,
                        count_solve=False,
                    )
                    for session in sessions
                )
            )

    def _ensure_open_locked(self) -> None:
        if self._closed:
            raise RuntimeError("session pool is closed")

    def _pop_idle_locked(self, key: str) -> Optional[BrowserSession]:
        bucket = self._idle.get(key)
        if not bucket:
            return None
        session = bucket.pop()
        if not bucket:
            del self._idle[key]
        return session

    def _evict_one_idle_locked(self) -> Optional[BrowserSession]:
        """Remove the least-valuable idle session for cross-bucket replacement."""
        worst_key: Optional[str] = None
        worst: Optional[BrowserSession] = None
        for key, bucket in self._idle.items():
            for session in bucket:
                rank = (session.reputation, -session.solves, session.created_at)
                if worst is None or rank < (
                    worst.reputation,
                    -worst.solves,
                    worst.created_at,
                ):
                    worst = session
                    worst_key = key
        if worst is None or worst_key is None:
            return None
        bucket = self._idle[worst_key]
        bucket.remove(worst)
        if not bucket:
            del self._idle[worst_key]
        return worst

    async def release(
        self, session: BrowserSession, *, success: bool, burned: bool = False
    ) -> None:
        """Return a checked-out session, or retire it when policy requires."""
        del burned  # reputation/max-solves policy is authoritative
        await self._release(session, success=success, count_solve=True)

    async def _release(
        self,
        session: BrowserSession,
        *,
        success: bool,
        count_solve: bool,
    ) -> None:
        """Internal release with a prewarm path that does not count as a solve.

        Duplicate or late releases are harmless: only the exact object currently
        recorded in ``_in_use`` can mutate pool state. A reusable release wakes
        checkout waiters even though live capacity is unchanged. Retirement
        keeps its slot reserved until the browser context has actually closed,
        then frees capacity and wakes waiters; this prevents transient
        oversubscription of expensive browser contexts.
        """
        retire = False

        async with self._condition:
            current = self._in_use.get(session.id)
            if current is not session:
                return
            self._in_use.pop(session.id, None)

            if count_solve:
                session.solves += 1
                if success:
                    session.reputation = min(1.0, session.reputation + 0.05)
                else:
                    session.reputation = max(0.0, session.reputation - 0.4)

            retire = (
                self._closed
                or session.solves >= self._max_solves
                or session.reputation < 0.3
            )
            if retire:
                session.warm = False
                self._closing += 1
            else:
                session.warm = True
                self._idle.setdefault(session.bucket_key, []).append(session)

            self._condition.notify_all()

        if retire:
            await _run_cleanup_uninterruptibly(
                self._finish_retirement(session)
            )

    async def _finish_retirement(self, session: BrowserSession) -> None:
        await _maybe_await_close(session.context)
        async with self._condition:
            self._closing -= 1
            self._live -= 1
            self._condition.notify_all()

    async def close_all(self) -> None:
        """Permanently close the pool and every context it owns.

        Blocked checkouts are woken and fail with ``RuntimeError``. Contexts that
        are still being constructed are allowed to finish their cancellation/
        cleanup path. This method waits until all creation and retirement work
        has released its reservation, making shutdown deterministic.
        """
        async with self._condition:
            self._closed = True
            sessions = list(self._in_use.values())
            for bucket in self._idle.values():
                sessions.extend(bucket)
            self._idle.clear()
            self._in_use.clear()
            self._closing += len(sessions)
            self._condition.notify_all()

        await _run_cleanup_uninterruptibly(
            self._close_sessions_and_wait(sessions)
        )

    async def _close_sessions_and_wait(
        self, sessions: List[BrowserSession]
    ) -> None:
        await asyncio.gather(
            *(_maybe_await_close(session.context) for session in sessions)
        )
        for session in sessions:
            session.warm = False

        async with self._condition:
            self._closing -= len(sessions)
            self._live -= len(sessions)
            self._condition.notify_all()
            while self._creating > 0 or self._closing > 0:
                await self._condition.wait()

    async def close(self) -> None:
        """Lifecycle alias used by the application composition root."""
        await self.close_all()

    async def report_outcome(self, session_id: str, *, success: bool) -> bool:
        """Nudge a live session's reputation after a real downstream outcome."""
        async with self._condition:
            target: Optional[BrowserSession] = self._in_use.get(session_id)
            if target is None:
                for bucket in self._idle.values():
                    target = next((s for s in bucket if s.id == session_id), None)
                    if target is not None:
                        break
            if target is None:
                return False
            if success:
                target.reputation = min(1.0, target.reputation + 0.05)
            else:
                target.reputation = max(0.0, target.reputation - 0.4)
            return True

    def stats(self) -> Dict[str, Any]:
        """Cheap operational counters for diagnostics and tests."""
        return {
            "capacity": self._size,
            "live": self._live,
            "idle": sum(len(bucket) for bucket in self._idle.values()),
            "in_use": len(self._in_use),
            "creating": self._creating,
            "closing": self._closing,
            "waiters": self._waiters,
            "closed": self._closed,
        }

    def snapshot(self) -> List[Dict[str, Any]]:
        """Serialisable view of live sessions for the admin endpoint."""
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
