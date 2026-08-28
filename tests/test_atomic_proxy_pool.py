"""Regressions for atomic proxy checkout and async inventory snapshots."""

from __future__ import annotations

import asyncio
from collections import Counter
from types import SimpleNamespace
from typing import Any

import pytest

from src.api import routes as routes_module
from src.assets.atomic_proxy_pool import (
    AtomicManagedProxyPool,
    AtomicRedisProxyPool,
    ProxyPoolBusy,
    build_atomic_proxy_pool,
    snapshot_proxy_pool,
)
from src.assets.proxy_pool import ProxyAsset, ProxyPoolProtocol

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


def _proxy(proxy_id: str) -> ProxyAsset:
    return ProxyAsset(
        id=proxy_id,
        server=f"http://{proxy_id}:1",
        success_count=10,
        sitekey_stats={"site": [10, 0]},
    )


class _NoClientSideMutationRedis:
    """Checkout may read indexes and EVAL, but not mutate in Python."""

    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate
        self.eval_calls = 0
        self.candidate_counts: list[int] = []

    async def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: Any,
    ) -> Any:
        self.eval_calls += 1
        args = keys_and_args[numkeys:]
        self.candidate_counts.append(int(args[5]))
        return await self._delegate.eval(script, numkeys, *keys_and_args)

    async def set(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("checkout must not acquire the legacy global lock")

    async def hget(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("candidate blobs must be read inside the Lua script")

    async def hmget(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("candidate blobs must be read inside the Lua script")

    async def hset(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("checkout writes must be committed by Lua")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


class _NoSyncSnapshotRedis:
    def hgetall(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("async admin snapshots must not call sync HGETALL")

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"unexpected sync Redis call: {name}")


def test_atomic_redis_checkout_is_one_server_side_commit() -> None:
    url = _redis_helper()
    prefix = "test:atomic:commit:"

    async def run() -> None:
        _flush_prefix(prefix)
        pool = AtomicRedisProxyPool(
            url,
            key_prefix=prefix,
            candidate_window=8,
        )
        delegate = pool._redis
        try:
            for index in range(16):
                pool.add(_proxy(f"p-{index}"))

            wrapped = _NoClientSideMutationRedis(delegate)
            pool._redis = wrapped
            chosen = await pool.checkout(sitekey="site")
            assert chosen is not None
            assert wrapped.eval_calls == 1
            assert len(wrapped.candidate_counts) == 1
            assert 0 < wrapped.candidate_counts[0] <= 8

            # Restore the real client because feedback intentionally still
            # observes the compatibility lock during this migration phase.
            pool._redis = delegate
            await pool.report(chosen.id, success=True)
        finally:
            pool._redis = delegate
            await pool.close()
            _flush_prefix(prefix)

    asyncio.run(run())


def test_atomic_checkout_preserves_empty_json_maps() -> None:
    url = _redis_helper()
    prefix = "test:atomic:empty-maps:"

    async def run() -> None:
        import json

        _flush_prefix(prefix)
        pool = AtomicRedisProxyPool(url, key_prefix=prefix)
        try:
            pool.add(ProxyAsset(id="p", server="http://p:1"))
            chosen = await pool.checkout()
            assert chosen is not None
            blob = await pool._redis.hget(pool._proxies_key, "p")
            assert blob is not None
            payload = json.loads(blob)
            assert payload["sitekey_stats"] == {}
            assert payload["real_sitekey_stats"] == {}
            await pool.report("p", success=True)
        finally:
            await pool.close()
            _flush_prefix(prefix)

    asyncio.run(run())


def test_atomic_redis_concurrent_checkouts_balance_identical_proxies() -> None:
    url = _redis_helper()
    prefix = "test:atomic:balance:"

    async def run() -> None:
        _flush_prefix(prefix)
        pool = AtomicRedisProxyPool(
            url,
            key_prefix=prefix,
            candidate_window=8,
        )
        try:
            for proxy_id in ("a", "b", "c", "d"):
                pool.add(_proxy(proxy_id))

            chosen = await asyncio.gather(
                *(pool.checkout(sitekey="site") for _ in range(20))
            )
            ids = [proxy.id for proxy in chosen if proxy is not None]
            assert len(ids) == 20
            counts = Counter(ids)
            assert set(counts) == {"a", "b", "c", "d"}
            assert max(counts.values()) - min(counts.values()) <= 1

            rows = {row["id"]: row for row in await pool.snapshot_async()}
            assert sum(row["in_flight"] for row in rows.values()) == 20
            for proxy_id in ids:
                await pool.report(proxy_id, success=True)
        finally:
            await pool.close()
            _flush_prefix(prefix)

    asyncio.run(run())


def test_atomic_checkout_respects_inflight_feedback_lock() -> None:
    url = _redis_helper()
    prefix = "test:atomic:compat-lock:"

    async def run() -> None:
        _flush_prefix(prefix)
        pool = AtomicRedisProxyPool(
            url,
            key_prefix=prefix,
            lock_wait_seconds=0.08,
        )
        try:
            pool.add(_proxy("p"))
            await pool._redis.set(pool._lock_key, "report-owner", ex=2)
            with pytest.raises(ProxyPoolBusy, match="atomic checkout"):
                await pool.checkout(sitekey="site")

            assert int(await pool._redis.zcard(pool._lease_key("p"))) == 0
            await pool._redis.delete(pool._lock_key)
            chosen = await pool.checkout(sitekey="site")
            assert chosen is not None and chosen.id == "p"
            await pool.report("p", success=True)
        finally:
            await pool.close()
            _flush_prefix(prefix)

    asyncio.run(run())


def test_atomic_checkout_repairs_corrupt_candidate_inside_script() -> None:
    url = _redis_helper()
    prefix = "test:atomic:repair:"

    async def run() -> None:
        _flush_prefix(prefix)
        pool = AtomicRedisProxyPool(url, key_prefix=prefix)
        try:
            pool.add(_proxy("good"))
            await pool._redis.hset(pool._proxies_key, "broken", "{not-json")
            await pool._redis.zadd(pool._active_all_key, {"broken": 10_000})
            await pool._redis.zadd(
                pool._active_kind_key("datacenter"),
                {"broken": 10_000},
            )

            chosen = await pool.checkout()
            assert chosen is not None and chosen.id == "good"
            assert await pool._redis.hget(pool._proxies_key, "broken") is None
            assert (
                await pool._redis.zscore(pool._active_all_key, "broken")
                is None
            )
            await pool.report("good", success=True)
        finally:
            await pool.close()
            _flush_prefix(prefix)

    asyncio.run(run())


def test_atomic_checkout_rehabilitates_expired_cooldown_with_active_peer() -> None:
    url = _redis_helper()
    prefix = "test:atomic:rehabilitate:"

    async def run() -> None:
        import time

        _flush_prefix(prefix)
        pool = AtomicRedisProxyPool(
            url,
            key_prefix=prefix,
            candidate_window=4,
        )
        try:
            pool.add(
                ProxyAsset(
                    id="active",
                    server="http://active:1",
                    success_count=1,
                    fail_count=9,
                    sitekey_stats={"site": [1, 9]},
                )
            )
            pool.add(
                ProxyAsset(
                    id="recovering",
                    server="http://recovering:1",
                    state="cooldown",
                    cooldown_until=time.time() + 60,
                    success_count=10,
                    sitekey_stats={"site": [10, 0]},
                )
            )

            blob = await pool._redis.hget(
                pool._proxies_key, "recovering"
            )
            assert blob is not None
            recovering = pool._deserialize(blob)
            recovering.cooldown_until = time.time() - 1
            await pool._redis.hset(
                pool._proxies_key,
                "recovering",
                pool._serialize(recovering),
            )
            await pool._redis.zadd(
                pool._cooldown_index_key,
                {"recovering": recovering.cooldown_until},
            )

            # The active index is deliberately non-empty. Checkout must still
            # reserve a bounded rehabilitation slot for the expired asset.
            chosen = await pool.checkout(sitekey="site")
            assert chosen is not None and chosen.id == "recovering"
            await pool.report(chosen.id, success=True)
        finally:
            await pool.close()
            _flush_prefix(prefix)

    asyncio.run(run())


def test_async_snapshot_uses_hscan_and_never_sync_hgetall() -> None:
    url = _redis_helper()
    prefix = "test:atomic:snapshot:"

    async def run() -> None:
        _flush_prefix(prefix)
        pool = AtomicRedisProxyPool(
            url,
            key_prefix=prefix,
            snapshot_batch_size=16,
        )
        sync_delegate = pool._sync_redis
        try:
            for index in range(40):
                pool.add(_proxy(f"p-{index:02d}"))
            leased = await pool.checkout(sitekey="site")
            assert leased is not None

            pool._sync_redis = _NoSyncSnapshotRedis()
            rows = await pool.snapshot_async()
            assert len(rows) == 40
            assert rows == sorted(rows, key=lambda row: row["id"])
            assert sum(row["in_flight"] for row in rows) == 1

            pool._sync_redis = sync_delegate
            await pool.report(leased.id, success=True)
        finally:
            pool._sync_redis = sync_delegate
            await pool.close()
            _flush_prefix(prefix)

    asyncio.run(run())


def test_snapshot_helper_offloads_legacy_sync_pool() -> None:
    called = False

    class LegacyPool:
        def snapshot(self) -> list[dict[str, object]]:
            nonlocal called
            called = True
            return [{"id": "legacy"}]

    async def run() -> None:
        rows = await snapshot_proxy_pool(LegacyPool())
        assert rows == [{"id": "legacy"}]
        assert called is True

    asyncio.run(run())


def test_admin_proxy_route_awaits_service_snapshot(monkeypatch) -> None:
    events: list[str] = []

    class Services:
        session_pool = None

        async def proxy_snapshot(self) -> list[dict[str, object]]:
            events.append("proxy.snapshot")
            await asyncio.sleep(0)
            return [{"id": "p"}]

    monkeypatch.setattr(routes_module, "get_services", lambda: Services())

    async def run() -> None:
        response = await routes_module.admin_proxies(
            clientKey=routes_module.config.client_key or ""
        )
        assert response == {
            "errorId": 0,
            "proxies": [{"id": "p"}],
            "sessions": [],
        }

    asyncio.run(run())
    assert events == ["proxy.snapshot"]


def test_atomic_managed_pool_protocol_and_async_snapshot() -> None:
    async def run() -> None:
        pool = AtomicManagedProxyPool()
        assert isinstance(pool, ProxyPoolProtocol)
        pool.add(_proxy("p"))
        chosen = await pool.checkout(sitekey="site")
        assert chosen is not None
        rows = await pool.snapshot_async()
        assert rows[0]["in_flight"] == 1
        await pool.report("p", success=True)
        await pool.close()

    asyncio.run(run())


def test_atomic_builder_selects_in_memory_backend() -> None:
    pool = build_atomic_proxy_pool(
        SimpleNamespace(
            redis_url=None,
            proxy_cooldown=60,
            proxy_max_consecutive_fails=3,
            proxy_max_gb=0,
            solve_timeout=120,
        )
    )
    assert isinstance(pool, AtomicManagedProxyPool)
