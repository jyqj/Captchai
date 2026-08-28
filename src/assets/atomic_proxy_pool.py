"""Atomic Redis checkout and non-blocking proxy inventory snapshots.

This layer builds on :mod:`src.assets.indexed_proxy_pool`. Candidate discovery
remains a bounded, read-only index lookup, while the consistency-sensitive part
of checkout is one Redis Lua transaction: revalidate candidates, prune expired
leases, rank by quality/load, update the selected asset, and create its lease.

The script also observes the legacy mutation lock used by report/sitekey writes.
That preserves compatibility while allowing concurrent checkouts to serialize
inside Redis without acquiring that global lock themselves.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import secrets
import time
from typing import Any, Awaitable, Dict, Iterable, List, Optional, Union, cast

from .indexed_proxy_pool import (
    IndexedRedisProxyPool,
    ManagedProxyPool,
    ProxyPoolBusy,
    _normalise_kinds,
    _redis_members,
    _redis_text,
)
from .proxy_pool import ProxyAsset

log = logging.getLogger(__name__)

KindFilter = Optional[Union[str, Iterable[str]]]
_KNOWN_KINDS = ("datacenter", "residential", "mobile")


_ATOMIC_CHECKOUT_LUA = r"""
local proxies_key = KEYS[1]
local active_all_key = KEYS[2]
local active_datacenter_key = KEYS[3]
local active_residential_key = KEYS[4]
local active_mobile_key = KEYS[5]
local cooldown_key = KEYS[6]
local sitekey_index_key = KEYS[7]
local mutation_lock_key = KEYS[8]

local now = tonumber(ARGV[1]) or 0
local lease_expiry = tonumber(ARGV[2]) or now
local lease_token = ARGV[3]
local requested_sitekey = ARGV[4]
local allowed_csv = ARGV[5]
local candidate_count = tonumber(ARGV[6]) or 0

-- report()/sitekey mutations still use the legacy lock. Checking it inside
-- the script is race-free because Redis executes this entire script atomically.
if redis.call('EXISTS', mutation_lock_key) == 1 then
    return {'BUSY'}
end

local kind_keys = {
    datacenter = active_datacenter_key,
    residential = active_residential_key,
    mobile = active_mobile_key,
}

local allowed = nil
if allowed_csv ~= '' then
    allowed = {}
    for kind in string.gmatch(allowed_csv, '[^,]+') do
        allowed[kind] = true
    end
end

local function remove_from_static_indexes(proxy_id)
    redis.call('ZREM', active_all_key, proxy_id)
    redis.call('ZREM', active_datacenter_key, proxy_id)
    redis.call('ZREM', active_residential_key, proxy_id)
    redis.call('ZREM', active_mobile_key, proxy_id)
    redis.call('ZREM', cooldown_key, proxy_id)
    if requested_sitekey ~= '' then
        redis.call('ZREM', sitekey_index_key, proxy_id)
    end
end

local function stats_rate(stats)
    if type(stats) ~= 'table' then
        return nil
    end
    local successes = tonumber(stats[1] or 0) or 0
    local failures = tonumber(stats[2] or 0) or 0
    local total = successes + failures
    if total <= 0 then
        return nil
    end
    return successes / total
end

local function site_signal(data)
    if requested_sitekey == '' then
        return 0.5, false
    end

    local real_bucket = data['real_sitekey_stats']
    local token_bucket = data['sitekey_stats']
    local real_stats = nil
    local token_stats = nil
    if type(real_bucket) == 'table' then
        real_stats = real_bucket[requested_sitekey]
    end
    if type(token_bucket) == 'table' then
        token_stats = token_bucket[requested_sitekey]
    end

    local real_rate = stats_rate(real_stats)
    if real_rate ~= nil then
        return real_rate, true
    end
    local token_rate = stats_rate(token_stats)
    if token_rate ~= nil then
        return token_rate, true
    end
    return 0.5, false
end

local function overall_rate(data)
    local successes = tonumber(data['success_count'] or 0) or 0
    local failures = tonumber(data['fail_count'] or 0) or 0
    local total = successes + failures
    if total <= 0 then
        return 1.0
    end
    return successes / total
end

local best_id = nil
local best_data = nil
local best_lease_key = nil
local best_kind = nil
local best_site_signal = 0.5
local best_has_site_stats = false
local best_utility = nil
local best_load = nil
local best_last_used = nil

for index = 1, candidate_count do
    local proxy_id = ARGV[6 + index]
    local lease_key = KEYS[8 + index]
    local lru_key = KEYS[8 + candidate_count + index]
    local blob = redis.call('HGET', proxies_key, proxy_id)

    if not blob then
        remove_from_static_indexes(proxy_id)
        redis.call('DEL', lease_key)
        redis.call('DEL', lru_key)
    else
        local ok, data = pcall(cjson.decode, blob)
        if not ok or type(data) ~= 'table' then
            redis.call('HDEL', proxies_key, proxy_id)
            remove_from_static_indexes(proxy_id)
            redis.call('DEL', lease_key)
            redis.call('DEL', lru_key)
        else
            local state = tostring(data['state'] or 'healthy')
            local kind = tostring(data['kind'] or 'datacenter')
            local cooldown_until = tonumber(data['cooldown_until'] or 0) or 0
            local last_used = tonumber(data['last_used_at'] or 0) or 0
            local changed = false

            if state == 'cooldown' and cooldown_until <= now then
                state = 'healthy'
                data['state'] = 'healthy'
                data['consecutive_fails'] = 0
                changed = true
            end

            local active = state ~= 'burned'
                and not (state == 'cooldown' and cooldown_until > now)

            if active then
                local active_score = -last_used
                redis.call('ZADD', active_all_key, active_score, proxy_id)
                for known_kind, key in pairs(kind_keys) do
                    if known_kind == kind then
                        redis.call('ZADD', key, active_score, proxy_id)
                    else
                        redis.call('ZREM', key, proxy_id)
                    end
                end
                redis.call('ZREM', cooldown_key, proxy_id)

                local signal, has_site_stats = site_signal(data)
                if requested_sitekey ~= '' then
                    if has_site_stats then
                        redis.call(
                            'ZADD',
                            sitekey_index_key,
                            signal * 1000000000000.0 - last_used,
                            proxy_id
                        )
                    else
                        redis.call('ZREM', sitekey_index_key, proxy_id)
                    end
                end

                redis.call('ZREMRANGEBYSCORE', lease_key, '-inf', now)
                local load = tonumber(redis.call('ZCARD', lease_key)) or 0
                local kind_allowed = allowed == nil or allowed[kind] == true

                if kind_allowed then
                    local utility = (
                        0.75 * signal + 0.25 * overall_rate(data)
                    ) / (1.0 + load)
                    local better = best_id == nil
                        or utility > best_utility
                        or (
                            utility == best_utility
                            and (
                                load < best_load
                                or (
                                    load == best_load
                                    and (
                                        last_used < best_last_used
                                        or (
                                            last_used == best_last_used
                                            and proxy_id < best_id
                                        )
                                    )
                                )
                            )
                        )

                    if better then
                        best_id = proxy_id
                        best_data = data
                        best_lease_key = lease_key
                        best_kind = kind
                        best_site_signal = signal
                        best_has_site_stats = has_site_stats
                        best_utility = utility
                        best_load = load
                        best_last_used = last_used
                    end
                end

                if changed then
                    redis.call(
                        'HSET', proxies_key, proxy_id, cjson.encode(data)
                    )
                end
            else
                redis.call('ZREM', active_all_key, proxy_id)
                redis.call('ZREM', active_datacenter_key, proxy_id)
                redis.call('ZREM', active_residential_key, proxy_id)
                redis.call('ZREM', active_mobile_key, proxy_id)
                if requested_sitekey ~= '' then
                    redis.call('ZREM', sitekey_index_key, proxy_id)
                end
                if state == 'cooldown' and cooldown_until > now then
                    redis.call('ZADD', cooldown_key, cooldown_until, proxy_id)
                else
                    redis.call('ZREM', cooldown_key, proxy_id)
                end
            end
        end
    end
end

if best_id == nil then
    return {}
end

best_data['last_used_at'] = now
local selected_successes = tonumber(best_data['success_count'] or 0) or 0
if selected_successes == 0 then
    best_data['state'] = 'probing'
end

local updated_blob = cjson.encode(best_data)
redis.call('HSET', proxies_key, best_id, updated_blob)
redis.call('ZADD', best_lease_key, lease_expiry, lease_token)
redis.call('ZADD', active_all_key, -now, best_id)
for known_kind, key in pairs(kind_keys) do
    if known_kind == best_kind then
        redis.call('ZADD', key, -now, best_id)
    else
        redis.call('ZREM', key, best_id)
    end
end
redis.call('ZREM', cooldown_key, best_id)

if requested_sitekey ~= '' then
    if best_has_site_stats then
        redis.call(
            'ZADD',
            sitekey_index_key,
            best_site_signal * 1000000000000.0 - now,
            best_id
        )
    else
        redis.call('ZREM', sitekey_index_key, best_id)
    end
end

return {best_id, updated_blob, tostring(best_load + 1)}
"""


def _gb_to_bytes(value: Any) -> int:
    try:
        gb = float(value)
    except (TypeError, ValueError):
        return 0
    return int(gb * 1024**3) if gb > 0 else 0


def _snapshot_row(
    proxy: ProxyAsset,
    *,
    now: float,
    in_flight: int,
) -> Dict[str, Any]:
    return {
        "id": proxy.id,
        "server": proxy.server,
        "kind": proxy.kind,
        "state": proxy.state,
        "success_count": proxy.success_count,
        "fail_count": proxy.fail_count,
        "consecutive_fails": proxy.consecutive_fails,
        "success_rate": round(proxy.success_rate(), 4),
        "cooldown_remaining": max(0.0, proxy.cooldown_until - now),
        "cost_per_gb": proxy.cost_per_gb,
        "bytes_used": proxy.bytes_used,
        "sticky_session_id": proxy.sticky_session_id,
        "country": proxy.country,
        "timezone": proxy.timezone,
        "locale": proxy.locale,
        "geo_probed": proxy.geo_probed,
        "in_flight": in_flight,
        "sitekeys": {
            sitekey: {"success": stats[0], "fail": stats[1]}
            for sitekey, stats in proxy.sitekey_stats.items()
        },
        "real_sitekeys": {
            sitekey: {"success": stats[0], "fail": stats[1]}
            for sitekey, stats in proxy.real_sitekey_stats.items()
        },
    }


class AtomicManagedProxyPool(ManagedProxyPool):
    """In-memory parity backend with an awaitable snapshot surface."""

    async def snapshot_async(self) -> List[Dict[str, Any]]:
        async with self._get_lock():
            return self.snapshot()


class AtomicRedisProxyPool(IndexedRedisProxyPool):
    """Indexed Redis scheduler whose checkout commit is one Lua transaction."""

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
        snapshot_batch_size: int = 128,
    ) -> None:
        super().__init__(
            url,
            cooldown_seconds=cooldown_seconds,
            max_consecutive_fails=max_consecutive_fails,
            key_prefix=key_prefix,
            lock_timeout_seconds=lock_timeout_seconds,
            lock_wait_seconds=lock_wait_seconds,
            max_bytes_per_proxy=max_bytes_per_proxy,
            candidate_window=candidate_window,
            sitekey_limit=sitekey_limit,
            lease_ttl_seconds=lease_ttl_seconds,
        )
        self._snapshot_batch_size = max(16, int(snapshot_batch_size))

    async def _bounded_candidate_ids(
        self,
        kinds: Optional[frozenset[str]],
        sitekey: Optional[str],
    ) -> List[str]:
        """Read a quality/exploration mix whose total never exceeds the window."""

        window = self._candidate_window
        kind_count = len(kinds) if kinds else 1
        minimum_general = min(window, max(1, kind_count))
        quality_budget = 0
        if sitekey:
            quality_budget = min(
                window // 2,
                max(0, window - minimum_general),
            )
        general_budget = window - quality_budget

        pipe = self._redis.pipeline(transaction=False)
        queued: List[str] = []
        if quality_budget:
            pipe.zrevrange(
                self._sitekey_index_key(sitekey or ""),
                0,
                quality_budget - 1,
            )
            queued.append("quality")

        if kinds is None:
            if general_budget:
                pipe.zrevrange(
                    self._active_all_key,
                    0,
                    general_budget - 1,
                )
                queued.append("general")
        else:
            ordered_kinds = sorted(kinds)
            base = general_budget // len(ordered_kinds)
            remainder = general_budget % len(ordered_kinds)
            for index, value in enumerate(ordered_kinds):
                count = base + (1 if index < remainder else 0)
                if count <= 0:
                    continue
                pipe.zrevrange(
                    self._active_kind_key(value),
                    0,
                    count - 1,
                )
                queued.append(value)

        if not queued:
            return []
        chunks = await pipe.execute()
        ids: List[str] = []
        for chunk in chunks:
            ids.extend(_redis_members(chunk))
        return list(dict.fromkeys(ids))[:window]

    async def _expired_cooldown_ids(self, now: float) -> List[str]:
        values = await self._redis.zrangebyscore(
            self._cooldown_index_key,
            "-inf",
            now,
            start=0,
            num=self._candidate_window,
        )
        return _redis_members(values)[: self._candidate_window]

    def _checkout_keys(
        self,
        candidate_ids: List[str],
        sitekey: Optional[str],
    ) -> List[str]:
        static = [
            self._proxies_key,
            self._active_all_key,
            self._active_kind_key("datacenter"),
            self._active_kind_key("residential"),
            self._active_kind_key("mobile"),
            self._cooldown_index_key,
            self._sitekey_index_key(sitekey or ""),
            self._lock_key,
        ]
        leases = [self._lease_key(proxy_id) for proxy_id in candidate_ids]
        lrus = [self._sitekey_lru_key(proxy_id) for proxy_id in candidate_ids]
        return [*static, *leases, *lrus]

    async def _atomic_select(
        self,
        candidate_ids: List[str],
        *,
        kinds: Optional[frozenset[str]],
        sitekey: Optional[str],
        now: float,
    ) -> List[str]:
        lease_token = secrets.token_hex(16)
        keys = self._checkout_keys(candidate_ids, sitekey)
        args: List[Union[str, int, float]] = [
            now,
            now + self._lease_ttl_seconds,
            lease_token,
            sitekey or "",
            ",".join(sorted(kinds)) if kinds else "",
            len(candidate_ids),
            *candidate_ids,
        ]
        raw = await self._redis.eval(
            _ATOMIC_CHECKOUT_LUA,
            len(keys),
            *keys,
            *args,
        )
        return _redis_members(raw)

    async def checkout(
        self,
        *,
        kind: KindFilter = None,
        sitekey: Optional[str] = None,
    ) -> Optional[ProxyAsset]:
        """Select and lease a proxy atomically without acquiring the global lock."""

        kinds = _normalise_kinds(kind)
        deadline = time.monotonic() + self._lock_wait_seconds
        stale_retries = 0

        while True:
            now = time.time()
            candidate_ids = await self._bounded_candidate_ids(kinds, sitekey)
            expired_ids = await self._expired_cooldown_ids(now)
            if expired_ids:
                # Reserve one slot for rehabilitation even while the active
                # index is non-empty. Without this, an expired cooldown proxy
                # can remain stranded forever behind a continuously available
                # incumbent. When no active candidates exist, the full bounded
                # window is available for cooldown recovery.
                recovery_budget = (
                    self._candidate_window if not candidate_ids else 1
                )
                candidate_ids = list(
                    dict.fromkeys(
                        [*expired_ids[:recovery_budget], *candidate_ids]
                    )
                )[: self._candidate_window]
            if not candidate_ids:
                return None

            result = await self._atomic_select(
                candidate_ids,
                kinds=kinds,
                sitekey=sitekey,
                now=now,
            )
            if result and result[0] == "BUSY":
                if time.monotonic() >= deadline:
                    raise ProxyPoolBusy(
                        "proxy scheduler is busy; atomic checkout timed out"
                    )
                await asyncio.sleep(0.025)
                continue
            if not result:
                # The script may have repaired stale index rows. Re-read the
                # bounded indexes a few times before declaring the pool empty.
                stale_retries += 1
                if stale_retries <= 2:
                    continue
                return None
            if len(result) < 2:
                raise RuntimeError("atomic proxy checkout returned an invalid result")
            return self._deserialize(_redis_text(result[1]))

    async def snapshot_async(self) -> List[Dict[str, Any]]:
        """Cooperatively scan Redis without blocking the FastAPI event loop."""

        cursor = 0
        rows: List[Dict[str, Any]] = []
        now = time.time()
        try:
            while True:
                cursor, payload = await self._redis.hscan(
                    self._proxies_key,
                    cursor=cursor,
                    count=self._snapshot_batch_size,
                )
                if isinstance(payload, dict):
                    proxies: List[ProxyAsset] = []
                    for blob in payload.values():
                        try:
                            proxies.append(
                                self._deserialize(_redis_text(blob))
                            )
                        except (
                            ValueError,
                            TypeError,
                            KeyError,
                            AttributeError,
                        ):
                            continue

                    pipe = self._redis.pipeline(transaction=False)
                    for proxy in proxies:
                        lease_key = self._lease_key(proxy.id)
                        pipe.zremrangebyscore(lease_key, "-inf", now)
                        pipe.zcard(lease_key)
                    lease_results = await pipe.execute() if proxies else []
                    for index, proxy in enumerate(proxies):
                        in_flight = int(lease_results[index * 2 + 1] or 0)
                        rows.append(
                            _snapshot_row(
                                proxy,
                                now=now,
                                in_flight=in_flight,
                            )
                        )

                if int(cursor) == 0:
                    break
                await asyncio.sleep(0)
        except Exception:  # noqa: BLE001 - admin snapshot is best-effort
            log.exception("Could not build async proxy snapshot")
            return []

        rows.sort(key=lambda row: str(row["id"]))
        return rows


async def snapshot_proxy_pool(pool: Any) -> List[Dict[str, Any]]:
    """Use an awaitable snapshot when available, otherwise offload sync work."""

    snapshot_async = getattr(pool, "snapshot_async", None)
    if callable(snapshot_async):
        result = snapshot_async()
        if not inspect.isawaitable(result):
            raise TypeError("snapshot_async must return an awaitable")
        return await cast(Awaitable[List[Dict[str, Any]]], result)
    return await asyncio.to_thread(pool.snapshot)


def build_atomic_proxy_pool(
    config: Any,
) -> "AtomicManagedProxyPool | AtomicRedisProxyPool":
    """Build the atomic scheduler while preserving the existing pool protocol."""

    max_bytes = _gb_to_bytes(getattr(config, "proxy_max_gb", 0.0))
    lease_ttl = max(
        60,
        int(getattr(config, "solve_timeout", 180)) + 30,
    )
    cooldown_seconds = int(getattr(config, "proxy_cooldown", 120))
    max_consecutive_fails = int(
        getattr(config, "proxy_max_consecutive_fails", 3)
    )
    sitekey_limit = int(getattr(config, "proxy_sitekey_limit", 128))
    redis_url = getattr(config, "redis_url", None)
    if redis_url:
        return AtomicRedisProxyPool(
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
            snapshot_batch_size=int(
                getattr(config, "proxy_snapshot_batch_size", 128)
            ),
        )
    return AtomicManagedProxyPool(
        cooldown_seconds=cooldown_seconds,
        max_consecutive_fails=max_consecutive_fails,
        max_bytes_per_proxy=max_bytes,
        sitekey_limit=sitekey_limit,
        lease_ttl_seconds=lease_ttl,
    )
