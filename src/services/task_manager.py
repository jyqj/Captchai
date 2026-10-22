"""Async task manager: bounded admission, split concurrency, TTL + cancellation.

Runtime plane of the service. Key properties (vs. the original single-semaphore,
fire-and-forget dict):

* **Split concurrency.** Browser solves and pure-vision (classification /
  recognition) calls draw from *separate* semaphores, so a burst of image tasks
  can never starve the browser solvers.
* **Bounded admission / backpressure.** In-flight tasks are capped; when the cap
  is reached ``create_task`` raises :class:`QueueFull` and the API returns
  ``ERROR_NO_SLOT_AVAILABLE`` instead of spawning unbounded coroutines/contexts.
  Admission is capped per *category* (browser / vision), with the global
  ``queue_max_size`` retained as a backstop: a burst of image tasks can fill
  the vision budget without consuming the browser one. The category comes from
  the task-type registry (``core.task_types.TaskType.category``), the single
  source of truth shared with solver wiring.
* **Deadline from admission.** The solve budget starts when the task is
  *admitted*, not when it acquires a concurrency slot. Queue wait is metered on
  the task (``queue_wait_ms``) and subtracted from the budget, so a task that
  sat in the queue cannot then run for a full ``solve_timeout``.
* **Cancellation on TTL / timeout.** Each task's ``asyncio.Task`` is tracked and
  cancelled when the wall-clock budget is exceeded or the record expires, so a
  hung page releases its browser context (via the solver's ``finally``) instead
  of leaking it until the next sweep.
* **Idempotency.** An optional idempotency key coalesces client retries onto the
  same task so a proxy / model call is not double-spent.
* **Runtime config.** Limits come from a :class:`Config` via :meth:`configure`
  rather than being frozen into class attributes at import time.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Optional, Protocol, cast

from ..core.task_types import category_for
from ..orchestration.store import IdempotencyConflict, TaskStore, build_store
from .browser_solver import has_task_proxy, initial_proxy_kind

try:  # pragma: no cover - the API/models wave lands ``redact_params`` in parallel
    from ..models.task import redact_params
except ImportError:  # pragma: no cover - local fallback until that helper lands
    _REDACTED = "<redacted>"

    def redact_params(params: dict[str, Any]) -> dict[str, Any]:
        """Return a copy of ``params`` that is safe to persist / log.

        Fallback copy of ``src.models.task.redact_params`` used only when the
        shared helper is missing. Drops internal (``_``-prefixed) keys such as
        ``_clientKey``, masks proxy passwords, and replaces base64 image
        payloads with a placeholder. Never mutates the live solver params.
        """
        safe: dict[str, Any] = {}
        for key, value in params.items():
            if key.startswith("_"):
                continue
            if "password" in key.lower():
                safe[key] = _REDACTED
            elif key in ("body", "image", "images"):
                safe[key] = _REDACTED
            elif (
                key in ("proxy", "proxyUrl")
                and isinstance(value, str)
                and "@" in value
            ):
                # Convenience form "scheme://user:pass@host:port": the password
                # embedded in the URL must not reach the store either.
                safe[key] = _REDACTED
            else:
                safe[key] = value
        return safe


log = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _fingerprints_conflict(
    fingerprint: str | None, existing_fingerprint: str | None
) -> bool:
    """Only two *known* fingerprints can conflict; ``None`` means legacy/unknown."""
    return (
        fingerprint is not None
        and existing_fingerprint is not None
        and fingerprint != existing_fingerprint
    )


# Params that carry raw image bytes. Removed unconditionally before a store
# write: the shared redaction policy in ``models.task`` only drops image-like
# values on some keys, and a base64 body must never be persisted regardless.
_IMAGE_PARAM_KEYS = ("body", "image", "images")
# Credentials that must never reach the store even if the shared policy changes.
_CREDENTIAL_PARAM_KEYS = ("_clientKey", "proxyPassword")


def _persistable_params(params: dict[str, Any]) -> dict[str, Any]:
    """Redact solver params for persistence without mutating the live params."""
    safe = redact_params(dict(params))
    for key in (*_CREDENTIAL_PARAM_KEYS, *_IMAGE_PARAM_KEYS):
        safe.pop(key, None)
    return safe


def _key_digest(value: str | None) -> str | None:
    """One-way digest of the persisted idempotency key.

    The API layer scopes keys with the caller's ``clientKey``, so the raw value
    is a credential; the store record keeps only this digest. The claim itself
    still lives under ``captcha:idem:<key>`` (that key *is* the mechanism).
    """
    if value is None:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class TaskStatus(str, Enum):
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


class TaskCategory(str, Enum):
    """Which concurrency pool a task type draws from."""

    BROWSER = "browser"
    VISION = "vision"


class QueueFull(Exception):
    """Raised by :meth:`TaskManager.create_task` when admission is at capacity."""


class BudgetExceeded(QueueFull):
    """Admission rejected because a configured budget cap would be exceeded.

    Subclasses :class:`QueueFull` so the existing admission-error path in the
    API (``ERROR_NO_SLOT_AVAILABLE`` carrying the rejection reason) handles it
    without a new error code. ``reason`` is the guard's cap diagnosis.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"budget cap exceeded at admission: {reason}")


class CategoryFull(QueueFull):
    """Admission rejected because a task category's in-flight budget is full.

    Subclasses :class:`QueueFull` so the API's existing admission-error path
    (``ERROR_NO_SLOT_AVAILABLE``) handles it unchanged; ``category`` / ``cap`` /
    ``inflight`` make the rejection machine-readable instead of forcing callers
    to parse the message.
    """

    def __init__(self, category: "TaskCategory", cap: int, inflight: int) -> None:
        self.category = category
        self.cap = cap
        self.inflight = inflight
        super().__init__(
            f"{category.value} in-flight tasks at capacity ({inflight}/{cap})"
        )


# Admission budget for the vision category when the config does not declare
# ``vision_max_concurrency``. Optional on purpose: per-category admission works
# without a deployment config change, and existing Config objects (including
# the minimal ones tests build) keep admitting.
_DEFAULT_VISION_MAX_CONCURRENCY = 8


# Conservative per-task budget estimate used when the config does not set
# ``budget_task_estimate_usd``. One browser solve makes several vision calls
# (voting / retries), so a flat floor well above a single call keeps concurrent
# admissions from each assuming almost no spend.
_DEFAULT_TASK_ESTIMATE_USD = 0.05


@dataclass
class Task:
    id: str
    type: str
    params: dict[str, Any]
    status: TaskStatus = TaskStatus.PROCESSING
    solution: dict[str, Any] | None = None
    error_code: str | None = None
    error_description: str | None = None
    created_at: datetime = field(default_factory=_utcnow)
    idempotency_key: str | None = None
    # Monotonic admission timestamp: the solve budget is measured from here
    # (queue wait + execution), so a task that queued for most of its budget
    # only gets the remainder to run. Not serialized — a reconstructed record
    # is read-only for the local process.
    admitted_at: float = field(default_factory=time.monotonic)
    # Milliseconds spent waiting for a concurrency slot, recorded when the
    # runner acquires its semaphore. ``None`` until the solve actually starts
    # (and for tasks that never ran, e.g. an unsupported type).
    queue_wait_ms: int | None = None
    # Monotonic persistence revision: bumped on every store mirror so a slow
    # (fire-and-forget) write can't overwrite a newer status written after it.
    revision: int = 0
    # Budget hold taken at admission and the guard that issued it (settled in
    # the runner's ``finally``). Not serialized: a restarted process has no
    # in-flight budget to release.
    budget_reservation: Any = None
    budget_guard: Any = None


class Solver(Protocol):
    async def solve(self, params: dict[str, Any]) -> dict[str, Any]: ...


class TaskManager:
    TASK_TTL = timedelta(minutes=10)

    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}
        self._solvers: dict[str, Solver] = {}
        self._categories: dict[str, TaskCategory] = {}
        # Strong references to in-flight coroutines (asyncio holds only weak refs)
        # plus a task_id -> asyncio.Task map so we can cancel on expiry / timeout.
        self._running: dict[str, asyncio.Task[None]] = {}
        # Idempotency key -> (task_id, payload_fingerprint). The fingerprint is
        # ``None`` for callers that predate payload-aware idempotency.
        self._idempotency: dict[str, tuple[str, str | None]] = {}

        # Persistent task store (Redis when configured, in-memory otherwise).
        # Built in ``configure``; ``None`` until then. The in-memory ``_tasks``
        # dict remains the authoritative mutable copy — the store mirrors it for
        # restart survival and cross-process visibility.
        self._store: TaskStore | None = None
        # Proxy pool reference for best-effort peek routing in ``_semaphore_for``.
        # Set in ``configure``; ``None`` until then (and in tests that don't wire it).
        self._proxy_pool: Any = None
        # Runtime config + optional injected BudgetGuard. When no guard is
        # injected, admission resolves the process-wide services' guard lazily
        # (``get_services().budget``) so the production wiring needs no change.
        self._config: Any = None
        self._budget: Any = None
        # Strong refs for fire-and-forget store calls so the GC doesn't reap
        # them mid-flight (see asyncio.create_task docs).
        self._pending_persist: set[asyncio.Task] = set()

        # Limits + pools. Populated with env defaults now; overridden by
        # ``configure`` once the app Config is available.
        self._browser_concurrency = int(os.environ.get("BROWSER_CONCURRENCY", "4"))
        self._browser_proxyless_concurrency = int(
            os.environ.get(
                "BROWSER_PROXYLESS_CONCURRENCY",
                os.environ.get("BROWSER_CONCURRENCY", "4"),
            )
        )
        self._browser_proxied_concurrency = int(
            os.environ.get(
                "BROWSER_PROXIED_CONCURRENCY",
                os.environ.get("BROWSER_CONCURRENCY", "4"),
            )
        )
        self._browser_pool_proxy_concurrency = int(
            os.environ.get(
                "BROWSER_POOL_PROXY_CONCURRENCY",
                os.environ.get(
                    "BROWSER_PROXIED_CONCURRENCY",
                    os.environ.get("BROWSER_CONCURRENCY", "4"),
                ),
            )
        )
        self._vision_concurrency = int(os.environ.get("VISION_CONCURRENCY", "8"))
        self._queue_max_size = int(os.environ.get("QUEUE_MAX_SIZE", "128"))
        self._solve_timeout = int(
            os.environ.get("CAPTCHA_SOLVE_TIMEOUT", os.environ.get("SOLVE_TIMEOUT", "180"))
        )
        self._browser_sem = asyncio.Semaphore(self._browser_concurrency)
        self._browser_proxyless_sem = asyncio.Semaphore(
            self._browser_proxyless_concurrency
        )
        self._browser_proxied_sem = asyncio.Semaphore(
            self._browser_proxied_concurrency
        )
        self._browser_pool_proxy_sem = asyncio.Semaphore(
            self._browser_pool_proxy_concurrency
        )
        self._vision_sem = asyncio.Semaphore(self._vision_concurrency)

        # Resolved task-type -> category cache (populated lazily from the
        # registry / registration map; cleared when a solver is registered).
        self._category_cache: dict[str, TaskCategory] = {}
        # Per-category admission budgets + the global backstop. Derived from the
        # concurrency limits; recomputed by ``configure``.
        self._category_budgets: dict[TaskCategory, int] = {}
        self._vision_budget = _DEFAULT_VISION_MAX_CONCURRENCY
        self._browser_budget = 0
        self._refresh_limits()

    def _refresh_limits(self) -> None:
        """Derive the admission budgets from the current concurrency limits.

        Browser: the sum of the per-egress pools. A browser task occupies
        exactly one of them, and every limit is ``browser_concurrency``-derived
        in config, so the sum is the category's real simultaneous capacity — a
        cap equal to the base ``browser_concurrency`` alone would reject tasks
        the egress pools could run concurrently.

        Vision: ``vision_max_concurrency`` when the deployment sets it, else
        :data:`_DEFAULT_VISION_MAX_CONCURRENCY`. It is admission-only, so it may
        exceed the execution semaphore (``vision_concurrency``): admitted tasks
        queue for a slot, which is exactly what ``queue_wait_ms`` meters.
        """
        vision_budget = getattr(
            self._config, "vision_max_concurrency", _DEFAULT_VISION_MAX_CONCURRENCY
        )
        try:
            vision_budget = int(vision_budget)
        except (TypeError, ValueError):
            vision_budget = _DEFAULT_VISION_MAX_CONCURRENCY
        if vision_budget <= 0:
            vision_budget = _DEFAULT_VISION_MAX_CONCURRENCY

        self._vision_budget = vision_budget
        self._browser_budget = max(
            1,
            self._browser_proxyless_concurrency
            + self._browser_proxied_concurrency
            + self._browser_pool_proxy_concurrency,
        )
        self._category_budgets = {
            TaskCategory.BROWSER: self._browser_budget,
            TaskCategory.VISION: self._vision_budget,
        }

    # ── configuration ──────────────────────────────────────────

    def configure(
        self,
        config: Any,
        store: TaskStore | None = None,
        *,
        proxy_pool: Any = None,
        budget: Any = None,
    ) -> None:
        """Apply runtime limits from the application Config.

        ``store`` lets tests inject a fake; when omitted a store is built from
        the config via :func:`build_store` (Redis when ``redis_url`` is set,
        in-memory otherwise).

        ``proxy_pool`` lets the scheduler peek whether pool proxies are
        available at enqueue time so ``egress="auto"`` + no-task-proxy tasks
        can be routed to the dedicated pool-proxy semaphore instead of
        contesting the proxyless pool. Best-effort: a wrong peek still runs
        the task (just on a suboptimal semaphore).

        ``budget`` optionally injects a :class:`~src.consumption.budget.BudgetGuard`.
        When omitted, admission resolves the process-wide guard from
        :func:`~src.core.services.get_services` lazily (the production path),
        so a deployment with budget caps gets race-free per-task holds without
        changing the startup wiring.
        """
        self._config = config
        self._budget = budget
        self._browser_concurrency = config.browser_concurrency
        self._browser_proxyless_concurrency = getattr(
            config, "browser_proxyless_concurrency", self._browser_concurrency
        )
        self._browser_proxied_concurrency = getattr(
            config, "browser_proxied_concurrency", self._browser_concurrency
        )
        self._browser_pool_proxy_concurrency = getattr(
            config,
            "browser_pool_proxy_concurrency",
            self._browser_proxied_concurrency,
        )
        self._vision_concurrency = config.vision_concurrency
        self._queue_max_size = config.queue_max_size
        self._solve_timeout = config.solve_timeout
        self._browser_sem = asyncio.Semaphore(self._browser_concurrency)
        self._browser_proxyless_sem = asyncio.Semaphore(
            self._browser_proxyless_concurrency
        )
        self._browser_proxied_sem = asyncio.Semaphore(
            self._browser_proxied_concurrency
        )
        self._browser_pool_proxy_sem = asyncio.Semaphore(
            self._browser_pool_proxy_concurrency
        )
        self._vision_sem = asyncio.Semaphore(self._vision_concurrency)
        self._refresh_limits()
        self._store = store if store is not None else build_store(config)
        self._proxy_pool = proxy_pool

    def register_solver(
        self,
        task_type: str,
        solver: Solver,
        category: TaskCategory | str = TaskCategory.BROWSER,
    ) -> None:
        self._solvers[task_type] = solver
        self._categories[task_type] = TaskCategory(category)
        # A (re-)registration may change the fallback category for an
        # unregistered type; drop the resolved value so it is re-derived.
        self._category_cache.pop(task_type, None)

    # ── admission ──────────────────────────────────────────────

    def create_task(
        self,
        task_type: str,
        params: dict[str, Any],
        idempotency_key: str | None = None,
        *,
        task_id: str | None = None,
        payload_fingerprint: str | None = None,
    ) -> str:
        """Synchronous admission (same-worker idempotency only).

        Kept for callers/tests that admit within one process. For cross-worker
        deduplication (Redis configured) prefer :meth:`acreate_task`, which adds
        an atomic store-level claim before spawning the solve. Budget holds are
        taken on that async admission path: this synchronous method cannot
        await ``BudgetGuard.reserve``, so it admits without one.

        ``payload_fingerprint`` lets a retry prove it replays the same request:
        a same-key admission with a different fingerprint raises
        :class:`~src.orchestration.store.IdempotencyConflict` instead of
        returning the earlier task's id. ``None`` keeps the legacy behaviour.
        """
        self._cleanup_expired()

        # Coalesce retries carrying the same idempotency key onto one task.
        if idempotency_key:
            existing_id = self._coalesced_task_id(
                idempotency_key, payload_fingerprint
            )
            if existing_id:
                return existing_id

        self._check_admission(task_type)

        return self._admit(
            task_type,
            params,
            idempotency_key,
            task_id or str(uuid.uuid4()),
            payload_fingerprint=payload_fingerprint,
        )

    async def acreate_task(
        self,
        task_type: str,
        params: dict[str, Any],
        idempotency_key: str | None = None,
        *,
        payload_fingerprint: str | None = None,
    ) -> str:
        """Async admission with a cross-worker idempotency claim.

        A same-worker retry is coalesced via the in-memory map (fast path). For
        a cross-worker retry, the store's atomic ``claim_idempotency`` (Redis
        ``SET NX``) decides ownership *before* the solve is spawned, so a client
        retry that lands on a different worker returns the original task id
        instead of double-spending a proxy / model call. The queue check runs
        before the claim so a rejected (full) admission never leaves a dangling
        claim that a retry would resolve to a non-existent task.

        ``payload_fingerprint`` binds the claim to the request payload: a replay
        of the key with a different payload raises
        :class:`~src.orchestration.store.IdempotencyConflict` (mapped by the API
        layer to a distinct client error) rather than returning a result for
        someone else's request. ``None`` behaves exactly as before.

        The idempotency key is passed to the store verbatim, so callers may scope
        it (e.g. ``f"{clientKey}:{idempotencyKey}"``) without the manager
        rewriting it.

        When a budget guard is wired, admission also takes a per-task budget
        hold (``reserve``) before the claim / spawn, so concurrent admissions
        can't each see the same cap headroom; the hold is released in the
        runner's ``finally`` with the ledger's recorded cost. A denied hold
        raises :class:`BudgetExceeded` (a :class:`QueueFull`) and admits
        nothing.
        """
        self._cleanup_expired()

        if idempotency_key:
            existing_id = self._coalesced_task_id(
                idempotency_key, payload_fingerprint
            )
            if existing_id:
                return existing_id

        self._check_admission(task_type)

        # Race-free budget admission: check the cap against spent + in-flight
        # and hold the estimate before anything is claimed or spawned.
        budget_guard, reservation = await self._reserve_task_budget(
            task_type, params
        )

        task_id = str(uuid.uuid4())
        if idempotency_key and self._store is not None:
            claim = getattr(self._store, "claim_idempotency", None)
            if claim is not None:
                try:
                    owner = await self._claim_idempotency(
                        claim, idempotency_key, task_id, payload_fingerprint
                    )
                except BaseException:
                    await self._release_task_budget(reservation, budget_guard)
                    raise
                if owner and owner != task_id:
                    # Another worker owns this key; release the unused hold.
                    await self._release_task_budget(reservation, budget_guard)
                    return owner

        try:
            admitted = self._admit(
                task_type,
                params,
                idempotency_key,
                task_id,
                payload_fingerprint=payload_fingerprint,
                persist=False,
                reservation=reservation,
                budget_guard=budget_guard,
            )
        except BaseException:
            await self._release_task_budget(reservation, budget_guard)
            raise
        # Await the initial mirror (no fire-and-forget): the record exists before
        # the response is returned, so a restart or another worker can resolve
        # the task. ``_persist_task`` logs and swallows store failures, so an
        # outage can neither fail admission nor crash the solve.
        task = self._tasks.get(admitted)
        if task is not None:
            await self._persist_task(task)
        return admitted

    def _coalesced_task_id(
        self, idempotency_key: str, payload_fingerprint: str | None
    ) -> str | None:
        """Local task a retry coalesces onto, if one is live.

        Raises :class:`IdempotencyConflict` when the key is live but was first
        used with a different payload fingerprint — a retry must replay the same
        request, not silently receive another request's result.
        """
        entry = self._idempotency.get(idempotency_key)
        if entry is None:
            return None
        existing_id, existing_fingerprint = entry
        if existing_id not in self._tasks:
            return None
        if _fingerprints_conflict(existing_fingerprint, payload_fingerprint):
            raise IdempotencyConflict(
                idempotency_key,
                existing_id,
                payload_fingerprint,
                existing_fingerprint,
            )
        return existing_id

    async def _claim_idempotency(
        self,
        claim: Any,
        idempotency_key: str,
        task_id: str,
        payload_fingerprint: str | None,
    ) -> str:
        """Run the store's atomic claim, never failing admission on store errors.

        A store implementation that predates the ``fingerprint`` keyword is
        retried without it (legacy compatibility). An
        :class:`IdempotencyConflict` always propagates: it is a caller error,
        not a store outage.
        """
        ttl = self._idempotency_ttl()
        try:
            try:
                return await claim(
                    idempotency_key,
                    task_id,
                    ttl,
                    fingerprint=payload_fingerprint,
                )
            except TypeError:
                # Store predates the fingerprint kwarg — legacy claim semantics
                # (coalesce regardless of payload) still apply.
                return await claim(idempotency_key, task_id, ttl)
        except IdempotencyConflict:
            raise
        except Exception as exc:  # noqa: BLE001 - never fail admission on claim
            log.warning(
                "Idempotency claim failed for task %s (%s); admitting locally",
                task_id,
                exc,
            )
            return task_id

    def _idempotency_ttl(self) -> int:
        """TTL (seconds) for a cross-worker idempotency claim.

        Matches the task store's record TTL (``solve_timeout * 4``, see
        ``build_store``) with a small floor so a retry within the task's
        lifetime resolves to the original id, and the claim expires afterwards.
        """
        return max(60, int(self._solve_timeout) * 4)

    # ── budget admission ───────────────────────────────────────

    def _budget_guard(self) -> Any:
        """The injected guard, else the process-wide services' guard (or None).

        Lazy so the production lifespan can keep calling ``configure`` before
        / without passing a guard: ``set_services`` runs first and admission
        picks the shared :class:`~src.consumption.budget.BudgetGuard` up here.
        """
        if self._budget is not None:
            return self._budget
        try:
            from ..core.services import get_services
        except Exception:  # noqa: BLE001 - optional wiring
            return None
        services = get_services()
        return getattr(services, "budget", None) if services is not None else None

    def _ledger_service(self) -> Any:
        """The process-wide ledger that recorded this task's spend, if any."""
        try:
            from ..core.services import get_services
        except Exception:  # noqa: BLE001 - optional wiring
            return None
        services = get_services()
        return getattr(services, "ledger", None) if services is not None else None

    def _task_estimate_usd(self, task_type: str) -> float:
        """Conservative per-task spend estimate used for the admission hold.

        ``config.budget_task_estimate_usd`` may be a flat number or a mapping of
        task type -> estimate; a mapping falls back to its largest value (the
        most conservative admission) for an unlisted type. Malformed / missing
        config yields :data:`_DEFAULT_TASK_ESTIMATE_USD`.
        """
        raw = getattr(self._config, "budget_task_estimate_usd", None)
        if raw is None:
            return _DEFAULT_TASK_ESTIMATE_USD
        if isinstance(raw, dict):
            value = raw.get(task_type)
            if value is None:
                try:
                    value = max(raw.values()) if raw else _DEFAULT_TASK_ESTIMATE_USD
                except (TypeError, ValueError):
                    value = _DEFAULT_TASK_ESTIMATE_USD
            raw = value
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return _DEFAULT_TASK_ESTIMATE_USD

    async def _reserve_task_budget(
        self, task_type: str, params: dict[str, Any]
    ) -> "tuple[Any, Any]":
        """Take a budget hold for an admitted task: ``(guard, reservation)``.

        Returns ``(None, None)`` when no guard is wired or the hold failed
        best-effort. Raises :class:`BudgetExceeded` when the guard denies the
        reservation — admission is rejected exactly like a full queue so the
        caller can't spend past the cap.
        """
        guard = self._budget_guard()
        reserve = getattr(guard, "reserve", None) if guard is not None else None
        if not callable(reserve):
            return None, None
        client_key = params.get("_clientKey")
        estimate = self._task_estimate_usd(task_type)
        try:
            try:
                reservation = reserve(client_key, estimate, model="cloud")
                if inspect.isawaitable(reservation):
                    reservation = await reservation
            except TypeError:
                # Guard predates the ``model`` keyword.
                reservation = reserve(client_key, estimate)
                if inspect.isawaitable(reservation):
                    reservation = await reservation
        except Exception as exc:  # noqa: BLE001 - a budget backend outage must not block solves
            log.warning(
                "budget reserve failed for %s task (%s); admitting without a hold",
                task_type,
                exc,
            )
            return None, None
        if reservation is None:
            return None, None
        if not getattr(reservation, "allowed", True):
            reason = getattr(reservation, "reason", "") or "cap exceeded"
            log.warning(
                "Rejecting %s task at admission: budget cap exceeded (%s)",
                task_type,
                reason,
            )
            raise BudgetExceeded(reason)
        return guard, reservation

    async def _recorded_task_cost(self, task_id: str) -> float:
        """Total spend the ledger recorded for ``task_id`` (0 when unknown)."""
        ledger = self._ledger_service()
        if ledger is None or not task_id:
            return 0.0
        task_cost = getattr(ledger, "task_cost_usd", None)
        if callable(task_cost):
            value = task_cost(task_id)
            if inspect.isawaitable(value):
                value = await value
            # Duck-typed ledger return; float() validates the runtime value.
            return float(cast(Any, value) or 0.0)
        get_by_task = getattr(ledger, "get_by_task_id", None)
        if callable(get_by_task):
            rec = get_by_task(task_id)
            if inspect.isawaitable(rec):
                rec = await rec
            if rec is not None:
                return float(getattr(rec, "est_cost_usd", 0.0) or 0.0)
        return 0.0

    async def _settle_task_budget(self, task: Task) -> None:
        """Release ``task``'s budget hold and book its recorded actual cost.

        Runs from the runner's ``finally`` so cancellation / timeout / error
        paths all release the hold. Best-effort: a settle failure is logged and
        never masks the solve result.
        """
        reservation = getattr(task, "budget_reservation", None)
        if reservation is None:
            return
        guard = getattr(task, "budget_guard", None) or self._budget_guard()
        settle = getattr(guard, "settle", None) if guard is not None else None
        if not callable(settle):
            return
        try:
            actual = await self._recorded_task_cost(task.id)
        except Exception as exc:  # noqa: BLE001 - settle with what we know
            log.debug("ledger cost lookup failed for task %s: %s", task.id, exc)
            actual = 0.0
        try:
            result = settle(reservation, actual)
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001 - budget bookkeeping must not crash a solve
            log.warning(
                "budget settle failed for task %s (reservation %s)",
                task.id,
                getattr(reservation, "est_cost_usd", "?"),
                exc_info=True,
            )

    async def _release_task_budget(self, reservation: Any, guard: Any) -> None:
        """Settle a hold that never became a task (coalesced / failed admit)."""
        settle = getattr(guard, "settle", None) if guard is not None else None
        if reservation is None or not callable(settle):
            return
        try:
            result = settle(reservation, 0.0)
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001 - releasing is best-effort
            log.warning("budget release failed for an unadmitted task", exc_info=True)

    def _admit(
        self,
        task_type: str,
        params: dict[str, Any],
        idempotency_key: str | None,
        task_id: str,
        *,
        payload_fingerprint: str | None = None,
        persist: bool = True,
        reservation: Any = None,
        budget_guard: Any = None,
    ) -> str:
        """Register a task, mirror it to the store, and spawn its runner.

        ``persist=False`` lets an async caller await the initial mirror itself
        (see :meth:`acreate_task`); otherwise it is scheduled fire-and-forget
        for sync callers that cannot await. ``reservation`` / ``budget_guard``
        attach an admission budget hold, settled by the runner's ``finally``.
        """
        task = Task(
            id=task_id,
            type=task_type,
            params=params,
            idempotency_key=idempotency_key,
            budget_reservation=reservation,
            budget_guard=budget_guard,
        )
        self._tasks[task_id] = task
        if idempotency_key:
            self._idempotency[idempotency_key] = (task_id, payload_fingerprint)

        if persist:
            # Fire-and-forget so a sync caller doesn't block; the revision-
            # ordered upsert keeps it from clobbering the runner's later status.
            self._persist_fire_and_forget(task)

        runner = asyncio.create_task(self._process_task(task))
        self._running[task_id] = runner
        runner.add_done_callback(lambda _t, tid=task_id: self._running.pop(tid, None))
        return task_id

    def get_task(self, task_id: str) -> Task | None:
        # Sync fast path: the in-memory dict is authoritative. Cross-process
        # reads via the store need an await — see ``aget_task``.
        return self._tasks.get(task_id)

    async def aget_task(self, task_id: str) -> Task | None:
        """Async getter that falls back to the store for cross-process visibility."""
        task = self._tasks.get(task_id)
        if task is not None:
            return task
        if self._store is None:
            return None
        record = await self._store.get(task_id)
        if record is None:
            return None
        return self._deserialize_task(record)

    def supported_types(self) -> list[str]:
        return list(self._solvers.keys())

    def _inflight_count(self) -> int:
        return sum(
            1 for t in self._tasks.values() if t.status == TaskStatus.PROCESSING
        )

    def _category_of(self, task_type: str) -> TaskCategory:
        """The concurrency pool ``task_type`` draws from.

        The task-type registry (``core.task_types.TaskType.category``) is the
        single source of truth: a type it declares always resolves to its
        declared category, even if ``register_solver`` passed a different one.
        Types the registry does not know (test doubles, dynamically added
        solvers) fall back to the registration-time category, then browser.
        """
        cached = self._category_cache.get(task_type)
        if cached is not None:
            return cached
        category: TaskCategory | None = None
        declared = category_for(task_type)
        if declared is not None:
            try:
                category = TaskCategory(declared)
            except ValueError:  # pragma: no cover - registry is internal data
                category = None
        if category is None:
            category = self._categories.get(task_type, TaskCategory.BROWSER)
        self._category_cache[task_type] = category
        return category

    def _inflight_for_category(self, category: TaskCategory) -> int:
        """Admitted (still processing) tasks drawing from ``category``."""
        return sum(
            1
            for task in self._tasks.values()
            if task.status == TaskStatus.PROCESSING
            and self._category_of(task.type) == category
        )

    def _check_admission(self, task_type: str) -> None:
        """Enforce the global backstop, then the task's category budget.

        Raised errors subclass :class:`QueueFull`, so the API keeps mapping any
        admission rejection to ``ERROR_NO_SLOT_AVAILABLE``:
        :class:`CategoryFull` when the category budget is exhausted and a plain
        :class:`QueueFull` when the global ``queue_max_size`` backstop is hit.
        """
        inflight = self._inflight_count()
        if self._queue_max_size and inflight >= self._queue_max_size:
            raise QueueFull(
                f"in-flight tasks at capacity ({self._queue_max_size})"
            )
        category = self._category_of(task_type)
        cap = self._category_budgets.get(category, 0)
        if cap > 0:
            category_inflight = self._inflight_for_category(category)
            if category_inflight >= cap:
                log.info(
                    "Rejecting %s task at admission: %s budget exhausted (%d/%d)",
                    task_type,
                    category.value,
                    category_inflight,
                    cap,
                )
                raise CategoryFull(category, cap, category_inflight)

    # ── execution ──────────────────────────────────────────────

    def _semaphore_for(self, task: Task) -> asyncio.Semaphore:
        category = self._category_of(task.type)
        if category == TaskCategory.VISION:
            return self._vision_sem
        # ``egress`` explicitly declares the caller's intent and wins outright.
        # ``auto`` (default) preserves the legacy behaviour of classifying by
        # caller-supplied proxy fields, with a best-effort peek at the proxy
        # pool: when no task proxy is supplied AND the pool has proxies
        # available, route to the pool_proxy semaphore so pool-bound solves
        # don't contest the proxyless pool. The peek is lock-free and may race
        # with a concurrent ``report``; a wrong guess still runs the task.
        egress = task.params.get("egress") or "auto"
        if egress == "pool":
            return self._browser_pool_proxy_sem
        if egress == "proxyless":
            return self._browser_proxyless_sem
        if egress == "task":
            return self._browser_proxied_sem
        if has_task_proxy(task.params):
            return self._browser_proxied_sem
        if self._proxy_pool is not None and self._proxy_pool.has_available():
            return self._browser_pool_proxy_sem
        return self._browser_proxyless_sem

    async def _process_task(self, task: Task) -> None:
        solver = self._solvers.get(task.type)
        if not solver:
            task.status = TaskStatus.FAILED
            task.error_code = "ERROR_TASK_NOT_SUPPORTED"
            task.error_description = f"Task type '{task.type}' is not supported"
            # No solve will run, so release the admission hold here (this early
            # return skips the try/finally that normally settles it).
            await self._settle_task_budget(task)
            await self._persist_task(task)
            return

        # Make the task id available to the solver (for consumption records)
        # without changing the public solve() signature.
        task.params.setdefault("_taskId", task.id)
        task.params.setdefault("_proxyKind", initial_proxy_kind(task.params).value)

        semaphore = self._semaphore_for(task)
        try:
            async with semaphore:
                # Meter the queue wait now that a slot is held. The deadline is
                # anchored at ADMISSION (``task.admitted_at``), not at this
                # point: queue time is part of the solve budget, so a task that
                # waited for a slot gets only the remainder to execute.
                task.queue_wait_ms = int(
                    max(0.0, time.monotonic() - task.admitted_at) * 1000.0
                )
                log.info(
                    "Task %s acquired a %s slot after %dms queue wait",
                    task.id,
                    self._category_of(task.type).value,
                    task.queue_wait_ms,
                )
                remaining = self._solve_timeout - (
                    time.monotonic() - task.admitted_at
                )
                if remaining <= 0:
                    # Queue wait consumed the whole budget; fail through the
                    # same timeout mapping instead of starting a doomed solve.
                    raise asyncio.TimeoutError()
                solution = await asyncio.wait_for(
                    solver.solve(task.params), timeout=remaining
                )
            task.solution = solution
            task.status = TaskStatus.READY
            log.info("Task %s completed successfully", task.id)
        except asyncio.TimeoutError:
            task.status = TaskStatus.FAILED
            task.error_code = "ERROR_CAPTCHA_TIMEOUT"
            task.error_description = f"Solve exceeded {self._solve_timeout}s budget"
            log.error(
                "Task %s timed out (%sms of the budget went to queue wait)",
                task.id,
                task.queue_wait_ms if task.queue_wait_ms is not None else "?",
            )
        except asyncio.CancelledError:
            # Expiry sweep or shutdown cancelled us; record and re-raise so the
            # event loop can finish teardown. The solver's own ``finally`` closes
            # its browser context as CancelledError propagates through it.
            if task.status == TaskStatus.PROCESSING:
                task.status = TaskStatus.FAILED
                task.error_code = "ERROR_CAPTCHA_TIMEOUT"
                task.error_description = "Task cancelled (expired or shutting down)"
            log.warning("Task %s cancelled", task.id)
            raise
        except Exception as exc:
            task.status = TaskStatus.FAILED
            task.error_code = "ERROR_CAPTCHA_UNSOLVABLE"
            task.error_description = str(exc)
            log.error("Task %s failed: %s", task.id, exc)
        finally:
            # Release the admission budget hold with the cost the ledger
            # recorded for this task. Runs for success, failure, timeout and
            # cancellation alike; failures inside are logged and swallowed.
            await self._settle_task_budget(task)
            # Mirror the final status into the persistent store. CancelledError
            # (BaseException) is intentionally not caught — it propagates and
            # the in-memory dict remains the authoritative source of truth.
            try:
                await self._persist_task(task)
            except Exception:
                log.exception("Failed to persist task %s", task.id)

    def _cleanup_expired(self) -> None:
        now = _utcnow()
        expired = [
            tid
            for tid, t in self._tasks.items()
            if now - t.created_at > self.TASK_TTL
        ]
        for tid in expired:
            # Cancel a still-running coroutine so its browser context is released
            # rather than leaked until the process exits.
            runner = self._running.get(tid)
            if runner is not None and not runner.done():
                runner.cancel()
            task = self._tasks.pop(tid, None)
            if task and task.idempotency_key:
                self._idempotency.pop(task.idempotency_key, None)
            # Sync method → can't await; fire-and-forget the store delete.
            self._delete_fire_and_forget(tid)

    async def shutdown(self) -> None:
        """Cancel every in-flight task and flush pending store writes."""
        runners = [r for r in self._running.values() if not r.done()]
        for r in runners:
            r.cancel()
        for r in runners:
            try:
                await r
            except (asyncio.CancelledError, Exception):
                pass
        # Runners persist their final state in ``finally``; drain the remaining
        # fire-and-forget writes (sync admission / expiry deletes) before the
        # store is closed underneath them.
        await self._drain_pending_writes()
        if self._store is not None:
            try:
                await self._store.close()
            except Exception:
                log.exception("Failed to close task store")

    # ── store helpers ───────────────────────────────────────────

    def _serialize_task(self, task: Task) -> dict[str, Any]:
        """Convert a Task to a JSON-safe dict for the persistent store.

        Params go through :func:`redact_params` on a *copy*: the live solver
        params keep ``_clientKey`` / proxy credentials, while the persisted
        mirror never contains them (nor base64 image bodies).
        """
        return {
            "id": task.id,
            "type": task.type,
            "params": _persistable_params(task.params),
            "status": task.status.value,
            "solution": task.solution,
            "error_code": task.error_code,
            "error_description": task.error_description,
            "created_at": task.created_at.isoformat(),
            "queue_wait_ms": task.queue_wait_ms,
            "idempotency_key": _key_digest(task.idempotency_key),
            "revision": task.revision,
        }

    def _deserialize_task(self, record: dict[str, Any]) -> Task:
        """Reconstruct a Task from a stored record (best-effort)."""
        status_raw = record.get("status", "processing")
        try:
            status_enum = TaskStatus(status_raw)
        except ValueError:
            status_enum = TaskStatus.PROCESSING
        created_at = _utcnow()
        created_at_raw = record.get("created_at")
        if created_at_raw:
            try:
                parsed = datetime.fromisoformat(created_at_raw)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                created_at = parsed
            except (TypeError, ValueError):
                pass
        try:
            revision = int(record.get("revision") or 0)
        except (TypeError, ValueError):
            revision = 0
        queue_wait_ms = record.get("queue_wait_ms")
        if queue_wait_ms is not None:
            try:
                queue_wait_ms = int(queue_wait_ms)
            except (TypeError, ValueError):
                queue_wait_ms = None
        return Task(
            id=record.get("id", ""),
            type=record.get("type", ""),
            params=record.get("params") or {},
            status=status_enum,
            solution=record.get("solution"),
            error_code=record.get("error_code"),
            error_description=record.get("error_description"),
            created_at=created_at,
            queue_wait_ms=queue_wait_ms,
            idempotency_key=record.get("idempotency_key"),
            revision=revision,
        )

    async def _persist_task(self, task: Task) -> None:
        """Mirror the current task state into the store (upsert; never raises).

        A monotonic ``revision`` is bumped on every write so an in-flight slow
        write can't regress a newer status, and the record is created if a lost
        initial put left it absent. Store failures are logged and swallowed: a
        persistence outage must never crash an in-flight solve.
        """
        if self._store is None:
            return
        task.revision += 1
        try:
            record = self._serialize_task(task)
            upsert = getattr(self._store, "upsert", None)
            if upsert is not None:
                await upsert(task.id, record)
            else:
                # Legacy store: merge what we have; may no-op if the record is
                # absent (the old interface has no create-or-update primitive).
                await self._store.update(task.id, **record)
        except Exception:
            log.exception("Failed to persist task %s", task.id)

    def _persist_fire_and_forget(self, task: Task) -> None:
        """Schedule a revision-ordered upsert (used from sync ``create_task``)."""
        if self._store is None:
            return
        fut = asyncio.create_task(self._persist_task(task))
        self._pending_persist.add(fut)
        fut.add_done_callback(self._pending_persist.discard)

    def _delete_fire_and_forget(self, task_id: str) -> None:
        """Schedule a store.delete without awaiting (used from sync cleanup)."""
        if self._store is None:
            return
        fut = asyncio.create_task(self._safe_delete(task_id))
        self._pending_persist.add(fut)
        fut.add_done_callback(self._pending_persist.discard)

    async def _safe_delete(self, task_id: str) -> None:
        """Delete a task record, logging (not raising) on a store outage."""
        if self._store is None:
            return
        try:
            await self._store.delete(task_id)
        except Exception:
            log.exception("Failed to delete task %s from the store", task_id)

    async def _drain_pending_writes(self, timeout: float = 5.0) -> None:
        """Await in-flight fire-and-forget store writes (bounded by ``timeout``)."""
        pending = {f for f in self._pending_persist if not f.done()}
        if not pending:
            return
        _done, still_pending = await asyncio.wait(pending, timeout=timeout)
        for fut in still_pending:
            fut.cancel()
        self._pending_persist.difference_update(pending)


task_manager = TaskManager()
