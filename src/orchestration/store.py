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
from typing import Any, Optional, Protocol, cast

# Default lifetime of an idempotency claim when the caller doesn't supply one.
# Mirrors the TaskManager's in-memory coalescing TTL (``solve_timeout * 4``,
# floored at 60s) for a default config, so a retry within the task lifetime
# resolves to the original id and the claim expires afterwards.
_DEFAULT_IDEMPOTENCY_TTL = 3600


class IdempotencyConflict(Exception):
    """A live idempotency claim exists for the same key with a different payload.

    Returning the prior task id in this case would hand the caller a result for
    a *different* request, so the claim refuses and lets the API layer map the
    condition to a distinct client error instead of coalescing.
    """

    def __init__(
        self,
        key: str,
        task_id: str,
        fingerprint: str | None,
        existing_fingerprint: str | None,
    ) -> None:
        super().__init__(
            f"idempotency key {key!r} is already claimed by task {task_id!r} "
            "with a different payload fingerprint"
        )
        self.key = key
        self.task_id = task_id
        self.fingerprint = fingerprint
        self.existing_fingerprint = existing_fingerprint


def _fingerprints_conflict(
    fingerprint: str | None, existing_fingerprint: str | None
) -> bool:
    """Only two *known* fingerprints can conflict; ``None`` means legacy/unknown."""
    return (
        fingerprint is not None
        and existing_fingerprint is not None
        and fingerprint != existing_fingerprint
    )


def _claim_ttl(ttl_seconds: int | None) -> int:
    """Resolve a claim TTL, falling back to one hour when none/zero is given."""
    try:
        ttl = int(ttl_seconds) if ttl_seconds is not None else 0
    except (TypeError, ValueError):
        ttl = 0
    return ttl if ttl > 0 else _DEFAULT_IDEMPOTENCY_TTL


def _parse_claim(raw: str) -> tuple[str, str | None]:
    """Decode a stored claim value into ``(owner_task_id, fingerprint)``.

    New claims with a fingerprint are JSON blobs; legacy claims (and claims
    made without a fingerprint) are the bare task id, which keeps mixed-version
    workers interoperable.
    """
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return raw, None
    if isinstance(payload, dict):
        owner = payload.get("taskId") or payload.get("task_id")
        if owner:
            return str(owner), payload.get("fingerprint")
    return raw, None


def _stale_revision(
    incoming: dict[str, Any], existing: dict[str, Any]
) -> bool:
    """True when ``incoming`` carries a revision that is not newer than stored.

    Records without a revision (callers that predate this field) always merge,
    preserving the old last-writer-wins behaviour.
    """
    incoming_rev = incoming.get("revision")
    stored_rev = existing.get("revision")
    if incoming_rev is None or stored_rev is None:
        return False
    try:
        return int(incoming_rev) <= int(stored_rev)
    except (TypeError, ValueError):
        return False


class TaskStore(Protocol):
    async def put(self, task_id: str, record: dict[str, Any]) -> None: ...
    async def get(self, task_id: str) -> Optional[dict[str, Any]]: ...
    async def update(self, task_id: str, **fields: Any) -> None: ...
    async def upsert(
        self, task_id: str, record: dict[str, Any] | None = None, **fields: Any
    ) -> None: ...
    async def delete(self, task_id: str) -> None: ...
    async def all_ids(self) -> list[str]: ...
    async def claim_idempotency(
        self,
        key: str,
        task_id: str,
        ttl_seconds: int | None = None,
        *,
        fingerprint: str | None = None,
    ) -> str: ...
    async def close(self) -> None: ...


class InMemoryTaskStore:
    """Process-local store. TTL is enforced lazily on read/sweep."""

    def __init__(self, ttl_seconds: int = 600) -> None:
        self._ttl = ttl_seconds
        self._data: dict[str, dict[str, Any]] = {}
        # idempotency key -> (owning task_id, expires_at, payload_fingerprint).
        # Mirrors the Redis SET-NX claim so the in-process path shares the same
        # interface; the fingerprint lets a same-key/different-payload retry be
        # rejected instead of silently coalesced.
        self._idem: dict[str, tuple[str, float, str | None]] = {}

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

    async def upsert(
        self, task_id: str, record: dict[str, Any] | None = None, **fields: Any
    ) -> None:
        """Create the record if absent, else merge (rejecting stale revisions)."""
        incoming = dict(record or {})
        incoming.update(fields)
        rec = self._data.get(task_id)
        if rec is not None and self._expired(rec):
            self._data.pop(task_id, None)
            rec = None
        if rec is None:
            incoming.setdefault("stored_at", time.time())
            self._data[task_id] = incoming
            return
        if _stale_revision(incoming, rec):
            return
        rec.update(incoming)

    async def delete(self, task_id: str) -> None:
        self._data.pop(task_id, None)

    async def all_ids(self) -> list[str]:
        return list(self._data.keys())

    async def claim_idempotency(
        self,
        key: str,
        task_id: str,
        ttl_seconds: int | None = None,
        *,
        fingerprint: str | None = None,
    ) -> str:
        """Claim ``key`` for ``task_id``; return the owner (this or the prior).

        Returns ``task_id`` when this call won the claim (or the prior claim
        expired), or the existing owner's task id when another claim is live.
        Raises :class:`IdempotencyConflict` when the live claim was made for a
        different ``fingerprint``.
        """
        ttl = _claim_ttl(ttl_seconds)
        now = time.time()
        existing = self._idem.get(key)
        if existing is not None and existing[1] > now:
            owner, _, existing_fingerprint = existing
            if _fingerprints_conflict(fingerprint, existing_fingerprint):
                raise IdempotencyConflict(
                    key, owner, fingerprint, existing_fingerprint
                )
            return owner
        self._idem[key] = (task_id, now + ttl, fingerprint)
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

    async def upsert(
        self, task_id: str, record: dict[str, Any] | None = None, **fields: Any
    ) -> None:
        """Create the record if absent, else merge (rejecting stale revisions).

        The read-merge-write is race-free in practice: a task record is written
        only by the worker that admitted it, so concurrent writers for the same
        id don't exist (the idempotency claim guarantees a single owner).
        """
        key = self._KEY.format(task_id)
        incoming = dict(record or {})
        incoming.update(fields)
        existing = await self.get(task_id)
        if existing is not None:
            if _stale_revision(incoming, existing):
                return
            merged = {**existing, **incoming}
            merged.setdefault("stored_at", time.time())
        else:
            merged = incoming
            merged.setdefault("stored_at", time.time())
        await self._redis.set(key, json.dumps(merged), ex=self._ttl)
        if existing is None:
            await self._redis.sadd(self._INDEX, task_id)

    async def delete(self, task_id: str) -> None:
        await self._redis.delete(self._KEY.format(task_id))
        await self._redis.srem(self._INDEX, task_id)

    async def all_ids(self) -> list[str]:
        # ``decode_responses=True``: members are ``str`` at runtime, but
        # redis-py types SMEMBERS as the bytes/str union.
        return cast("list[str]", list(await self._redis.smembers(self._INDEX)))

    async def claim_idempotency(
        self,
        key: str,
        task_id: str,
        ttl_seconds: int | None = None,
        *,
        fingerprint: str | None = None,
    ) -> str:
        """Atomic cross-worker claim via ``SET {key} {value} NX EX ttl``.

        The first worker to set the key wins and gets ``task_id`` back; every
        other worker (concurrent or a later retry within the TTL) reads the
        stored owner and gets it back instead, so the expensive solve is spawned
        exactly once per idempotency key across the whole cluster.

        The claim value carries ``fingerprint`` (JSON) when one was supplied;
        claims without one keep the legacy bare task-id value so rolling
        deployments stay interoperable. A replay with a different fingerprint
        raises :class:`IdempotencyConflict`.
        """
        redis_key = f"captcha:idem:{key}"
        ttl = _claim_ttl(ttl_seconds)
        value = (
            task_id
            if fingerprint is None
            else json.dumps({"taskId": task_id, "fingerprint": fingerprint})
        )
        won = await self._redis.set(redis_key, value, nx=True, ex=ttl)
        if won:
            return task_id
        existing = await self._redis.get(redis_key)
        if existing is None:
            # The claim expired between SET NX and GET: this caller is the
            # owner rather than the inheritor of a phantom task id.
            return task_id
        # ``decode_responses=True``: GET returns ``str``; the stub types the
        # reply as bytes/str because it cannot see the client option.
        owner, existing_fingerprint = _parse_claim(cast(str, existing))
        if _fingerprints_conflict(fingerprint, existing_fingerprint):
            raise IdempotencyConflict(
                key, owner, fingerprint, existing_fingerprint
            )
        return owner

    async def close(self) -> None:
        await self._redis.aclose()


def build_store(config: Any) -> TaskStore:
    """Select a store implementation from Config (Redis if configured)."""
    ttl = int(getattr(config, "solve_timeout", 600)) * 4
    redis_url = getattr(config, "redis_url", None)
    if redis_url:
        return RedisTaskStore(redis_url, ttl_seconds=ttl)
    return InMemoryTaskStore(ttl_seconds=ttl)
