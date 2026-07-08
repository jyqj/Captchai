"""Composition root: build and hold the shared asset / consumption / vision layers.

Solvers no longer own their model client or build their own pools. Instead a
single :class:`SolverServices` is constructed at startup and injected into the
solvers, so the ledger, budget guard, success accounting, model/vision router,
proxy pool and warm session pool are shared process-wide and can be surfaced
by admin/metrics endpoints.
"""

from __future__ import annotations

import logging
from typing import Optional

from ..assets.model_pool import ModelPool
from ..assets.proxy_pool import build_proxy_pool, proxy_from_params
from ..assets.session_pool import SessionPool
from ..consumption.accounting import build_accounting
from ..consumption.budget import BudgetGuard
from ..consumption.ledger import build_ledger
from ..parsing.vision import VisionRouter
from .config import Config

log = logging.getLogger(__name__)


class SolverServices:
    """Container for every shared cross-cutting service."""

    def __init__(self, config: Config) -> None:
        self.config = config

        # Consumption plane. Each backend uses build_* so a Redis backend is
        # selected when REDIS_URL is set — otherwise a restart zeroes spend,
        # routing stats, and proxy health. The in-memory path is byte-for-byte
        # equivalent to the pre-WP7 behavior (default when REDIS_URL unset).
        self.ledger = build_ledger(config)
        self.budget = BudgetGuard(
            self.ledger,
            global_cap_usd=config.budget_global_cap_usd,
            per_client_cap_usd=config.budget_per_client_cap_usd,
        )
        self.accounting = build_accounting(config)

        # Parsing / model plane.
        self.model_pool = ModelPool(config)
        self.vision_router = VisionRouter(
            self.model_pool, config, ledger=self.ledger, budget=self.budget,
            accounting=self.accounting,
        )

        # Asset plane.
        self.proxy_pool = build_proxy_pool(config)
        self.session_pool: Optional[SessionPool] = None

        self._load_proxy_inventory()

    def attach_browser(self, manager) -> None:
        """Wire the warm session pool once the browser manager is available."""
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

    def _load_proxy_inventory(self) -> None:
        """Seed the proxy pool from PROXY_POOL env (one proxy per line/comma)."""
        import os

        raw = os.environ.get("PROXY_POOL", "").strip()
        if not raw:
            return
        entries = [e.strip() for e in raw.replace(",", "\n").splitlines() if e.strip()]
        count = 0
        for entry in entries:
            asset = proxy_from_params({"proxy": entry})
            if asset is not None:
                self.proxy_pool.add(asset)
                count += 1
        if count:
            log.info("Loaded %d proxies into the proxy pool", count)

    async def close(self) -> None:
        if self.session_pool is not None:
            await self.session_pool.close_all()
        # Redis backends hold connection pools that must be released on
        # shutdown. The in-memory backends don't define ``close`` — the
        # getattr guard keeps this path a no-op for them.
        close_ledger = getattr(self.ledger, "close", None)
        if close_ledger is not None:
            await close_ledger()
        close_proxy = getattr(self.proxy_pool, "close", None)
        if close_proxy is not None:
            await close_proxy()
        close_acc = getattr(self.accounting, "close", None)
        if close_acc is not None:
            await close_acc()


# Process-wide singleton, set at startup so API routes (getBalance, admin) can
# read the shared ledger / pools without threading the object through every call.
_services: Optional[SolverServices] = None


def set_services(services: Optional[SolverServices]) -> None:
    global _services
    _services = services


def get_services() -> Optional[SolverServices]:
    return _services
