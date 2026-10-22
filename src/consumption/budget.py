"""Budget enforcement over the cost ledger."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from src.consumption.ledger import CostLedger


@dataclass
class BudgetDecision:
    allowed: bool
    reason: str = ""
    downgrade_to: str | None = None   # e.g. suggest "local" when cloud budget exhausted


@dataclass
class Reservation:
    """A budget hold returned by :meth:`BudgetGuard.reserve`.

    ``allowed`` is ``False`` when the cap would be exceeded; a denied
    reservation holds no in-flight budget and :meth:`BudgetGuard.settle` is a
    no-op for it. ``settled`` guards against double-settling (a retry or an
    exception path that settles twice), so a hold is released exactly once.
    """

    allowed: bool
    client_key: str | None
    est_cost_usd: float
    reason: str = ""
    downgrade_to: str | None = None
    settled: bool = False


class BudgetGuard:
    """Checks estimated spend against optional global / per-client caps.

    Two entry points share one atomic accounting model:

    * :meth:`check` is the read-only advisory gate used by callers that make a
      single call and don't need a hold. It never mutates budget state.
    * :meth:`reserve` + :meth:`settle` is the race-free path for paid calls.
      ``reserve`` checks the cap against ``spent + in-flight`` and, when
      admitted, records an in-flight hold under a lock; ``settle`` releases the
      hold and books the actual cost. Without the hold, N concurrent requests
      would all observe the same headroom and over-admit.

    A multi-call flow (a solve that makes several model calls) can hold ONE
    reservation across all of its calls — reserve the estimated total up
    front, then settle once with the summed actual cost. Reservations are not
    tied to a single model call.
    """

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
        # Serialises cap-check + in-flight-add so concurrent reserves can't
        # all observe the same headroom (check-then-act race).
        self._lock = asyncio.Lock()
        self._inflight_global: float = 0.0
        self._inflight_by_client: dict[str, float] = {}
        # Actual cost of settled reservations. The ledger stays the source of
        # truth for recorded spend; these monotonic floors cover the window
        # between settling and the matching SolveRecord landing in the ledger
        # (and reservation flows that never write a ledger record). Taking the
        # max of the two avoids double counting once the record is written.
        self._settled_global: float = 0.0
        self._settled_by_client: dict[str, float] = {}

    async def check(
        self,
        client_key: str | None,
        est_cost_usd: float,
        *,
        model: str | None = None,
    ) -> BudgetDecision:
        """Advisory check against ``spent + in-flight`` (no hold is taken).

        Read-only callers keep using this exactly as before; callers that will
        actually spend should use :meth:`reserve` so concurrent requests can't
        both slip through the same headroom.
        """
        # Free operations (e.g. local models) never breach a cap.
        if est_cost_usd <= 0:
            return BudgetDecision(allowed=True)

        async with self._lock:
            return await self._decide(client_key, est_cost_usd, model)

    async def reserve(
        self,
        client_key: str | None,
        est_cost_usd: float,
        *,
        model: str | None = None,
    ) -> Reservation:
        """Atomically admit and hold ``est_cost_usd`` of budget.

        Under the guard lock: check the global / per-client caps against
        ``spent + in-flight``, then add the estimate to the in-flight total and
        return a :class:`Reservation` token. A denied reservation carries the
        cap reason and ``downgrade_to`` (a paid cloud call can fall back to the
        free local backend) and holds no budget.

        Hold one reservation across a multi-call flow (reserve the expected
        total once, settle once at the end) — the hold is not per model call.
        """
        if est_cost_usd <= 0:
            # Free operations need no hold; settle on it is a no-op.
            return Reservation(
                allowed=True, client_key=client_key, est_cost_usd=0.0
            )

        async with self._lock:
            decision = await self._decide(client_key, est_cost_usd, model)
            if not decision.allowed:
                return Reservation(
                    allowed=False,
                    client_key=client_key,
                    est_cost_usd=est_cost_usd,
                    reason=decision.reason,
                    downgrade_to=decision.downgrade_to,
                )
            self._inflight_global += est_cost_usd
            if client_key is not None:
                self._inflight_by_client[client_key] = (
                    self._inflight_by_client.get(client_key, 0.0) + est_cost_usd
                )
            return Reservation(
                allowed=True, client_key=client_key, est_cost_usd=est_cost_usd
            )

    async def settle(
        self, reservation: Reservation, actual_cost_usd: float
    ) -> None:
        """Release a reservation's hold and book its actual cost.

        Moves the estimate out of in-flight and adds ``actual_cost_usd`` to the
        settled floor used by subsequent cap checks (the actual may differ from
        the estimate — that's the point of settling with the real cost).
        Idempotent: settling the same reservation twice (or a denied one) is a
        no-op.
        """
        if not reservation.allowed or reservation.settled:
            return
        actual = max(0.0, float(actual_cost_usd))
        async with self._lock:
            if reservation.settled:
                return
            reservation.settled = True
            self._release_inflight(reservation)
            self._settled_global += actual
            if reservation.client_key is not None:
                self._settled_by_client[reservation.client_key] = (
                    self._settled_by_client.get(reservation.client_key, 0.0)
                    + actual
                )

    def _release_inflight(self, reservation: Reservation) -> None:
        """Remove a reservation's estimate from the in-flight totals (lock held)."""
        est = reservation.est_cost_usd
        self._inflight_global = max(0.0, self._inflight_global - est)
        if reservation.client_key is not None:
            remaining = (
                self._inflight_by_client.get(reservation.client_key, 0.0) - est
            )
            if remaining <= 1e-12:
                self._inflight_by_client.pop(reservation.client_key, None)
            else:
                self._inflight_by_client[reservation.client_key] = remaining

    async def _decide(
        self, client_key: str | None, est_cost_usd: float, model: str | None
    ) -> BudgetDecision:
        """Cap check against lifetime spend + in-flight holds (lock held)."""
        if self._global_cap_usd is not None:
            spent = max(
                await self._ledger.total_cost_usd(), self._settled_global
            )
            committed = spent + self._inflight_global
            if committed + est_cost_usd > self._global_cap_usd:
                return self._deny(
                    "global budget cap {:.4f} USD would be exceeded "
                    "(spent {:.4f}, in-flight {:.4f}, requested {:.4f})".format(
                        self._global_cap_usd,
                        spent,
                        self._inflight_global,
                        est_cost_usd,
                    ),
                    model,
                )

        if self._per_client_cap_usd is not None and client_key is not None:
            client_spent = max(
                await self._ledger.total_cost_usd(client_key),
                self._settled_by_client.get(client_key, 0.0),
            )
            client_inflight = self._inflight_by_client.get(client_key, 0.0)
            if client_spent + client_inflight + est_cost_usd > self._per_client_cap_usd:
                return self._deny(
                    "per-client budget cap {:.4f} USD would be exceeded for "
                    "'{}' (spent {:.4f}, in-flight {:.4f}, requested {:.4f})".format(
                        self._per_client_cap_usd,
                        client_key,
                        client_spent,
                        client_inflight,
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
