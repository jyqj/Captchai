"""Application composition root for shared asset and consumption services.

One ``SolverServices`` instance owns process-wide clients, pools, and ledgers.
Keeping ownership here makes startup/shutdown deterministic and prevents each
solver from constructing duplicate network clients or asset inventories.
"""

from __future__ import annotations

import inspect
import logging
import os
from typing import Any, Optional

from ..assets.inventory import inventory_proxy_id
from ..assets.model_pool import ModelPool
from ..assets.proxy_pool import build_proxy_pool, proxy_from_params
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

    Shutdown must continue even when one backend is unavailable. The failure is
    logged with the resource name while later resources are still attempted.
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
    except Exception:  # noqa: BLE001 - shutdown should drain the full ownership graph
        log.exception("Failed to close owned resource %s", name)


class SolverServices:
    """Container and lifecycle owner for cross-cutting solver services."""

    def __init__(self, config: Config) -> None:
        self.config = config

        self.ledger = build_ledger(config)
        self.budget = BudgetGuard(
            self.ledger,
            global_cap_usd=config.budget_global_cap_usd,
            per_client_cap_usd=config.budget_per_client_cap_usd,
        )
        self.accounting = build_accounting(config)
        self.token_verifier = build_token_verifier(config)

        self.model_pool = ModelPool(config)
        self.vision_router = VisionRouter(
            self.model_pool,
            config,
            ledger=self.ledger,
            budget=self.budget,
            accounting=self.accounting,
        )

        self.proxy_pool = build_proxy_pool(config)
        self.session_pool: Optional[SessionPool] = None
        # BrowserManager is attached but owned/stopped by main.lifespan.
        self.browser_manager = None

        self._load_proxy_inventory()

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

    def _load_proxy_inventory(self) -> None:
        """Seed durable proxy inventory from ``PROXY_POOL``.

        Configured proxies receive deterministic ids. For a Redis-backed pool we
        inspect the existing snapshot once at startup and do not overwrite an
        already-known id, preserving health, bandwidth, and per-sitekey history
        across restarts. Duplicate entries in the environment are collapsed in
        the same pass.
        """
        raw = os.environ.get("PROXY_POOL", "").strip()
        if not raw:
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
            except Exception:  # noqa: BLE001 - add() will still fail loud if backend is down
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
                "Proxy inventory reconciled (added=%d, preserved=%d, duplicates=%d)",
                loaded,
                reused,
                duplicates,
            )

    async def close(self) -> None:
        """Close every resource owned by this composition root.

        Order drains browser contexts first, then outbound HTTP/model clients,
        and finally persistent state backends. Each resource is attempted even
        if an earlier close fails.
        """
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


_services: Optional[SolverServices] = None


def set_services(services: Optional[SolverServices]) -> None:
    global _services
    _services = services


def get_services() -> Optional[SolverServices]:
    return _services
