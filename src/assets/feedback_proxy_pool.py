"""Atomic proxy feedback mutations with exact lease attribution.

This layer completes the Redis asset-plane migration started by
:mod:`src.assets.atomic_proxy_pool`. Checkout already commits selection and
lease creation in one Lua script; this module moves the remaining write-side
operations into atomic scripts as well:

* solve outcomes update health and release the exact checkout lease;
* token-obtained and real-outcome sitekey counters update without lost writes;
* LRU retention and quality indexes are maintained in the same transaction;
* geo probe results persist without the compatibility mutation lock.

The public pool protocol stays compatible. A checkout lease token is attached
transiently to the returned :class:`ProxyAsset` and also tracked in task-local
context so existing solver call sites can report by ``proxy_id`` without
serialising or exposing the token.
"""

from __future__ import annotations

import asyncio
import contextvars
import copy
import logging
import secrets
import time
from collections import OrderedDict
from typing import Any, Dict, Iterable, List, Optional, Union

from .atomic_proxy_pool import (
    AtomicManagedProxyPool,
    AtomicRedisProxyPool,
    _ATOMIC_CHECKOUT_LUA,
    snapshot_proxy_pool,
)
from .indexed_proxy_pool import (
    _is_active,
    _load_adjusted_utility,
    _normalise_kinds,
    _ordered_sitekeys,
    _redis_members,
    _redis_text,
)
from .proxy_pool import ProxyAsset

log = logging.getLogger(__name__)

KindFilter = Optional[Union[str, Iterable[str]]]
_LEASE_TOKEN_ATTR = "_captchai_proxy_lease_token"


def proxy_lease_token(proxy: Any) -> Optional[str]:
    """Return the transient checkout lease token attached to ``proxy``."""

    value = getattr(proxy, _LEASE_TOKEN_ATTR, None)
    return str(value) if value else None


_ATOMIC_FEEDBACK_LUA = r"""
local proxies_key = KEYS[1]
local active_all_key = KEYS[2]
local active_datacenter_key = KEYS[3]
local active_residential_key = KEYS[4]
local active_mobile_key = KEYS[5]
local cooldown_key = KEYS[6]
local lease_key = KEYS[7]
local current_sitekey_index_key = KEYS[8]
local sitekey_lru_key = KEYS[9]
local sitekey_index_map_key = KEYS[10]
local mutation_lock_key = KEYS[11]

local proxy_id = ARGV[1]
local now = tonumber(ARGV[2]) or 0
local operation = ARGV[3]
local success = ARGV[4] == '1'
local bytes_used = tonumber(ARGV[5]) or 0
local lease_token = ARGV[6]
local sitekey = ARGV[7]
local sitekey_limit = math.max(1, tonumber(ARGV[8]) or 1)
local cooldown_seconds = tonumber(ARGV[9]) or 0
local max_consecutive_fails = math.max(1, tonumber(ARGV[10]) or 1)
local max_bytes_per_proxy = tonumber(ARGV[11]) or 0
local country = ARGV[12]
local timezone = ARGV[13]
local locale = ARGV[14]
local geo_probed = ARGV[15] == '1'

-- Mixed-version workers may still perform lock-protected Python RMW writes.
-- Observe (but never acquire) that compatibility lock so an old writer cannot
-- overwrite an atomic feedback commit during a rolling deployment.
if redis.call('EXISTS', mutation_lock_key) == 1 then
    return {'BUSY'}
end

local kind_keys = {
    datacenter = active_datacenter_key,
    residential = active_residential_key,
    mobile = active_mobile_key,
}

local function remove_static_indexes()
    redis.call('ZREM', active_all_key, proxy_id)
    redis.call('ZREM', active_datacenter_key, proxy_id)
    redis.call('ZREM', active_residential_key, proxy_id)
    redis.call('ZREM', active_mobile_key, proxy_id)
    redis.call('ZREM', cooldown_key, proxy_id)
end

local function remove_all_quality_indexes()
    local index_keys = redis.call('HVALS', sitekey_index_map_key)
    for _, index_key in ipairs(index_keys) do
        redis.call('ZREM', index_key, proxy_id)
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

local function site_signal(data, target_sitekey)
    if target_sitekey == '' then
        return 0.5, false
    end
    local real_bucket = data['real_sitekey_stats']
    local token_bucket = data['sitekey_stats']
    local real_stats = nil
    local token_stats = nil
    if type(real_bucket) == 'table' then
        real_stats = real_bucket[target_sitekey]
    end
    if type(token_bucket) == 'table' then
        token_stats = token_bucket[target_sitekey]
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

local function active_state(data)
    local state = tostring(data['state'] or 'healthy')
    local cooldown_until = tonumber(data['cooldown_until'] or 0) or 0
    return state ~= 'burned'
        and not (state == 'cooldown' and cooldown_until > now)
end

local function apply_availability(data)
    local state = tostring(data['state'] or 'healthy')
    local kind = tostring(data['kind'] or 'datacenter')
    local cooldown_until = tonumber(data['cooldown_until'] or 0) or 0
    local last_used = tonumber(data['last_used_at'] or 0) or 0
    local active = state ~= 'burned'
        and not (state == 'cooldown' and cooldown_until > now)

    if active then
        redis.call('ZADD', active_all_key, -last_used, proxy_id)
        for known_kind, key in pairs(kind_keys) do
            if known_kind == kind then
                redis.call('ZADD', key, -last_used, proxy_id)
            else
                redis.call('ZREM', key, proxy_id)
            end
        end
        redis.call('ZREM', cooldown_key, proxy_id)
    else
        remove_static_indexes()
        if state == 'cooldown' and cooldown_until > now then
            redis.call('ZADD', cooldown_key, cooldown_until, proxy_id)
        end
    end
    return active
end

local function update_quality_index(data, target_sitekey, index_key, active)
    if target_sitekey == '' then
        return
    end
    if not active then
        redis.call('ZREM', index_key, proxy_id)
        return
    end
    local signal, has_stats = site_signal(data, target_sitekey)
    if has_stats then
        local last_used = tonumber(data['last_used_at'] or 0) or 0
        redis.call(
            'ZADD',
            index_key,
            signal * 1000000000000.0 - last_used,
            proxy_id
        )
    else
        redis.call('ZREM', index_key, proxy_id)
    end
end

local function refresh_all_quality_indexes(data, active)
    local mappings = redis.call('HGETALL', sitekey_index_map_key)
    for index = 1, #mappings, 2 do
        update_quality_index(
            data,
            mappings[index],
            mappings[index + 1],
            active
        )
    end
end

redis.call('ZREMRANGEBYSCORE', lease_key, '-inf', now)

local blob = redis.call('HGET', proxies_key, proxy_id)
if not blob then
    if lease_token ~= '' then
        redis.call('ZREM', lease_key, lease_token)
    end
    remove_static_indexes()
    remove_all_quality_indexes()
    redis.call('DEL', lease_key)
    redis.call('DEL', sitekey_lru_key)
    redis.call('DEL', sitekey_index_map_key)
    return {'MISSING'}
end

local ok, data = pcall(cjson.decode, blob)
if not ok or type(data) ~= 'table' then
    if lease_token ~= '' then
        redis.call('ZREM', lease_key, lease_token)
    end
    redis.call('HDEL', proxies_key, proxy_id)
    remove_static_indexes()
    remove_all_quality_indexes()
    redis.call('DEL', lease_key)
    redis.call('DEL', sitekey_lru_key)
    redis.call('DEL', sitekey_index_map_key)
    return {'CORRUPT'}
end

if operation == 'solve' then
    if lease_token == '' then
        return {'LEASE_TOKEN_REQUIRED'}
    end
    local released = tonumber(redis.call('ZREM', lease_key, lease_token)) or 0
    if released == 0 then
        return {'STALE_LEASE'}
    end
end

local state_before = tostring(data['state'] or 'healthy')
local cooldown_before = tonumber(data['cooldown_until'] or 0) or 0
-- Index membership reflects the persisted state, not whether the cooldown
-- deadline has merely elapsed. A persisted cooldown row remains absent from
-- active/quality indexes until a transaction rehabilitates it.
local was_active = state_before ~= 'burned' and state_before ~= 'cooldown'
if state_before == 'cooldown' and cooldown_before <= now then
    data['state'] = 'healthy'
    data['consecutive_fails'] = 0
end
local was_burned = state_before == 'burned'

local update_health = operation == 'solve'
    or operation == 'health'
    or operation == 'real'
local update_sitekey = operation == 'solve'
    or operation == 'sitekey_token'
    or operation == 'sitekey_real'
    or operation == 'real'
local use_real_bucket = operation == 'sitekey_real' or operation == 'real'

if update_health then
    local stored_bytes = tonumber(data['bytes_used'] or 0) or 0
    data['bytes_used'] = stored_bytes + math.max(0, bytes_used)
    if success then
        data['success_count'] = (tonumber(data['success_count'] or 0) or 0) + 1
        data['consecutive_fails'] = 0
        if tostring(data['state'] or 'healthy') ~= 'burned' then
            data['state'] = 'healthy'
        end
    else
        data['fail_count'] = (tonumber(data['fail_count'] or 0) or 0) + 1
        local consecutive = (tonumber(data['consecutive_fails'] or 0) or 0) + 1
        data['consecutive_fails'] = consecutive
        if consecutive >= max_consecutive_fails then
            data['state'] = 'cooldown'
            data['cooldown_until'] = now + cooldown_seconds
        end
    end
    if was_burned or (
        max_bytes_per_proxy > 0
        and (tonumber(data['bytes_used'] or 0) or 0) >= max_bytes_per_proxy
    ) then
        data['state'] = 'burned'
    end
end

if operation == 'geo' then
    if country ~= '' then
        data['country'] = country
    end
    if timezone ~= '' then
        data['timezone'] = timezone
    end
    if locale ~= '' then
        data['locale'] = locale
    end
    data['geo_probed'] = geo_probed
end

if update_sitekey and sitekey ~= '' then
    local bucket_name = 'sitekey_stats'
    if use_real_bucket then
        bucket_name = 'real_sitekey_stats'
    end
    local bucket = data[bucket_name]
    if type(bucket) ~= 'table' then
        bucket = {}
        data[bucket_name] = bucket
    end
    local stats = bucket[sitekey]
    if type(stats) ~= 'table' then
        stats = {0, 0}
        bucket[sitekey] = stats
    end
    local slot = 2
    if success then
        slot = 1
    end
    stats[slot] = (tonumber(stats[slot] or 0) or 0) + 1

    redis.call('ZADD', sitekey_lru_key, now, sitekey)
    redis.call('HSET', sitekey_index_map_key, sitekey, current_sitekey_index_key)
    local count = tonumber(redis.call('ZCARD', sitekey_lru_key)) or 0
    if count > sitekey_limit then
        local evicted = redis.call(
            'ZPOPMIN', sitekey_lru_key, count - sitekey_limit
        )
        for index = 1, #evicted, 2 do
            local stale = evicted[index]
            if type(data['sitekey_stats']) == 'table' then
                data['sitekey_stats'][stale] = nil
            end
            if type(data['real_sitekey_stats']) == 'table' then
                data['real_sitekey_stats'][stale] = nil
            end
            local stale_index_key = redis.call(
                'HGET', sitekey_index_map_key, stale
            )
            if stale_index_key then
                redis.call('ZREM', stale_index_key, proxy_id)
            end
            redis.call('HDEL', sitekey_index_map_key, stale)
        end
    end
end

local is_active = apply_availability(data)
if was_active ~= is_active then
    refresh_all_quality_indexes(data, is_active)
elseif update_sitekey and sitekey ~= '' then
    update_quality_index(
        data,
        sitekey,
        current_sitekey_index_key,
        is_active
    )
end

local updated_blob = cjson.encode(data)
redis.call('HSET', proxies_key, proxy_id, updated_blob)
return {'OK', updated_blob, tostring(redis.call('ZCARD', lease_key))}
"""


def _context_pop(
    variable: contextvars.ContextVar[Dict[str, List[str]]],
    proxy_id: str,
) -> Optional[str]:
    current = variable.get()
    values = current.get(proxy_id)
    if not values:
        return None
    token = values[-1]
    updated = {key: list(items) for key, items in current.items()}
    updated_values = updated.get(proxy_id, [])
    if updated_values:
        updated_values.pop()
    if updated_values:
        updated[proxy_id] = updated_values
    else:
        updated.pop(proxy_id, None)
    variable.set(updated)
    return token


def _context_append(
    variable: contextvars.ContextVar[Dict[str, List[str]]],
    proxy_id: str,
    token: str,
) -> None:
    current = variable.get()
    updated = {key: list(items) for key, items in current.items()}
    updated.setdefault(proxy_id, []).append(token)
    variable.set(updated)


def _context_set_result(
    variable: contextvars.ContextVar[Dict[str, bool]],
    proxy_id: str,
    accepted: bool,
) -> None:
    current = dict(variable.get())
    current[proxy_id] = accepted
    variable.set(current)


def _context_pop_result(
    variable: contextvars.ContextVar[Dict[str, bool]],
    proxy_id: str,
) -> Optional[bool]:
    current = variable.get()
    if proxy_id not in current:
        return None
    accepted = current[proxy_id]
    updated = dict(current)
    updated.pop(proxy_id, None)
    variable.set(updated)
    return accepted


class _LeaseContextMixin:
    """Task-local exact leases without proxy-scoped release guessing."""

    def _init_lease_context(self) -> None:
        self._task_leases: contextvars.ContextVar[Dict[str, List[str]]] = (
            contextvars.ContextVar(
                f"captchai-proxy-leases-{id(self)}",
                default={},
            )
        )
        self._task_report_results: contextvars.ContextVar[Dict[str, bool]] = (
            contextvars.ContextVar(
                f"captchai-proxy-report-results-{id(self)}",
                default={},
            )
        )
        self._pending_checkout_token: contextvars.ContextVar[Optional[str]] = (
            contextvars.ContextVar(
                f"captchai-pending-proxy-lease-{id(self)}",
                default=None,
            )
        )

    def _remember_lease(self, proxy: ProxyAsset, token: str) -> None:
        setattr(proxy, _LEASE_TOKEN_ATTR, token)
        _context_append(self._task_leases, proxy.id, token)

    def _discard_task_token(self, proxy_id: str, token: str) -> bool:
        current = self._task_leases.get()
        values = list(current.get(proxy_id, ()))
        if token not in values:
            return False
        values.remove(token)
        updated = {key: list(items) for key, items in current.items()}
        if values:
            updated[proxy_id] = values
        else:
            updated.pop(proxy_id, None)
        self._task_leases.set(updated)
        return True

    def _restore_lease(self, proxy_id: str, token: str) -> None:
        current = self._task_leases.get()
        if token not in current.get(proxy_id, ()):
            _context_append(self._task_leases, proxy_id, token)

    def _claim_lease(
        self,
        proxy_id: str,
        explicit: Optional[str],
    ) -> Optional[str]:
        if explicit:
            token = str(explicit)
            self._discard_task_token(proxy_id, token)
            return token
        return _context_pop(self._task_leases, proxy_id)


class FeedbackManagedProxyPool(_LeaseContextMixin, AtomicManagedProxyPool):
    """In-memory backend with exact token-scoped lease release."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._init_lease_context()
        self._token_leases: Dict[str, OrderedDict[str, float]] = {}

    def _lease_count_locked(self, proxy_id: str, now: float) -> int:
        leases = self._token_leases.get(proxy_id)
        if not leases:
            return 0
        active = OrderedDict(
            (token, expiry)
            for token, expiry in leases.items()
            if expiry > now
        )
        if active:
            self._token_leases[proxy_id] = active
        else:
            self._token_leases.pop(proxy_id, None)
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
            token = secrets.token_hex(16)
            self._token_leases.setdefault(best.id, OrderedDict())[token] = (
                now + self._lease_ttl_seconds
            )
            leased = copy.copy(best)
            self._remember_lease(leased, token)
            return leased

    def _apply_health_locked(
        self,
        proxy: ProxyAsset,
        *,
        success: bool,
        bytes_used: int,
        now: float,
    ) -> None:
        proxy.bytes_used += max(0, bytes_used)
        if success:
            proxy.success_count += 1
            proxy.consecutive_fails = 0
            if proxy.state != "burned":
                proxy.state = "healthy"
        else:
            proxy.fail_count += 1
            proxy.consecutive_fails += 1
            if proxy.consecutive_fails >= self._max_consecutive_fails:
                proxy.state = "cooldown"
                proxy.cooldown_until = now + self._cooldown_seconds
        if (
            self._max_bytes_per_proxy
            and proxy.bytes_used >= self._max_bytes_per_proxy
        ):
            proxy.state = "burned"

    def _record_sitekey_locked(
        self,
        proxy: ProxyAsset,
        sitekey: str,
        *,
        success: bool,
        real: bool,
        now: float,
    ) -> None:
        bucket = proxy.real_sitekey_stats if real else proxy.sitekey_stats
        stats = bucket.setdefault(sitekey, [0, 0])
        stats[0 if success else 1] += 1
        lru = self._sitekey_lru.setdefault(proxy.id, OrderedDict())
        lru.pop(sitekey, None)
        lru[sitekey] = now
        while len(lru) > self._sitekey_limit:
            stale, _ = lru.popitem(last=False)
            proxy.sitekey_stats.pop(stale, None)
            proxy.real_sitekey_stats.pop(stale, None)

    async def report_solve(
        self,
        proxy_id: str,
        *,
        lease_token: str,
        success: bool,
        bytes_used: int = 0,
        sitekey: str = "",
    ) -> bool:
        tracked = self._discard_task_token(proxy_id, lease_token)
        try:
            async with self._get_lock():
                now = time.monotonic()
                leases = self._token_leases.get(proxy_id)
                if not leases:
                    return False
                expiry = leases.pop(lease_token, None)
                if not leases:
                    self._token_leases.pop(proxy_id, None)
                if expiry is None or expiry <= now:
                    return False
                proxy = self._proxies.get(proxy_id)
                if proxy is None:
                    return False
                self._apply_health_locked(
                    proxy,
                    success=success,
                    bytes_used=bytes_used,
                    now=now,
                )
                if sitekey:
                    self._record_sitekey_locked(
                        proxy,
                        sitekey,
                        success=success,
                        real=False,
                        now=now,
                    )
                return True
        except BaseException:
            if tracked:
                self._restore_lease(proxy_id, lease_token)
            raise

    async def report(
        self,
        proxy_id: str,
        *,
        success: bool,
        bytes_used: int = 0,
        lease_token: Optional[str] = None,
    ) -> None:
        token = self._claim_lease(proxy_id, lease_token)
        if token:
            try:
                accepted = await self.report_solve(
                    proxy_id,
                    lease_token=token,
                    success=success,
                    bytes_used=bytes_used,
                )
            except BaseException:
                self._restore_lease(proxy_id, token)
                raise
            _context_set_result(
                self._task_report_results, proxy_id, accepted
            )
            return
        async with self._get_lock():
            proxy = self._proxies.get(proxy_id)
            if proxy is not None:
                self._apply_health_locked(
                    proxy,
                    success=success,
                    bytes_used=bytes_used,
                    now=time.monotonic(),
                )

    async def report_real_outcome(
        self,
        proxy_id: str,
        sitekey: str,
        *,
        success: bool,
    ) -> None:
        async with self._get_lock():
            proxy = self._proxies.get(proxy_id)
            if proxy is None:
                return
            now = time.monotonic()
            self._apply_health_locked(proxy, success=success, bytes_used=0, now=now)
            if sitekey:
                self._record_sitekey_locked(
                    proxy,
                    sitekey,
                    success=success,
                    real=True,
                    now=now,
                )

    async def report_sitekey(
        self,
        proxy_id: str,
        sitekey: str,
        *,
        success: bool,
    ) -> None:
        accepted = _context_pop_result(self._task_report_results, proxy_id)
        if accepted is False:
            return
        async with self._get_lock():
            proxy = self._proxies.get(proxy_id)
            if proxy is not None:
                self._record_sitekey_locked(
                    proxy,
                    sitekey,
                    success=success,
                    real=False,
                    now=time.monotonic(),
                )

    async def report_sitekey_real(
        self,
        proxy_id: str,
        sitekey: str,
        *,
        success: bool,
    ) -> None:
        async with self._get_lock():
            proxy = self._proxies.get(proxy_id)
            if proxy is not None:
                self._record_sitekey_locked(
                    proxy,
                    sitekey,
                    success=success,
                    real=True,
                    now=time.monotonic(),
                )


class FeedbackRedisProxyPool(_LeaseContextMixin, AtomicRedisProxyPool):
    """Redis backend whose checkout and feedback commits are all atomic."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._init_lease_context()
        self._sitekey_index_map_prefix = f"{self._prefix}:sitekey-index-map"
        self._reconcile_feedback_indexes_sync()

    def _sitekey_index_map_key(self, proxy_id: str) -> str:
        return f"{self._sitekey_index_map_prefix}:{self._digest(proxy_id)}"

    def _reconcile_feedback_indexes_sync(self) -> None:
        raw = self._sync_redis.hgetall(self._proxies_key)
        if not raw:
            return
        now = time.time()
        pipe = self._sync_redis.pipeline(transaction=False)
        for raw_proxy_id, blob in raw.items():
            proxy_id = _redis_text(raw_proxy_id)
            try:
                proxy = self._deserialize(_redis_text(blob))
            except (KeyError, TypeError, ValueError):
                continue
            map_key = self._sitekey_index_map_key(proxy_id)
            pipe.delete(map_key)
            active = _is_active(proxy, now)
            for sitekey in _ordered_sitekeys(proxy):
                index_key = self._sitekey_index_key(sitekey)
                pipe.hset(map_key, sitekey, index_key)
                if active:
                    pipe.zadd(
                        index_key,
                        {proxy_id: self._sitekey_score(proxy, sitekey)},
                    )
                else:
                    pipe.zrem(index_key, proxy_id)
        pipe.execute()

    def add(self, proxy: ProxyAsset) -> None:
        super().add(proxy)
        map_key = self._sitekey_index_map_key(proxy.id)
        pipe = self._sync_redis.pipeline(transaction=False)
        pipe.delete(map_key)
        for sitekey in _ordered_sitekeys(proxy):
            pipe.hset(map_key, sitekey, self._sitekey_index_key(sitekey))
        pipe.execute()

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
        result = _redis_members(raw)
        if result and result[0] != "BUSY":
            self._pending_checkout_token.set(lease_token)
        return result

    async def checkout(
        self,
        *,
        kind: KindFilter = None,
        sitekey: Optional[str] = None,
    ) -> Optional[ProxyAsset]:
        marker = self._pending_checkout_token.set(None)
        try:
            proxy = await super().checkout(kind=kind, sitekey=sitekey)
            if proxy is None:
                return None
            pending = self._pending_checkout_token.get()
            if not pending:
                raise RuntimeError("atomic checkout completed without a lease token")
            self._remember_lease(proxy, pending)
            return proxy
        finally:
            self._pending_checkout_token.reset(marker)

    def _feedback_keys(self, proxy_id: str, sitekey: str) -> List[str]:
        return [
            self._proxies_key,
            self._active_all_key,
            self._active_kind_key("datacenter"),
            self._active_kind_key("residential"),
            self._active_kind_key("mobile"),
            self._cooldown_index_key,
            self._lease_key(proxy_id),
            self._sitekey_index_key(sitekey),
            self._sitekey_lru_key(proxy_id),
            self._sitekey_index_map_key(proxy_id),
            self._lock_key,
        ]

    async def _run_feedback(
        self,
        proxy_id: str,
        *,
        operation: str,
        success: bool,
        bytes_used: int = 0,
        lease_token: Optional[str] = None,
        sitekey: str = "",
        country: Optional[str] = None,
        timezone: Optional[str] = None,
        locale: Optional[str] = None,
        geo_probed: bool = False,
    ) -> List[str]:
        keys = self._feedback_keys(proxy_id, sitekey)
        args: List[Union[str, int, float]] = [
            proxy_id,
            time.time(),
            operation,
            1 if success else 0,
            max(0, int(bytes_used)),
            lease_token or "",
            sitekey,
            self._sitekey_limit,
            self._cooldown_seconds,
            self._max_consecutive_fails,
            self._max_bytes_per_proxy,
            country or "",
            timezone or "",
            locale or "",
            1 if geo_probed else 0,
        ]
        deadline = time.monotonic() + self._lock_wait_seconds
        while True:
            # A compatibility writer may hold the lock for part of the wait
            # budget. Refresh server-time arguments on every attempt so lease
            # expiry and cooldown deadlines are based on commit time, not on
            # the first blocked attempt.
            args[1] = time.time()
            raw = await self._redis.eval(
                _ATOMIC_FEEDBACK_LUA,
                len(keys),
                *keys,
                *args,
            )
            result = _redis_members(raw)
            if not result or result[0] != "BUSY":
                return result
            if time.monotonic() >= deadline:
                log.warning(
                    "proxy feedback skipped because a compatibility writer "
                    "remained busy"
                )
                return result
            await asyncio.sleep(0.025)

    async def report_solve(
        self,
        proxy_id: str,
        *,
        lease_token: str,
        success: bool,
        bytes_used: int = 0,
        sitekey: str = "",
    ) -> bool:
        tracked = self._discard_task_token(proxy_id, lease_token)
        try:
            result = await self._run_feedback(
                proxy_id,
                operation="solve",
                success=success,
                bytes_used=bytes_used,
                lease_token=lease_token,
                sitekey=sitekey,
            )
        except BaseException:
            if tracked:
                self._restore_lease(proxy_id, lease_token)
            raise
        return bool(result and result[0] == "OK")

    async def report(
        self,
        proxy_id: str,
        *,
        success: bool,
        bytes_used: int = 0,
        lease_token: Optional[str] = None,
    ) -> None:
        token = self._claim_lease(proxy_id, lease_token)
        if token:
            try:
                accepted = await self.report_solve(
                    proxy_id,
                    lease_token=token,
                    success=success,
                    bytes_used=bytes_used,
                )
            except BaseException:
                self._restore_lease(proxy_id, token)
                raise
            _context_set_result(
                self._task_report_results, proxy_id, accepted
            )
            return
        await self._run_feedback(
            proxy_id,
            operation="health",
            success=success,
            bytes_used=bytes_used,
        )

    async def report_real_outcome(
        self,
        proxy_id: str,
        sitekey: str,
        *,
        success: bool,
    ) -> None:
        await self._run_feedback(
            proxy_id,
            operation="real",
            success=success,
            sitekey=sitekey,
        )

    async def report_sitekey(
        self,
        proxy_id: str,
        sitekey: str,
        *,
        success: bool,
    ) -> None:
        accepted = _context_pop_result(self._task_report_results, proxy_id)
        if accepted is False:
            return
        await self._run_feedback(
            proxy_id,
            operation="sitekey_token",
            success=success,
            sitekey=sitekey,
        )

    async def report_sitekey_real(
        self,
        proxy_id: str,
        sitekey: str,
        *,
        success: bool,
    ) -> None:
        await self._run_feedback(
            proxy_id,
            operation="sitekey_real",
            success=success,
            sitekey=sitekey,
        )

    async def set_geo(
        self,
        proxy_id: str,
        *,
        country: Optional[str],
        timezone: Optional[str],
        locale: Optional[str],
        geo_probed: bool = True,
    ) -> None:
        await self._run_feedback(
            proxy_id,
            operation="geo",
            success=True,
            country=country,
            timezone=timezone,
            locale=locale,
            geo_probed=geo_probed,
        )


def build_feedback_proxy_pool(
    config: Any,
) -> "FeedbackManagedProxyPool | FeedbackRedisProxyPool":
    """Build the fully atomic feedback backend behind the existing seam."""

    raw_max_gb = getattr(config, "proxy_max_gb", 0.0)
    try:
        max_gb = float(raw_max_gb)
    except (TypeError, ValueError):
        max_gb = 0.0
    max_bytes = int(max_gb * 1024**3) if max_gb > 0 else 0
    lease_ttl = max(60, int(getattr(config, "solve_timeout", 180)) + 30)
    cooldown_seconds = int(getattr(config, "proxy_cooldown", 120))
    max_consecutive_fails = int(
        getattr(config, "proxy_max_consecutive_fails", 3)
    )
    sitekey_limit = int(getattr(config, "proxy_sitekey_limit", 128))
    redis_url = getattr(config, "redis_url", None)

    if redis_url:
        return FeedbackRedisProxyPool(
            redis_url,
            cooldown_seconds=cooldown_seconds,
            max_consecutive_fails=max_consecutive_fails,
            max_bytes_per_proxy=max_bytes,
            sitekey_limit=sitekey_limit,
            lease_ttl_seconds=lease_ttl,
            lock_wait_seconds=float(
                getattr(config, "proxy_lock_wait_seconds", 2.0)
            ),
            candidate_window=int(
                getattr(config, "proxy_candidate_window", 32)
            ),
            snapshot_batch_size=int(
                getattr(config, "proxy_snapshot_batch_size", 128)
            ),
        )
    return FeedbackManagedProxyPool(
        cooldown_seconds=cooldown_seconds,
        max_consecutive_fails=max_consecutive_fails,
        max_bytes_per_proxy=max_bytes,
        sitekey_limit=sitekey_limit,
        lease_ttl_seconds=lease_ttl,
    )


__all__ = [
    "FeedbackManagedProxyPool",
    "FeedbackRedisProxyPool",
    "build_feedback_proxy_pool",
    "proxy_lease_token",
    "snapshot_proxy_pool",
]
