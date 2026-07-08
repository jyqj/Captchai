"""In-memory cost ledger for captcha solve attempts."""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional

# Dollars per 1k tokens: (input, output). Local models are effectively free.
DEFAULT_PRICE_TABLE: Dict[str, Any] = {
    "gpt-4o": (0.0025, 0.01),
    "gpt-4o-mini": (0.00015, 0.0006),
    "gpt-4.1": (0.002, 0.008),
    "gpt-4.1-mini": (0.0004, 0.0016),
    "claude-3-5-sonnet": (0.003, 0.015),
    "claude-3-5-haiku": (0.0008, 0.004),
    "gemini-1.5-pro": (0.00125, 0.005),
    "gemini-1.5-flash": (0.000075, 0.0003),
    "local": (0.0, 0.0),
}

# Fallback for unknown cloud models (assume a cheap cloud tier).
_UNKNOWN_CLOUD_PRICE = (0.0005, 0.0015)

_LOCAL_MARKERS = ("local", "ollama", "llama", "qwen", "vllm", "lmstudio")


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    price_table: dict | None = None,
) -> float:
    """Estimate USD cost of a model call from token counts.

    ``price_table`` maps model name to ``(input_per_1k, output_per_1k)``;
    falls back to the default table, then to heuristics (local-looking
    model names cost 0, unknown cloud models get a cheap default rate).
    """
    table = price_table if price_table is not None else DEFAULT_PRICE_TABLE
    key = (model or "").lower()

    prices = table.get(key)
    if prices is None:
        # Prefix match, e.g. "gpt-4o-2024-08-06" -> "gpt-4o".
        for name, p in table.items():
            if key.startswith(str(name).lower()):
                prices = p
                break
    if prices is None:
        if any(marker in key for marker in _LOCAL_MARKERS):
            return 0.0
        prices = _UNKNOWN_CLOUD_PRICE

    in_price, out_price = float(prices[0]), float(prices[1])
    return (input_tokens / 1000.0) * in_price + (output_tokens / 1000.0) * out_price


@dataclass
class SolveRecord:
    task_id: str
    sitekey: str
    task_type: str
    proxy_id: str | None = None
    proxy_kind: str | None = None
    session_id: str | None = None
    model: str | None = None
    challenge_shape: str | None = None      # "grid_select" | "area_bbox" | ...
    rounds: int = 0
    vision_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    proxy_bytes: int = 0
    wall_ms: int = 0
    outcome: str = "failed"                  # "ready" | "failed" | "timeout"
    est_cost_usd: float = 0.0
    client_key: str | None = None
    created_at: float = 0.0                  # epoch seconds; auto-set if 0
    # Idempotency guard for /reportCorrect // /reportIncorrect: flipped to
    # True by ``CostLedger.try_claim_reported`` atomically BEFORE the first
    # report's downstream side effects run, so concurrent / retried reports
    # for the same task can't double-count real-outcome accounting, sitekey
    # stats, or session reputation. Defaults to False for backward
    # compatibility with records serialized before the field existed. The
    # in-memory ledger treats this field as the source of truth (under the
    # lock); the Redis ledger uses a dedicated SET-NX key as the source of
    # truth and syncs this field into the by-task blob only for
    # introspection / admin.
    reported: bool = False


class CostLedger:
    """Append-only, bounded, async-safe ledger of solve attempts."""

    def __init__(self, max_records: int = 100_000) -> None:
        self._records: Deque[SolveRecord] = deque(maxlen=max_records)
        self._lock = asyncio.Lock()

    async def record(self, rec: SolveRecord) -> None:
        if rec.created_at == 0:
            rec.created_at = time.time()
        async with self._lock:
            self._records.append(rec)

    async def total_cost_usd(self, client_key: str | None = None) -> float:
        async with self._lock:
            return sum(
                r.est_cost_usd
                for r in self._records
                if client_key is None or r.client_key == client_key
            )

    async def records(
        self,
        client_key: str | None = None,
        sitekey: str | None = None,
        limit: int | None = None,
    ) -> list[SolveRecord]:
        async with self._lock:
            matched = [
                r
                for r in self._records
                if (client_key is None or r.client_key == client_key)
                and (sitekey is None or r.sitekey == sitekey)
            ]
        if limit is not None:
            matched = matched[-limit:]
        return matched

    async def get_by_task_id(self, task_id: str) -> Optional[SolveRecord]:
        """Return the most recent record for ``task_id``, or ``None`` if none.

        Scans under the lock and picks the record with the latest ``created_at``
        when multiple records share the same task id (e.g. a retry).
        """
        async with self._lock:
            best: Optional[SolveRecord] = None
            for r in self._records:
                if r.task_id != task_id:
                    continue
                if best is None or r.created_at > best.created_at:
                    best = r
            return best

    async def try_claim_reported(self, task_id: str) -> bool:
        """Atomically claim the "reported" flag for ``task_id``.

        The single atomic primitive used by /reportCorrect // /reportIncorrect
        to guarantee at most one set of downstream side effects (proxy-pool
        ranking, accounting, session reputation) per task — even under N
        concurrent reports for the same task id. Replaces the old
        read-check-then-write race where the route pre-checked
        ``rec.reported`` and post-marked via ``mark_reported``.

        Returns ``True`` only if THIS call flipped the most recent record for
        ``task_id`` from unreported → reported. Returns ``False`` if the
        record was already reported (a prior call claimed it) or no record
        matches ``task_id``. Atomic because the lookup-and-flip runs entirely
        under ``self._lock``: concurrent callers serialize on the lock, so
        the first caller sees ``reported=False``, flips it, and returns
        ``True``; every later caller sees ``reported=True`` and returns
        ``False``. The "most recent record" selection matches
        ``get_by_task_id`` (latest ``created_at``).
        """
        async with self._lock:
            best: Optional[SolveRecord] = None
            for r in self._records:
                if r.task_id != task_id:
                    continue
                if best is None or r.created_at > best.created_at:
                    best = r
            if best is None:
                return False
            if best.reported:
                return False
            best.reported = True
            return True

    async def summary(self) -> dict[str, Any]:
        async with self._lock:
            snapshot = list(self._records)

        by_outcome: Dict[str, int] = {}
        by_model: Dict[str, Dict[str, float]] = {}
        by_proxy_kind: Dict[str, int] = {}
        total_cost = 0.0
        for r in snapshot:
            total_cost += r.est_cost_usd
            by_outcome[r.outcome] = by_outcome.get(r.outcome, 0) + 1
            proxy_kind = r.proxy_kind or "unknown"
            by_proxy_kind[proxy_kind] = by_proxy_kind.get(proxy_kind, 0) + 1
            model = r.model or "unknown"
            slot = by_model.setdefault(model, {"count": 0, "cost_usd": 0.0})
            slot["count"] += 1
            slot["cost_usd"] += r.est_cost_usd

        successes = by_outcome.get("ready", 0)
        cost_per_success = (total_cost / successes) if successes else 0.0

        return {
            "count": len(snapshot),
            "cost_usd": total_cost,
            "by_outcome": by_outcome,
            "by_model": by_model,
            "by_proxy_kind": by_proxy_kind,
            "cost_per_success": cost_per_success,
        }


# ── serialisation helpers (shared by RedisCostLedger) ───────────────

def _serialize_record(rec: SolveRecord) -> dict[str, Any]:
    return {
        "task_id": rec.task_id,
        "sitekey": rec.sitekey,
        "task_type": rec.task_type,
        "proxy_id": rec.proxy_id,
        "proxy_kind": rec.proxy_kind,
        "session_id": rec.session_id,
        "model": rec.model,
        "challenge_shape": rec.challenge_shape,
        "rounds": rec.rounds,
        "vision_calls": rec.vision_calls,
        "input_tokens": rec.input_tokens,
        "output_tokens": rec.output_tokens,
        "proxy_bytes": rec.proxy_bytes,
        "wall_ms": rec.wall_ms,
        "outcome": rec.outcome,
        "est_cost_usd": rec.est_cost_usd,
        "client_key": rec.client_key,
        "created_at": rec.created_at,
        "reported": rec.reported,
    }


def _deserialize_record(data: dict[str, Any]) -> SolveRecord:
    return SolveRecord(
        task_id=data.get("task_id", ""),
        sitekey=data.get("sitekey", ""),
        task_type=data.get("task_type", ""),
        proxy_id=data.get("proxy_id"),
        proxy_kind=data.get("proxy_kind"),
        session_id=data.get("session_id"),
        model=data.get("model"),
        challenge_shape=data.get("challenge_shape"),
        rounds=data.get("rounds", 0),
        vision_calls=data.get("vision_calls", 0),
        input_tokens=data.get("input_tokens", 0),
        output_tokens=data.get("output_tokens", 0),
        proxy_bytes=data.get("proxy_bytes", 0),
        wall_ms=data.get("wall_ms", 0),
        outcome=data.get("outcome", "failed"),
        est_cost_usd=data.get("est_cost_usd", 0.0),
        client_key=data.get("client_key"),
        created_at=data.get("created_at", 0.0),
        reported=data.get("reported", False),
    )


# TTL for the Redis "reported" claim key. Generous (7 days) so the claim
# outlives task / record retention and a long-delayed client retry can't
# re-claim a task that was already reported (which would re-run the
# proxy-pool / accounting / session-pool side effects).
_REPORTED_TTL_SECONDS = 60 * 60 * 24 * 7  # 7 days


class RedisCostLedger:
    """Redis-backed ledger with O(1) balance reads and bounded record history.

    Records are JSON blobs in a Redis list (``captcha:ledger:records``) capped
    at ``max_records``. Running totals (cost + count) are kept in Redis strings
    keyed globally and per-client so ``total_cost_usd`` is a single GET rather
    than a full scan — important because ``getBalance`` calls it on every poll.

    A restart no longer zeroes spend: the totals survive in Redis and the
    record history is preserved up to ``max_records``.
    """

    _RECORDS_KEY = "captcha:ledger:records"
    _TOTAL_GLOBAL = "captcha:ledger:total:global"
    _COUNT_GLOBAL = "captcha:ledger:count:global"

    def __init__(
        self,
        url: str,
        max_records: int = 100_000,
        *,
        key_prefix: str = "captcha:ledger",
    ) -> None:
        try:
            import redis.asyncio as aioredis  # noqa: WPS433
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "REDIS_URL is set but the 'redis' package is not installed; "
                "add redis>=4.2 to requirements or unset REDIS_URL"
            ) from exc
        self._max = max_records
        self._redis = aioredis.from_url(url, decode_responses=True)
        self._prefix = key_prefix
        self._records_key = f"{key_prefix}:records"
        self._total_global = f"{key_prefix}:total:global"
        self._count_global = f"{key_prefix}:count:global"
        # Hash index: task_id -> latest record blob, so reportCorrect /
        # reportIncorrect can look up a record by task id in O(1) without
        # scanning the full records list. Latest write wins (HSET overwrites).
        self._by_task_key = f"{key_prefix}:by_task"

    def _total_key(self, client_key: str | None) -> str:
        return f"{self._prefix}:total:{client_key or 'anon'}"

    def _count_key(self, client_key: str | None) -> str:
        return f"{self._prefix}:count:{client_key or 'anon'}"

    async def record(self, rec: SolveRecord) -> None:
        if rec.created_at == 0:
            rec.created_at = time.time()
        blob = json.dumps(_serialize_record(rec))
        pipe = self._redis.pipeline()
        pipe.lpush(self._records_key, blob)
        pipe.ltrim(self._records_key, 0, self._max - 1)
        pipe.incrbyfloat(self._total_global, rec.est_cost_usd)
        pipe.incrbyfloat(self._total_key(rec.client_key), rec.est_cost_usd)
        pipe.incr(self._count_global)
        pipe.incr(self._count_key(rec.client_key))
        # Maintain the by-task index so reports can look up the record in O(1).
        # HSET overwrites, so the latest record for a task id wins — which is
        # what reportCorrect / reportIncorrect want (the most recent attempt).
        pipe.hset(self._by_task_key, rec.task_id, blob)
        await pipe.execute()

    async def total_cost_usd(self, client_key: str | None = None) -> float:
        raw = await self._redis.get(
            self._total_global if client_key is None else self._total_key(client_key)
        )
        return float(raw) if raw is not None else 0.0

    async def records(
        self,
        client_key: str | None = None,
        sitekey: str | None = None,
        limit: int | None = None,
    ) -> list[SolveRecord]:
        raw_list = await self._redis.lrange(self._records_key, 0, -1)
        out: list[SolveRecord] = []
        for blob in raw_list:
            try:
                data = json.loads(blob)
            except json.JSONDecodeError:
                continue
            if client_key is not None and data.get("client_key") != client_key:
                continue
            if sitekey is not None and data.get("sitekey") != sitekey:
                continue
            out.append(_deserialize_record(data))
        if limit is not None:
            out = out[-limit:]
        return out

    async def get_by_task_id(self, task_id: str) -> Optional[SolveRecord]:
        """Return the latest record for ``task_id`` via the by-task hash index."""
        raw = await self._redis.hget(self._by_task_key, task_id)
        if raw is None:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return _deserialize_record(data)

    async def try_claim_reported(self, task_id: str) -> bool:
        """Atomically claim the "reported" flag for ``task_id`` via Redis SET NX.

        Uses a dedicated key ``{prefix}:reported:{task_id}`` with
        ``SET ... NX EX <ttl>`` (ttl = ``_REPORTED_TTL_SECONDS``, 7 days) so
        the claim is atomic at the Redis server: exactly one concurrent
        caller wins (Redis returns OK), everyone else gets nil (the key
        already existed from a prior call) and returns ``False``. The
        SET-NX key is the source of truth for "claimed"; the ``reported``
        field in the by-task blob is informational only and is synced
        best-effort for admin / introspection. Replaces the old
        non-atomic ``mark_reported`` (HGET / modify / HSET) which allowed
        N concurrent reports to all read ``reported=False`` and all run
        side effects before any of them marked.

        Returns ``False`` if no ledger record exists for ``task_id``
        (defensive — the route already checked via ``get_by_task_id``,
        but a race could evict the by-task blob; we never claim a report
        flag for a task with no record). Returns ``False`` if the key
        already existed (a prior call already claimed this task).
        Returns ``True`` iff this call set the key (won the claim).
        """
        # Don't claim a report flag for a task with no ledger record.
        raw = await self._redis.hget(self._by_task_key, task_id)
        if raw is None:
            return False
        # Atomic claim: SET NX EX is atomic at the Redis server. The first
        # caller to set the key wins; everyone else gets nil and returns
        # False. redis-py returns True (or "OK") on success, None on NX
        # failure — ``bool(result)`` collapses both decode_responses modes.
        key = f"{self._prefix}:reported:{task_id}"
        result = await self._redis.set(
            key, "1", nx=True, ex=_REPORTED_TTL_SECONDS
        )
        if not result:
            return False
        # Best-effort: sync reported=True into the by-task blob for
        # introspection / admin. The SET-NX key above is the source of
        # truth; a failure here doesn't undo the claim (a retry would
        # still see the SET-NX key and short-circuit).
        try:
            data = json.loads(raw)
            data["reported"] = True
            await self._redis.hset(self._by_task_key, task_id, json.dumps(data))
        except Exception:  # noqa: BLE001 - informational only
            pass
        return True

    async def summary(self) -> dict[str, Any]:
        raw_list = await self._redis.lrange(self._records_key, 0, -1)
        records: list[SolveRecord] = []
        for blob in raw_list:
            try:
                records.append(_deserialize_record(json.loads(blob)))
            except json.JSONDecodeError:
                continue

        by_outcome: Dict[str, int] = {}
        by_model: Dict[str, Dict[str, float]] = {}
        by_proxy_kind: Dict[str, int] = {}
        total_cost = 0.0
        for r in records:
            total_cost += r.est_cost_usd
            by_outcome[r.outcome] = by_outcome.get(r.outcome, 0) + 1
            proxy_kind = r.proxy_kind or "unknown"
            by_proxy_kind[proxy_kind] = by_proxy_kind.get(proxy_kind, 0) + 1
            model = r.model or "unknown"
            slot = by_model.setdefault(model, {"count": 0, "cost_usd": 0.0})
            slot["count"] += 1
            slot["cost_usd"] += r.est_cost_usd

        successes = by_outcome.get("ready", 0)
        cost_per_success = (total_cost / successes) if successes else 0.0

        return {
            "count": len(records),
            "cost_usd": total_cost,
            "by_outcome": by_outcome,
            "by_model": by_model,
            "by_proxy_kind": by_proxy_kind,
            "cost_per_success": cost_per_success,
        }

    async def close(self) -> None:
        await self._redis.aclose()


def build_ledger(config: Any) -> "CostLedger | RedisCostLedger":
    """Select a ledger backend: Redis when configured, else in-memory.

    Using Redis means a process restart no longer zeroes ``getBalance`` spend
    and the record history survives up to ``max_records``.
    """
    redis_url = getattr(config, "redis_url", None)
    if redis_url:
        return RedisCostLedger(redis_url, max_records=100_000)
    return CostLedger()
