"""Tests for the Redis-backed proxy pool (``RedisProxyPool``).

The suite uses a real Redis server when one is reachable at
``localhost:6379``. Otherwise it runs against an in-process ``fakeredis``
server shared by every async/sync client (``from_url`` is routed through a
fixture), so the tests actually execute instead of skipping. They skip cleanly
only when neither a server nor ``fakeredis`` is available.

``fakeredis`` without the ``lupa`` extra rejects ``EVAL`` — precisely the
scripting-less client the pool's pure-Python fallback exists for. Tests that
need real server-side Lua skip when ``lupa`` is absent; fallback tests force a
``ResponseError`` from a wrapper client instead.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import cast

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    _ = sys.path.insert(0, str(PROJECT_ROOT))

from src.assets.proxy_pool import ProxyAsset, build_proxy_pool  # noqa: E402


_REAL_REDIS_URL = "redis://localhost:6379/0"
_FAKE_REDIS_URL = "fakeredis://captchai-tests"
_ACTIVE_URL = _REAL_REDIS_URL
_REAL_AVAILABLE: "bool | None" = None
_FAKE_SERVER = None


def _real_server_available() -> bool:
    """Ping ``localhost:6379`` once and cache the answer.

    ``INFO`` is checked too: a minimal in-process mock listening on 6379
    answers PING but is not a real Redis (and its EVAL is a stub), so it must
    not take the tests off the fakeredis path.
    """
    global _REAL_AVAILABLE
    if _REAL_AVAILABLE is None:
        try:
            import redis as sync_redis

            client = sync_redis.from_url(
                _REAL_REDIS_URL,
                decode_responses=True,
                socket_connect_timeout=0.5,
                socket_timeout=0.5,
            )
            client.ping()
            info = client.info("server")
            client.close()
            _REAL_AVAILABLE = bool(info.get("redis_version"))
        except Exception:
            _REAL_AVAILABLE = False
    return _REAL_AVAILABLE


def _ensure_fake_server():
    global _FAKE_SERVER
    if _FAKE_SERVER is None:
        import fakeredis

        _FAKE_SERVER = fakeredis.FakeServer()
    return _FAKE_SERVER


def _redis_helper():
    """Return (aioredis, url): real server when reachable, else fakeredis."""
    global _ACTIVE_URL
    aioredis = pytest.importorskip("redis.asyncio")
    if _real_server_available():
        _ACTIVE_URL = _REAL_REDIS_URL
        return aioredis, _REAL_REDIS_URL
    pytest.importorskip("fakeredis")
    pytest.importorskip("fakeredis.aioredis")
    _ensure_fake_server()
    _ACTIVE_URL = _FAKE_REDIS_URL
    return aioredis, _FAKE_REDIS_URL


@pytest.fixture(autouse=True)
def _redis_backend(monkeypatch):
    """Route ``from_url('fakeredis://...')`` at one in-process fake server."""
    if _real_server_available():
        yield
        return
    try:
        import fakeredis
        import fakeredis.aioredis as fakeredis_async
        import redis as sync_redis
        import redis.asyncio as async_redis
    except ImportError:
        yield
        return
    server = _ensure_fake_server()
    # Fresh state per test so a failed test cannot leak into the next one.
    fakeredis.FakeRedis(server=server, decode_responses=True).flushall()
    orig_sync = sync_redis.from_url
    orig_async = async_redis.from_url

    def sync_from_url(url, **kwargs):
        if str(url).startswith("fakeredis://"):
            kwargs.setdefault("decode_responses", True)
            return fakeredis.FakeRedis(server=server, **kwargs)
        return orig_sync(url, **kwargs)

    def async_from_url(url, **kwargs):
        if str(url).startswith("fakeredis://"):
            kwargs.setdefault("decode_responses", True)
            return fakeredis_async.FakeRedis(server=server, **kwargs)
        return orig_async(url, **kwargs)

    monkeypatch.setattr(sync_redis, "from_url", sync_from_url)
    monkeypatch.setattr(async_redis, "from_url", async_from_url)
    yield


def _flush_prefix(prefix: str) -> None:
    """Synchronously flush test keys so tests don't see stale data."""
    import redis as sync_redis

    r = sync_redis.from_url(_ACTIVE_URL, decode_responses=True)
    for key in r.scan_iter(f"{prefix}*"):
        r.delete(key)
    r.close()


def _sync_client():
    """Sync client for the active backend (used by storage-shape assertions)."""
    import redis as sync_redis

    return sync_redis.from_url(_ACTIVE_URL, decode_responses=True)


def _require_lua_if_fake() -> None:
    """Skip Lua-path tests when fakeredis lacks its optional Lua engine."""
    if not _real_server_available():
        pytest.importorskip("lupa")


# --------------------------------------------------------------------------- #
# Basic lifecycle
# --------------------------------------------------------------------------- #


def test_redis_proxy_pool_checkout_returns_healthier_proxy() -> None:
    aioredis, url = _redis_helper()  # noqa: F841
    prefix = "test:proxy:checkout:"

    async def run() -> None:
        from src.assets.proxy_pool import RedisProxyPool

        _flush_prefix(prefix)
        pool = RedisProxyPool(
            url, cooldown_seconds=60, max_consecutive_fails=3, key_prefix=prefix
        )
        try:
            good = ProxyAsset(id="good", server="http://good:1")
            other = ProxyAsset(id="other", server="http://other:1")
            pool.add(good)
            pool.add(other)

            # Give "good" a strong per-sitekey record; "other" has none.
            await pool.report_sitekey("good", "site-1", success=True)
            await pool.report_sitekey("good", "site-1", success=True)
            await pool.report_sitekey("other", "site-1", success=False)

            chosen = await pool.checkout(sitekey="site-1")
            assert chosen is not None
            assert chosen.id == "good"
        finally:
            await pool.close()
            _flush_prefix(prefix)

    asyncio.run(run())


def test_redis_proxy_pool_checkout_returns_none_when_empty() -> None:
    aioredis, url = _redis_helper()  # noqa: F841
    prefix = "test:proxy:empty:"

    async def run() -> None:
        from src.assets.proxy_pool import RedisProxyPool

        _flush_prefix(prefix)
        pool = RedisProxyPool(url, key_prefix=prefix)
        try:
            assert await pool.checkout() is None
        finally:
            await pool.close()
            _flush_prefix(prefix)

    asyncio.run(run())


def test_redis_proxy_pool_report_updates_state_and_counters() -> None:
    aioredis, url = _redis_helper()  # noqa: F841
    prefix = "test:proxy:report:"

    async def run() -> None:
        from src.assets.proxy_pool import RedisProxyPool

        _flush_prefix(prefix)
        pool = RedisProxyPool(
            url, cooldown_seconds=60, max_consecutive_fails=3, key_prefix=prefix
        )
        try:
            proxy = ProxyAsset(id="p", server="http://p:1")
            pool.add(proxy)

            await pool.report("p", success=False, bytes_used=100)
            await pool.report("p", success=False, bytes_used=50)

            snap = pool.snapshot()
            assert len(snap) == 1
            row = snap[0]
            assert row["fail_count"] == 2
            assert row["consecutive_fails"] == 2
            assert row["bytes_used"] == 150
            assert row["state"] == "healthy"  # not yet at threshold (3)

            # Third failure triggers cooldown.
            await pool.report("p", success=False)
            snap = pool.snapshot()
            assert snap[0]["state"] == "cooldown"
            assert snap[0]["cooldown_remaining"] > 0

            # Success rehabilitates and resets the streak.
            await pool.report("p", success=True)
            snap = pool.snapshot()
            assert snap[0]["state"] == "healthy"
            assert snap[0]["consecutive_fails"] == 0
            assert snap[0]["success_count"] == 1
        finally:
            await pool.close()
            _flush_prefix(prefix)

    asyncio.run(run())


def test_redis_proxy_pool_cooldown_blocks_checkout() -> None:
    aioredis, url = _redis_helper()  # noqa: F841
    prefix = "test:proxy:cooldown:"

    async def run() -> None:
        from src.assets.proxy_pool import RedisProxyPool

        _flush_prefix(prefix)
        pool = RedisProxyPool(
            url, cooldown_seconds=60, max_consecutive_fails=2, key_prefix=prefix
        )
        try:
            proxy = ProxyAsset(id="p", server="http://p:1")
            pool.add(proxy)
            # Two failures → cooldown (max_consecutive_fails=2).
            await pool.report("p", success=False)
            await pool.report("p", success=False)
            assert await pool.checkout() is None
        finally:
            await pool.close()
            _flush_prefix(prefix)

    asyncio.run(run())


def test_redis_proxy_pool_report_sitekey_updates_per_sitekey_stats() -> None:
    aioredis, url = _redis_helper()  # noqa: F841
    prefix = "test:proxy:sitekey:"

    async def run() -> None:
        from src.assets.proxy_pool import RedisProxyPool

        _flush_prefix(prefix)
        pool = RedisProxyPool(url, key_prefix=prefix)
        try:
            proxy = ProxyAsset(id="p", server="http://p:1")
            pool.add(proxy)
            await pool.report_sitekey("p", "sk-a", success=True)
            await pool.report_sitekey("p", "sk-a", success=False)
            await pool.report_sitekey("p", "sk-b", success=True)

            snap = pool.snapshot()
            assert len(snap) == 1
            sitekeys = snap[0]["sitekeys"]
            assert sitekeys["sk-a"] == {"success": 1, "fail": 1}
            assert sitekeys["sk-b"] == {"success": 1, "fail": 0}
        finally:
            await pool.close()
            _flush_prefix(prefix)

    asyncio.run(run())


def test_redis_proxy_pool_report_sitekey_real_separate_bucket() -> None:
    """report_sitekey and report_sitekey_real write to separate buckets."""
    aioredis, url = _redis_helper()  # noqa: F841
    prefix = "test:proxy:realsk:"

    async def run() -> None:
        from src.assets.proxy_pool import RedisProxyPool

        _flush_prefix(prefix)
        pool = RedisProxyPool(url, key_prefix=prefix)
        try:
            proxy = ProxyAsset(id="p", server="http://p:1")
            pool.add(proxy)
            await pool.report_sitekey("p", "sk-1", success=True)
            await pool.report_sitekey_real("p", "sk-1", success=False)

            snap = pool.snapshot()
            assert len(snap) == 1
            row = snap[0]
            assert row["sitekeys"] == {"sk-1": {"success": 1, "fail": 0}}
            assert row["real_sitekeys"] == {"sk-1": {"success": 0, "fail": 1}}
        finally:
            await pool.close()
            _flush_prefix(prefix)

    asyncio.run(run())


def test_redis_proxy_pool_checkout_prefers_real_over_token() -> None:
    """checkout ranks by the real bucket when present, else falls back to token."""
    aioredis, url = _redis_helper()  # noqa: F841
    prefix = "test:proxy:realrank:"

    async def run() -> None:
        from src.assets.proxy_pool import RedisProxyPool

        _flush_prefix(prefix)
        pool = RedisProxyPool(url, key_prefix=prefix)
        try:
            real_good = ProxyAsset(id="real-good", server="http://rg:1")
            real_bad = ProxyAsset(id="real-bad", server="http://rb:1")
            pool.add(real_good)
            pool.add(real_bad)
            # Identical token-obtained records...
            await pool.report_sitekey("real-good", "sk-1", success=True)
            await pool.report_sitekey("real-bad", "sk-1", success=True)
            # ...but divergent real outcomes.
            await pool.report_sitekey_real("real-good", "sk-1", success=True)
            await pool.report_sitekey_real("real-bad", "sk-1", success=False)

            chosen = await pool.checkout(sitekey="sk-1")
            assert chosen is not None
            assert chosen.id == "real-good"
        finally:
            await pool.close()
            _flush_prefix(prefix)

    asyncio.run(run())


def test_redis_proxy_pool_checkout_falls_back_to_token_when_no_real() -> None:
    """With no real-outcome data, checkout falls back to the token-obtained rate."""
    aioredis, url = _redis_helper()  # noqa: F841
    prefix = "test:proxy:tokenfallback:"

    async def run() -> None:
        from src.assets.proxy_pool import RedisProxyPool

        _flush_prefix(prefix)
        pool = RedisProxyPool(url, key_prefix=prefix)
        try:
            good = ProxyAsset(id="good", server="http://good:1")
            other = ProxyAsset(id="other", server="http://other:1")
            pool.add(good)
            pool.add(other)
            await pool.report_sitekey("good", "sk-1", success=True)
            await pool.report_sitekey("other", "sk-1", success=False)

            chosen = await pool.checkout(sitekey="sk-1")
            assert chosen is not None
            assert chosen.id == "good"
        finally:
            await pool.close()
            _flush_prefix(prefix)

    asyncio.run(run())


def test_redis_proxy_pool_snapshot_includes_real_sitekeys() -> None:
    aioredis, url = _redis_helper()  # noqa: F841
    prefix = "test:proxy:realsnap:"

    async def run() -> None:
        from src.assets.proxy_pool import RedisProxyPool

        _flush_prefix(prefix)
        pool = RedisProxyPool(url, key_prefix=prefix)
        try:
            proxy = ProxyAsset(id="p", server="http://p:1")
            pool.add(proxy)
            await pool.report_sitekey("p", "sk-1", success=True)
            await pool.report_sitekey_real("p", "sk-1", success=False)
            await pool.report_sitekey_real("p", "sk-2", success=True)

            snap = pool.snapshot()
            assert len(snap) == 1
            row = snap[0]
            assert row["sitekeys"] == {"sk-1": {"success": 1, "fail": 0}}
            assert row["real_sitekeys"] == {
                "sk-1": {"success": 0, "fail": 1},
                "sk-2": {"success": 1, "fail": 0},
            }
        finally:
            await pool.close()
            _flush_prefix(prefix)

    asyncio.run(run())


def test_redis_proxy_pool_real_sitekey_stats_survive_reconnect() -> None:
    """real_sitekey_stats round-trip through Redis across pool restarts."""
    aioredis, url = _redis_helper()  # noqa: F841
    prefix = "test:proxy:realrestart:"

    async def run() -> None:
        from src.assets.proxy_pool import RedisProxyPool

        _flush_prefix(prefix)
        pool1 = RedisProxyPool(url, key_prefix=prefix)
        try:
            pool1.add(ProxyAsset(id="p", server="http://p:1"))
            await pool1.report_sitekey_real("p", "sk", success=True)
            await pool1.report_sitekey_real("p", "sk", success=False)
        finally:
            await pool1.close()

        pool2 = RedisProxyPool(url, key_prefix=prefix)
        try:
            snap = pool2.snapshot()
            assert len(snap) == 1
            assert snap[0]["real_sitekeys"]["sk"] == {"success": 1, "fail": 1}
            # Token bucket stays empty — the two buckets persist independently.
            assert snap[0]["sitekeys"] == {}
        finally:
            await pool2.close()
            _flush_prefix(prefix)

    asyncio.run(run())


def test_redis_proxy_pool_snapshot_reflects_state() -> None:
    aioredis, url = _redis_helper()  # noqa: F841
    prefix = "test:proxy:snapshot:"

    async def run() -> None:
        from src.assets.proxy_pool import RedisProxyPool

        _flush_prefix(prefix)
        pool = RedisProxyPool(url, key_prefix=prefix)
        try:
            pool.add(
                ProxyAsset(
                    id="p",
                    server="http://p:1",
                    kind="residential",
                    country="DE",
                    timezone="Europe/Berlin",
                    locale="de-DE",
                )
            )
            snap = pool.snapshot()
            assert len(snap) == 1
            row = snap[0]
            assert row["id"] == "p"
            assert row["kind"] == "residential"
            assert row["country"] == "DE"
            assert row["timezone"] == "Europe/Berlin"
            assert row["locale"] == "de-DE"
            assert "success_rate" in row
            assert "state" in row
        finally:
            await pool.close()
            _flush_prefix(prefix)

    asyncio.run(run())


def test_redis_proxy_pool_has_available_sync_peek() -> None:
    aioredis, url = _redis_helper()  # noqa: F841
    prefix = "test:proxy:has:"

    async def run() -> None:
        from src.assets.proxy_pool import RedisProxyPool

        _flush_prefix(prefix)
        pool = RedisProxyPool(
            url, cooldown_seconds=60, max_consecutive_fails=2, key_prefix=prefix
        )
        try:
            # Empty pool → False.
            assert pool.has_available() is False

            pool.add(ProxyAsset(id="p1", server="http://p1:1", kind="residential"))
            pool.add(ProxyAsset(id="p2", server="http://p2:1", kind="datacenter"))
            assert pool.has_available() is True
            # kind filter
            assert pool.has_available(kind="residential") is True
            assert pool.has_available(kind="mobile") is False

            # Burn p1 via two failures (max_consecutive_fails=2 → cooldown).
            await pool.report("p1", success=False)
            await pool.report("p1", success=False)
            # p2 still available.
            assert pool.has_available() is True
            assert pool.has_available(kind="residential") is False
        finally:
            await pool.close()
            _flush_prefix(prefix)

    asyncio.run(run())


def test_redis_proxy_pool_kind_filter() -> None:
    aioredis, url = _redis_helper()  # noqa: F841
    prefix = "test:proxy:kind:"

    async def run() -> None:
        from src.assets.proxy_pool import RedisProxyPool

        _flush_prefix(prefix)
        pool = RedisProxyPool(url, key_prefix=prefix)
        try:
            pool.add(ProxyAsset(id="dc", server="http://dc:1", kind="datacenter"))
            pool.add(ProxyAsset(id="res", server="http://res:1", kind="residential"))
            chosen = await pool.checkout(kind="residential")
            assert chosen is not None
            assert chosen.id == "res"
            assert chosen.kind == "residential"
        finally:
            await pool.close()
            _flush_prefix(prefix)

    asyncio.run(run())


def test_redis_proxy_pool_kind_list_filter() -> None:
    """Fix #2: RedisProxyPool.checkout accepts a kind list (residential OR mobile)."""
    aioredis, url = _redis_helper()  # noqa: F841
    prefix = "test:proxy:kindlist:"

    async def run() -> None:
        from src.assets.proxy_pool import RedisProxyPool

        _flush_prefix(prefix)
        pool = RedisProxyPool(url, key_prefix=prefix)
        try:
            pool.add(ProxyAsset(id="dc", server="http://dc:1", kind="datacenter"))
            pool.add(ProxyAsset(id="mob", server="http://mob:1", kind="mobile"))
            # List form: residential OR mobile → selects the mobile proxy.
            chosen = await pool.checkout(kind=["residential", "mobile"])
            assert chosen is not None
            assert chosen.id == "mob"
            assert chosen.kind == "mobile"
            # Tuple form is also accepted.
            chosen_t = await pool.checkout(kind=("residential", "mobile"))
            assert chosen_t is not None
            assert chosen_t.kind == "mobile"
            # Only-datacenter pool → None for the residential+mobile list.
            pool2 = RedisProxyPool(url, key_prefix=prefix + "2")
            try:
                pool2.add(
                    ProxyAsset(id="dc2", server="http://dc2:1", kind="datacenter")
                )
                assert (
                    await pool2.checkout(kind=["residential", "mobile"]) is None
                )
            finally:
                await pool2.close()
                _flush_prefix(prefix + "2")
            # has_available also honours the kind list.
            assert pool.has_available(kind=["residential", "mobile"]) is True
            # This pool holds a datacenter + a mobile proxy, so a list that
            # matches neither (residential only) returns False; a list that
            # includes datacenter matches the dc proxy and returns True.
            assert pool.has_available(kind=["residential"]) is False
            assert pool.has_available(kind=["residential", "datacenter"]) is True
        finally:
            await pool.close()
            _flush_prefix(prefix)

    asyncio.run(run())


def test_redis_proxy_pool_survives_reconnect() -> None:
    """A new pool pointing at the same Redis sees the persisted state."""
    aioredis, url = _redis_helper()  # noqa: F841
    prefix = "test:proxy:restart:"

    async def run() -> None:
        from src.assets.proxy_pool import RedisProxyPool

        _flush_prefix(prefix)
        pool1 = RedisProxyPool(url, key_prefix=prefix)
        try:
            pool1.add(ProxyAsset(id="p", server="http://p:1"))
            await pool1.report_sitekey("p", "sk", success=True)
            await pool1.report("p", success=True)
        finally:
            await pool1.close()

        # New instance, same Redis → state survives.
        pool2 = RedisProxyPool(url, key_prefix=prefix)
        try:
            snap = pool2.snapshot()
            assert len(snap) == 1
            assert snap[0]["success_count"] == 1
            assert snap[0]["sitekeys"]["sk"] == {"success": 1, "fail": 0}
        finally:
            await pool2.close()
            _flush_prefix(prefix)

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# build_proxy_pool factory routing
# --------------------------------------------------------------------------- #


def test_build_proxy_pool_selects_redis_when_configured() -> None:
    aioredis, url = _redis_helper()  # noqa: F841
    prefix = "test:proxy:build:"

    async def run() -> None:
        from types import SimpleNamespace

        from src.assets.proxy_pool import ProxyPool, RedisProxyPool

        # No redis_url → in-memory ProxyPool.
        cfg_mem = SimpleNamespace(
            redis_url=None, proxy_cooldown=120, proxy_max_consecutive_fails=3
        )
        assert isinstance(build_proxy_pool(cfg_mem), ProxyPool)

        # redis_url set → RedisProxyPool.
        cfg_redis = SimpleNamespace(
            redis_url=url, proxy_cooldown=120, proxy_max_consecutive_fails=3
        )
        pool = build_proxy_pool(cfg_redis)
        assert isinstance(pool, RedisProxyPool)
        await pool.close()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# Lua checkout / EVAL fallback / new storage layout
# --------------------------------------------------------------------------- #


def _response_error(message: str) -> Exception:
    from redis.exceptions import ResponseError

    return ResponseError(message)


class _NoEvalPipeline:
    """Pipeline wrapper whose ``eval`` behaves like a scripting-less Redis."""

    def __init__(self, inner) -> None:
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def eval(self, *args, **kwargs):
        raise _response_error("unknown command 'eval'")


class _NoEvalRedis:
    """Async client wrapper that rejects EVAL, like fakeredis without lupa."""

    def __init__(self, inner) -> None:
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def pipeline(self, *args, **kwargs):
        return _NoEvalPipeline(self._inner.pipeline(*args, **kwargs))

    async def eval(self, *args, **kwargs):
        raise _response_error("unknown command 'eval'")


def test_lua_checkout_reserves_distinct_proxies_under_concurrency() -> None:
    """EVAL checkout is atomic: concurrent callers reserve different proxies."""
    _aioredis, url = _redis_helper()  # noqa: F841
    _require_lua_if_fake()
    prefix = "test:proxy:lua:reserve:"

    async def run() -> None:
        from src.assets.proxy_pool import RedisProxyPool

        _flush_prefix(prefix)
        pool = RedisProxyPool(url, key_prefix=prefix)
        try:
            for index in range(4):
                pool.add(
                    ProxyAsset(id=f"p{index}", server=f"http://p{index}:1")
                )
            chosen = await asyncio.gather(*(pool.checkout() for _ in range(4)))
            assert all(proxy is not None for proxy in chosen)
            chosen_proxies = cast("list[ProxyAsset]", chosen)
            assert {proxy.id for proxy in chosen_proxies} == {"p0", "p1", "p2", "p3"}
            # The script path was actually used (no EVAL rejection logged).
            assert pool._eval_supported is True
            # Reservation is persisted in the per-proxy hash.
            row = _sync_client().hgetall(pool._proxy_prefix + "p0")
            assert row["id"] == "p0"
            assert float(row["last_used_at"]) > 0
        finally:
            await pool.close()
            _flush_prefix(prefix)

    asyncio.run(run())


def test_eval_rejection_falls_back_to_python_checkout_and_report() -> None:
    """A client that rejects EVAL keeps working through the Python fallback."""
    _aioredis, url = _redis_helper()  # noqa: F841
    prefix = "test:proxy:fallback:"

    async def run() -> None:
        from src.assets.proxy_pool import RedisProxyPool

        _flush_prefix(prefix)
        pool = RedisProxyPool(
            url, cooldown_seconds=60, max_consecutive_fails=3, key_prefix=prefix
        )
        try:
            pool.add(ProxyAsset(id="good", server="http://good:1"))
            pool.add(ProxyAsset(id="other", server="http://other:1"))
            # Sitekey ranking still works through the fallback path.
            await pool.report_sitekey("good", "sk", success=True)
            await pool.report_sitekey("other", "sk", success=False)

            # Test wrapper mimics Redis' async surface but isn't a Redis subclass.
            pool._redis = _NoEvalRedis(pool._redis)  # type: ignore[reportAttributeAccessIssue]
            chosen = await pool.checkout(sitekey="sk")
            assert chosen is not None
            assert chosen.id == "good"
            assert pool._eval_supported is False

            # report/report_bytes take the locked branch and stay correct.
            await pool.report("good", success=False, bytes_used=25)
            await pool.report("good", success=False, bytes_used=25)
            row = next(r for r in pool.snapshot() if r["id"] == "good")
            assert row["fail_count"] == 2
            assert row["consecutive_fails"] == 2
            assert row["bytes_used"] == 50
            # The checkout before this reserved a fresh proxy (0 successes),
            # which transitions it to "probing"; two failures stay below the
            # three-failure cooldown threshold.
            assert row["state"] == "probing"

            await pool.report("good", success=False)
            row = next(r for r in pool.snapshot() if r["id"] == "good")
            assert row["state"] == "cooldown"
            assert row["cooldown_remaining"] > 0

            # report_bytes also works without scripting.
            await pool.report_bytes("other", 10)
            row = next(r for r in pool.snapshot() if r["id"] == "other")
            assert row["bytes_used"] == 10
        finally:
            await pool.close()
            _flush_prefix(prefix)

    asyncio.run(run())


def test_report_bytes_burns_proxy_at_cap() -> None:
    """Byte accounting increments one field and burns at the configured cap."""
    _aioredis, url = _redis_helper()  # noqa: F841
    prefix = "test:proxy:bytes:"

    async def run() -> None:
        from src.assets.proxy_pool import RedisProxyPool

        _flush_prefix(prefix)
        pool = RedisProxyPool(
            url, max_bytes_per_proxy=100, key_prefix=prefix
        )
        try:
            pool.add(ProxyAsset(id="p", server="http://p:1"))
            await pool.report_bytes("p", 60)
            row = pool.snapshot()[0]
            assert row["bytes_used"] == 60
            assert row["state"] == "healthy"

            await pool.report_bytes("p", 50)
            row = pool.snapshot()[0]
            assert row["bytes_used"] == 110
            assert row["state"] == "burned"

            assert await pool.checkout() is None
        finally:
            await pool.close()
            _flush_prefix(prefix)

    asyncio.run(run())


def test_storage_uses_per_proxy_hash_and_ttl_sitekey_keys() -> None:
    """Storage is per-field hashes + TTL'd per-sitekey keys, not JSON blobs."""
    _aioredis, url = _redis_helper()  # noqa: F841
    prefix = "test:proxy:shape:"

    async def run() -> None:
        from src.assets.proxy_pool import RedisProxyPool

        _flush_prefix(prefix)
        pool = RedisProxyPool(url, key_prefix=prefix)
        try:
            pool.add(
                ProxyAsset(
                    id="p",
                    server="http://p:1",
                    username="user",
                    password="pass",
                )
            )
            await pool.report_sitekey("p", "sk-a", success=True)
            await pool.report_sitekey_real("p", "sk-a", success=False)

            client = _sync_client()
            assert client.sismember(pool._ids_key, "p")
            fields = client.hgetall(pool._proxy_prefix + "p")
            assert fields["server"] == "http://p:1"
            assert fields["username"] == "user"
            assert fields["success_count"] == "0"

            # The legacy JSON-blob hash is no longer written.
            assert client.hget(pool._proxies_key, "p") is None

            token_key = pool._sitekey_prefix + "p:sk-a"
            real_key = pool._real_sitekey_prefix + "p:sk-a"
            assert client.hget(token_key, "ok") == "1"
            assert client.hget(real_key, "fail") == "1"
            assert client.ttl(token_key) > 0
            assert client.ttl(real_key) > 0
            assert client.ttl(pool._sitekey_zset_prefix + "p") > 0
            assert client.ttl(pool._real_sitekey_zset_prefix + "p") > 0
        finally:
            await pool.close()
            _flush_prefix(prefix)

    asyncio.run(run())


def test_sitekey_cardinality_is_bounded_by_top_n() -> None:
    """Only the ``max_sitekeys_per_proxy`` most active sitekeys survive."""
    _aioredis, url = _redis_helper()  # noqa: F841
    prefix = "test:proxy:sitekeycap:"

    async def run() -> None:
        from src.assets.proxy_pool import RedisProxyPool

        _flush_prefix(prefix)
        pool = RedisProxyPool(
            url, key_prefix=prefix, max_sitekeys_per_proxy=3
        )
        try:
            pool.add(ProxyAsset(id="p", server="http://p:1"))
            # Activity scores: sk-0=1, sk-1=2, sk-2=3, sk-3=4.
            for index in range(4):
                for _ in range(index + 1):
                    await pool.report_sitekey(
                        "p", f"sk-{index}", success=True
                    )

            sitekeys = pool.snapshot()[0]["sitekeys"]
            assert set(sitekeys) == {"sk-1", "sk-2", "sk-3"}
            client = _sync_client()
            assert client.exists(pool._sitekey_prefix + "p:sk-0") == 0
            for sitekey in ("sk-1", "sk-2", "sk-3"):
                key = pool._sitekey_prefix + f"p:{sitekey}"
                assert client.ttl(key) > 0
        finally:
            await pool.close()
            _flush_prefix(prefix)

    asyncio.run(run())


def test_add_migrates_legacy_json_blob_health() -> None:
    """The first add() after an upgrade keeps health from the JSON blob."""
    _aioredis, url = _redis_helper()  # noqa: F841
    prefix = "test:proxy:legacy:"

    async def run() -> None:
        from src.assets.proxy_pool import RedisProxyPool

        _flush_prefix(prefix)
        pool = RedisProxyPool(url, key_prefix=prefix)
        try:
            client = _sync_client()
            legacy = ProxyAsset(
                id="p",
                server="http://p:1",
                success_count=7,
                fail_count=2,
                consecutive_fails=2,
                state="cooldown",
                cooldown_until=time.time() + 30,
                sitekey_stats={"sk": [3, 1]},
            )
            client.hset(pool._proxies_key, "p", pool._serialize(legacy))

            pool.add(ProxyAsset(id="p", server="http://p:1"))

            fields = client.hgetall(pool._proxy_prefix + "p")
            assert fields["success_count"] == "7"
            assert fields["fail_count"] == "2"
            assert fields["consecutive_fails"] == "2"
            assert fields["state"] == "cooldown"
            # The migrated entry is a real hash key now.
            assert client.type(pool._proxy_prefix + "p") == "hash"
            assert client.sismember(pool._ids_key, "p")
            # Legacy per-sitekey stats are re-seeded into their own key.
            assert pool.snapshot()[0]["sitekeys"] == {
                "sk": {"success": 3, "fail": 1}
            }
        finally:
            await pool.close()
            _flush_prefix(prefix)

    asyncio.run(run())
