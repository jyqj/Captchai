"""Async task manager: bounded admission, split concurrency, TTL + cancellation.

Runtime plane of the service. Key properties (vs. the original single-semaphore,
fire-and-forget dict):

* **Split concurrency.** Browser solves and pure-vision (classification /
  recognition) calls draw from *separate* semaphores, so a burst of image tasks
  can never starve the browser solvers.
* **Bounded admission / backpressure.** In-flight tasks are capped; when the cap
  is reached ``create_task`` raises :class:`QueueFull` and the API returns
  ``ERROR_NO_SLOT_AVAILABLE`` instead of spawning unbounded coroutines/contexts.
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
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Optional, Protocol

from ..orchestration.store import TaskStore, build_store
from .browser_solver import has_task_proxy, initial_proxy_kind

log = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
        # Idempotency key -> task_id.
        self._idempotency: dict[str, str] = {}

        # Persistent task store (Redis when configured, in-memory otherwise).
        # Built in ``configure``; ``None`` until then. The in-memory ``_tasks``
        # dict remains the authoritative mutable copy — the store mirrors it for
        # restart survival and cross-process visibility.
        self._store: TaskStore | None = None
        # Proxy pool reference for best-effort peek routing in ``_semaphore_for``.
        # Set in ``configure``; ``None`` until then (and in tests that don't wire it).
        self._proxy_pool: Any = None
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

    # ── configuration ──────────────────────────────────────────

    def configure(
        self,
        config: Any,
        store: TaskStore | None = None,
        *,
        proxy_pool: Any = None,
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
        """
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

    # ── admission ──────────────────────────────────────────────

    def create_task(
        self,
        task_type: str,
        params: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> str:
        self._cleanup_expired()

        # Coalesce retries carrying the same idempotency key onto one task.
        if idempotency_key:
            existing_id = self._idempotency.get(idempotency_key)
            if existing_id and existing_id in self._tasks:
                return existing_id

        if self._queue_max_size and self._inflight_count() >= self._queue_max_size:
            raise QueueFull(
                f"in-flight tasks at capacity ({self._queue_max_size})"
            )

        task_id = str(uuid.uuid4())
        task = Task(
            id=task_id,
            type=task_type,
            params=params,
            idempotency_key=idempotency_key,
        )
        self._tasks[task_id] = task
        if idempotency_key:
            self._idempotency[idempotency_key] = task_id

        # Best-effort persistence. ``create_task`` is sync (the API route does
        # not await it), so we can't await the store here; fire-and-forget.
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

    # ── execution ──────────────────────────────────────────────

    def _semaphore_for(self, task: Task) -> asyncio.Semaphore:
        category = self._categories.get(task.type, TaskCategory.BROWSER)
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
            await self._persist_task(task)
            return

        # Make the task id available to the solver (for consumption records)
        # without changing the public solve() signature.
        task.params.setdefault("_taskId", task.id)
        task.params.setdefault("_proxyKind", initial_proxy_kind(task.params).value)

        semaphore = self._semaphore_for(task)
        try:
            async with semaphore:
                solution = await asyncio.wait_for(
                    solver.solve(task.params), timeout=self._solve_timeout
                )
            task.solution = solution
            task.status = TaskStatus.READY
            log.info("Task %s completed successfully", task.id)
        except asyncio.TimeoutError:
            task.status = TaskStatus.FAILED
            task.error_code = "ERROR_CAPTCHA_TIMEOUT"
            task.error_description = f"Solve exceeded {self._solve_timeout}s budget"
            log.error("Task %s timed out", task.id)
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
        """Cancel every in-flight task (used on app shutdown)."""
        runners = [r for r in self._running.values() if not r.done()]
        for r in runners:
            r.cancel()
        for r in runners:
            try:
                await r
            except (asyncio.CancelledError, Exception):
                pass
        if self._store is not None:
            try:
                await self._store.close()
            except Exception:
                log.exception("Failed to close task store")

    # ── store helpers ───────────────────────────────────────────

    def _serialize_task(self, task: Task) -> dict[str, Any]:
        """Convert a Task to a JSON-safe dict for the persistent store."""
        return {
            "id": task.id,
            "type": task.type,
            "params": task.params,
            "status": task.status.value,
            "solution": task.solution,
            "error_code": task.error_code,
            "error_description": task.error_description,
            "created_at": task.created_at.isoformat(),
            "idempotency_key": task.idempotency_key,
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
        return Task(
            id=record.get("id", ""),
            type=record.get("type", ""),
            params=record.get("params") or {},
            status=status_enum,
            solution=record.get("solution"),
            error_code=record.get("error_code"),
            error_description=record.get("error_description"),
            created_at=created_at,
            idempotency_key=record.get("idempotency_key"),
        )

    async def _persist_task(self, task: Task) -> None:
        """Await-able mirror of the current status/solution into the store."""
        if self._store is None:
            return
        await self._store.update(
            task.id,
            status=task.status.value,
            solution=task.solution,
            error_code=task.error_code,
            error_description=task.error_description,
        )

    def _persist_fire_and_forget(self, task: Task) -> None:
        """Schedule a store.put without awaiting (used from sync ``create_task``)."""
        if self._store is None:
            return
        fut = asyncio.create_task(self._store.put(task.id, self._serialize_task(task)))
        self._pending_persist.add(fut)
        fut.add_done_callback(self._pending_persist.discard)

    def _delete_fire_and_forget(self, task_id: str) -> None:
        """Schedule a store.delete without awaiting (used from sync cleanup)."""
        if self._store is None:
            return
        fut = asyncio.create_task(self._store.delete(task_id))
        self._pending_persist.add(fut)
        fut.add_done_callback(self._pending_persist.discard)


task_manager = TaskManager()
