"""Tests for the orchestration task store (in-memory backend)."""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.orchestration.store import InMemoryTaskStore, build_store  # noqa: E402
from tests.conftest import flush_prefix as _flush_prefix  # noqa: E402
from tests.conftest import redis_helper as _redis_helper  # noqa: E402


def test_put_get_roundtrip() -> None:
    async def run() -> None:
        store = InMemoryTaskStore(ttl_seconds=60)
        await store.put("t1", {"status": "processing"})
        rec = await store.get("t1")
        assert rec is not None
        assert rec["status"] == "processing"
        assert "stored_at" in rec

    asyncio.run(run())


def test_update_and_delete() -> None:
    async def run() -> None:
        store = InMemoryTaskStore(ttl_seconds=60)
        await store.put("t1", {"status": "processing"})
        await store.update("t1", status="ready", solution={"token": "x"})
        rec = await store.get("t1")
        assert rec["status"] == "ready"
        assert rec["solution"] == {"token": "x"}
        await store.delete("t1")
        assert await store.get("t1") is None

    asyncio.run(run())


def test_expiry() -> None:
    async def run() -> None:
        store = InMemoryTaskStore(ttl_seconds=0)
        await store.put("t1", {"status": "processing"})
        # stored_at is now; ttl 0 => already expired on next read
        time.sleep(0.01)
        assert await store.get("t1") is None
        assert await store.all_ids() == []

    asyncio.run(run())


def test_all_ids() -> None:
    async def run() -> None:
        store = InMemoryTaskStore(ttl_seconds=60)
        await store.put("a", {"x": 1})
        await store.put("b", {"x": 2})
        assert set(await store.all_ids()) == {"a", "b"}

    asyncio.run(run())


def test_build_store_defaults_in_memory() -> None:
    from types import SimpleNamespace

    cfg = SimpleNamespace(solve_timeout=180, redis_url=None)
    store = build_store(cfg)
    assert isinstance(store, InMemoryTaskStore)


# --------------------------------------------------------------------------- #
# RedisTaskStore.update — atomic read-merge-write (5.2)
# --------------------------------------------------------------------------- #


def test_redis_task_store_update_roundtrip() -> None:
    aioredis, url = _redis_helper()  # noqa: F841
    prefix = "captcha:task:teststore-rt"

    async def run() -> None:
        from src.orchestration.store import RedisTaskStore

        _flush_prefix(prefix)
        store = RedisTaskStore(url, ttl_seconds=60)
        tid = "teststore-rt-1"
        try:
            await store.put(tid, {"id": tid, "status": "processing", "params": {}})
            await store.update(tid, status="ready", solution={"token": "x"})
            rec = await store.get(tid)
            assert rec is not None
            assert rec["status"] == "ready"
            assert rec["solution"] == {"token": "x"}
            # Unrelated fields survive the merge.
            assert rec["id"] == tid
            assert rec["params"] == {}
            # A None field round-trips as JSON null (not a dropped key).
            await store.update(tid, error_code=None, error_description=None)
            rec2 = await store.get(tid)
            assert rec2 is not None
            assert rec2["error_code"] is None
        finally:
            await store.delete(tid)
            await store.close()
            _flush_prefix(prefix)

    asyncio.run(run())


def test_redis_task_store_update_missing_is_noop() -> None:
    aioredis, url = _redis_helper()  # noqa: F841
    prefix = "captcha:task:teststore-missing"

    async def run() -> None:
        from src.orchestration.store import RedisTaskStore

        _flush_prefix(prefix)
        store = RedisTaskStore(url, ttl_seconds=60)
        try:
            # Updating an absent key must not resurrect it.
            await store.update("teststore-missing-x", status="ready")
            assert await store.get("teststore-missing-x") is None
        finally:
            await store.close()
            _flush_prefix(prefix)

    asyncio.run(run())


def test_redis_task_store_update_atomic_no_lost_update() -> None:
    """Concurrent updates each setting a distinct field must all survive.

    With the old non-atomic get→merge→set, concurrent writers could clobber
    each other's fields. The WATCH/MULTI transaction retries on conflict so
    every field lands.
    """
    aioredis, url = _redis_helper()  # noqa: F841
    prefix = "captcha:task:teststore-atomic"

    async def run() -> None:
        from src.orchestration.store import RedisTaskStore

        _flush_prefix(prefix)
        store = RedisTaskStore(url, ttl_seconds=60)
        tid = "teststore-atomic-1"
        try:
            await store.put(tid, {"id": tid, "status": "processing"})
            n = 25
            await asyncio.gather(
                *(store.update(tid, **{f"f{i}": i}) for i in range(n))
            )
            rec = await store.get(tid)
            assert rec is not None
            for i in range(n):
                assert rec[f"f{i}"] == i, f"lost update for field f{i}"
        finally:
            await store.delete(tid)
            await store.close()
            _flush_prefix(prefix)

    asyncio.run(run())
