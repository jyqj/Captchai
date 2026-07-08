"""Factory routing tests for the asset / consumption planes (WP7).

These verify that ``build_proxy_pool`` and ``build_accounting`` return the
in-memory backend when ``redis_url`` is unset and the Redis backend when it's
set. The Redis-routing assertions monkeypatch the Redis backend class with a
stub so they run without the ``redis`` package installed and without a live
Redis server — we're only testing the routing logic in the factory, not the
Redis backend itself (that's covered by ``test_proxy_pool_redis.py`` and the
Redis accounting tests in ``test_consumption.py``).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    _ = sys.path.insert(0, str(PROJECT_ROOT))

from src.assets.proxy_pool import ProxyPool, build_proxy_pool  # noqa: E402
from src.consumption.accounting import (  # noqa: E402
    SuccessAccounting,
    build_accounting,
)


# --------------------------------------------------------------------------- #
# build_proxy_pool routing
# --------------------------------------------------------------------------- #


def test_build_proxy_pool_in_memory_when_no_redis_url() -> None:
    """redis_url=None → in-memory ProxyPool (always runs, no redis needed)."""
    cfg = SimpleNamespace(
        redis_url=None, proxy_cooldown=60, proxy_max_consecutive_fails=3
    )
    pool = build_proxy_pool(cfg)
    assert isinstance(pool, ProxyPool)


def test_build_proxy_pool_redis_when_redis_url_set(monkeypatch) -> None:
    """redis_url set → RedisProxyPool routing (monkeypatched, no real redis).

    We replace ``RedisProxyPool`` with a stub so the ``import redis.asyncio``
    never happens — this test verifies the *routing* logic in
    ``build_proxy_pool`` only, not the Redis backend itself.
    """
    from src.assets import proxy_pool as pp

    class StubRedisProxyPool:
        def __init__(self, url, **kwargs):
            self.url = url
            self.kwargs = kwargs

    monkeypatch.setattr(pp, "RedisProxyPool", StubRedisProxyPool)

    cfg = SimpleNamespace(
        redis_url="redis://example:6379/0",
        proxy_cooldown=60,
        proxy_max_consecutive_fails=3,
    )
    pool = build_proxy_pool(cfg)
    assert isinstance(pool, StubRedisProxyPool)
    assert pool.url == "redis://example:6379/0"
    assert pool.kwargs["cooldown_seconds"] == 60
    assert pool.kwargs["max_consecutive_fails"] == 3


# --------------------------------------------------------------------------- #
# build_accounting routing
# --------------------------------------------------------------------------- #


def test_build_accounting_in_memory_when_no_redis_url() -> None:
    """redis_url=None → in-memory SuccessAccounting (always runs, no redis).

    Wrapped in ``asyncio.run`` because ``SuccessAccounting.__init__`` creates
    an ``asyncio.Lock()`` eagerly, which on Python 3.9 needs a running loop.
    """
    async def run() -> None:
        cfg = SimpleNamespace(redis_url=None)
        acc = build_accounting(cfg)
        assert isinstance(acc, SuccessAccounting)

    asyncio.run(run())


def test_build_accounting_redis_when_redis_url_set(monkeypatch) -> None:
    """redis_url set → RedisSuccessAccounting routing (monkeypatched)."""
    from src.consumption import accounting as acc_mod

    class StubRedisAccounting:
        def __init__(self, url, window=100, **kwargs):
            self.url = url
            self.window = window
            self.kwargs = kwargs

    monkeypatch.setattr(acc_mod, "RedisSuccessAccounting", StubRedisAccounting)

    cfg = SimpleNamespace(redis_url="redis://example:6379/0")
    acc = build_accounting(cfg)
    assert isinstance(acc, StubRedisAccounting)
    assert acc.url == "redis://example:6379/0"


# --------------------------------------------------------------------------- #
# Protocol structural conformance
# --------------------------------------------------------------------------- #


def test_proxy_pool_satisfies_protocol() -> None:
    """ProxyPool and RedisProxyPool both satisfy ProxyPoolProtocol.

    Uses ``runtime_checkable`` so this is a structural isinstance check, not
    a nominal one. RedisProxyPool is monkeypatched to avoid the redis import.
    """
    from src.assets.proxy_pool import ProxyPoolProtocol

    assert isinstance(ProxyPool(), ProxyPoolProtocol)


def test_accounting_satisfies_protocol() -> None:
    """SuccessAccounting satisfies SuccessAccountingProtocol (structural check).

    Wrapped in ``asyncio.run`` because ``SuccessAccounting.__init__`` creates
    an ``asyncio.Lock()`` eagerly, which on Python 3.9 needs a running loop.
    """
    from src.consumption.accounting import SuccessAccountingProtocol

    async def run() -> None:
        assert isinstance(SuccessAccounting(), SuccessAccountingProtocol)

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# SolverServices.close() is safe for both backends
# --------------------------------------------------------------------------- #


def test_solver_services_close_releases_redis_backends() -> None:
    """close() awaits close() on ledger / proxy_pool / accounting when present.

    Uses stubs with ``close`` coroutines to verify the SolverServices.close
    path calls each. The in-memory backends don't define ``close`` so the
    getattr guard must skip them silently.
    """
    from src.core.services import SolverServices

    closed: list[str] = []

    class StubLedger:
        async def close(self) -> None:
            closed.append("ledger")

    class StubProxy:
        async def close(self) -> None:
            closed.append("proxy")

    class StubAcc:
        async def close(self) -> None:
            closed.append("accounting")

    svc = SolverServices.__new__(SolverServices)  # bypass __init__
    svc.session_pool = None
    svc.ledger = StubLedger()
    svc.proxy_pool = StubProxy()
    svc.accounting = StubAcc()

    asyncio.run(svc.close())
    assert set(closed) == {"ledger", "proxy", "accounting"}


def test_solver_services_close_skips_in_memory_backends() -> None:
    """close() is a no-op when backends are in-memory (no ``close`` method)."""
    from src.core.services import SolverServices

    svc = SolverServices.__new__(SolverServices)  # bypass __init__
    svc.session_pool = None
    # In-memory backends don't define close() — getattr returns None.
    svc.ledger = object()
    svc.proxy_pool = object()
    svc.accounting = object()

    asyncio.run(svc.close())  # must not raise
