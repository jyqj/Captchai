"""Tests for the Redis-backed proxy pool (``RedisProxyPool``).

Mirrors the existing Redis ledger test pattern in ``test_consumption.py``:
each test calls ``pytest.importorskip("redis.asyncio")`` so the suite skips
cleanly when the ``redis`` package isn't installed, and flushes the test key
prefix before/after so runs don't see stale data. A real Redis server is
required — when one isn't reachable the tests skip too (best-effort ping)
rather than failing the suite.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    _ = sys.path.insert(0, str(PROJECT_ROOT))

from src.assets.proxy_pool import ProxyAsset, build_proxy_pool  # noqa: E402


_REDIS_URL = "redis://localhost:6379/0"


def _redis_helper():
    """Return (aioredis, url) or skip the test if redis isn't available."""
    pytest = __import__("pytest")
    aioredis = pytest.importorskip("redis.asyncio")
    # Skip if no real Redis server is reachable — these tests need one.
    import redis as sync_redis

    try:
        client = sync_redis.from_url(_REDIS_URL, decode_responses=True)
        client.ping()
        client.close()
    except Exception:
        pytest.skip("no reachable Redis server at localhost:6379", allow_module_level=True)
    return aioredis, _REDIS_URL


def _flush_prefix(prefix: str) -> None:
    """Synchronously flush test keys so tests don't see stale data."""
    import redis as sync_redis

    r = sync_redis.from_url(_REDIS_URL, decode_responses=True)
    for key in r.scan_iter(f"{prefix}*"):
        r.delete(key)
    r.close()


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
