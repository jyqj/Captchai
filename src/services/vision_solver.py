"""Shared base for the free-form (pure-image) vision solvers.

``ClassificationSolver`` and ``CaptchaRecognizer`` are the two adapters over the
deep model-call seam (:class:`~src.parsing.model_call.ModelInvoker`). Each one
only supplies its own prompt corpus and output parsing; everything else — the
model client, tier→backend routing, the budget gate, connection-error backend
fallback and model-pool concurrency — is inherited from the seam.

Before this base existed the two solvers each built their own ``AsyncOpenAI``
client and their own retry loop, and — the bug that motivated the seam — their
spend never reached the cost ledger and was never checked against the budget
cap. Routing them through one invoker closes that metering gap: every solve now
appends a :class:`~src.consumption.ledger.SolveRecord` so pure-image spend is as
visible as browser-solve spend.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Optional

from ..parsing.model_call import ModelCallRequest, ModelCallResult, ModelInvoker

log = logging.getLogger(__name__)


def parse_json_object(text: str) -> dict[str, Any]:
    """Parse a strict-JSON model reply, tolerating a ```json fenced block.

    The single JSON parser both free-form vision planes share (each used to
    carry its own byte-identical copy). Raises ``ValueError`` when the reply
    isn't a JSON object.
    """
    match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    cleaned = match.group(1) if match else text.strip()
    data = json.loads(cleaned)
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object, got {type(data).__name__}")
    return data


class VisionSolveBase:
    """Wires a solver to the shared model-call seam + cost metering.

    Construct with a live :class:`~src.core.services.SolverServices` (production)
    so the ledger / budget / accounting / model pool are shared process-wide, or
    with explicit pieces / nothing (tests) — a bare ``VisionSolveBase(config)``
    builds its own model pool and simply skips metering (no ledger wired).
    """

    def __init__(
        self,
        config: Any,
        *,
        services: Any = None,
        model_pool: Any = None,
        ledger: Any = None,
        budget: Any = None,
        accounting: Any = None,
    ) -> None:
        self._config = config
        if services is not None:
            model_pool = model_pool or getattr(services, "model_pool", None)
            ledger = ledger if ledger is not None else getattr(services, "ledger", None)
            budget = budget if budget is not None else getattr(services, "budget", None)
            accounting = (
                accounting
                if accounting is not None
                else getattr(services, "accounting", None)
            )
        if model_pool is None:
            from ..assets.model_pool import ModelPool

            model_pool = ModelPool(config)
        self._invoker = ModelInvoker(model_pool, config, budget=budget)
        self._ledger = ledger
        self._accounting = accounting

    async def _invoke(
        self,
        req: ModelCallRequest,
        params: dict[str, Any],
        *,
        challenge_shape: str,
    ) -> ModelCallResult:
        """Run the model call with the solvers' retry budget, then meter it.

        Preserves the pre-seam retry count (``captcha_retries``): a transient
        failure retries the whole invoke (which itself does one connection-error
        backend fallback). Exactly one :class:`SolveRecord` is written per
        solve — on the successful call, or once after the final failed attempt.
        """
        retries = max(1, int(getattr(self._config, "captcha_retries", 3)))
        client_key = params.get("_clientKey")
        started = time.monotonic()
        last_error: Optional[Exception] = None
        for attempt in range(retries):
            try:
                result = await self._invoker.invoke(req, client_key=client_key)
                await self._meter(params, result, "ready", started, challenge_shape)
                return result
            except Exception as exc:  # noqa: BLE001 - retry then surface below
                last_error = exc
                log.warning(
                    "%s attempt %d/%d failed: %s",
                    type(self).__name__,
                    attempt + 1,
                    retries,
                    exc,
                )
        await self._meter(params, None, "failed", started, challenge_shape)
        raise RuntimeError(
            f"{type(self).__name__} failed after {retries} attempts: {last_error}"
        )

    async def _meter(
        self,
        params: dict[str, Any],
        result: Optional[ModelCallResult],
        outcome: str,
        started: float,
        challenge_shape: str,
    ) -> None:
        """Append one SolveRecord so pure-image spend reaches the cost ledger.

        No-op when no ledger is wired (bare-config construction in tests).
        Accounting is intentionally *not* touched — its per-sitekey buckets
        drive proxy/session routing for browser solves and carry no signal for
        image tasks (which have no sitekey / egress). Failures are swallowed so
        metering never fails a solve.
        """
        if self._ledger is None:
            return
        from ..consumption.ledger import SolveRecord, estimate_cost

        model = result.model if result is not None else None
        in_tok = result.usage.input_tokens if result is not None else 0
        out_tok = result.usage.output_tokens if result is not None else 0
        wall_ms = int((time.monotonic() - started) * 1000)
        try:
            await self._ledger.record(
                SolveRecord(
                    task_id=str(params.get("_taskId") or ""),
                    sitekey=str(params.get("websiteKey") or ""),
                    task_type=str(params.get("type") or "unknown"),
                    model=model,
                    challenge_shape=challenge_shape,
                    vision_calls=1 if result is not None else 0,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    wall_ms=wall_ms,
                    vision_ms=wall_ms,
                    outcome=outcome,
                    est_cost_usd=estimate_cost(model or "local", in_tok, out_tok),
                    client_key=params.get("_clientKey"),
                )
            )
        except Exception as exc:  # noqa: BLE001 - metering never fails a solve
            log.debug("vision ledger record failed: %s", exc)
