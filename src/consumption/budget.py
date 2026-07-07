"""Budget enforcement over the cost ledger."""

from __future__ import annotations

from dataclasses import dataclass

from src.consumption.ledger import CostLedger


@dataclass
class BudgetDecision:
    allowed: bool
    reason: str = ""
    downgrade_to: str | None = None   # e.g. suggest "local" when cloud budget exhausted


class BudgetGuard:
    """Checks estimated spend against optional global / per-client caps."""

    def __init__(
        self,
        ledger: CostLedger,
        *,
        global_cap_usd: float | None = None,
        per_client_cap_usd: float | None = None,
    ) -> None:
        self._ledger = ledger
        self._global_cap_usd = global_cap_usd
        self._per_client_cap_usd = per_client_cap_usd

    async def check(
        self,
        client_key: str | None,
        est_cost_usd: float,
        *,
        model: str | None = None,
    ) -> BudgetDecision:
        # Free operations (e.g. local models) never breach a cap.
        if est_cost_usd <= 0:
            return BudgetDecision(allowed=True)

        if self._global_cap_usd is not None:
            spent = await self._ledger.total_cost_usd()
            if spent + est_cost_usd > self._global_cap_usd:
                return self._deny(
                    "global budget cap {:.4f} USD would be exceeded "
                    "(spent {:.4f}, requested {:.4f})".format(
                        self._global_cap_usd, spent, est_cost_usd
                    ),
                    model,
                )

        if self._per_client_cap_usd is not None and client_key is not None:
            client_spent = await self._ledger.total_cost_usd(client_key)
            if client_spent + est_cost_usd > self._per_client_cap_usd:
                return self._deny(
                    "per-client budget cap {:.4f} USD would be exceeded for "
                    "'{}' (spent {:.4f}, requested {:.4f})".format(
                        self._per_client_cap_usd,
                        client_key,
                        client_spent,
                        est_cost_usd,
                    ),
                    model,
                )

        return BudgetDecision(allowed=True)

    @staticmethod
    def _deny(reason: str, model: str | None) -> BudgetDecision:
        # A paid (cloud) model can be downgraded to a free local one.
        downgrade = "local" if model != "local" else None
        return BudgetDecision(allowed=False, reason=reason, downgrade_to=downgrade)
