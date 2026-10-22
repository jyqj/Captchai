"""Composition root: build and hold the shared asset / consumption / vision layers.

Solvers no longer own their model client or build their own pools. Instead a
single :class:`SolverServices` is constructed at startup and injected into the
solvers, so the ledger, budget guard, success accounting, model/vision router,
proxy pool and warm session pool are shared process-wide and can be surfaced
by admin/metrics endpoints.

Construction is deliberately failure-tolerant: a subsystem that can't start
(e.g. ``REDIS_URL`` points at an unreachable Redis, or the optional ``redis``
package is missing) is logged, recorded in :attr:`SolverServices.degraded`, and
replaced by its in-memory fallback where that is meaningful. The app then still
serves the task types whose path survived, instead of failing the whole process
because one plane is down.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Optional

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


# ── In-memory fallbacks (used when a configured backend can't start) ────


def _in_memory_ledger() -> Any:
    from ..consumption.ledger import CostLedger

    return CostLedger()


def _in_memory_accounting() -> Any:
    from ..consumption.accounting import SuccessAccounting

    return SuccessAccounting()


def _in_memory_proxy_pool(config: Config) -> Any:
    from ..assets.proxy_pool import ProxyPool

    try:
        quota_gb = float(getattr(config, "proxy_max_gb", 0.0) or 0.0)
    except (TypeError, ValueError):
        quota_gb = 0.0
    return ProxyPool(
        cooldown_seconds=int(getattr(config, "proxy_cooldown", 120)),
        max_consecutive_fails=int(getattr(config, "proxy_max_consecutive_fails", 3)),
        max_bytes_per_proxy=int(quota_gb * 1024**3) if quota_gb > 0 else 0,
    )


class SolverServices:
    """Container for every shared cross-cutting service."""

    def __init__(self, config: Config) -> None:
        self.config = config
        # Subsystem name -> failure reason. Empty = fully healthy. The lifespan
        # logs this and mirrors it onto ``app.state`` so an operator can see a
        # degraded start without scraping tracebacks.
        self.degraded: dict[str, str] = {}

        # Consumption plane. Each backend uses build_* so a Redis backend is
        # selected when REDIS_URL is set — otherwise a restart zeroes spend,
        # routing stats, and proxy health. The in-memory path is byte-for-byte
        # equivalent to the pre-WP7 behavior (default when REDIS_URL unset).
        # A configured backend that can't be built degrades to in-memory: spend
        # keeps being metered within this process (only persistence is lost).
        self.ledger = self._build(
            "ledger", lambda: build_ledger(config), fallback=_in_memory_ledger
        )
        self.budget: Optional[BudgetGuard] = None
        if self.ledger is not None:
            self.budget = self._build(
                "budget",
                lambda: BudgetGuard(
                    self.ledger,
                    global_cap_usd=config.budget_global_cap_usd,
                    per_client_cap_usd=config.budget_per_client_cap_usd,
                ),
            )
        self.accounting = self._build(
            "accounting",
            lambda: build_accounting(config),
            fallback=_in_memory_accounting,
        )
        # Optional token-trust verification (off unless TOKEN_VERIFY_ENABLED +
        # secrets are configured). When present, the browser solvers verify a
        # minted token against the provider's siteverify endpoint and close the
        # real-outcome loop automatically instead of waiting on the caller's
        # /reportCorrect. ``None`` = the pre-existing caller-driven behaviour
        # (or a degraded verifier that failed to initialise).
        self.token_verifier = self._build(
            "token_verifier", lambda: build_token_verifier(config)
        )

        # Parsing / model plane.
        self.model_pool = self._build("model_pool", lambda: ModelPool(config))
        self.vision_router: Optional[VisionRouter] = None
        if self.model_pool is not None:
            self.vision_router = self._build(
                "vision_router",
                lambda: VisionRouter(
                    self.model_pool,
                    config,
                    ledger=self.ledger,
                    budget=self.budget,
                    accounting=self.accounting,
                ),
            )
        else:
            self.degraded.setdefault("vision_router", "model pool unavailable")

        # Asset plane.
        self.proxy_pool = self._build(
            "proxy_pool",
            lambda: build_proxy_pool(config),
            fallback=lambda: _in_memory_proxy_pool(config),
        )
        self.session_pool: Optional[SessionPool] = None
        # Set by ``attach_browser`` so admin/health can surface the actual
        # browser runtime (post-fallback) without reaching into module globals.
        self.browser_manager = None
        # Backends replaced during startup (e.g. a Redis pool that rejected the
        # inventory). They are closed at shutdown so their clients can't leak.
        self._discarded_pools: list[Any] = []

        self._load_proxy_inventory()

    # ── construction helpers ──────────────────────────────────

    def _build(self, name: str, factory, *, fallback=None) -> Any:
        """Build one subsystem, degrading instead of failing startup.

        The failure is logged with its traceback and recorded in
        :attr:`degraded`. When a meaningful in-memory equivalent exists it is
        returned instead (metering / rotation keep working; only cross-process
        durability is lost).
        """
        try:
            return factory()
        except Exception as exc:  # noqa: BLE001 - startup must survive one bad plane
            log.exception("Failed to initialise %s; running degraded", name)
            self.degraded[name] = str(exc) or exc.__class__.__name__
            if fallback is None:
                return None
            try:
                return fallback()
            except Exception as fallback_exc:  # noqa: BLE001
                log.exception("In-memory fallback for %s also failed", name)
                self.degraded[name] = (
                    f"{self.degraded[name]}; fallback failed: {fallback_exc}"
                )
                return None

    def attach_browser(self, manager) -> None:
        """Wire the warm session pool once the browser manager is available."""
        self.browser_manager = manager
        # A failed browser start passes ``None`` here; the solvers are not
        # registered for browser task types in that case (see lifespan).
        if manager is None or self.config.session_pool_size <= 0:
            self.session_pool = None
            return
        # Stale-session hygiene: forward the configured TTLs only when this
        # SessionPool build accepts them (older/alternate pools keep their own
        # defaults). ``getattr`` fallbacks mirror the config defaults so this
        # stays correct if the pool is built from a partial config object.
        kwargs: dict[str, Any] = {
            "size": self.config.session_pool_size,
            "max_solves": self.config.session_max_solves,
        }
        try:
            pool_params = inspect.signature(SessionPool).parameters
        except (TypeError, ValueError):  # pragma: no cover - defensive
            pool_params = {}
        for param, attr, default in (
            ("idle_ttl_seconds", "session_idle_ttl_seconds", 300),
            ("max_age_seconds", "session_max_age_seconds", 1800),
        ):
            if param in pool_params:
                kwargs[param] = getattr(self.config, attr, default)
        try:
            self.session_pool = SessionPool(manager.context_factory, **kwargs)
        except Exception as exc:  # noqa: BLE001 - warm sessions are an optimisation
            log.exception("Failed to build the warm session pool; running degraded")
            self.degraded["session_pool"] = str(exc) or exc.__class__.__name__
            self.session_pool = None

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
        if not raw or self.proxy_pool is None:
            return
        entries = [e.strip() for e in raw.replace(",", "\n").splitlines() if e.strip()]
        assets = []
        for entry in entries:
            asset = proxy_from_params({"proxy": entry})
            if asset is not None:
                assets.append(asset)
        # Only replace the inventory when at least one entry parsed; an
        # all-unparseable PROXY_POOL must not prune a previously loaded pool.
        if not assets:
            return

        try:
            self._seed_proxies(self.proxy_pool, assets)
        except Exception as exc:  # noqa: BLE001 - e.g. Redis down at startup
            log.exception(
                "Configured proxy pool rejected the PROXY_POOL inventory; "
                "falling back to an in-memory pool"
            )
            self.degraded["proxy_pool"] = str(exc) or exc.__class__.__name__
            # Retain the rejected backend so shutdown releases its clients.
            self._discarded_pools.append(self.proxy_pool)
            fallback = _in_memory_proxy_pool(self.config)
            self.proxy_pool = fallback
            # Re-seed from scratch on the fallback backend.
            try:
                self._seed_proxies(fallback, assets)
            except Exception:  # noqa: BLE001 - never abort startup on a seed
                log.exception("Failed to seed proxies into the fallback pool")
            log.info("Loaded %d proxies into the fallback proxy pool", len(assets))
            return

        log.info("Loaded %d proxies into the proxy pool", len(assets))

    @staticmethod
    def _seed_proxies(pool: Any, assets: list[Any]) -> None:
        """Load the inventory in one batch when the pool supports it.

        ``add_all`` is the batch entry point: on the in-memory backend it also
        prunes ids that are no longer in the inventory, which per-proxy ``add``
        can't know about. Pools that only implement ``add`` still work.
        """
        add_all = getattr(pool, "add_all", None)
        if callable(add_all):
            add_all(assets)
            return
        for asset in assets:
            pool.add(asset)

    async def close(self) -> None:
        """Tear down every subsystem that started, in reverse startup order.

        Each step is individually guarded: a backend whose connection already
        dropped (or one that only partially initialised) must not prevent the
        remaining resources from being released.
        """
        await self._close(
            "session_pool", getattr(self, "session_pool", None), "close_all"
        )
        # Redis backends hold connection pools that must be released on
        # shutdown. The in-memory backends don't define ``close`` — the
        # getattr guard keeps this path a no-op for them.
        await self._close("proxy_pool", getattr(self, "proxy_pool", None))
        for discarded in getattr(self, "_discarded_pools", ()):
            await self._close("discarded proxy pool", discarded)
        # The model pool owns lazy SDK clients per backend; close them before
        # the ledger/accounting backends they report into. Absent on builds
        # where the pool failed to construct — guarded by the helper.
        await self._close("model_pool", getattr(self, "model_pool", None), "aclose")
        await self._close("accounting", getattr(self, "accounting", None))
        await self._close("ledger", getattr(self, "ledger", None))

    @staticmethod
    async def _close(name: str, obj: Any, method_name: str = "close") -> None:
        if obj is None:
            return
        closer = getattr(obj, method_name, None)
        if closer is None:
            return
        try:
            await closer()
        except Exception:  # noqa: BLE001 - shutdown attempts every closer
            log.exception("Failed to close %s during shutdown", name)


# Process-wide singleton, set at startup so API routes (getBalance, admin) can
# read the shared ledger / pools without threading the object through every call.
_services: Optional[SolverServices] = None


def set_services(services: Optional[SolverServices]) -> None:
    global _services
    _services = services


def get_services() -> Optional[SolverServices]:
    return _services
