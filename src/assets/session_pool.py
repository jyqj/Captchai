"""Warm browser-session pool bucketed by egress identity.

Launching a fresh Playwright context per solve is slow and throws away the
"warmed up" browser state (cookies, JS JIT, TLS session) that makes a session
look human. ``SessionPool`` keeps a bounded set of live sessions, hands out a
warm idle one when available, and only creates a new one — via an injected
async ``context_factory`` — when it must. Sessions accrue a reputation and are
retired (context closed) when their reputation drops below a threshold or they
exceed ``max_solves``; a single solve failure no longer forces retirement.

Permits bound only *in-use* sessions: a permit is held from ``checkout`` until
``release``, so warm idle sessions never occupy a slot. Total live sessions are
still bounded by ``size`` (idle sessions are evicted when a new egress identity
needs room). Idle sessions are validated before reuse — a context whose browser
disconnected, that was closed underneath us, or that exceeded the idle TTL /
max age (``idle_ttl_seconds`` / ``max_age_seconds``, config-overridable) is
retired rather than handed out.

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
from collections.abc import Sized
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple, cast

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

# Idle-session hygiene defaults (overridable via constructor / config):
# a session that has sat idle longer than ``idle_ttl`` or that is older than
# ``max_age`` is retired on the next checkout instead of being handed out.
DEFAULT_IDLE_TTL_SECONDS = 300.0
DEFAULT_MAX_AGE_SECONDS = 1800.0


def _seconds_setting(
    explicit: Optional[float],
    config: Any,
    attr: str,
    default: float,
) -> float:
    """Resolve a duration setting: explicit arg > ``getattr(config, attr)`` > default.

    Non-positive / unparsable values disable the bound (returned as ``0.0``).
    """
    value = explicit
    if value is None:
        value = getattr(config, attr, default) if config is not None else default
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return default
    return seconds if seconds > 0 else 0.0


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
    # Monotonic timestamp of the last ``release`` into the idle buckets (0.0
    # while in use). Drives the idle-TTL check at checkout.
    idle_since: float = 0.0


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
        idle_ttl_seconds: Optional[float] = None,
        max_age_seconds: Optional[float] = None,
        config: Any = None,
    ) -> None:
        self._factory = context_factory
        self._size = size
        self._max_solves = max_solves
        # Idle sessions must not hold permits: a permit is held only between
        # ``checkout`` and ``release`` (i.e. while a session is in use), so an
        # idle warm pool cannot starve new checkouts. Total live sessions
        # (in-use + idle + in-flight creations) is still bounded by ``size``
        # via the ``_creating`` / eviction accounting below.
        self._slots = asyncio.Semaphore(size)
        # Idle sessions bucketed by egress key: ``proxy.id`` for pool-proxy
        # sessions, ``"proxyless"`` for server-IP sessions. A checkout for a
        # given key only reuses sessions from that bucket, so a sticky proxy
        # identity is preserved across solves.
        self._idle: Dict[str, List[BrowserSession]] = {}
        self._in_use: Dict[str, BrowserSession] = {}
        # Sessions currently holding a semaphore permit (exactly-once release
        # guard, including cancellation during checkout).
        self._permit_holders: set = set()
        # Sessions whose context factory is currently running (they hold a
        # permit but are not registered in ``_in_use`` yet).
        self._creating = 0
        self._lock = asyncio.Lock()
        # Stale-session hygiene (read from config via ``getattr`` when a config
        # object is supplied; constructor kwargs win).
        self._idle_ttl_seconds = _seconds_setting(
            idle_ttl_seconds, config, "session_idle_ttl_seconds",
            DEFAULT_IDLE_TTL_SECONDS,
        )
        self._max_age_seconds = _seconds_setting(
            max_age_seconds, config, "session_max_age_seconds",
            DEFAULT_MAX_AGE_SECONDS,
        )

    async def checkout(
        self,
        *,
        key: str,
        proxy: Optional[ProxyAsset] = None,
        sitekey: Optional[str] = None,
    ) -> BrowserSession:
        """Return a warm idle session for ``key``, or create one (bounded by ``size``).

        Reuses the most recently idled session in the requested bucket when
        possible. When none is idle it waits for a free permit and builds a new
        session via the factory, so the number of concurrently live sessions
        never exceeds ``size``. The fingerprint is seeded by ``proxy.id`` when
        a proxy is given (so a sticky proxy keeps a stable coherent identity
        across re-warms); proxyless sessions draw from entropy.
        """
        del sitekey  # accepted for call-site compatibility; not used for selection
        # A permit is held only for the checkout→release lifetime. Acquiring it
        # first makes the cancellation window explicit: if this coroutine is
        # cancelled after the permit is granted but before the session is
        # handed off, the guard below releases the permit exactly once and
        # closes any context the factory already opened.
        await self._slots.acquire()
        permit_held = True
        created_context: Any = None
        creating = False
        try:
            while True:
                evicted: Optional[BrowserSession] = None
                create = False
                async with self._lock:
                    now = time.monotonic()
                    session = self._pop_idle(key)
                    if session is not None:
                        if self._session_usable(session, now):
                            # Reusing a warm idle session: the permit stays held
                            # until the caller releases it. Clear the idle clock
                            # at checkout so the time the session spends in use
                            # (a long solve) is never counted against the idle
                            # TTL — it restarts at the next release.
                            session.idle_since = 0.0
                            self._in_use[session.id] = session
                            self._permit_holders.add(session.id)
                            permit_held = False
                            return session
                        # Stale / disconnected / over-age: retire, don't hand out.
                        evicted = session
                    else:
                        live = (
                            len(self._in_use)
                            + self._creating
                            + self._idle_count()
                        )
                        if live < self._size:
                            # Room for a new session under the global bound.
                            self._creating += 1
                            creating = True
                            create = True
                        else:
                            # Buckets share the global size bound, so warming a
                            # new egress identity may retire an idle session
                            # from another bucket. With a permit in hand there
                            # is always one evictable unless accounting drifted;
                            # in that case the permit is the authoritative
                            # bound, so build rather than spin.
                            evicted = self._evict_one_idle()
                            if evicted is None:
                                self._creating += 1
                                creating = True
                                create = True
                if evicted is not None:
                    await _maybe_await_close(evicted.context)
                    evicted.warm = False
                    continue
                if not create:
                    continue

                # Build outside the lock: the factory is slow and must not
                # stall other checkouts.
                try:
                    # Seed by proxy id when given so a sticky proxy keeps a
                    # stable coherent identity; proxyless sessions draw from
                    # entropy. The proxy's exit-IP geo (timezone / locale)
                    # drives the fingerprint so a German residential IP presents
                    # Europe/Berlin + de-DE rather than en-US/New_York; ``None``
                    # falls back to a random coherent identity (no regression).
                    fingerprint = generate_fingerprint(
                        seed=proxy.id if proxy else None,
                        timezone_id=proxy.timezone if proxy else None,
                        locale=proxy.locale if proxy else None,
                        # P1-4: a mobile pool proxy gets an Android Chrome
                        # fingerprint.
                        mobile=bool(
                            proxy and getattr(proxy, "kind", None) == "mobile"
                        ),
                    )
                    context, user_agent = await self._factory(fingerprint, proxy)
                except BaseException:
                    self._creating = max(0, self._creating - 1)
                    creating = False
                    raise
                created_context = context
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
                    self._creating = max(0, self._creating - 1)
                    creating = False
                    self._in_use[session.id] = session
                    self._permit_holders.add(session.id)
                # Handoff complete: ownership of the permit (and context) has
                # moved to the caller / session.
                permit_held = False
                created_context = None
                return session
        except BaseException:
            # CancelledError (or any failure) after acquiring the permit but
            # before handoff. Clean up without double-releasing.
            if creating:
                self._creating = max(0, self._creating - 1)
            if created_context is not None:
                await _maybe_await_close(created_context)
            if permit_held:
                self._slots.release()
            raise

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

    def _idle_count(self) -> int:
        """Number of live idle sessions across all buckets."""
        return sum(len(bucket) for bucket in self._idle.values())

    def _session_expired(self, session: BrowserSession, now: float) -> bool:
        """True when contiguous idle time or absolute max age has lapsed.

        ``idle_since`` is cleared at checkout and re-stamped at release, so the
        idle-TTL arm measures only time spent *continuously idle*. In-use time
        (a long solve) is never counted against the idle TTL; ``max_age``
        remains anchored to ``created_at``.
        """
        if (
            self._idle_ttl_seconds
            and session.idle_since
            and now - session.idle_since > self._idle_ttl_seconds
        ):
            return True
        if (
            self._max_age_seconds
            and now - session.created_at > self._max_age_seconds
        ):
            return True
        return False

    def _session_usable(self, session: BrowserSession, now: float) -> bool:
        """Validate an idle session before handing it out.

        Cheap synchronous checks only (this runs under ``self._lock``): TTL /
        max age, then a liveness probe of the Playwright context. A closed
        context raises on property access (``pages``) or reports a disconnected
        browser (``browser.is_connected()``); either way the session is
        considered dead and is retired by the caller instead of handed out.
        Fakes without these attributes are treated as usable.
        """
        if self._session_expired(session, now):
            return False
        context = session.context
        if context is None:
            return False
        try:
            # ``pages`` is a property on a live Playwright context and raises
            # once the context has been closed.
            pages = getattr(context, "pages", None)
            if callable(pages):
                pages = pages()
            if pages is not None:
                # ``pages`` is a duck-typed Playwright reply; ``len`` is only
                # here to touch the property so a closed context raises.
                len(cast(Sized, pages))
        except Exception:  # noqa: BLE001 - a probe failure means "not usable"
            return False
        try:
            browser = getattr(context, "browser", None)
            if browser is not None:
                is_connected = getattr(browser, "is_connected", None)
                if callable(is_connected) and not is_connected():
                    return False
        except Exception:  # noqa: BLE001
            return False
        return True

    def _evict_one_idle(self) -> Optional[BrowserSession]:
        """Pop the *least valuable* idle session to free its slot for a new one.

        Buckets share the global ``size`` bound, so warming a new egress
        identity when every slot is held may require retiring an idle session
        bound to a different identity. Rather than evicting an arbitrary bucket
        (which could throw away a high-reputation, freshly-warmed session and
        keep a nearly-exhausted low-reputation one), pick the worst idle session
        across all buckets: lowest reputation first, then most solves (closest
        to ``max_solves``), then oldest. This preserves the warmest, most
        trusted sessions — the ones most likely to keep passing enterprise
        risk checks — under slot pressure.

        Returns ``None`` when nothing is idle (all slots held by in-use
        sessions). Must be called under ``self._lock``.
        """
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
        """Return a session to its bucket, updating its reputation.

        Each release counts as one completed solve. A session is retired (its
        context closed and its checkout permit freed so a replacement can be
        created lazily on the next ``checkout``) when it has reached
        ``max_solves`` OR its reputation has dropped below the eviction
        threshold (0.3) OR it has outlived the idle TTL / max age. A single
        solve failure no longer forces retirement: a fresh session (rep=1.0)
        survives one failure (1.0→0.6) and is only evicted after a second
        consecutive failure (0.6→0.2). Otherwise the session goes back to its
        egress-keyed warm idle bucket **without holding a permit** — permits
        bound only concurrent in-use sessions.

        ``burned`` is accepted for backward compat (callers such as
        ``browser_solver`` still pass ``burned=not solved``) but no longer
        forces retirement; reputation decay handles eviction.

        The session keeps its ``ProxyAsset`` reference for its whole lifetime;
        the caller (``browser_solver``) is responsible for ``proxy_pool.report``
        on each solve. This method releases the checkout permit exactly once
        (guarded by ``_permit_holders``) even if called twice.
        """
        async with self._lock:
            known = self._in_use.pop(session.id, None) is not None
            held = session.id in self._permit_holders
            if held:
                self._permit_holders.discard(session.id)
        if not known and not held:
            # Already retired / released: never double-release the permit.
            return

        session.solves += 1
        if success:
            session.reputation = min(1.0, session.reputation + 0.05)
        else:
            session.reputation = max(0.0, session.reputation - 0.4)

        now = time.monotonic()
        # ``idle_since`` is 0 while the session is in use (cleared at
        # checkout), so this expiry check only sees contiguous idle time plus
        # the absolute max age from ``created_at`` — a long solve is not
        # mistaken for a long idle period.
        retire = (
            session.solves >= self._max_solves
            or session.reputation < 0.3
            or self._session_expired(session, now)
        )
        if retire:
            try:
                await _maybe_await_close(session.context)
            finally:
                session.warm = False
                if held:
                    self._slots.release()
            return

        session.warm = True
        session.idle_since = now
        key = _bucket_key(session.proxy)
        try:
            async with self._lock:
                self._idle.setdefault(key, []).append(session)
                # Idle sessions hold no permit: hand it back once the session
                # is safely in the idle bucket (so a waiting checkout sees it).
                if held:
                    self._slots.release()
                    held = False
        except BaseException:
            # Cancelled while returning the session to idle: never leak the
            # permit, and keep the session tracked so close_all can retire its
            # context (it is no longer in ``_in_use``).
            if held:
                self._slots.release()
                held = False
            self._idle.setdefault(key, []).append(session)
            raise

    async def close_all(self) -> None:
        """Retire every session (idle and in-use) and free all permits.

        Only in-use sessions hold permits now; idle sessions are closed
        without touching the semaphore.
        """
        async with self._lock:
            sessions = list(self._in_use.values())
            for bucket in self._idle.values():
                sessions.extend(bucket)
            self._idle.clear()
            self._in_use.clear()
            permits = len(self._permit_holders)
            self._permit_holders.clear()
        for session in sessions:
            await _maybe_await_close(session.context)
            session.warm = False
        for _ in range(permits):
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
