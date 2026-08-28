"""Composition root for shared asset, consumption, and vision services.

``SolverServices`` is the owner of process-wide clients, pools, and ledgers.
Production startup goes through :meth:`SolverServices.create`, which makes
construction transactional: if a late backend or inventory step fails, every
resource acquired earlier is still reachable and is closed before the original
exception is re-raised.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
from typing import Any, Optional

from ..assets.atomic_proxy_pool import (
    build_atomic_proxy_pool as build_proxy_pool,
    snapshot_proxy_pool,
)
from ..assets.inventory import inventory_proxy_id
from ..assets.model_pool import ModelPool
from ..assets.proxy_pool import proxy_from_params
from ..assets.session_pool import SessionPool
from ..consumption.accounting import build_accounting
from ..consumption.budget import BudgetGuard
from ..consumption.ledger import build_ledger
from ..consumption.token_verify import build_token_verifier
from ..parsing.vision import VisionRouter
from .config import Config

log = logging.getLogger(__name__)


async def _close_owned_resource(name: str, resource: Any) -> None:
    """Best-effort close for resources exposing ``aclose`` or ``close``.

    One backend failure must not prevent later resources from being drained.
    """

    if resource is None:
        return
    closer = getattr(resource, "aclose", None)
    if closer is None:
        closer = getattr(resource, "close", None)
    if closer is None:
        return
    try:
        result = closer()
        if inspect.isawaitable(result):
            await result
    except Exception:  # noqa: BLE001 - shutdown must drain the full graph
        log.exception("Failed to close owned resource %s", name)


class SolverServices:
    """Container and lifecycle owner for cross-cutting solver services."""

    def __init__(self, config: Config) -> None:
        """Build synchronously for tests and compatibility.

        Application startup should use :meth:`create` so a constructor failure
        can be followed by asynchronous cleanup of already-created resources.
        """

        self._initialize_state(config)
        self._build_components()
        self._load_proxy_inventory()

    def _initialize_state(self, config: Config) -> None:
        """Install close-safe defaults before constructing any component."""

        self.config = config
        self.ledger: Any = None
        self.budget: Any = None
        self.accounting: Any = None
        self.token_verifier: Any = None
        self.model_pool: Any = None
        self.vision_router: Any = None
        self.proxy_pool: Any = None
        self.session_pool: Optional[SessionPool] = None
        # BrowserManager is attached but owned/stopped by main.lifespan.
        self.browser_manager: Any = None
        self._close_task: Optional[asyncio.Task[None]] = None

    def _build_components(self) -> None:
        """Construct the process-wide dependency graph in ownership order."""

        self.ledger = build_ledger(self.config)
        self.budget = BudgetGuard(
            self.ledger,
            global_cap_usd=self.config.budget_global_cap_usd,
            per_client_cap_usd=self.config.budget_per_client_cap_usd,
        )
        self.accounting = build_accounting(self.config)
        self.token_verifier = build_token_verifier(self.config)

        self.model_pool = ModelPool(self.config)
        self.vision_router = VisionRouter(
            self.model_pool,
            self.config,
            ledger=self.ledger,
            budget=self.budget,
            accounting=self.accounting,
        )

        self.proxy_pool = build_proxy_pool(self.config)

    @classmethod
    async def create(cls, config: Config) -> "SolverServices":
        """Build the graph and close partial ownership when startup fails."""

        services = cls.__new__(cls)
        services._initialize_state(config)
        try:
            services._build_components()
            services._load_proxy_inventory()
        except BaseException:
            await services.close()
            raise
        return services

    def attach_browser(self, manager: Any) -> None:
        """Wire the warm-session pool once the browser manager is available."""

        self.browser_manager = manager
        if self.config.session_pool_size <= 0:
            self.session_pool = None
            return
        self.session_pool = SessionPool(
            manager.context_factory,
            size=self.config.session_pool_size,
            max_solves=self.config.session_max_solves,
        )

    async def prewarm_sessions(self) -> int:
        """Pre-create proxyless browser sessions when enabled."""

        if self.session_pool is None or not self.config.session_prewarm:
            return 0
        count = await self.session_pool.prewarm()
        if count:
            log.info("Prewarmed %d proxyless browser sessions", count)
        return count

    async def proxy_snapshot(self) -> list[dict[str, Any]]:
        """Return the proxy inventory without blocking the application loop."""

        return await snapshot_proxy_pool(self.proxy_pool)

    def _load_proxy_inventory(self) -> None:
        """Seed durable proxy inventory from ``PROXY_POOL`` idempotently.

        Configured proxies receive deterministic ids. Existing backend rows are
        preserved so health, cooldown, bandwidth, and sitekey history survive
        restarts; duplicate entries in one boot are collapsed.
        """

        raw = os.environ.get("PROXY_POOL", "").strip()
        if not raw or self.proxy_pool is None:
            return

        existing_ids: set[str] = set()
        snapshot = getattr(self.proxy_pool, "snapshot", None)
        if snapshot is not None:
            try:
                existing_ids = {
                    str(row["id"])
                    for row in snapshot()
                    if isinstance(row, dict) and row.get("id")
                }
            except Exception:  # noqa: BLE001 - add() remains fail-loud
                log.exception("Could not inspect existing proxy inventory")

        entries = [
            entry.strip()
            for entry in raw.replace(",", "\n").splitlines()
            if entry.strip()
        ]
        seen: set[str] = set()
        loaded = 0
        reused = 0
        duplicates = 0

        for entry in entries:
            asset = proxy_from_params({"proxy": entry})
            if asset is None:
                continue
            asset.id = inventory_proxy_id(asset)
            if asset.id in seen:
                duplicates += 1
                continue
            seen.add(asset.id)
            if asset.id in existing_ids:
                reused += 1
                continue
            self.proxy_pool.add(asset)
            existing_ids.add(asset.id)
            loaded += 1

        if loaded or reused or duplicates:
            log.info(
                "Proxy inventory reconciled "
                "(added=%d, preserved=%d, duplicates=%d)",
                loaded,
                reused,
                duplicates,
            )

    async def _close_resources(self) -> None:
        """Drain the complete owned-resource graph in dependency order."""

        resources = (
            ("session_pool", getattr(self, "session_pool", None)),
            ("token_verifier", getattr(self, "token_verifier", None)),
            ("model_pool", getattr(self, "model_pool", None)),
            ("accounting", getattr(self, "accounting", None)),
            ("proxy_pool", getattr(self, "proxy_pool", None)),
            ("ledger", getattr(self, "ledger", None)),
        )
        for name, resource in resources:
            await _close_owned_resource(name, resource)

    async def close(self) -> None:
        """Close exactly once and finish cleanup despite caller cancellation.

        Concurrent callers share one close task. A completed task is consumed
        directly so repeated calls from a fresh event loop are safe as well.
        """

        task = getattr(self, "_close_task", None)
        if task is not None and task.done():
            task.result()
            return
        if task is None:
            task = asyncio.create_task(
                self._close_resources(),
                name="captchai-services-close",
            )
            self._close_task = task

        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # Resource ownership must settle before cancellation propagates.
            await task
            raise


_services: Optional[SolverServices] = None


def set_services(services: Optional[SolverServices]) -> None:
    global _services
    _services = services


def get_services() -> Optional[SolverServices]:
    return _services
