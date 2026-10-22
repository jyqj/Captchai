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

import asyncio
import json
import logging
import re
import time
from typing import Any, Callable, Optional

from ..parsing.model_call import (
    ModelCallRequest,
    ModelCallResult,
    ModelInvoker,
    is_connection_error,
)

log = logging.getLogger(__name__)


def _is_transient_model_error(exc: BaseException) -> bool:
    """Classify a model-call / parse failure as retryable.

    Transient: connection/timeout failures, rate limits, 5xx responses and an
    unparseable model reply (a fresh sample usually fixes a malformed one).
    Permanent: bad requests, auth/permission errors, unsupported models — a
    retry would spend tokens on the same guaranteed failure.
    """
    if isinstance(exc, ValueError):
        # ``json.JSONDecodeError`` is a ValueError; so is a non-object reply.
        return True
    if is_connection_error(exc):
        return True
    for klass in type(exc).__mro__:
        if klass.__name__ in (
            "RateLimitError",
            "APIRateLimitError",
            "InternalServerError",
            "ServiceUnavailableError",
        ):
            return True
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and (status == 429 or status >= 500):
        return True
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int) and (status == 429 or status >= 500):
        return True
    return False


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
        parse: Optional[Callable[[ModelCallResult], Any]] = None,
    ) -> Any:
        """Run the model call with the solvers' retry budget, then meter it.

        Preserves the pre-seam retry count (``captcha_retries``). ``parse`` (the
        free-form solvers' JSON parsing/normalisation) runs INSIDE the retried
        and metered unit, so a malformed model reply is retried instead of
        surfacing after the retry budget was spent on transport successes.

        Every attempt books its own :class:`SolveRecord`: a successful attempt
        as ``"ready"``; a failed one as ``"failed"`` with the usage that attempt
        actually consumed (a reply that parsed badly was still billed by the
        provider). Metering failures never fail a solve.

        Retry classification: transient failures (connection / timeout / rate
        limit / 5xx / unparseable reply) back off with deterministic jitter;
        permanent request failures surface immediately.
        """
        retries = max(1, int(getattr(self._config, "captcha_retries", 3)))
        client_key = params.get("_clientKey")
        last_error: Optional[Exception] = None
        for attempt in range(retries):
            attempt_started = time.perf_counter()
            result: Optional[ModelCallResult] = None
            try:
                result = await self._invoker.invoke(req, client_key=client_key)
                parsed = parse(result) if parse is not None else result
                await self._meter(
                    params, result, "ready", attempt_started, challenge_shape
                )
                return parsed
            except Exception as exc:  # noqa: BLE001 - classified below
                last_error = exc
                # Book the attempt even when it failed: the invoke may have
                # consumed tokens (e.g. the reply was unparseable).
                await self._meter(
                    params, result, "failed", attempt_started, challenge_shape
                )
                log.warning(
                    "%s attempt %d/%d failed: %s",
                    type(self).__name__,
                    attempt + 1,
                    retries,
                    exc,
                )
                if not _is_transient_model_error(exc):
                    raise
                if attempt < retries - 1:
                    await self._sleep_retry(attempt)
        raise RuntimeError(
            f"{type(self).__name__} failed after {retries} attempts: {last_error}"
        )

    async def _sleep_retry(self, attempt: int) -> None:
        """Exponential backoff with deterministic jitter between attempts.

        No ``random`` import: the jitter is derived from the attempt index
        (``attempt * 0.37`` fractional part), which is enough to de-synchronise
        concurrent retries without making tests flaky. ``retry_backoff_base`` /
        ``retry_backoff_max`` are honoured (set them to 0 in tests).
        """
        base = float(getattr(self._config, "retry_backoff_base", 1.0))
        cap = float(getattr(self._config, "retry_backoff_max", 8.0))
        delay = min(cap, base * (2 ** attempt))
        delay += ((attempt * 0.37) % 1.0) * delay * 0.25
        if delay > 0:
            await asyncio.sleep(delay)

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

        # Concrete served model id (``ModelClient.model``) — what pricing keys
        # on. ``backend`` is the routing alias ("local"/"cloud") that actually
        # served the call; it is kept as the record's routing dimension AND
        # passed to pricing, where a local backend is authoritative and free
        # even if the configured local model id has no local marker.
        actual_model = result.model if result is not None else None
        backend = getattr(result, "backend", "") if result is not None else ""
        backend_kind = (backend or "").strip().lower() or None
        model = backend or actual_model
        in_tok = result.usage.input_tokens if result is not None else 0
        out_tok = result.usage.output_tokens if result is not None else 0
        wall_ms = int((time.perf_counter() - started) * 1000)
        try:
            rec = SolveRecord(
                task_id=str(params.get("_taskId") or ""),
                sitekey=str(params.get("websiteKey") or ""),
                task_type=str(params.get("type") or "unknown"),
                model=model,
                actual_model=actual_model,
                challenge_shape=challenge_shape,
                vision_calls=1 if result is not None else 0,
                input_tokens=in_tok,
                output_tokens=out_tok,
                wall_ms=wall_ms,
                vision_ms=wall_ms,
                outcome=outcome,
                est_cost_usd=estimate_cost(
                    model or "local",
                    in_tok,
                    out_tok,
                    actual_model=actual_model,
                    backend=backend_kind,
                ),
                client_key=params.get("_clientKey"),
            )
            try:
                await self._ledger.record(
                    rec, actual_model=actual_model, backend=backend_kind
                )
            except TypeError:
                # Duck-typed ledgers that predate the Wave-A kwargs: the
                # concrete id still rides on the record (`actual_model`), which
                # the ledger's `_apply_actual_model` reads.
                await self._ledger.record(rec)
        except Exception as exc:  # noqa: BLE001 - metering never fails a solve
            log.debug("vision ledger record failed: %s", exc)
