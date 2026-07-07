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
