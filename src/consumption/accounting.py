"""Rolling success-rate accounting used for routing/selection decisions."""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any, Deque, Dict, Optional, Protocol, Tuple, cast, runtime_checkable

_Key = Tuple[str, Any, Any]

# Hard ceiling on the number of Redis bucket keys tracked per prefix. Once
# exceeded, the lowest-count buckets are evicted so a stream of unique
# (sitekey, proxy_kind, model) combinations can't grow Redis without bound.
_MAX_BUCKETS = 1000

# Redis glob metacharacters. User-controlled sitekeys are embedded in a SCAN
# ``MATCH`` pattern, so they must be backslash-escaped — otherwise a sitekey
# containing ``*`` / ``?`` / ``[...]`` would match other sitekeys' buckets.
_GLOB_SPECIAL = frozenset("\\[]*?^-")


def _glob_escape(value: str) -> str:
    """Escape Redis glob metacharacters in a literal key segment.

    Redis's ``MATCH`` uses shell-style globs (``*``, ``?``, ``[...]``, with
    ``^`` / ``-`` special inside a bracket expression). Backslash makes the
    next character literal, so escaping every metacharacter lets a
    user-controlled sitekey be matched exactly.
    """
    return "".join(
        "\\" + ch if ch in _GLOB_SPECIAL else ch for ch in (value or "")
    )


class SuccessAccounting:
    """Rolling per-(sitekey[, proxy_kind, model]) success stats to drive routing/selection."""

    def __init__(self, window: int = 100) -> None:
        self._window = window
        # Token-obtained outcomes (optimistic: "ready" means we got a token,
        # not that the caller's downstream verification accepted it).
        self._outcomes: Dict[_Key, Deque[bool]] = {}
        # Real outcomes (token was actually accepted by the downstream service,
        # reported via /reportCorrect / /reportIncorrect). Same key shape as
        # _outcomes but tracked separately so selection can weight this higher.
        self._real_outcomes: Dict[_Key, Deque[bool]] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _key(sitekey: str, proxy_kind: str | None, model: str | None) -> _Key:
        return (sitekey, proxy_kind, model)

    async def record(
        self,
        sitekey: str,
        outcome: str,
        *,
        proxy_kind: str | None = None,
        model: str | None = None,
    ) -> None:
        success = outcome == "ready"
        async with self._lock:
            key = self._key(sitekey, proxy_kind, model)
            bucket = self._outcomes.get(key)
            if bucket is None:
                bucket = deque(maxlen=self._window)
                self._outcomes[key] = bucket
            bucket.append(success)

    async def record_real_outcome(
        self,
        sitekey: str,
        *,
        success: bool,
        proxy_kind: str | None = None,
        model: str | None = None,
    ) -> None:
        """Record a real (token-actually-accepted) outcome for a sitekey.

        Fed by the /reportCorrect / /reportIncorrect endpoints. Maintained in
        a separate bucket set from the optimistic token-obtained outcomes so
        the two signals can be weighted independently by selection code.
        """
        async with self._lock:
            key = self._key(sitekey, proxy_kind, model)
            bucket = self._real_outcomes.get(key)
            if bucket is None:
                bucket = deque(maxlen=self._window)
                self._real_outcomes[key] = bucket
            bucket.append(success)

    async def success_rate(
        self,
        sitekey: str,
        *,
        proxy_kind: str | None = None,
        model: str | None = None,
    ) -> float:
        async with self._lock:
            bucket = self._outcomes.get(self._key(sitekey, proxy_kind, model))
            if not bucket:
                return 1.0  # optimistic default when no data yet
            return sum(bucket) / len(bucket)

    async def real_success_rate(
        self,
        sitekey: str,
        *,
        proxy_kind: str | None = None,
        model: str | None = None,
    ) -> float:
        """Real-outcome success rate (token actually accepted downstream)."""
        async with self._lock:
            bucket = self._real_outcomes.get(self._key(sitekey, proxy_kind, model))
            if not bucket:
                return 1.0  # optimistic default when no data yet
            return sum(bucket) / len(bucket)

    async def stats(self, sitekey: str) -> dict[str, Any]:
        """Aggregate stats across all (proxy_kind, model) buckets for a sitekey.

        Includes both token-obtained (optimistic) and real-outcome (token
        actually accepted) buckets. The real_* fields let callers compare the
        two signals — a high ``rate`` with a low ``real_rate`` means tokens are
        being obtained but rejected downstream, pointing at fingerprint / IP
        issues rather than solving failures.
        """
        async with self._lock:
            attempts = 0
            successes = 0
            real_attempts = 0
            real_successes = 0
            for (key_sitekey, _proxy, _model), bucket in self._outcomes.items():
                if key_sitekey != sitekey:
                    continue
                attempts += len(bucket)
                successes += sum(bucket)
            for (key_sitekey, _proxy, _model), bucket in self._real_outcomes.items():
                if key_sitekey != sitekey:
                    continue
                real_attempts += len(bucket)
                real_successes += sum(bucket)
        rate = (successes / attempts) if attempts else 1.0
        real_rate = (real_successes / real_attempts) if real_attempts else 1.0
        return {
            "attempts": attempts,
            "successes": successes,
            "rate": rate,
            "real_attempts": real_attempts,
            "real_successes": real_successes,
            "real_rate": real_rate,
        }


# ── Protocol + Redis backend ─────────────────────────────────────────


@runtime_checkable
class SuccessAccountingProtocol(Protocol):
    """Surface callers depend on. Both backends satisfy this."""

    async def record(
        self,
        sitekey: str,
        outcome: str,
        *,
        proxy_kind: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None: ...
    async def record_real_outcome(
        self,
        sitekey: str,
        *,
        success: bool,
        proxy_kind: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None: ...
    async def success_rate(
        self,
        sitekey: str,
        *,
        proxy_kind: Optional[str] = None,
        model: Optional[str] = None,
    ) -> float: ...
    async def real_success_rate(
        self,
        sitekey: str,
        *,
        proxy_kind: Optional[str] = None,
        model: Optional[str] = None,
    ) -> float: ...
    async def stats(self, sitekey: str) -> dict[str, Any]: ...


def _bucket_key(
    prefix: str, sitekey: str, proxy_kind: Optional[str], model: Optional[str], *, real: bool
) -> str:
    """Build a Redis key for an accounting bucket.

    Key shape: ``{prefix}:{real|opt}:{sitekey}:{proxy_kind|_}:{model|_}``.

    The ``real`` vs ``opt`` segment separates the two bucket sets so a
    ``stats(sitekey)`` scan can sum across both with a single ``KEYS`` pattern.
    The literal ``_`` is used for ``None`` proxy_kind / model so the key is
    always a non-empty, collision-free string.
    """
    bucket = "real" if real else "opt"
    pk = proxy_kind or "_"
    md = model or "_"
    return f"{prefix}:{bucket}:{sitekey}:{pk}:{md}"


class RedisSuccessAccounting:
    """Redis-backed success accounting shared across workers.

    Each (sitekey, proxy_kind, model) bucket is a Redis list capped at
    ``window`` entries via ``LPUSH`` + ``LTRIM``. ``1`` / ``0`` entries
    represent success / failure outcomes. The window bound means
    ``success_rate`` is O(window) via ``LRANGE`` (small, e.g. 100 entries) —
    acceptable for v1; a Lua-script sum or a running counter pair would be
    O(1) but adds complexity.

    ``stats(sitekey)`` enumerates buckets via two ``SCAN`` calls — one for
    the optimistic side (``{prefix}:opt:{sitekey}:*``) and one for the
    real-outcome side (``{prefix}:real:{sitekey}:*``). ``SCAN`` is
    cursor-based and non-blocking, so it's safe on a busy Redis; the caveat
    is that it can yield a bucket more than once across cursors when keys
    are mutated mid-scan, so the helper de-duplicates. The sitekey is
    user-controlled and is glob-escaped before being embedded in the pattern,
    so a sitekey containing ``*`` / ``?`` / ``[...]`` matches only its own
    buckets.

    The number of bucket keys is capped: a ``{prefix}:buckets`` ZSET scores
    each bucket key by lifetime writes, and the lowest-count keys beyond
    ``max_buckets`` (default 1000) are evicted on write. This keeps
    arbitrarily many distinct sitekeys / models from growing Redis without
    bound. The cap is approximate under concurrent writers (each writer may
    trim a slightly different set) — good enough for routing statistics.

    Optimistic 1.0 is returned for any empty bucket — matches the in-memory
    backend so selection code doesn't penalise fresh sitekeys.

    A restart no longer zeroes routing stats: the rolling windows survive in
    Redis across processes and across restarts.
    """

    def __init__(
        self,
        url: str,
        window: int = 100,
        *,
        key_prefix: str = "captcha:accounting",
        max_buckets: int = _MAX_BUCKETS,
    ) -> None:
        try:
            import redis.asyncio as aioredis  # noqa: WPS433
        except ImportError as exc:  # pragma: no cover - exercised only w/o redis
            raise RuntimeError(
                "REDIS_URL is set but the 'redis' package is not installed; "
                "add redis>=4.2 to requirements or unset REDIS_URL"
            ) from exc
        self._window = window
        self._prefix = key_prefix
        self._max_buckets = max_buckets
        # Bucket-key index: member = bucket key, score = lifetime write count.
        # Used to evict the lowest-count buckets once the cap is exceeded.
        self._index_key = f"{key_prefix}:buckets"
        self._redis = aioredis.from_url(url, decode_responses=True)

    async def _push(
        self,
        sitekey: str,
        proxy_kind: Optional[str],
        model: Optional[str],
        value: int,
        *,
        real: bool,
    ) -> None:
        key = _bucket_key(self._prefix, sitekey, proxy_kind, model, real=real)
        pipe = self._redis.pipeline()
        pipe.lpush(key, value)
        pipe.ltrim(key, 0, self._window - 1)
        pipe.zincrby(self._index_key, 1, key)
        pipe.zcard(self._index_key)
        results = await pipe.execute()
        total = int(results[-1] or 0)
        if total > self._max_buckets:
            await self._evict_lowest(total - self._max_buckets, protect=key)

    async def _evict_lowest(self, overflow: int, *, protect: str) -> None:
        """Evict the lowest-count buckets beyond the cap.

        ``protect`` (the bucket just written) is never evicted. Fetches one
        extra candidate so there are still ``overflow`` victims when the
        freshly-created bucket is itself among the lowest-count keys.
        """
        candidates = await self._redis.zrange(self._index_key, 0, overflow)
        # ``decode_responses=True``: ZRANGE members are always ``str`` at
        # runtime; redis-py types the reply as the full bytes/str union.
        stale = cast(
            "list[str]", [k for k in candidates if k != protect][:overflow]
        )
        if not stale:
            return
        pipe = self._redis.pipeline()
        pipe.zrem(self._index_key, *stale)
        pipe.delete(*stale)
        await pipe.execute()

    async def record(
        self,
        sitekey: str,
        outcome: str,
        *,
        proxy_kind: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        success = outcome == "ready"
        await self._push(
            sitekey, proxy_kind, model, 1 if success else 0, real=False
        )

    async def record_real_outcome(
        self,
        sitekey: str,
        *,
        success: bool,
        proxy_kind: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        await self._push(
            sitekey, proxy_kind, model, 1 if success else 0, real=True
        )

    async def _rate(
        self,
        sitekey: str,
        proxy_kind: Optional[str],
        model: Optional[str],
        *,
        real: bool,
    ) -> float:
        key = _bucket_key(self._prefix, sitekey, proxy_kind, model, real=real)
        entries = await self._redis.lrange(key, 0, -1)
        if not entries:
            return 1.0  # optimistic default when no data yet
        total = len(entries)
        successes = sum(1 for v in entries if v == "1" or v == 1)
        return successes / total

    async def success_rate(
        self,
        sitekey: str,
        *,
        proxy_kind: Optional[str] = None,
        model: Optional[str] = None,
    ) -> float:
        return await self._rate(sitekey, proxy_kind, model, real=False)

    async def real_success_rate(
        self,
        sitekey: str,
        *,
        proxy_kind: Optional[str] = None,
        model: Optional[str] = None,
    ) -> float:
        return await self._rate(sitekey, proxy_kind, model, real=True)

    async def _scan_buckets(self, sitekey: str, *, real: bool) -> list[str]:
        """Return all bucket keys for ``sitekey`` on the given side.

        Uses ``SCAN`` (cursor-based, non-blocking) with pattern
        ``{prefix}:{real|opt}:{sitekey}:*``. De-duplicates because a SCAN can
        yield the same key more than once when keys are mutated mid-scan.
        """
        bucket = "real" if real else "opt"
        # ``sitekey`` is caller-controlled: escape Redis glob metacharacters so
        # it matches its own buckets literally instead of acting as a pattern.
        pattern = f"{self._prefix}:{bucket}:{_glob_escape(sitekey)}:*"
        seen: set[str] = set()
        cursor = 0
        while True:
            cursor, keys = await self._redis.scan(cursor=cursor, match=pattern, count=100)
            for k in keys or []:
                if isinstance(k, bytes):
                    k = k.decode("utf-8")
                seen.add(k)
            if cursor == 0 or int(cursor) == 0:
                break
        return list(seen)

    async def stats(self, sitekey: str) -> dict[str, Any]:
        """Aggregate stats across all (proxy_kind, model) buckets for a sitekey."""
        opt_keys = await self._scan_buckets(sitekey, real=False)
        real_keys = await self._scan_buckets(sitekey, real=True)

        attempts = 0
        successes = 0
        for k in opt_keys:
            entries = await self._redis.lrange(k, 0, -1)
            attempts += len(entries)
            successes += sum(1 for v in entries if v == "1" or v == 1)

        real_attempts = 0
        real_successes = 0
        for k in real_keys:
            entries = await self._redis.lrange(k, 0, -1)
            real_attempts += len(entries)
            real_successes += sum(1 for v in entries if v == "1" or v == 1)

        rate = (successes / attempts) if attempts else 1.0
        real_rate = (real_successes / real_attempts) if real_attempts else 1.0
        return {
            "attempts": attempts,
            "successes": successes,
            "rate": rate,
            "real_attempts": real_attempts,
            "real_successes": real_successes,
            "real_rate": real_rate,
        }

    async def close(self) -> None:
        try:
            await self._redis.aclose()
        except Exception:
            pass


def build_accounting(config: Any) -> "SuccessAccounting | RedisSuccessAccounting":
    """Select an accounting backend: Redis when configured, else in-memory.

    Using Redis means routing stats are shared across workers and survive
    restarts, so a sitekey that's failing for one worker isn't retried
    optimistically by another.
    """
    redis_url = getattr(config, "redis_url", None)
    if redis_url:
        return RedisSuccessAccounting(
            redis_url,
            window=int(getattr(config, "accounting_window", 100)),
        )
    return SuccessAccounting()
