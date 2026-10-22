"""In-memory cost ledger for captcha solve attempts."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, cast

log = logging.getLogger(__name__)

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

# Names that identify a *backend tier* rather than a concrete served model.
# A record created with one of these (the solver routes by tier) should be
# stored / priced with the concrete model id when the usage payload carries it.
_MODEL_ALIASES = frozenset({"", "cloud", "local", "auto", "default"})


def _is_model_alias(model: str | None) -> bool:
    """True when ``model`` names a backend tier rather than a concrete model."""
    return (model or "").strip().lower() in _MODEL_ALIASES


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    price_table: dict | None = None,
    *,
    actual_model: str | None = None,
    backend: str | None = None,
) -> float:
    """Estimate USD cost of a model call from token counts.

    ``price_table`` maps model name to ``(input_per_1k, output_per_1k)``;
    falls back to the default table, then to heuristics (local-looking
    model names cost 0, unknown cloud models get a cheap default rate).

    ``actual_model`` is the concrete model id that served the call (e.g.
    ``config.cloud_model``) when ``model`` is only the backend alias
    (``"cloud"`` / ``"local"``). When supplied it is preferred for pricing, so
    an aliased call is billed at the served model's rate instead of the
    unknown-cloud fallback.

    ``backend`` is the backend kind that served the call (``"local"`` /
    ``"cloud"``), threaded from ``ModelCallResult.backend``. It is
    authoritative when known: a local backend is always free, even when its
    configured served-model id carries no local marker (e.g. an arbitrary
    self-hosted HF repo name), and a cloud backend is never zeroed by a
    local-looking substring in its model id.
    """
    kind = (backend or "").strip().lower()
    if kind == "local":
        return 0.0

    table = price_table if price_table is not None else DEFAULT_PRICE_TABLE
    key = (actual_model or model or "").lower()

    prices = table.get(key)
    if prices is None:
        # Longest-prefix match, e.g. "gpt-4o-mini-2024-07-18" -> "gpt-4o-mini"
        # (not the shorter "gpt-4o"). Table order is irrelevant here: picking
        # the longest matching name is what keeps dated/mini ids honest.
        matches = [
            (str(name).lower(), p)
            for name, p in table.items()
            if key.startswith(str(name).lower())
        ]
        if matches:
            matches.sort(key=lambda item: len(item[0]), reverse=True)
            prices = matches[0][1]
    if prices is None:
        if kind != "cloud" and any(marker in key for marker in _LOCAL_MARKERS):
            return 0.0
        prices = _UNKNOWN_CLOUD_PRICE

    in_price, out_price = float(prices[0]), float(prices[1])
    return (input_tokens / 1000.0) * in_price + (output_tokens / 1000.0) * out_price


def _apply_actual_model(
    rec: SolveRecord,
    actual_model: str | None,
    backend: str | None = None,
) -> None:
    """Store the concrete served model and re-price the record.

    ``actual_model`` (the served id passed to :meth:`CostLedger.record`) wins
    over a value already on the record. ``backend`` (``"local"`` / ``"cloud"``)
    is authoritative when supplied: a local backend zeroes the record's cost
    even when the configured model id has no local marker.

    Pricing is otherwise recomputed only when ``rec.model`` is a backend alias
    — a record whose ``model`` is already a concrete id keeps the
    caller-computed cost. ``rec.model`` itself is left untouched: it is the
    routing / accounting dimension that ``/reportCorrect`` uses to pair with
    the solver's optimistic bucket.
    """
    concrete = (actual_model or rec.actual_model or "").strip()
    kind = (backend or "").strip().lower()
    if concrete:
        rec.actual_model = concrete
    if kind == "local":
        # The backend kind is authoritative — local inference is free.
        rec.est_cost_usd = 0.0
        return
    if concrete and _is_model_alias(rec.model):
        rec.est_cost_usd = estimate_cost(
            rec.model or "",
            rec.input_tokens,
            rec.output_tokens,
            actual_model=concrete,
            backend=kind or None,
        )


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
    # Phase breakdown of ``wall_ms`` (all 0 when not instrumented). Lets an
    # operator see whether a slow solve was dominated by page load, the
    # challenge interaction loop, or model latency — the total ``wall_ms``
    # alone can't localise where time went.
    page_load_ms: int = 0
    challenge_ms: int = 0
    vision_ms: int = 0
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
    # Concrete served model id (e.g. ``gpt-4o-2024-08-06``) when ``model`` is
    # only the backend alias (``"cloud"`` / ``"local"``). Pricing, lifetime
    # totals and per-model reporting prefer this; ``model`` stays as the
    # routing / accounting dimension so /reportCorrect pairs with the
    # solver's optimistic accounting bucket. Appended last so positional
    # construction of older records is unchanged.
    actual_model: str | None = None


class CostLedger:
    """Append-only, bounded, async-safe ledger of solve attempts."""

    def __init__(self, max_records: int = 100_000) -> None:
        self._records: Deque[SolveRecord] = deque(maxlen=max_records)
        self._lock = asyncio.Lock()
        # Monotonic lifetime totals so ``total_cost_usd`` (called on every
        # getBalance poll and by BudgetGuard before every cloud call) is O(1)
        # instead of a full ledger scan. These are deliberately INDEPENDENT of
        # the bounded record ring: evicting an old record never decrements
        # spend, so churning the ring can't make an already-spent budget look
        # available again. ``_total_by_client`` only holds non-None client
        # keys; ``_total_global`` covers every record.
        self._total_global: float = 0.0
        self._total_by_client: Dict[str, float] = {}
        # task_id -> retained records for that id, in append order. Gives the
        # /reportCorrect // /reportIncorrect path O(records-per-task) lookups
        # instead of two full ring scans under the global lock per request.
        # Entries are appended on write and dropped when the ring evicts the
        # record, so the index never outlives the bounded record history.
        self._by_task: Dict[str, Deque[SolveRecord]] = {}

    async def record(
        self,
        rec: SolveRecord,
        *,
        actual_model: str | None = None,
        backend: str | None = None,
    ) -> None:
        """Append ``rec`` and update the monotonic lifetime totals.

        ``actual_model`` is the concrete model id that served the call. When
        the record was created with only a backend alias (``model="cloud"`` /
        ``"local"``), the concrete id is stored on the record and its token
        usage is re-priced at the concrete model's rate. ``backend``
        (``"local"`` / ``"cloud"``) is authoritative for pricing: local
        backends are always billed at zero.
        """
        _apply_actual_model(rec, actual_model, backend)
        if rec.created_at == 0:
            rec.created_at = time.time()
        async with self._lock:
            maxlen = self._records.maxlen
            if maxlen is not None and len(self._records) == maxlen:
                # Eviction only drops record history — lifetime counters are
                # intentionally not decremented here.
                self._drop_task_entry(self._records[0])
            self._records.append(rec)
            self._index_task_entry(rec)
            self._total_global += rec.est_cost_usd
            if rec.client_key is not None:
                self._total_by_client[rec.client_key] = (
                    self._total_by_client.get(rec.client_key, 0.0)
                    + rec.est_cost_usd
                )

    def _index_task_entry(self, rec: SolveRecord) -> None:
        """Track ``rec`` under its task id (caller holds ``self._lock``)."""
        if not rec.task_id:
            return
        bucket = self._by_task.get(rec.task_id)
        if bucket is None:
            bucket = deque()
            self._by_task[rec.task_id] = bucket
        bucket.append(rec)

    def _drop_task_entry(self, rec: SolveRecord) -> None:
        """Drop an evicted record from the task index (caller holds the lock)."""
        bucket = self._by_task.get(rec.task_id) if rec.task_id else None
        if not bucket:
            return
        for i, item in enumerate(bucket):
            if item is rec:
                del bucket[i]
                break
        if not bucket:
            self._by_task.pop(rec.task_id, None)

    def _best_task_record(self, task_id: str) -> Optional[SolveRecord]:
        """Latest-by-``created_at`` retained record for ``task_id`` (lock held)."""
        bucket = self._by_task.get(task_id)
        if not bucket:
            return None
        best = bucket[0]
        for r in bucket:
            if r.created_at > best.created_at:
                best = r
        return best

    async def total_cost_usd(self, client_key: str | None = None) -> float:
        async with self._lock:
            if client_key is None:
                return self._total_global
            return self._total_by_client.get(client_key, 0.0)

    async def task_cost_usd(self, task_id: str) -> float:
        """Total recorded spend for every retained attempt of ``task_id``.

        A solve may append several records under one task id (retries, voting
        attempts); the budget settlement at task finalize needs their sum, not
        just the latest record. Resolves through the task index, so this is
        O(records retained for this task id) rather than a ring scan.
        """
        if not task_id:
            return 0.0
        async with self._lock:
            bucket = self._by_task.get(task_id)
            if not bucket:
                return 0.0
            return sum(r.est_cost_usd for r in bucket)

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

        Uses the task index (O(records retained for this task id)) rather than
        scanning the whole ring — the report route calls this on every
        /reportCorrect // /reportIncorrect. Picks the record with the latest
        ``created_at`` when multiple records share the same task id (e.g. a
        retry). Empty task ids are not indexed (nothing reportable can target
        them); those fall back to the legacy scan.
        """
        async with self._lock:
            if task_id:
                return self._best_task_record(task_id)
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
        ``get_by_task_id`` (latest ``created_at``) and resolves through the
        task index, so the report path does not rescan the ring.
        """
        async with self._lock:
            if task_id:
                best = self._best_task_record(task_id)
            else:
                best = None
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
            # Report by the concrete served model when known; fall back to the
            # backend alias (or "unknown") for records priced before the
            # actual-model plumbing existed.
            model = r.actual_model or r.model or "unknown"
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
        "actual_model": rec.actual_model,
        "challenge_shape": rec.challenge_shape,
        "rounds": rec.rounds,
        "vision_calls": rec.vision_calls,
        "input_tokens": rec.input_tokens,
        "output_tokens": rec.output_tokens,
        "proxy_bytes": rec.proxy_bytes,
        "wall_ms": rec.wall_ms,
        "page_load_ms": rec.page_load_ms,
        "challenge_ms": rec.challenge_ms,
        "vision_ms": rec.vision_ms,
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
        actual_model=data.get("actual_model"),
        challenge_shape=data.get("challenge_shape"),
        rounds=data.get("rounds", 0),
        vision_calls=data.get("vision_calls", 0),
        input_tokens=data.get("input_tokens", 0),
        output_tokens=data.get("output_tokens", 0),
        proxy_bytes=data.get("proxy_bytes", 0),
        wall_ms=data.get("wall_ms", 0),
        page_load_ms=data.get("page_load_ms", 0),
        challenge_ms=data.get("challenge_ms", 0),
        vision_ms=data.get("vision_ms", 0),
        outcome=data.get("outcome", "failed"),
        est_cost_usd=data.get("est_cost_usd", 0.0),
        client_key=data.get("client_key"),
        created_at=data.get("created_at", 0.0),
        reported=data.get("reported", False),
    )


# TTL for the Redis "reported" claim key. 7 days so a long-delayed client
# retry can't re-claim a task that was already reported (which would re-run
# the proxy-pool / accounting / session-pool side effects).
_REPORTED_TTL_SECONDS = 60 * 60 * 24 * 7  # 7 days

# Retention TTL for the bounded record history and its by-task index. Matches
# the reported-claim TTL: once a record has expired there is nothing left to
# report against, so a stale claim can't cause a double side-effect run.
_RECORDS_TTL_SECONDS = 60 * 60 * 24 * 7  # 7 days


class RedisCostLedger:
    """Redis-backed ledger with O(1) balance reads and bounded record history.

    Records are JSON blobs in a Redis list (``{prefix}:records``) capped at
    ``max_records`` via ``LTRIM`` and given a sliding retention TTL. Lifetime
    totals (cost + count) are kept in dedicated Redis strings — global under
    ``{prefix}:global``, per-client under ``{prefix}:client:{key}`` — so
    ``total_cost_usd`` is a single GET rather than a full scan and eviction
    from the record list never reduces spend.

    The by-task report index is a hash (task id -> latest record blob) ordered
    by a ZSET of write times; the ZSET is rank-trimmed to ``max_records`` and
    both keys carry a sliding TTL, so the index cannot grow without bound even
    though records are written for arbitrarily many task ids over the process
    lifetime.

    A restart no longer zeroes spend: the totals survive in Redis and the
    record history is preserved up to ``max_records`` / the retention TTL.
    """

    _RECORDS_KEY = "captcha:ledger:records"
    _TOTAL_GLOBAL = "captcha:ledger:global"
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
        # Lifetime counters live in dedicated, TTL-less keys, namespaced so a
        # client literally named "global" (or "anon") can't alias the global /
        # anonymous counter. INCRBYFLOAT never decrements on list eviction.
        self._total_global = f"{key_prefix}:global"
        self._count_global = f"{key_prefix}:count:global"
        # Report index: hash of task_id -> latest record blob (latest write
        # wins) plus a ZSET of task ids scored by write time. The ZSET is
        # rank-trimmed to ``max_records`` and both keys carry a sliding TTL,
        # so the index is bounded in count as well as time.
        self._by_task_key = f"{key_prefix}:by_task"
        self._by_task_index_key = f"{key_prefix}:by_task_index"
        # Legacy lifetime counters used to live under ``{prefix}:total:global``
        # and ``{prefix}:total:{client}`` (counts under ``{prefix}:count:{...}``).
        # They were renamed without a migration, which would silently reset a
        # deployment's spend (and its budget headroom) on upgrade. The copy is
        # done once, lazily on first use so construction stays synchronous.
        self._legacy_migration_done = False
        self._legacy_migration_lock: "asyncio.Lock | None" = None

    def _total_key(self, client_key: str) -> str:
        return f"{self._prefix}:client:{client_key}"

    def _count_key(self, client_key: str) -> str:
        return f"{self._prefix}:count:client:{client_key}"

    def _task_cost_key(self, task_id: str) -> str:
        return f"{self._prefix}:task_cost:{task_id}"

    # -- legacy key migration ---------------------------------------------

    async def _ensure_legacy_totals_migrated(self) -> None:
        """Run the one-shot legacy-counter migration before first use.

        Cheap after the first call (one boolean). Concurrent first callers
        serialise on a lazily-created lock; a migration failure is logged and
        never fails the ledger operation itself.
        """
        if self._legacy_migration_done:
            return
        if self._legacy_migration_lock is None:
            self._legacy_migration_lock = asyncio.Lock()
        async with self._legacy_migration_lock:
            if self._legacy_migration_done:
                return
            try:
                await self._migrate_legacy_totals()
            except Exception as exc:  # noqa: BLE001 - migration must not break metering
                log.warning("legacy ledger counter migration failed: %s", exc)
            finally:
                self._legacy_migration_done = True

    async def _migrate_legacy_totals(self) -> None:
        """Copy pre-rename lifetime counters to their new keys, then drop old.

        Old → new:
        ``{prefix}:total:global`` → ``{prefix}:global`` and
        ``{prefix}:total:{client}`` → ``{prefix}:client:{client}`` (counts:
        ``{prefix}:count:{client}`` → ``{prefix}:count:client:{client}``).
        A new key is only written when absent, so spend already recorded under
        the new name is never overwritten; the legacy key is deleted either way
        so the migration is not repeated on the next process start.
        """
        legacy_global = f"{self._prefix}:total:global"
        pairs: list[tuple[str, str]] = [
            (legacy_global, self._total_global),
        ]
        await self._collect_legacy_keys(
            f"{self._prefix}:total:", ":client:", pairs
        )
        await self._collect_legacy_keys(
            f"{self._prefix}:count:", ":count:client:", pairs
        )
        found, copied = await self._copy_if_absent(pairs)
        if found:
            log.info(
                "migrated %d legacy ledger counter(s) under %r (%d copied, "
                "%d already present)",
                found,
                self._prefix,
                copied,
                found - copied,
            )

    async def _collect_legacy_keys(
        self,
        pattern: str,
        new_segment: str,
        pairs: "list[tuple[str, str]]",
    ) -> None:
        """Append ``(old_key, new_key)`` pairs found by SCAN for a legacy prefix.

        ``pattern`` is the legacy prefix (e.g. ``{prefix}:total:``); keys whose
        suffix is ``global`` (the global counter handled separately / already
        under its new name) or already starts with ``new_segment`` (already a
        new-scheme key) are skipped.
        """
        marker = new_segment.lstrip(":")
        cursor = 0
        while True:
            cursor, keys = await self._redis.scan(
                cursor=cursor, match=f"{pattern}*", count=200
            )
            for raw in keys or []:
                key = raw.decode() if isinstance(raw, bytes) else str(raw)
                suffix = key[len(pattern):]
                if not suffix or suffix == "global" or suffix.startswith(marker):
                    continue
                pairs.append((key, f"{self._prefix}{new_segment}{suffix}"))
            if int(cursor or 0) == 0:
                break

    async def _copy_if_absent(
        self, pairs: "list[tuple[str, str]]"
    ) -> "tuple[int, int]":
        """GET legacy values in a pipeline, SET only absent new keys, DEL old.

        Returns ``(found, copied)``: how many legacy values existed at all and
        how many were written to a new key (the rest were already present under
        the new name and are only deleted). Pipelines keep this to a constant
        number of round trips regardless of how many clients were tracked.
        """
        if not pairs:
            return 0, 0
        pipe = self._redis.pipeline()
        for old_key, _ in pairs:
            pipe.get(old_key)
        old_values = await pipe.execute()
        pipe = self._redis.pipeline()
        for _, new_key in pairs:
            pipe.get(new_key)
        new_values = await pipe.execute()
        pipe = self._redis.pipeline()
        found = 0
        copied = 0
        for (old_key, new_key), value, existing in zip(
            pairs, old_values, new_values
        ):
            if value is None:
                continue
            found += 1
            if existing is None:
                pipe.set(new_key, value)
                copied += 1
            pipe.delete(old_key)
        await pipe.execute()
        return found, copied

    async def task_cost_usd(self, task_id: str) -> float:
        """Total recorded spend for ``task_id`` from its Redis accumulator."""
        if not task_id:
            return 0.0
        await self._ensure_legacy_totals_migrated()
        raw = await self._redis.get(self._task_cost_key(task_id))
        return float(raw) if raw is not None else 0.0

    async def record(
        self,
        rec: SolveRecord,
        *,
        actual_model: str | None = None,
        backend: str | None = None,
    ) -> None:
        """Append ``rec``, bump the lifetime counters, refresh the report index.

        ``actual_model`` / ``backend`` behave exactly as in
        :meth:`CostLedger.record`: an aliased record is stored / re-priced with
        the concrete served model, and a local backend is always billed at zero.
        """
        await self._ensure_legacy_totals_migrated()
        _apply_actual_model(rec, actual_model, backend)
        if rec.created_at == 0:
            rec.created_at = time.time()
        blob = json.dumps(_serialize_record(rec))
        pipe = self._redis.pipeline()
        pipe.lpush(self._records_key, blob)
        pipe.ltrim(self._records_key, 0, self._max - 1)
        pipe.expire(self._records_key, _RECORDS_TTL_SECONDS)
        # Monotonic lifetime spend (and the informational counts). These keys
        # deliberately get no TTL: spend must survive record-ring expiry.
        pipe.incrbyfloat(self._total_global, rec.est_cost_usd)
        pipe.incr(self._count_global)
        if rec.client_key is not None:
            pipe.incrbyfloat(self._total_key(rec.client_key), rec.est_cost_usd)
            pipe.incr(self._count_key(rec.client_key))
        # Per-task spend accumulator so budget settlement at task finalize can
        # read the solve's recorded cost with a single GET (a solve may append
        # several records under one task id). Same sliding TTL as the records.
        if rec.task_id:
            task_cost_key = self._task_cost_key(rec.task_id)
            pipe.incrbyfloat(task_cost_key, rec.est_cost_usd)
            pipe.expire(task_cost_key, _RECORDS_TTL_SECONDS)
        # Report index: HSET overwrites, so the latest record for a task id
        # wins; a ZSET scored by write time orders task ids for rank-trimming
        # to ``max_records``. Both keys carry a sliding TTL so an idle
        # deployment forgets its index.
        pipe.hset(self._by_task_key, rec.task_id, blob)
        pipe.zadd(self._by_task_index_key, {rec.task_id: time.time()})
        pipe.expire(self._by_task_key, _RECORDS_TTL_SECONDS)
        pipe.expire(self._by_task_index_key, _RECORDS_TTL_SECONDS)
        # ZCARD last: its result is the post-ZADD cardinality used to trim.
        pipe.zcard(self._by_task_index_key)
        results = await pipe.execute()
        indexed = int(results[-1] or 0)
        if indexed > self._max:
            await self._evict_stale_tasks(indexed - self._max, protect=rec.task_id)

    async def _evict_stale_tasks(self, overflow: int, *, protect: str) -> None:
        """Drop the oldest task-index entries beyond ``max_records``.

        Evicts both the ZSET member and its hash blob. ``protect`` (the task
        just written) is never evicted. Fetches one extra candidate so there
        are still ``overflow`` victims when the fresh task id is itself the
        lowest-ranked (e.g. writes within the same clock tick).
        """
        candidates = await self._redis.zrange(self._by_task_index_key, 0, overflow)
        # ``decode_responses=True``: ZRANGE members are always ``str`` at
        # runtime; redis-py types the reply as the full bytes/str union.
        stale = cast("list[str]", [t for t in candidates if t != protect][:overflow])
        if not stale:
            return
        pipe = self._redis.pipeline()
        pipe.hdel(self._by_task_key, *stale)
        pipe.zrem(self._by_task_index_key, *stale)
        await pipe.execute()

    async def total_cost_usd(self, client_key: str | None = None) -> float:
        await self._ensure_legacy_totals_migrated()
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
        await self._ensure_legacy_totals_migrated()
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
        """Return the latest record for ``task_id`` via the capped by-task index."""
        await self._ensure_legacy_totals_migrated()
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
        await self._ensure_legacy_totals_migrated()
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
            pipe = self._redis.pipeline()
            pipe.hset(self._by_task_key, task_id, json.dumps(data))
            pipe.expire(self._by_task_key, _RECORDS_TTL_SECONDS)
            await pipe.execute()
        except Exception:  # noqa: BLE001 - informational only
            pass
        return True

    async def summary(self) -> dict[str, Any]:
        await self._ensure_legacy_totals_migrated()
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
            # Report by the concrete served model when known; fall back to the
            # backend alias (or "unknown") for records priced before the
            # actual-model plumbing existed.
            model = r.actual_model or r.model or "unknown"
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
