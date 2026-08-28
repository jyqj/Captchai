"""Regressions for the indexed, load-aware proxy scheduler."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest

from src.assets.indexed_proxy_pool import (
    IndexedRedisProxyPool,
    ManagedProxyPool,
    ProxyPoolBusy,
    build_managed_proxy_pool,
)
from src.assets.proxy_pool import (
    ProxyAsset,
    ProxyPoolProtocol,
    RedisProxyPool,
)

_REDIS_URL = "redis://localhost:6379/0"


def _redis_helper() -> str:
    pytest.importorskip("redis.asyncio")
    sync_redis = pytest.importorskip("redis")
    try:
        client = sync_redis.from_url(
            _REDIS_URL,
            decode_responses=True,
        )
        client.ping()
        client.close()
    except Exception:
        pytest.skip("no reachable Redis server at localhost:6379")
    return _REDIS_URL


def _flush_prefix(prefix: str) -> None:
    import redis

    client = redis.from_url(_REDIS_URL, decode_responses=True)
    for key in client.scan_iter(f"{prefix}*"):
        client.delete(key)
    client.close()


def _proxy(
    proxy_id: str,
    *,
    sitekey_rate: tuple[int, int],
) -> ProxyAsset:
    return ProxyAsset(
        id=proxy_id,
        server=f"http://{proxy_id}:1",
        sitekey_stats={
            "site": [sitekey_rate[0], sitekey_rate[1]]
        },
    )


def test_managed_pool_spreads_load_then_returns_to_best_proxy() -> None:
    async def run() -> None:
        pool = ManagedProxyPool(sitekey_limit=8)
        pool.add(_proxy("good", sitekey_rate=(10, 0)))
        pool.add(_proxy("other", sitekey_rate=(6, 4)))

        first = await pool.checkout(sitekey="site")
        second = await pool.checkout(sitekey="site")
        assert first is not None and first.id == "good"
        assert second is not None and second.id == "other"

        rows = {row["id"]: row for row in pool.snapshot()}
        assert rows["good"]["in_flight"] == 1
        assert rows["other"]["in_flight"] == 1

        await pool.report(first.id, success=True)
        await pool.report(second.id, success=True)
        third = await pool.checkout(sitekey="site")
        assert third is not None and third.id == "good"
        await pool.report(third.id, success=True)
        await pool.close()

    asyncio.run(run())


def test_managed_pool_bounds_sitekey_history_across_buckets() -> None:
    async def run() -> None:
        pool = ManagedProxyPool(sitekey_limit=2)
        proxy = ProxyAsset(id="p", server="http://p:1")
        pool.add(proxy)

        await pool.report_sitekey("p", "old", success=True)
        await pool.report_sitekey_real(
            "p", "middle", success=False
        )
        await pool.report_sitekey("p", "new", success=True)

        row = pool.snapshot()[0]
        assert set(row["sitekeys"]) == {"new"}
        assert set(row["real_sitekeys"]) == {"middle"}
        assert "old" not in proxy.sitekey_stats
        await pool.close()

    asyncio.run(run())


def test_managed_pool_satisfies_existing_protocol() -> None:
    assert isinstance(ManagedProxyPool(), ProxyPoolProtocol)


class _BoundedCheckoutRedis:
    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate
        self.max_hmget = 0

    async def hgetall(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("checkout must not execute HGETALL")

    async def hmget(
        self,
        key: str,
        values: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        self.max_hmget = max(self.max_hmget, len(values))
        return await self._delegate.hmget(
            key, values, *args, **kwargs
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


class _NeverLocksRedis:
    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate

    async def set(self, *args: Any, **kwargs: Any) -> bool:
        return False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


def test_indexed_redis_checkout_is_bounded_and_never_hgetall() -> None:
    url = _redis_helper()
    prefix = "test:indexed:bounded:"

    async def run() -> None:
        _flush_prefix(prefix)
        pool = IndexedRedisProxyPool(
            url,
            key_prefix=prefix,
            candidate_window=4,
        )
        try:
            for index in range(24):
                pool.add(
                    ProxyAsset(
                        id=f"p-{index}",
                        server=f"http://p-{index}:1",
                    )
                )

            wrapped = _BoundedCheckoutRedis(pool._redis)
            pool._redis = wrapped
            chosen = await pool.checkout()
            assert chosen is not None
            assert wrapped.max_hmget <= 4
            await pool.report(chosen.id, success=True)
        finally:
            await pool.close()
            _flush_prefix(prefix)

    asyncio.run(run())


def test_indexed_redis_spreads_concurrent_leases() -> None:
    url = _redis_helper()
    prefix = "test:indexed:leases:"

    async def run() -> None:
        _flush_prefix(prefix)
        pool = IndexedRedisProxyPool(url, key_prefix=prefix)
        try:
            pool.add(_proxy("good", sitekey_rate=(10, 0)))
            pool.add(_proxy("other", sitekey_rate=(6, 4)))

            first = await pool.checkout(sitekey="site")
            second = await pool.checkout(sitekey="site")
            assert first is not None and first.id == "good"
            assert second is not None and second.id == "other"

            rows = {row["id"]: row for row in pool.snapshot()}
            assert rows["good"]["in_flight"] == 1
            assert rows["other"]["in_flight"] == 1

            await pool.report(first.id, success=True)
            await pool.report(second.id, success=True)
            assert all(
                row["in_flight"] == 0
                for row in pool.snapshot()
            )
        finally:
            await pool.close()
            _flush_prefix(prefix)

    asyncio.run(run())


def test_indexed_redis_lock_timeout_never_mutates_unlocked() -> None:
    url = _redis_helper()
    prefix = "test:indexed:lock:"

    async def run() -> None:
        _flush_prefix(prefix)
        pool = IndexedRedisProxyPool(
            url,
            key_prefix=prefix,
            lock_wait_seconds=0.05,
        )
        try:
            pool.add(ProxyAsset(id="p", server="http://p:1"))
            pool._redis = _NeverLocksRedis(pool._redis)

            with pytest.raises(ProxyPoolBusy, match="unlocked"):
                await pool.checkout()

            # Feedback is best-effort but still fail-closed: it is skipped
            # rather than written without ownership of the lock.
            await pool.report("p", success=False)
            row = pool.snapshot()[0]
            assert row["in_flight"] == 0
            assert row["fail_count"] == 0
            assert row["state"] == "healthy"
        finally:
            await pool.close()
            _flush_prefix(prefix)

    asyncio.run(run())


def test_indexed_redis_bounds_sitekey_history_and_indexes() -> None:
    url = _redis_helper()
    prefix = "test:indexed:sitekey-limit:"

    async def run() -> None:
        _flush_prefix(prefix)
        pool = IndexedRedisProxyPool(
            url,
            key_prefix=prefix,
            sitekey_limit=2,
        )
        try:
            pool.add(ProxyAsset(id="p", server="http://p:1"))
            await pool.report_sitekey("p", "old", success=True)
            await pool.report_sitekey_real(
                "p", "middle", success=False
            )
            await pool.report_sitekey("p", "new", success=True)

            row = pool.snapshot()[0]
            assert set(row["sitekeys"]) == {"new"}
            assert set(row["real_sitekeys"]) == {"middle"}
            assert (
                await pool._redis.zscore(
                    pool._sitekey_index_key("old"),
                    "p",
                )
                is None
            )
        finally:
            await pool.close()
            _flush_prefix(prefix)

    asyncio.run(run())


def test_indexed_redis_reconciles_and_compacts_legacy_hash() -> None:
    url = _redis_helper()
    prefix = "test:indexed:migration:"

    async def run() -> None:
        _flush_prefix(prefix)
        legacy = RedisProxyPool(url, key_prefix=prefix)
        try:
            legacy.add(
                ProxyAsset(
                    id="legacy",
                    server="http://legacy:1",
                    sitekey_stats={
                        "old": [1, 0],
                        "middle": [1, 0],
                        "new": [1, 0],
                    },
                )
            )
        finally:
            await legacy.close()

        pool = IndexedRedisProxyPool(
            url,
            key_prefix=prefix,
            sitekey_limit=2,
        )
        try:
            assert pool.has_available() is True
            row = pool.snapshot()[0]
            assert set(row["sitekeys"]) == {"middle", "new"}

            chosen = await pool.checkout(sitekey="new")
            assert chosen is not None and chosen.id == "legacy"
            await pool.report(chosen.id, success=True)
        finally:
            await pool.close()
            _flush_prefix(prefix)

    asyncio.run(run())


def test_indexed_redis_promotes_expired_cooldown_from_index() -> None:
    url = _redis_helper()
    prefix = "test:indexed:cooldown:"

    async def run() -> None:
        _flush_prefix(prefix)
        pool = IndexedRedisProxyPool(
            url,
            key_prefix=prefix,
            cooldown_seconds=60,
            max_consecutive_fails=1,
        )
        try:
            pool.add(ProxyAsset(id="p", server="http://p:1"))
            first = await pool.checkout()
            assert first is not None
            await pool.report("p", success=False)

            blob = await pool._redis.hget(pool._proxies_key, "p")
            assert blob is not None
            proxy = pool._deserialize(blob)
            proxy.cooldown_until = time.time() - 1
            await pool._redis.hset(
                pool._proxies_key,
                "p",
                pool._serialize(proxy),
            )
            await pool._redis.zadd(
                pool._cooldown_index_key,
                {"p": proxy.cooldown_until},
            )

            assert pool.has_available() is True
            chosen = await pool.checkout()
            assert chosen is not None and chosen.id == "p"
            await pool.report("p", success=True)
        finally:
            await pool.close()
            _flush_prefix(prefix)

    asyncio.run(run())


def test_managed_builder_selects_in_memory_backend() -> None:
    pool = build_managed_proxy_pool(
        SimpleNamespace(
            redis_url=None,
            proxy_cooldown=60,
            proxy_max_consecutive_fails=3,
            proxy_max_gb=0,
            solve_timeout=120,
        )
    )
    assert isinstance(pool, ManagedProxyPool)
