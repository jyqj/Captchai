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
    async def claim_idempotency(
        self, key: str, task_id: str, ttl_seconds: int
    ) -> str: ...
    async def close(self) -> None: ...


class InMemoryTaskStore:
    """Process-local store. TTL is enforced lazily on read/sweep."""

    def __init__(self, ttl_seconds: int = 600) -> None:
        self._ttl = ttl_seconds
        self._data: dict[str, dict[str, Any]] = {}
        # idempotency key -> (owning task_id, expires_at). Mirrors the Redis
        # SET-NX claim so the in-process path shares the same interface.
        self._idem: dict[str, tuple[str, float]] = {}

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

    async def claim_idempotency(
        self, key: str, task_id: str, ttl_seconds: int
    ) -> str:
        """Claim ``key`` for ``task_id``; return the owner (this or the prior).

        Returns ``task_id`` when this call won the claim (or the prior claim
        expired), or the existing owner's task id when another claim is live.
        """
        now = time.time()
        existing = self._idem.get(key)
        if existing is not None and existing[1] > now:
            return existing[0]
        self._idem[key] = (task_id, now + ttl_seconds)
        return task_id

    async def close(self) -> None:
        self._data.clear()
        self._idem.clear()

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

    async def claim_idempotency(
        self, key: str, task_id: str, ttl_seconds: int
    ) -> str:
        """Atomic cross-worker claim via ``SET {key} {task_id} NX EX ttl``.

        The first worker to set the key wins and gets ``task_id`` back; every
        other worker (concurrent or a later retry within the TTL) reads the
        stored owner and gets it back instead, so the expensive solve is spawned
        exactly once per idempotency key across the whole cluster.
        """
        redis_key = f"captcha:idem:{key}"
        won = await self._redis.set(redis_key, task_id, nx=True, ex=ttl_seconds)
        if won:
            return task_id
        existing = await self._redis.get(redis_key)
        return existing or task_id

    async def close(self) -> None:
        await self._redis.aclose()


def build_store(config: Any) -> TaskStore:
    """Select a store implementation from Config (Redis if configured)."""
    ttl = int(getattr(config, "solve_timeout", 600)) * 4
    redis_url = getattr(config, "redis_url", None)
    if redis_url:
        return RedisTaskStore(redis_url, ttl_seconds=ttl)
    return InMemoryTaskStore(ttl_seconds=ttl)
