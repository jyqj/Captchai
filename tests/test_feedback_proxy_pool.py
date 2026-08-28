"""Regressions for the atomic proxy feedback write plane."""

from __future__ import annotations

import asyncio
import contextvars
import time
from types import SimpleNamespace
from typing import Any

import pytest

from src.assets.feedback_proxy_pool import (
    FeedbackManagedProxyPool,
    FeedbackRedisProxyPool,
    build_feedback_proxy_pool,
    proxy_lease_token,
)
from src.assets.proxy_pool import ProxyAsset, ProxyPoolProtocol

_REDIS_URL = "redis://localhost:6379/0"


def _redis_helper() -> str:
    pytest.importorskip("redis.asyncio")
    sync_redis = pytest.importorskip("redis")
    try:
        client = sync_redis.from_url(_REDIS_URL, decode_responses=True)
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


def _proxy(proxy_id: str = "p") -> ProxyAsset:
    return ProxyAsset(
        id=proxy_id,
        server=f"http://{proxy_id}:1",
        success_count=10,
        sitekey_stats={"site": [10, 0]},
    )


class _BusyOnceRedis:
    """Return BUSY once, then delegate while recording feedback time args."""

    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate
        self.commit_times: list[float] = []

    async def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: Any,
    ) -> Any:
        self.commit_times.append(float(keys_and_args[numkeys + 1]))
        if len(self.commit_times) == 1:
            return ["BUSY"]
        return await self._delegate.eval(script, numkeys, *keys_and_args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


class _EvalOnlyRedis:
    """Feedback may execute one script, never the compatibility lock/RMW path."""

    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate
        self.eval_calls = 0

    async def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: Any,
    ) -> Any:
        self.eval_calls += 1
        return await self._delegate.eval(script, numkeys, *keys_and_args)

    async def set(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("feedback must not acquire the compatibility lock")

    async def hget(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("feedback reads must happen inside Lua")

    async def hset(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("feedback writes must happen inside Lua")

    async def zpopmin(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("lease release must happen inside Lua")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


def test_feedback_report_is_one_atomic_eval_without_global_lock() -> None:
    url = _redis_helper()
    prefix = "test:feedback:one-eval:"

    async def run() -> None:
        _flush_prefix(prefix)
        pool = FeedbackRedisProxyPool(url, key_prefix=prefix)
        delegate = pool._redis
        try:
            pool.add(_proxy())
            chosen = await pool.checkout(sitekey="site")
            assert chosen is not None
            token = proxy_lease_token(chosen)
            assert token

            wrapped = _EvalOnlyRedis(delegate)
            pool._redis = wrapped
            accepted = await pool.report_solve(
                chosen.id,
                success=True,
                bytes_used=123,
                lease_token=token,
                sitekey="site",
            )
            assert accepted is True
            assert wrapped.eval_calls == 1

            pool._redis = delegate
            row = (await pool.snapshot_async())[0]
            assert row["bytes_used"] == 123
            assert row["sitekeys"]["site"] == {"success": 11, "fail": 0}
        finally:
            pool._redis = delegate
            await pool.close()
            _flush_prefix(prefix)

    asyncio.run(run())


def test_exact_out_of_order_release_never_removes_another_lease() -> None:
    url = _redis_helper()
    prefix = "test:feedback:exact-release:"

    async def run() -> None:
        _flush_prefix(prefix)
        pool = FeedbackRedisProxyPool(url, key_prefix=prefix)
        try:
            pool.add(_proxy())
            first = await pool.checkout(sitekey="site")
            second = await pool.checkout(sitekey="site")
            assert first is not None and second is not None
            first_token = proxy_lease_token(first)
            second_token = proxy_lease_token(second)
            assert first_token and second_token and first_token != second_token

            await pool.report(
                "p", success=True, lease_token=second_token
            )
            members = set(
                await pool._redis.zrange(pool._lease_key("p"), 0, -1)
            )
            assert members == {first_token}

            await pool.report("p", success=True, lease_token=first_token)
            assert int(await pool._redis.zcard(pool._lease_key("p"))) == 0
        finally:
            await pool.close()
            _flush_prefix(prefix)

    asyncio.run(run())


def test_duplicate_late_report_and_sitekey_feedback_are_idempotent() -> None:
    url = _redis_helper()
    prefix = "test:feedback:idempotent:"

    async def run() -> None:
        _flush_prefix(prefix)
        pool = FeedbackRedisProxyPool(url, key_prefix=prefix)
        try:
            pool.add(_proxy())
            chosen = await pool.checkout(sitekey="site")
            assert chosen is not None
            token = proxy_lease_token(chosen)
            assert token

            await pool.report("p", success=True, lease_token=token)
            await pool.report_sitekey("p", "site", success=True)
            await pool.report("p", success=False, lease_token=token)
            await pool.report_sitekey("p", "site", success=False)

            row = (await pool.snapshot_async())[0]
            assert row["success_count"] == 11
            assert row["fail_count"] == 0
            assert row["sitekeys"]["site"] == {"success": 11, "fail": 0}
        finally:
            await pool.close()
            _flush_prefix(prefix)

    asyncio.run(run())


def test_real_outcome_updates_health_without_releasing_solve_lease() -> None:
    url = _redis_helper()
    prefix = "test:feedback:real-no-release:"

    async def run() -> None:
        _flush_prefix(prefix)
        pool = FeedbackRedisProxyPool(url, key_prefix=prefix)
        try:
            pool.add(_proxy())
            chosen = await pool.checkout(sitekey="site")
            assert chosen is not None
            token = proxy_lease_token(chosen)
            assert token

            await pool.report_real_outcome("p", "site", success=False)
            assert await pool._redis.zscore(pool._lease_key("p"), token) is not None
            row = (await pool.snapshot_async())[0]
            assert row["fail_count"] == 1
            assert row["real_sitekeys"]["site"] == {"success": 0, "fail": 1}

            await pool.report("p", success=True, lease_token=token)
            assert int(await pool._redis.zcard(pool._lease_key("p"))) == 0
        finally:
            await pool.close()
            _flush_prefix(prefix)

    asyncio.run(run())


def test_concurrent_sitekey_feedback_has_no_lost_updates() -> None:
    url = _redis_helper()
    prefix = "test:feedback:concurrent-sitekey:"

    async def run() -> None:
        _flush_prefix(prefix)
        pool = FeedbackRedisProxyPool(url, key_prefix=prefix)
        try:
            pool.add(ProxyAsset(id="p", server="http://p:1"))
            await asyncio.gather(
                *(
                    pool.report_sitekey(
                        "p", "site", success=index % 2 == 0
                    )
                    for index in range(100)
                )
            )
            row = (await pool.snapshot_async())[0]
            assert row["sitekeys"]["site"] == {"success": 50, "fail": 50}
        finally:
            await pool.close()
            _flush_prefix(prefix)

    asyncio.run(run())


def test_lru_eviction_removes_both_buckets_and_quality_index() -> None:
    url = _redis_helper()
    prefix = "test:feedback:lru:"

    async def run() -> None:
        _flush_prefix(prefix)
        pool = FeedbackRedisProxyPool(
            url,
            key_prefix=prefix,
            sitekey_limit=2,
        )
        try:
            pool.add(ProxyAsset(id="p", server="http://p:1"))
            await pool.report_sitekey("p", "old", success=True)
            await pool.report_sitekey_real("p", "middle", success=False)
            await pool.report_sitekey("p", "new", success=True)

            row = (await pool.snapshot_async())[0]
            assert set(row["sitekeys"]) == {"new"}
            assert set(row["real_sitekeys"]) == {"middle"}
            assert (
                await pool._redis.zscore(
                    pool._sitekey_index_key("old"), "p"
                )
                is None
            )
            assert (
                await pool._redis.hget(
                    pool._sitekey_index_map_key("p"), "old"
                )
                is None
            )
        finally:
            await pool.close()
            _flush_prefix(prefix)

    asyncio.run(run())


def test_health_transition_removes_and_restores_all_quality_indexes() -> None:
    url = _redis_helper()
    prefix = "test:feedback:index-transition:"

    async def run() -> None:
        _flush_prefix(prefix)
        pool = FeedbackRedisProxyPool(
            url,
            key_prefix=prefix,
            max_consecutive_fails=1,
        )
        try:
            pool.add(
                ProxyAsset(
                    id="p",
                    server="http://p:1",
                    success_count=2,
                    sitekey_stats={"a": [1, 0], "b": [1, 0]},
                )
            )
            chosen = await pool.checkout(sitekey="a")
            assert chosen is not None
            token = proxy_lease_token(chosen)
            assert token
            await pool.report("p", success=False, lease_token=token)

            assert await pool._redis.zscore(pool._active_all_key, "p") is None
            assert await pool._redis.zscore(pool._sitekey_index_key("a"), "p") is None
            assert await pool._redis.zscore(pool._sitekey_index_key("b"), "p") is None
            assert await pool._redis.zscore(pool._cooldown_index_key, "p") is not None

            await pool.report_real_outcome("p", "a", success=True)
            assert await pool._redis.zscore(pool._active_all_key, "p") is not None
            assert await pool._redis.zscore(pool._sitekey_index_key("a"), "p") is not None
            assert await pool._redis.zscore(pool._sitekey_index_key("b"), "p") is not None
        finally:
            await pool.close()
            _flush_prefix(prefix)

    asyncio.run(run())


def test_feedback_observes_legacy_writer_lock_without_mutating() -> None:
    url = _redis_helper()
    prefix = "test:feedback:compat-lock:"

    async def run() -> None:
        _flush_prefix(prefix)
        pool = FeedbackRedisProxyPool(
            url,
            key_prefix=prefix,
            lock_wait_seconds=0.06,
        )
        try:
            pool.add(_proxy())
            chosen = await pool.checkout(sitekey="site")
            assert chosen is not None
            token = proxy_lease_token(chosen)
            assert token

            await pool._redis.set(pool._lock_key, "legacy-writer", ex=2)
            accepted = await pool.report_solve(
                "p",
                lease_token=token,
                success=False,
                sitekey="site",
            )
            assert accepted is False
            assert await pool._redis.zscore(pool._lease_key("p"), token) is not None
            row = (await pool.snapshot_async())[0]
            assert row["fail_count"] == 0
            assert row["sitekeys"]["site"] == {"success": 10, "fail": 0}

            await pool._redis.delete(pool._lock_key)
            accepted = await pool.report_solve(
                "p",
                lease_token=token,
                success=True,
                sitekey="site",
            )
            assert accepted is True
        finally:
            await pool.close()
            _flush_prefix(prefix)

    asyncio.run(run())


def test_feedback_refreshes_commit_time_after_compatibility_wait() -> None:
    url = _redis_helper()
    prefix = "test:feedback:retry-clock:"

    async def run() -> None:
        _flush_prefix(prefix)
        pool = FeedbackRedisProxyPool(
            url,
            key_prefix=prefix,
            lock_wait_seconds=0.5,
        )
        delegate = pool._redis
        try:
            pool.add(ProxyAsset(id="p", server="http://p:1"))
            wrapped = _BusyOnceRedis(delegate)
            pool._redis = wrapped
            await pool.set_geo(
                "p",
                country="DE",
                timezone="Europe/Berlin",
                locale="de-DE",
            )
            assert len(wrapped.commit_times) == 2
            assert wrapped.commit_times[1] > wrapped.commit_times[0]
        finally:
            pool._redis = delegate
            await pool.close()
            _flush_prefix(prefix)

    asyncio.run(run())


def test_set_geo_is_one_atomic_eval() -> None:
    url = _redis_helper()
    prefix = "test:feedback:geo:"

    async def run() -> None:
        _flush_prefix(prefix)
        pool = FeedbackRedisProxyPool(url, key_prefix=prefix)
        delegate = pool._redis
        try:
            pool.add(ProxyAsset(id="p", server="http://p:1"))
            wrapped = _EvalOnlyRedis(delegate)
            pool._redis = wrapped
            await pool.set_geo(
                "p",
                country="DE",
                timezone="Europe/Berlin",
                locale="de-DE",
                geo_probed=True,
            )
            assert wrapped.eval_calls == 1

            pool._redis = delegate
            row = (await pool.snapshot_async())[0]
            assert row["country"] == "DE"
            assert row["timezone"] == "Europe/Berlin"
            assert row["locale"] == "de-DE"
            assert row["geo_probed"] is True
        finally:
            pool._redis = delegate
            await pool.close()
            _flush_prefix(prefix)

    asyncio.run(run())


def test_task_local_tokens_release_correctly_under_concurrency() -> None:
    url = _redis_helper()
    prefix = "test:feedback:task-local:"

    async def run() -> None:
        _flush_prefix(prefix)
        pool = FeedbackRedisProxyPool(url, key_prefix=prefix)
        try:
            pool.add(_proxy())

            async def worker() -> None:
                chosen = await pool.checkout(sitekey="site")
                assert chosen is not None and proxy_lease_token(chosen)
                await asyncio.sleep(0)
                await pool.report(chosen.id, success=True)
                await pool.report_sitekey(chosen.id, "site", success=True)

            await asyncio.gather(*(worker() for _ in range(20)))
            row = (await pool.snapshot_async())[0]
            assert row["in_flight"] == 0
            assert row["success_count"] == 30
            assert row["sitekeys"]["site"] == {"success": 30, "fail": 0}
        finally:
            await pool.close()
            _flush_prefix(prefix)

    asyncio.run(run())


def test_proxy_scoped_feedback_from_foreign_context_never_releases_lease() -> None:
    url = _redis_helper()
    prefix = "test:feedback:foreign-context:"

    async def run() -> None:
        _flush_prefix(prefix)
        pool = FeedbackRedisProxyPool(url, key_prefix=prefix)
        try:
            pool.add(_proxy())
            chosen = await pool.checkout(sitekey="site")
            assert chosen is not None
            token = proxy_lease_token(chosen)
            assert token

            async def foreign_feedback() -> None:
                await pool.report("p", success=False)

            foreign_task = contextvars.Context().run(
                asyncio.create_task, foreign_feedback()
            )
            await foreign_task
            assert await pool._redis.zscore(pool._lease_key("p"), token) is not None
            row = (await pool.snapshot_async())[0]
            assert row["fail_count"] == 1

            await pool.report("p", success=True, lease_token=token)
            assert int(await pool._redis.zcard(pool._lease_key("p"))) == 0
        finally:
            await pool.close()
            _flush_prefix(prefix)

    asyncio.run(run())


def test_expired_cooldown_rehabilitation_restores_every_quality_index() -> None:
    url = _redis_helper()
    prefix = "test:feedback:expired-rehabilitation:"

    async def run() -> None:
        _flush_prefix(prefix)
        pool = FeedbackRedisProxyPool(
            url,
            key_prefix=prefix,
            max_consecutive_fails=1,
        )
        try:
            pool.add(
                ProxyAsset(
                    id="p",
                    server="http://p:1",
                    success_count=2,
                    sitekey_stats={"a": [1, 0], "b": [1, 0]},
                )
            )
            chosen = await pool.checkout(sitekey="a")
            assert chosen is not None
            token = proxy_lease_token(chosen)
            assert token
            await pool.report("p", success=False, lease_token=token)

            blob = await pool._redis.hget(pool._proxies_key, "p")
            assert blob is not None
            proxy = pool._deserialize(blob)
            proxy.cooldown_until = time.time() - 1
            await pool._redis.hset(
                pool._proxies_key, "p", pool._serialize(proxy)
            )
            await pool._redis.zadd(
                pool._cooldown_index_key, {"p": proxy.cooldown_until}
            )

            await pool.set_geo(
                "p",
                country="DE",
                timezone="Europe/Berlin",
                locale="de-DE",
            )
            assert await pool._redis.zscore(pool._active_all_key, "p") is not None
            assert await pool._redis.zscore(pool._sitekey_index_key("a"), "p") is not None
            assert await pool._redis.zscore(pool._sitekey_index_key("b"), "p") is not None
            assert await pool._redis.zscore(pool._cooldown_index_key, "p") is None
        finally:
            await pool.close()
            _flush_prefix(prefix)

    asyncio.run(run())


def test_feedback_managed_pool_exact_lease_parity() -> None:
    async def run() -> None:
        pool = FeedbackManagedProxyPool()
        assert isinstance(pool, ProxyPoolProtocol)
        pool.add(_proxy())
        first = await pool.checkout(sitekey="site")
        second = await pool.checkout(sitekey="site")
        assert first is not None and second is not None
        first_token = proxy_lease_token(first)
        second_token = proxy_lease_token(second)
        assert first_token and second_token

        await pool.report("p", success=True, lease_token=second_token)
        assert (await pool.snapshot_async())[0]["in_flight"] == 1
        await pool.report("p", success=True, lease_token=first_token)
        assert (await pool.snapshot_async())[0]["in_flight"] == 0
        await pool.close()

    asyncio.run(run())


def test_feedback_builder_selects_in_memory_backend() -> None:
    pool = build_feedback_proxy_pool(
        SimpleNamespace(
            redis_url=None,
            proxy_cooldown=60,
            proxy_max_consecutive_fails=3,
            proxy_max_gb=0,
            solve_timeout=120,
        )
    )
    assert isinstance(pool, FeedbackManagedProxyPool)


def test_browser_solver_propagates_exact_lease_to_release_path() -> None:
    from src.services.browser_solver import BaseBrowserSolver

    events: list[tuple[Any, ...]] = []

    class Context:
        _omc_bytes_used = 17

        async def close(self) -> None:
            events.append(("context.close",))

    class Manager:
        async def new_context(self, params: dict[str, Any]) -> tuple[Context, str]:
            return Context(), "ua"

    class Pool:
        async def checkout(self, **kwargs: Any) -> ProxyAsset:
            proxy = ProxyAsset(id="p", server="http://p:1")
            setattr(proxy, "_captchai_proxy_lease_token", "lease-123")
            return proxy

        async def report_solve(self, proxy_id: str, **kwargs: Any) -> bool:
            events.append(
                (
                    "report_solve",
                    proxy_id,
                    kwargs["lease_token"],
                    kwargs["success"],
                    kwargs["bytes_used"],
                    kwargs["sitekey"],
                )
            )
            return True

    async def run() -> None:
        pool = Pool()
        services = SimpleNamespace(proxy_pool=pool, session_pool=None)
        solver = BaseBrowserSolver(
            SimpleNamespace(
                pool_egress_expose_credentials=False,
                proxy_geo_probe=False,
            ),
            manager=Manager(),
            services=services,
        )
        params: dict[str, Any] = {"websiteKey": "site"}
        solve_context = await solver._acquire_pool(params)
        assert solve_context.proxy_lease_token == "lease-123"
        await solver._release_context(solve_context, True, params)

    asyncio.run(run())
    assert events == [
        ("context.close",),
        ("report_solve", "p", "lease-123", True, 17, "site"),
    ]


def test_real_outcome_uses_combined_atomic_feedback_when_available() -> None:
    from src.services.outcome import record_real_outcome

    events: list[tuple[Any, ...]] = []

    class Pool:
        async def report_real_outcome(
            self, proxy_id: str, sitekey: str, *, success: bool
        ) -> None:
            events.append((proxy_id, sitekey, success))

        async def report(self, *args: Any, **kwargs: Any) -> None:
            raise AssertionError("combined feedback should replace split report")

        async def report_sitekey_real(self, *args: Any, **kwargs: Any) -> None:
            raise AssertionError("combined feedback should replace split sitekey write")

    async def run() -> None:
        await record_real_outcome(
            SimpleNamespace(
                proxy_pool=Pool(),
                accounting=None,
                session_pool=None,
            ),
            SimpleNamespace(
                proxy_id="p",
                session_id=None,
                proxy_kind="pool_proxy",
            ),
            "site",
            success=False,
        )

    asyncio.run(run())
    assert events == [("p", "site", False)]
