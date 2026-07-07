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

        # Limits + pools. Populated with env defaults now; overridden by
        # ``configure`` once the app Config is available.
        self._browser_concurrency = int(os.environ.get("BROWSER_CONCURRENCY", "4"))
        self._vision_concurrency = int(os.environ.get("VISION_CONCURRENCY", "8"))
        self._queue_max_size = int(os.environ.get("QUEUE_MAX_SIZE", "128"))
        self._solve_timeout = int(
            os.environ.get("CAPTCHA_SOLVE_TIMEOUT", os.environ.get("SOLVE_TIMEOUT", "180"))
        )
        self._browser_sem = asyncio.Semaphore(self._browser_concurrency)
        self._vision_sem = asyncio.Semaphore(self._vision_concurrency)

    # ── configuration ──────────────────────────────────────────

    def configure(self, config: Any) -> None:
        """Apply runtime limits from the application Config."""
        self._browser_concurrency = config.browser_concurrency
        self._vision_concurrency = config.vision_concurrency
        self._queue_max_size = config.queue_max_size
        self._solve_timeout = config.solve_timeout
        self._browser_sem = asyncio.Semaphore(self._browser_concurrency)
        self._vision_sem = asyncio.Semaphore(self._vision_concurrency)

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

        runner = asyncio.create_task(self._process_task(task))
        self._running[task_id] = runner
        runner.add_done_callback(lambda _t, tid=task_id: self._running.pop(tid, None))
        return task_id

    def get_task(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    def supported_types(self) -> list[str]:
        return list(self._solvers.keys())

    def _inflight_count(self) -> int:
        return sum(
            1 for t in self._tasks.values() if t.status == TaskStatus.PROCESSING
        )

    # ── execution ──────────────────────────────────────────────

    def _semaphore_for(self, task_type: str) -> asyncio.Semaphore:
        category = self._categories.get(task_type, TaskCategory.BROWSER)
        if category == TaskCategory.VISION:
            return self._vision_sem
        return self._browser_sem

    async def _process_task(self, task: Task) -> None:
        solver = self._solvers.get(task.type)
        if not solver:
            task.status = TaskStatus.FAILED
            task.error_code = "ERROR_TASK_NOT_SUPPORTED"
            task.error_description = f"Task type '{task.type}' is not supported"
            return

        # Make the task id available to the solver (for consumption records)
        # without changing the public solve() signature.
        task.params.setdefault("_taskId", task.id)

        semaphore = self._semaphore_for(task.type)
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


task_manager = TaskManager()
