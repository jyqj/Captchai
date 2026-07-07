"""Persistent task store abstraction (in-memory default, optional Redis).

The original service kept tasks in a bare ``dict`` on a single process, so a
restart lost every in-flight task and there was no path to horizontal scale.
This module defines a small store interface with two implementations:

* :class:`InMemoryTaskStore` — the default; behaviour-compatible with the old
  dict but with explicit TTL/expiry semantics.
* :class:`RedisTaskStore` — used when ``REDIS_URL`` is configured, giving shared
  state across workers, restart survival, and server-side TTL.

Records are stored as plain JSON-serialisable dicts so both backends share the
same shape. The store is intentionally minimal: the :class:`TaskManager` still
owns concurrency, cancellation, and the live ``asyncio.Task`` handles (those are
process-local and cannot live in Redis).
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional, Protocol


class TaskStore(Protocol):
    async def put(self, task_id: str, record: dict[str, Any]) -> None: ...
    async def get(self, task_id: str) -> Optional[dict[str, Any]]: ...
    async def update(self, task_id: str, **fields: Any) -> None: ...
    async def delete(self, task_id: str) -> None: ...
    async def all_ids(self) -> list[str]: ...
    async def close(self) -> None: ...


class InMemoryTaskStore:
    """Process-local store. TTL is enforced lazily on read/sweep."""

    def __init__(self, ttl_seconds: int = 600) -> None:
        self._ttl = ttl_seconds
        self._data: dict[str, dict[str, Any]] = {}

    async def put(self, task_id: str, record: dict[str, Any]) -> None:
        record.setdefault("stored_at", time.time())
        self._data[task_id] = record

    async def get(self, task_id: str) -> Optional[dict[str, Any]]:
        rec = self._data.get(task_id)
        if rec is None:
            return None
        if self._expired(rec):
            self._data.pop(task_id, None)
            return None
        return rec

    async def update(self, task_id: str, **fields: Any) -> None:
        rec = self._data.get(task_id)
        if rec is not None:
            rec.update(fields)

    async def delete(self, task_id: str) -> None:
        self._data.pop(task_id, None)

    async def all_ids(self) -> list[str]:
        return list(self._data.keys())

    async def close(self) -> None:
        self._data.clear()

    def _expired(self, rec: dict[str, Any]) -> bool:
        stored_at = rec.get("stored_at", 0.0)
        return (time.time() - stored_at) > self._ttl


class RedisTaskStore:
    """Redis-backed store. Requires ``redis.asyncio`` (redis>=4.2).

    Records are JSON blobs at key ``captcha:task:<id>`` with a server-side TTL,
    plus a set ``captcha:tasks`` for enumeration.
    """

    _KEY = "captcha:task:{}"
    _INDEX = "captcha:tasks"

    def __init__(self, url: str, ttl_seconds: int = 600) -> None:
        try:
            import redis.asyncio as aioredis  # noqa: WPS433 (optional dep)
        except ImportError as exc:  # pragma: no cover - exercised only w/o redis
            raise RuntimeError(
                "REDIS_URL is set but the 'redis' package is not installed; "
                "add redis>=4.2 to requirements or unset REDIS_URL"
            ) from exc
        self._ttl = ttl_seconds
        self._redis = aioredis.from_url(url, decode_responses=True)

    async def put(self, task_id: str, record: dict[str, Any]) -> None:
        record.setdefault("stored_at", time.time())
        key = self._KEY.format(task_id)
        await self._redis.set(key, json.dumps(record), ex=self._ttl)
        await self._redis.sadd(self._INDEX, task_id)

    async def get(self, task_id: str) -> Optional[dict[str, Any]]:
        raw = await self._redis.get(self._KEY.format(task_id))
        if raw is None:
            await self._redis.srem(self._INDEX, task_id)
            return None
        return json.loads(raw)

    async def update(self, task_id: str, **fields: Any) -> None:
        rec = await self.get(task_id)
        if rec is None:
            return
        rec.update(fields)
        await self._redis.set(
            self._KEY.format(task_id), json.dumps(rec), ex=self._ttl
        )

    async def delete(self, task_id: str) -> None:
        await self._redis.delete(self._KEY.format(task_id))
        await self._redis.srem(self._INDEX, task_id)

    async def all_ids(self) -> list[str]:
        return list(await self._redis.smembers(self._INDEX))

    async def close(self) -> None:
        await self._redis.aclose()


def build_store(config: Any) -> TaskStore:
    """Select a store implementation from Config (Redis if configured)."""
    ttl = int(getattr(config, "solve_timeout", 600)) * 4
    redis_url = getattr(config, "redis_url", None)
    if redis_url:
        return RedisTaskStore(redis_url, ttl_seconds=ttl)
    return InMemoryTaskStore(ttl_seconds=ttl)
