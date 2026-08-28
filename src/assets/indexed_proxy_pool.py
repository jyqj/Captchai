"""Load-aware proxy scheduling backed by bounded Redis indexes.

The legacy Redis proxy pool keeps complete assets in one hash and performs
``HGETALL`` plus JSON deserialisation under a global lock for every checkout.
That makes the hot path O(total inventory), while lock timeout falls through to
an unlocked read-modify-write path.

This module preserves the existing ``ProxyPoolProtocol`` surface while changing
its internal invariants:

* every checkout creates a lease, and every solve report releases one lease;
* leases expire, so a crashed worker cannot permanently inflate proxy load;
* concurrent load participates in ranking instead of historical quality alone;
* Redis checkout reads a bounded candidate window, never the full inventory;
* lock acquisition is fail-closed rather than silently mutating without a lock;
* per-proxy sitekey history is bounded and compacted with an LRU policy.

The old classes remain import-compatible from :mod:`src.assets.proxy_pool`.
Production construction opts into the managed backends through
:func:`build_managed_proxy_pool`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import time
from collections import OrderedDict
from typing import Any, Dict, Iterable, List, Optional, Sequence, Union

from .proxy_pool import ProxyAsset, ProxyPool, RedisProxyPool

log = logging.getLogger(__name__)

KindFilter = Optional[Union[str, Iterable[str]]]
_KNOWN_KINDS = ("datacenter", "residential", "mobile")


class ProxyPoolBusy(RuntimeError):
    """Raised when the Redis scheduling lock cannot be acquired safely."""


def _normalise_kinds(kind: KindFilter) -> Optional[frozenset[str]]:
    if kind is None:
        return None
    if isinstance(kind, str):
        return frozenset({kind})
    values = frozenset(str(value) for value in kind)
    return values or None


def _redis_text(value: Any) -> str:
    """Normalise a decoded or byte Redis scalar into text."""

    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _redis_members(values: Any) -> List[str]:
    """Normalise Redis range responses across redis-py overloads."""

    if values is None:
        return []
    if isinstance(values, (bytes, str)):
        return [_redis_text(values)]

    members: List[str] = []
    for value in values:
        # The redis-py union also includes withscores=True responses even
        # though this scheduler never requests scores from range commands.
        if isinstance(value, (list, tuple)) and value:
            members.append(_redis_text(value[0]))
        else:
            members.append(_redis_text(value))
    return members


def _ordered_sitekeys(proxy: ProxyAsset) -> List[str]:
    """Return sitekeys in stable insertion order across both outcome buckets."""

    ordered: List[str] = []
    seen: set[str] = set()
    for bucket in (proxy.sitekey_stats, proxy.real_sitekey_stats):
        for sitekey in bucket:
            if sitekey not in seen:
                seen.add(sitekey)
                ordered.append(sitekey)
    return ordered


def _prune_sitekey_history(proxy: ProxyAsset, limit: int) -> List[str]:
    """Bound both sitekey buckets, keeping the most recently inserted keys."""

    ordered = _ordered_sitekeys(proxy)
    excess = max(0, len(ordered) - max(1, limit))
    evicted = ordered[:excess]
    for sitekey in evicted:
        proxy.sitekey_stats.pop(sitekey, None)
        proxy.real_sitekey_stats.pop(sitekey, None)
    return evicted


def _sitekey_signal(proxy: ProxyAsset, sitekey: Optional[str]) -> float:
    real = proxy.real_sitekey_rate(sitekey)
    if real is not None:
        return real
    token = proxy.sitekey_rate(sitekey)
    return token if token is not None else 0.5


def _load_adjusted_utility(
    proxy: ProxyAsset,
    sitekey: Optional[str],
    in_flight: int,
) -> float:
    """Blend historical quality with current pressure."""

    quality = 0.75 * _sitekey_signal(proxy, sitekey)
    quality += 0.25 * proxy.success_rate()
    return quality / (1.0 + max(0, in_flight))


def _is_active(proxy: ProxyAsset, now: float) -> bool:
    if proxy.state == "burned":
        return False
    return not (
        proxy.state == "cooldown" and proxy.cooldown_until > now
    )


def _gb_to_bytes(value: Any) -> int:
    try:
        gb = float(value)
    except (TypeError, ValueError):
        return 0
    return int(gb * 1024**3) if gb > 0 else 0


class ManagedProxyPool(ProxyPool):
    """In-memory proxy pool with load-aware checkout and bounded statistics."""

    def __init__(
        self,
        *,
        cooldown_seconds: int = 120,
        max_consecutive_fails: int = 3,
        max_bytes_per_proxy: int = 0,
        sitekey_limit: int = 128,
        lease_ttl_seconds: int = 240,
    ) -> None:
        super().__init__(
            cooldown_seconds=cooldown_seconds,
            max_consecutive_fails=max_consecutive_fails,
            max_bytes_per_proxy=max_bytes_per_proxy,
        )
        self._lease_ttl_seconds = max(30, int(lease_ttl_seconds))
        self._leases: Dict[str, List[float]] = {}
        self._sitekey_limit = max(1, int(sitekey_limit))
        self._sitekey_lru: Dict[
            str, OrderedDict[str, float]
        ] = {}

    def add(self, proxy: ProxyAsset) -> None:
        _prune_sitekey_history(proxy, self._sitekey_limit)
        super().add(proxy)
        self._sitekey_lru[proxy.id] = OrderedDict(
            (sitekey, float(index))
            for index, sitekey in enumerate(_ordered_sitekeys(proxy))
        )

    def _lease_count_locked(self, proxy_id: str, now: float) -> int:
        active = [
            expiry
            for expiry in self._leases.get(proxy_id, ())
            if expiry > now
        ]
        if active:
            self._leases[proxy_id] = active
        else:
            self._leases.pop(proxy_id, None)
        return len(active)

    async def checkout(
        self,
        *,
        kind: KindFilter = None,
        sitekey: Optional[str] = None,
    ) -> Optional[ProxyAsset]:
        kinds = _normalise_kinds(kind)
        async with self._get_lock():
            now = time.monotonic()
            candidates = [
                proxy
                for proxy in self._proxies.values()
                if self._is_available(proxy, now)
                and (kinds is None or proxy.kind in kinds)
            ]
            if not candidates:
                return None

            def rank(proxy: ProxyAsset) -> tuple[float, int, float]:
                load = self._lease_count_locked(proxy.id, now)
                return (
                    _load_adjusted_utility(proxy, sitekey, load),
                    -load,
                    -proxy.last_used_at,
                )

            best = max(candidates, key=rank)
            best.last_used_at = now
            if best.success_count == 0:
                best.state = "probing"
            self._leases.setdefault(best.id, []).append(
                now + self._lease_ttl_seconds
            )
            return best

    async def report(
        self,
        proxy_id: str,
        *,
        success: bool,
        bytes_used: int = 0,
    ) -> None:
        """Apply one solve outcome and release one active lease atomically."""

        async with self._get_lock():
            proxy = self._proxies.get(proxy_id)
            if proxy is not None:
                proxy.bytes_used += max(0, bytes_used)
                if success:
                    proxy.success_count += 1
                    proxy.consecutive_fails = 0
                    if proxy.state != "burned":
                        proxy.state = "healthy"
                else:
                    proxy.fail_count += 1
                    proxy.consecutive_fails += 1
                    if (
                        proxy.consecutive_fails
                        >= self._max_consecutive_fails
                    ):
                        proxy.state = "cooldown"
                        proxy.cooldown_until = (
                            time.monotonic() + self._cooldown_seconds
                        )
                if (
                    self._max_bytes_per_proxy
                    and proxy.bytes_used >= self._max_bytes_per_proxy
                ):
                    proxy.state = "burned"

            now = time.monotonic()
            leases = [
                expiry
                for expiry in self._leases.get(proxy_id, ())
                if expiry > now
            ]
            if leases:
                leases.pop(0)
            if leases:
                self._leases[proxy_id] = leases
            else:
                self._leases.pop(proxy_id, None)

    async def _record_sitekey(
        self,
        proxy_id: str,
        sitekey: str,
        *,
        success: bool,
        real: bool,
    ) -> None:
        async with self._get_lock():
            proxy = self._proxies.get(proxy_id)
            if proxy is None:
                return

            bucket = (
                proxy.real_sitekey_stats if real else proxy.sitekey_stats
            )
            stats = bucket.setdefault(sitekey, [0, 0])
            stats[0 if success else 1] += 1

            lru = self._sitekey_lru.setdefault(
                proxy_id, OrderedDict()
            )
            lru.pop(sitekey, None)
            lru[sitekey] = time.monotonic()
            while len(lru) > self._sitekey_limit:
                stale, _ = lru.popitem(last=False)
                proxy.sitekey_stats.pop(stale, None)
                proxy.real_sitekey_stats.pop(stale, None)

    async def report_sitekey(
        self,
        proxy_id: str,
        sitekey: str,
        *,
        success: bool,
    ) -> None:
        await self._record_sitekey(
            proxy_id,
            sitekey,
            success=success,
            real=False,
        )

    async def report_sitekey_real(
        self,
        proxy_id: str,
        sitekey: str,
        *,
        success: bool,
    ) -> None:
        await self._record_sitekey(
            proxy_id,
            sitekey,
            success=success,
            real=True,
        )

    def snapshot(self) -> List[Dict[str, Any]]:
        rows = super().snapshot()
        now = time.monotonic()
        for row in rows:
            row["in_flight"] = self._lease_count_locked(
                str(row["id"]), now
            )
        return rows

    async def close(self) -> None:
        """Lifecycle parity with the Redis backend."""
        return None


class IndexedRedisProxyPool(RedisProxyPool):
    """Redis proxy pool with bounded candidate selection and expiring leases."""

    def __init__(
        self,
        url: str,
        *,
        cooldown_seconds: int = 120,
        max_consecutive_fails: int = 3,
        key_prefix: str = "captcha:proxy",
        lock_timeout_seconds: int = 5,
        lock_wait_seconds: float = 2.0,
        max_bytes_per_proxy: int = 0,
        candidate_window: int = 32,
        sitekey_limit: int = 128,
        lease_ttl_seconds: int = 240,
    ) -> None:
        super().__init__(
            url,
            cooldown_seconds=cooldown_seconds,
            max_consecutive_fails=max_consecutive_fails,
            key_prefix=key_prefix,
            lock_timeout_seconds=lock_timeout_seconds,
            max_bytes_per_proxy=max_bytes_per_proxy,
        )
        self._lock_wait_seconds = max(0.05, float(lock_wait_seconds))
        self._candidate_window = max(4, int(candidate_window))
        self._sitekey_limit = max(1, int(sitekey_limit))
        self._lease_ttl_seconds = max(30, int(lease_ttl_seconds))

        self._active_all_key = f"{key_prefix}:index:active:all"
        self._active_kind_prefix = f"{key_prefix}:index:active:kind"
        self._cooldown_index_key = f"{key_prefix}:index:cooldown"
        self._sitekey_index_prefix = f"{key_prefix}:index:sitekey"
        self._sitekey_lru_prefix = f"{key_prefix}:sitekey-lru"
        self._lease_prefix = f"{key_prefix}:leases"

        # Migration is O(n) once per process start, not once per checkout. It
        # is additive and safe when several workers start concurrently.
        self._reconcile_indexes_sync()

    def _active_kind_key(self, kind: str) -> str:
        return f"{self._active_kind_prefix}:{kind}"

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]

    def _sitekey_index_key(self, sitekey: str) -> str:
        return f"{self._sitekey_index_prefix}:{self._digest(sitekey)}"

    def _sitekey_lru_key(self, proxy_id: str) -> str:
        return f"{self._sitekey_lru_prefix}:{self._digest(proxy_id)}"

    def _lease_key(self, proxy_id: str) -> str:
        return f"{self._lease_prefix}:{self._digest(proxy_id)}"

    @staticmethod
    def _active_score(proxy: ProxyAsset) -> float:
        # Higher scores are selected first; negation implements least-recently
        # used ordering on epoch timestamps.
        return -float(proxy.last_used_at or 0.0)

    @staticmethod
    def _sitekey_score(proxy: ProxyAsset, sitekey: str) -> float:
        # Quality dominates while last-used breaks equal-quality ties.
        return _sitekey_signal(proxy, sitekey) * 1_000_000_000_000.0 - float(
            proxy.last_used_at or 0.0
        )

    def _queue_availability_sync(
        self,
        pipe: Any,
        proxy: ProxyAsset,
        *,
        now: Optional[float] = None,
    ) -> None:
        current = time.time() if now is None else now
        active_keys = [
            self._active_all_key,
            *(self._active_kind_key(kind) for kind in _KNOWN_KINDS),
        ]
        if proxy.state == "burned":
            for key in active_keys:
                pipe.zrem(key, proxy.id)
            pipe.zrem(self._cooldown_index_key, proxy.id)
            return
        if (
            proxy.state == "cooldown"
            and proxy.cooldown_until > current
        ):
            for key in active_keys:
                pipe.zrem(key, proxy.id)
            pipe.zadd(
                self._cooldown_index_key,
                {proxy.id: float(proxy.cooldown_until)},
            )
            return

        pipe.zadd(
            self._active_all_key,
            {proxy.id: self._active_score(proxy)},
        )
        pipe.zadd(
            self._active_kind_key(proxy.kind),
            {proxy.id: self._active_score(proxy)},
        )
        for kind in _KNOWN_KINDS:
            if kind != proxy.kind:
                pipe.zrem(self._active_kind_key(kind), proxy.id)
        pipe.zrem(self._cooldown_index_key, proxy.id)

    def _queue_all_sitekey_indexes_sync(
        self,
        pipe: Any,
        proxy: ProxyAsset,
        *,
        active: bool,
        initialise_lru: bool = False,
    ) -> None:
        for index, sitekey in enumerate(_ordered_sitekeys(proxy)):
            key = self._sitekey_index_key(sitekey)
            if active:
                pipe.zadd(
                    key,
                    {proxy.id: self._sitekey_score(proxy, sitekey)},
                )
            else:
                pipe.zrem(key, proxy.id)
            if initialise_lru:
                pipe.zadd(
                    self._sitekey_lru_key(proxy.id),
                    {sitekey: float(index + 1)},
                    nx=True,
                )

    def _queue_full_index_sync(
        self,
        pipe: Any,
        proxy: ProxyAsset,
        *,
        now: Optional[float] = None,
        initialise_lru: bool = False,
    ) -> None:
        current = time.time() if now is None else now
        self._queue_availability_sync(pipe, proxy, now=current)
        self._queue_all_sitekey_indexes_sync(
            pipe,
            proxy,
            active=_is_active(proxy, current),
            initialise_lru=initialise_lru,
        )

    async def _index_proxy_locked(
        self,
        proxy: ProxyAsset,
        *,
        full: bool,
        now: Optional[float] = None,
    ) -> None:
        pipe = self._redis.pipeline(transaction=False)
        if full:
            self._queue_full_index_sync(pipe, proxy, now=now)
        else:
            self._queue_availability_sync(pipe, proxy, now=now)
        await pipe.execute()

    def _reconcile_indexes_sync(self) -> None:
        raw = self._sync_redis.hgetall(self._proxies_key)
        if not raw:
            return

        pipe = self._sync_redis.pipeline(transaction=False)
        for raw_proxy_id, blob in raw.items():
            proxy_id = _redis_text(raw_proxy_id)
            try:
                proxy = self._deserialize(_redis_text(blob))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
            evicted = _prune_sitekey_history(
                proxy, self._sitekey_limit
            )
            if evicted:
                pipe.hset(
                    self._proxies_key,
                    proxy_id,
                    self._serialize(proxy),
                )
                for sitekey in evicted:
                    pipe.zrem(
                        self._sitekey_index_key(sitekey),
                        proxy_id,
                    )
            self._queue_full_index_sync(
                pipe,
                proxy,
                initialise_lru=True,
            )

        # Remove stale ids from the finite availability indexes. Sitekey
        # indexes are cleaned lazily through their per-proxy reverse LRU.
        existing_ids = {_redis_text(proxy_id) for proxy_id in raw}
        for key in (
            self._active_all_key,
            self._cooldown_index_key,
            *(self._active_kind_key(kind) for kind in _KNOWN_KINDS),
        ):
            indexed = set(
                _redis_members(self._sync_redis.zrange(key, 0, -1))
            )
            stale = indexed - existing_ids
            if stale:
                pipe.zrem(key, *stale)
        pipe.execute()

    def add(self, proxy: ProxyAsset) -> None:
        evicted = _prune_sitekey_history(
            proxy, self._sitekey_limit
        )
        super().add(proxy)
        pipe = self._sync_redis.pipeline(transaction=False)
        for sitekey in evicted:
            pipe.zrem(self._sitekey_index_key(sitekey), proxy.id)
        self._queue_full_index_sync(
            pipe,
            proxy,
            initialise_lru=True,
        )
        pipe.execute()

    def has_available(self, *, kind: KindFilter = None) -> bool:
        """Constant-time best-effort scheduler peek using availability indexes."""

        kinds = _normalise_kinds(kind)
        try:
            if kinds is None:
                active = self._sync_redis.zcard(self._active_all_key)
            else:
                active = any(
                    self._sync_redis.zcard(
                        self._active_kind_key(value)
                    )
                    for value in kinds
                )
            if active:
                return True
            # An expired cooldown can be promoted by checkout. This global
            # fallback may be a kind false-positive, which is harmless because
            # the caller already treats has_available as a racy routing hint.
            return bool(
                self._sync_redis.zcount(
                    self._cooldown_index_key,
                    "-inf",
                    time.time(),
                )
            )
        except Exception:
            return False

    async def _acquire_lock(self) -> str:
        """Acquire the mutation lock or fail closed after a bounded wait."""

        token = secrets.token_hex(16)
        deadline = time.monotonic() + self._lock_wait_seconds
        while time.monotonic() < deadline:
            ok = await self._redis.set(
                self._lock_key,
                token,
                nx=True,
                ex=self._lock_timeout,
            )
            if ok:
                return token
            await asyncio.sleep(0.025)
        raise ProxyPoolBusy(
            "proxy scheduler is busy; refusing an unlocked state mutation"
        )

    async def _promote_expired_locked(self, now: float) -> None:
        raw_ids = await self._redis.zrangebyscore(
            self._cooldown_index_key,
            "-inf",
            now,
            start=0,
            num=self._candidate_window,
        )
        ids = _redis_members(raw_ids)
        if not ids:
            return

        blobs = await self._redis.hmget(self._proxies_key, ids)
        pipe = self._redis.pipeline(transaction=False)
        for proxy_id, blob in zip(ids, blobs):
            if blob is None:
                pipe.zrem(self._cooldown_index_key, proxy_id)
                pipe.delete(self._lease_key(str(proxy_id)))
                continue
            try:
                proxy = self._deserialize(_redis_text(blob))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                pipe.zrem(self._cooldown_index_key, proxy_id)
                continue
            if (
                proxy.state == "cooldown"
                and proxy.cooldown_until <= now
            ):
                proxy.state = "healthy"
                proxy.consecutive_fails = 0
                pipe.hset(
                    self._proxies_key,
                    proxy.id,
                    self._serialize(proxy),
                )
                self._queue_full_index_sync(
                    pipe,
                    proxy,
                    now=now,
                )
            elif proxy.state != "cooldown":
                self._queue_full_index_sync(
                    pipe,
                    proxy,
                    now=now,
                )
        await pipe.execute()

    async def _candidate_ids_locked(
        self,
        kinds: Optional[frozenset[str]],
        sitekey: Optional[str],
    ) -> List[str]:
        ids: List[str] = []
        if sitekey:
            ids.extend(
                _redis_members(
                    await self._redis.zrevrange(
                        self._sitekey_index_key(sitekey),
                        0,
                        self._candidate_window - 1,
                    )
                )
            )

        if kinds is None:
            ids.extend(
                _redis_members(
                    await self._redis.zrevrange(
                        self._active_all_key,
                        0,
                        self._candidate_window - 1,
                    )
                )
            )
        else:
            pipe = self._redis.pipeline(transaction=False)
            for value in sorted(kinds):
                pipe.zrevrange(
                    self._active_kind_key(value),
                    0,
                    self._candidate_window - 1,
                )
            for chunk in await pipe.execute():
                ids.extend(_redis_members(chunk))

        # Preserve quality-index priority while removing duplicates.
        return list(dict.fromkeys(ids))

    async def _lease_counts_locked(
        self,
        proxy_ids: Sequence[str],
        now: float,
    ) -> Dict[str, int]:
        if not proxy_ids:
            return {}
        pipe = self._redis.pipeline(transaction=False)
        for proxy_id in proxy_ids:
            lease_key = self._lease_key(proxy_id)
            pipe.zremrangebyscore(lease_key, "-inf", now)
            pipe.zcard(lease_key)
        results = await pipe.execute()
        return {
            proxy_id: int(results[index * 2 + 1] or 0)
            for index, proxy_id in enumerate(proxy_ids)
        }

    async def _remove_stale_candidate_locked(
        self,
        proxy_id: str,
        *,
        delete_hash: bool,
    ) -> None:
        lru_key = self._sitekey_lru_key(proxy_id)
        sitekeys = _redis_members(
            await self._redis.zrange(lru_key, 0, -1)
        )
        pipe = self._redis.pipeline(transaction=False)
        pipe.zrem(self._active_all_key, proxy_id)
        pipe.zrem(self._cooldown_index_key, proxy_id)
        for kind in _KNOWN_KINDS:
            pipe.zrem(self._active_kind_key(kind), proxy_id)
        for sitekey in sitekeys:
            pipe.zrem(
                self._sitekey_index_key(sitekey),
                proxy_id,
            )
        pipe.delete(self._lease_key(proxy_id))
        pipe.delete(lru_key)
        if delete_hash:
            pipe.hdel(self._proxies_key, proxy_id)
        await pipe.execute()

    async def checkout(
        self,
        *,
        kind: KindFilter = None,
        sitekey: Optional[str] = None,
    ) -> Optional[ProxyAsset]:
        """Lease the best proxy from a bounded, indexed candidate window."""

        kinds = _normalise_kinds(kind)
        token = await self._acquire_lock()
        try:
            now = time.time()
            await self._promote_expired_locked(now)
            proxy_ids = await self._candidate_ids_locked(kinds, sitekey)
            if not proxy_ids:
                return None

            blobs = await self._redis.hmget(self._proxies_key, proxy_ids)
            loads = await self._lease_counts_locked(proxy_ids, now)
            candidates: List[ProxyAsset] = []
            for proxy_id, blob in zip(proxy_ids, blobs):
                if blob is None:
                    await self._remove_stale_candidate_locked(
                        proxy_id,
                        delete_hash=False,
                    )
                    continue
                try:
                    proxy = self._deserialize(_redis_text(blob))
                except (
                    json.JSONDecodeError,
                    KeyError,
                    TypeError,
                    ValueError,
                ):
                    await self._remove_stale_candidate_locked(
                        proxy_id,
                        delete_hash=True,
                    )
                    continue
                if not _is_active(proxy, now):
                    await self._index_proxy_locked(
                        proxy,
                        full=True,
                        now=now,
                    )
                    continue
                if kinds is not None and proxy.kind not in kinds:
                    continue
                candidates.append(proxy)

            if not candidates:
                return None

            def rank(proxy: ProxyAsset) -> tuple[float, int, float]:
                load = loads.get(proxy.id, 0)
                return (
                    _load_adjusted_utility(proxy, sitekey, load),
                    -load,
                    -proxy.last_used_at,
                )

            best = max(candidates, key=rank)
            best.last_used_at = now
            if best.success_count == 0:
                best.state = "probing"

            lease_token = secrets.token_hex(16)
            pipe = self._redis.pipeline(transaction=False)
            pipe.hset(
                self._proxies_key,
                best.id,
                self._serialize(best),
            )
            pipe.zadd(
                self._lease_key(best.id),
                {lease_token: now + self._lease_ttl_seconds},
            )
            self._queue_availability_sync(pipe, best, now=now)
            if sitekey and (
                sitekey in best.sitekey_stats
                or sitekey in best.real_sitekey_stats
            ):
                pipe.zadd(
                    self._sitekey_index_key(sitekey),
                    {best.id: self._sitekey_score(best, sitekey)},
                )
            await pipe.execute()
            return best
        finally:
            await self._release_lock(token)

    async def _release_one_lease_locked(
        self,
        proxy_id: str,
        now: float,
    ) -> None:
        lease_key = self._lease_key(proxy_id)
        await self._redis.zremrangebyscore(lease_key, "-inf", now)
        await self._redis.zpopmin(lease_key, 1)

    async def report(
        self,
        proxy_id: str,
        *,
        success: bool,
        bytes_used: int = 0,
    ) -> None:
        """Apply one outcome and release one lease under the scheduler lock."""

        try:
            token = await self._acquire_lock()
        except ProxyPoolBusy:
            # Outcome feedback must never overturn an already-produced token.
            # The lease expires automatically and no unlocked mutation occurs.
            log.warning("proxy report skipped because scheduler is busy")
            return
        try:
            now = time.time()
            await self._release_one_lease_locked(proxy_id, now)
            blob = await self._redis.hget(self._proxies_key, proxy_id)
            if blob is None:
                return
            try:
                proxy = self._deserialize(_redis_text(blob))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                return

            was_active = _is_active(proxy, now)
            proxy.bytes_used += max(0, bytes_used)
            if success:
                proxy.success_count += 1
                proxy.consecutive_fails = 0
                if proxy.state != "burned":
                    proxy.state = "healthy"
            else:
                proxy.fail_count += 1
                proxy.consecutive_fails += 1
                if (
                    proxy.consecutive_fails
                    >= self._max_consecutive_fails
                ):
                    proxy.state = "cooldown"
                    proxy.cooldown_until = now + self._cooldown_seconds

            if (
                self._max_bytes_per_proxy
                and proxy.bytes_used >= self._max_bytes_per_proxy
            ):
                proxy.state = "burned"
            is_active = _is_active(proxy, now)

            pipe = self._redis.pipeline(transaction=False)
            pipe.hset(
                self._proxies_key,
                proxy_id,
                self._serialize(proxy),
            )
            self._queue_availability_sync(pipe, proxy, now=now)
            if was_active != is_active:
                self._queue_all_sitekey_indexes_sync(
                    pipe,
                    proxy,
                    active=is_active,
                )
            await pipe.execute()
        finally:
            await self._release_lock(token)

    async def _record_sitekey(
        self,
        proxy_id: str,
        sitekey: str,
        *,
        success: bool,
        real: bool,
    ) -> None:
        try:
            token = await self._acquire_lock()
        except ProxyPoolBusy:
            log.warning("sitekey feedback skipped because scheduler is busy")
            return
        try:
            blob = await self._redis.hget(self._proxies_key, proxy_id)
            if blob is None:
                return
            try:
                proxy = self._deserialize(_redis_text(blob))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                return

            bucket = (
                proxy.real_sitekey_stats if real else proxy.sitekey_stats
            )
            stats = bucket.setdefault(sitekey, [0, 0])
            stats[0 if success else 1] += 1

            lru_key = self._sitekey_lru_key(proxy_id)
            now = time.time()
            await self._redis.zadd(lru_key, {sitekey: now})
            count = int(await self._redis.zcard(lru_key) or 0)
            evicted: List[str] = []
            if count > self._sitekey_limit:
                raw_evicted = await self._redis.zpopmin(
                    lru_key,
                    count - self._sitekey_limit,
                )
                evicted = [_redis_text(item[0]) for item in raw_evicted]
                for stale in evicted:
                    proxy.sitekey_stats.pop(stale, None)
                    proxy.real_sitekey_stats.pop(stale, None)

            pipe = self._redis.pipeline(transaction=False)
            pipe.hset(
                self._proxies_key,
                proxy_id,
                self._serialize(proxy),
            )
            if _is_active(proxy, now):
                pipe.zadd(
                    self._sitekey_index_key(sitekey),
                    {proxy_id: self._sitekey_score(proxy, sitekey)},
                )
            else:
                pipe.zrem(
                    self._sitekey_index_key(sitekey),
                    proxy_id,
                )
            for stale in evicted:
                pipe.zrem(
                    self._sitekey_index_key(stale),
                    proxy_id,
                )
            await pipe.execute()
        finally:
            await self._release_lock(token)

    async def report_sitekey(
        self,
        proxy_id: str,
        sitekey: str,
        *,
        success: bool,
    ) -> None:
        await self._record_sitekey(
            proxy_id,
            sitekey,
            success=success,
            real=False,
        )

    async def report_sitekey_real(
        self,
        proxy_id: str,
        sitekey: str,
        *,
        success: bool,
    ) -> None:
        await self._record_sitekey(
            proxy_id,
            sitekey,
            success=success,
            real=True,
        )

    def snapshot(self) -> List[Dict[str, Any]]:
        rows = super().snapshot()
        if not rows:
            return rows

        now = time.time()
        pipe = self._sync_redis.pipeline(transaction=False)
        for row in rows:
            key = self._lease_key(str(row["id"]))
            pipe.zremrangebyscore(key, "-inf", now)
            pipe.zcard(key)
        results = pipe.execute()
        for index, row in enumerate(rows):
            row["in_flight"] = int(results[index * 2 + 1] or 0)
        return rows


def build_managed_proxy_pool(
    config: Any,
) -> "ManagedProxyPool | IndexedRedisProxyPool":
    """Select the managed backend while preserving the old pool contract."""

    max_bytes = _gb_to_bytes(getattr(config, "proxy_max_gb", 0.0))
    lease_ttl = max(
        60,
        int(getattr(config, "solve_timeout", 180)) + 30,
    )
    cooldown_seconds = int(
        getattr(config, "proxy_cooldown", 120)
    )
    max_consecutive_fails = int(
        getattr(config, "proxy_max_consecutive_fails", 3)
    )
    sitekey_limit = int(
        getattr(config, "proxy_sitekey_limit", 128)
    )
    redis_url = getattr(config, "redis_url", None)
    if redis_url:
        return IndexedRedisProxyPool(
            redis_url,
            cooldown_seconds=cooldown_seconds,
            max_consecutive_fails=max_consecutive_fails,
            max_bytes_per_proxy=max_bytes,
            sitekey_limit=sitekey_limit,
            lock_wait_seconds=float(
                getattr(config, "proxy_lock_wait_seconds", 2.0)
            ),
            candidate_window=int(
                getattr(config, "proxy_candidate_window", 32)
            ),
            lease_ttl_seconds=lease_ttl,
        )
    return ManagedProxyPool(
        cooldown_seconds=cooldown_seconds,
        max_consecutive_fails=max_consecutive_fails,
        max_bytes_per_proxy=max_bytes,
        sitekey_limit=sitekey_limit,
        lease_ttl_seconds=lease_ttl,
    )
