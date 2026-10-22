"""The one place a real (downstream-verified) solve outcome is recorded.

A "real outcome" is the ground truth that a minted token was actually accepted
(or rejected) by the target service — as opposed to the optimistic "we obtained
a token" signal the solver records at mint time. It arrives two ways:

* the caller reports it over HTTP (``/reportCorrect`` / ``/reportIncorrect``), or
* the solver verifies the token itself against the provider's siteverify
  endpoint and closes the loop inline (``token_verify`` path).

Both used to carry their own copy of the same four-sink fan-out — proxy health,
the per-sitekey *real* bucket, success accounting and session reputation — so a
change to the fan-out had to land in two places and could drift. This module
owns it once; both callers reduce to "authenticate / identify, then delegate".

Every sink is guarded independently: a missing pool, an absent record or a
transient backend error can never turn outcome feedback into an HTTP 500 or
fail a live solve.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Optional, Protocol, runtime_checkable

log = logging.getLogger(__name__)


def _accepts_stage(report: Any) -> bool:
    """True when a proxy pool's ``report`` takes the ``stage`` keyword.

    Wave A added ``stage`` (default ``"transport"``) to both pool backends.
    Duck-typed fakes / alternative pools that predate it must keep working, so
    the keyword is only sent when the signature can take it (or takes
    ``**kwargs``).
    """
    try:
        parameters = inspect.signature(report).parameters
    except (TypeError, ValueError):  # builtin / opaque callable
        return False
    if "stage" in parameters:
        return True
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )


@runtime_checkable
class OutcomeIdentity(Protocol):
    """The egress/session identity a real outcome is attributed to.

    Duck-typed so both a live :class:`~src.services.egress.SolveIdentity` (a
    *frozen* dataclass) and a stored
    :class:`~src.consumption.ledger.SolveRecord` (a mutable one) satisfy it —
    the only fields the fan-out needs are the proxy, the session and the proxy
    kind. Declared as read-only properties so the frozen identity qualifies (a
    plain attribute annotation would demand a writable field).
    """

    @property
    def proxy_id(self) -> Optional[str]: ...

    @property
    def session_id(self) -> Optional[str]: ...

    @property
    def proxy_kind(self) -> Optional[str]: ...


async def record_real_outcome(
    services: Any,
    identity: OutcomeIdentity,
    sitekey: str,
    *,
    success: bool,
    model: Optional[str] = None,
) -> None:
    """Fan a real outcome out to every learner that should update from it.

    * **proxy health** — an accepted token reinforces the pool proxy that
      produced it. A rejection is filed with ``stage="outcome"``: it is a
      downstream/solve signal, *not* a transport failure, so it is logged
      without pushing a working proxy toward cooldown. Pool proxies only
      (``identity.proxy_id`` set); caller task proxies are the caller's
      concern.
    * **per-sitekey real bucket** — the ``report_sitekey_real`` ranking signal,
      kept separate from the solver's token-obtained bucket so checkout ranking
      can prefer ground truth when present.
    * **success accounting** — recorded even for an empty sitekey.
    * **session reputation** — a nudge on the warm session that solved it
      (no-op if the session was already retired).
    """
    proxy_id = getattr(identity, "proxy_id", None)
    session_id = getattr(identity, "session_id", None)
    proxy_kind = getattr(identity, "proxy_kind", None)

    proxy_pool = getattr(services, "proxy_pool", None)
    if proxy_id and proxy_pool is not None:
        # A real downstream verdict is an *outcome* signal, not a transport
        # failure: a rejected token must not cool a proxy down as if its
        # connection had failed (the pool ignores non-transport failures).
        report = getattr(proxy_pool, "report", None)
        if report is not None:
            kwargs: dict[str, Any] = {"success": success}
            if _accepts_stage(report):
                kwargs["stage"] = "outcome"
            try:
                await report(proxy_id, **kwargs)
            except Exception:  # noqa: BLE001 - feedback must never 500 / fail a solve
                log.debug("proxy_pool.report (real outcome) failed", exc_info=True)
        if sitekey:
            try:
                await proxy_pool.report_sitekey_real(
                    proxy_id, sitekey, success=success
                )
            except Exception:  # noqa: BLE001
                log.debug("proxy_pool.report_sitekey_real failed", exc_info=True)

    accounting = getattr(services, "accounting", None)
    if accounting is not None:
        try:
            await accounting.record_real_outcome(
                sitekey, success=success, proxy_kind=proxy_kind, model=model
            )
        except Exception:  # noqa: BLE001
            log.debug("accounting.record_real_outcome failed", exc_info=True)

    session_pool = getattr(services, "session_pool", None)
    if session_id and session_pool is not None:
        report_outcome = getattr(session_pool, "report_outcome", None)
        if report_outcome is not None:
            try:
                await report_outcome(session_id, success=success)
            except Exception:  # noqa: BLE001
                log.debug("session_pool.report_outcome failed", exc_info=True)
