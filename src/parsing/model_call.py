"""The deep model-call seam shared by both vision planes.

Two families of task type reach an OpenAI-compatible vision model:

* the **grid plane** — ``VisionRouter`` classifying challenge tiles for the
  browser solvers (hCaptcha / Turnstile / reCAPTCHA), and
* the **free-form plane** — ``ClassificationSolver`` and ``CaptchaRecognizer``
  serving the pure-image task types (``*Classification`` / ``ImageToText*``).

Historically each built its own ``AsyncOpenAI`` client, its own retry loop and
its own JSON parser, and — worse — the free-form plane bypassed the budget cap
and the cost ledger entirely, so a third of the task catalogue spent money
invisibly. :class:`ModelInvoker` is the one place a model call is actually made:
it owns tier→backend routing, the budget gate, connection-error backend
fallback (local↔cloud) and the model-pool concurrency bound. Callers hand it a
system prompt, a user prompt and image data URLs and get back the raw content,
token usage, the backend that actually served the call and the concrete model id
that backend was configured to serve.

It also exposes the per-backend fan-out reservation
(:meth:`ModelInvoker.backend_semaphore`) used by the grid plane's
self-consistency voting: ``vision_vote_samples`` concurrent samples per solve,
across every concurrent solve, would otherwise multiply into a burst that no
single client-level limit necessarily covers (duck-typed / test clients have
no ``max_concurrency`` at all).

The module is intentionally decoupled from the consumption layer: ``budget`` is
an opaque, optional, duck-typed object (every use is guarded with
``is not None``), and metering is the caller's concern (a solve may be one call
or, under voting, several — the record is per-solve, not per-call).
"""

from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from ..assets.model_pool import ModelPool, ModelUsage

log = logging.getLogger(__name__)

# Concrete model ids that resolved to a backend by search (i.e. the id was not
# a pool key) and were already reported. Process-wide so an ambiguous id — the
# same model served by BOTH backends — is logged exactly once instead of once
# per model call.
_AMBIGUOUS_MODEL_LOGGED: set[str] = set()


# Default per-backend fan-out reservations used when neither the resolved
# client nor the config declares a limit. Cloud matches CLOUD_MAX_CONCURRENCY's
# default (the paid, rate-limited backend is the one worth protecting); local
# mirrors VISION_CONCURRENCY. 0 / unset never means "unbounded" for voting.
_DEFAULT_FANOUT_LIMITS = {"local": 8, "cloud": 4}


def _positive_int(value: Any) -> int:
    """Coerce a config / client limit to a positive int (0 when unusable)."""
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


# Exception class names that indicate a *connection / availability* failure
# (backend down, refused, timed out) rather than a bad request. Matched by
# class name across the MRO so we stay dependency-light (no hard import of
# openai / httpx) — covers openai.APIConnectionError / APITimeoutError, httpx
# ConnectError / ConnectTimeout / ReadTimeout / PoolTimeout, and the builtin
# ConnectionError / TimeoutError.
_CONNECTION_ERROR_NAMES = frozenset(
    {
        "APIConnectionError",
        "APITimeoutError",
        "InternalServerError",
        "ConnectionError",
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "ReadError",
        "PoolTimeout",
        "TimeoutError",
        "ConnectionRefusedError",
        "ConnectionResetError",
    }
)


def is_connection_error(exc: BaseException) -> bool:
    """True when ``exc`` (or any base) is a connection/availability failure."""
    for klass in type(exc).__mro__:
        if klass.__name__ in _CONNECTION_ERROR_NAMES:
            return True
    return False


def encode_png_data_urls(images: list[bytes]) -> list[str]:
    """Base64-encode raw PNG bytes into ``data:image/png;base64,...`` URLs."""
    return [
        "data:image/png;base64,{}".format(base64.b64encode(img).decode())
        for img in images
    ]


@dataclass
class ModelCallRequest:
    """One vision model call: prompts, image data URLs and routing hints.

    ``image_urls`` are fully-formed data URLs (``data:image/...;base64,...``)
    so each plane keeps its own encoding rules (the grid plane sends PNG tiles;
    the classifier sniffs the mime type of caller-supplied base64). ``tier``
    drives routing (1 = local/bulk, 2 = cloud/hard). ``est_cost`` is a crude
    positive estimate used only by the budget gate — kept caller-supplied so the
    invoker stays decoupled from the pricing table.
    """

    system_prompt: str
    user_text: str
    image_urls: list[str] = field(default_factory=list)
    tier: int = 1
    detail: str = "high"
    temperature: float = 0.0
    max_tokens: int = 512
    est_cost: float = 0.0


@dataclass
class ModelCallResult:
    content: str
    usage: ModelUsage
    #: Concrete served model id (``ModelClient.model``, e.g. the configured
    #: ``cloud_model``) — what pricing should key on. Falls back to the backend
    #: alias only when the client exposes no model id (duck-typed test fakes).
    model: str
    #: Backend alias that served the call (``"local"`` / ``"cloud"``), kept for
    #: callers that need the routing dimension rather than the served id.
    backend: str = ""


class ModelInvoker:
    """The single place a vision model call is routed, budgeted and made.

    Deep module, narrow interface: :meth:`invoke` is all a simple (single-call)
    caller needs. ``VisionRouter`` interleaves voting over one chosen backend,
    so it composes the finer-grained primitives (:meth:`route`,
    :meth:`guard_budget`, :meth:`call_with_fallback`, :meth:`call_backend`) that
    :meth:`invoke` itself is built from — but every model call in the process
    still funnels through this one object.
    """

    def __init__(self, model_pool: ModelPool, config: Any, *, budget: Any = None) -> None:
        self._pool = model_pool
        self._config = config
        self._budget = budget
        # backend key -> (event loop it was created on, semaphore). Cached with
        # the loop because an invoker is process-lived while callers (tests,
        # ``asyncio.run`` loops) may drive it from a fresh loop each time, and
        # an ``asyncio.Semaphore`` is bound to the loop of its first use.
        self._backend_sems: dict[str, "tuple[Any, asyncio.Semaphore]"] = {}

    # -- routing ----------------------------------------------------------

    def route(self, tier: int) -> str:
        """Map a task tier to a backend name (``"local"`` / ``"cloud"``)."""
        if tier >= 2 and getattr(self._config, "vision_cloud_enabled", False):
            return "cloud"
        return "local"

    # -- budget gate ------------------------------------------------------

    async def budget_allows_cloud(
        self, est_cost: float, client_key: "str | None"
    ) -> bool:
        """True when there is no budget, or the budget allows a cloud call.

        Used before a *connection-error* fallback to cloud (local→cloud) and
        the grid plane's inline escalation. A denial here does not downgrade —
        the caller keeps the result already in hand (local) as the fallback.
        """
        if self._budget is None:
            return True
        decision = await self._budget.check(client_key, est_cost, model="cloud")
        if decision is None:
            return True
        return bool(getattr(decision, "allowed", True))

    async def guard_budget(
        self, model_name: str, est_cost: float, client_key: "str | None"
    ) -> str:
        """Return the backend to use after the budget gate.

        Only paid (cloud) calls are checked. A denial with a suggested
        ``downgrade_to`` falls back to the cheaper backend; a denial with no
        suggested downgrade leaves the choice unchanged (the pre-existing
        behaviour — the budget guard's own policy decides whether to hard-stop).
        """
        if self._budget is None or model_name != "cloud":
            return model_name
        decision = await self._budget.check(client_key, est_cost, model=model_name)
        if decision is not None and not getattr(decision, "allowed", True):
            downgrade = getattr(decision, "downgrade_to", None)
            if downgrade:
                return downgrade
        return model_name

    # -- message building -------------------------------------------------

    def build_messages(
        self, system_prompt: str, user_text: str, image_urls: list[str], detail: str
    ) -> list:
        user_content: list = [{"type": "text", "text": user_text}]
        for url in image_urls:
            user_content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": url, "detail": detail},
                }
            )
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

    # -- model call -------------------------------------------------------

    def _resolve_client(self, model_name: str) -> Any:
        """Resolve a backend alias *or* a concrete served model id to its client.

        Routing happens in aliases (``"local"`` / ``"cloud"``), but callers such
        as the grid plane's voting loop receive the concrete served id back from
        :meth:`call_with_fallback` and pass it straight to :meth:`call_backend`.
        Accepting both keeps that path working without making callers peel the
        alias back out of the result.
        """
        try:
            return self._pool.get(model_name)
        except (KeyError, TypeError, ValueError) as first_error:
            matches: list[tuple[str, Any]] = []
            for alias in ("local", "cloud"):
                try:
                    client = self._pool.get(alias)
                except (KeyError, TypeError, ValueError):
                    continue
                if getattr(client, "model", None) == model_name:
                    matches.append((alias, client))
            if matches:
                if len(matches) > 1 and model_name not in _AMBIGUOUS_MODEL_LOGGED:
                    # Both backends are configured to serve the same concrete
                    # id; resolution deterministically prefers "local". Make
                    # that choice observable (once per process / model id)
                    # instead of silently returning the first backend.
                    _AMBIGUOUS_MODEL_LOGGED.add(model_name)
                    log.info(
                        "concrete model id %r is served by both backends (%s); "
                        "resolving to %r",
                        model_name,
                        ", ".join(alias for alias, _ in matches),
                        matches[0][0],
                    )
                return matches[0][1]
            raise first_error

    @staticmethod
    def _served_identity(client: Any, fallback_name: str) -> "tuple[str, str]":
        """Return ``(backend_alias, concrete_model_id)`` for a served client."""
        backend = str(getattr(client, "name", None) or fallback_name)
        model = str(getattr(client, "model", None) or backend)
        return backend, model

    # -- per-backend fan-out reservation ----------------------------------

    def _try_resolve_client(self, model_name: str) -> Any:
        """Resolve ``model_name`` to its client, or ``None`` when unknown."""
        try:
            return self._resolve_client(model_name)
        except (KeyError, TypeError, ValueError):
            return None

    @staticmethod
    def _backend_alias(client: Any, fallback_name: str) -> str:
        """Normalise a client (or model id) to a fan-out namespace.

        Known aliases keep their name; an unresolvable concrete model id is
        treated as the paid backend so it inherits the tighter default.
        """
        name = str(getattr(client, "name", None) or fallback_name)
        return name if name in _DEFAULT_FANOUT_LIMITS else "cloud"

    def backend_limit(self, model_name: str) -> int:
        """Concurrent-call budget for the backend that serves ``model_name``.

        Resolution order: the resolved client's own ``max_concurrency`` (the
        model-pool bound), then the matching config field
        (``cloud_max_concurrency`` / ``local_max_concurrency``), then the
        conservative default in :data:`_DEFAULT_FANOUT_LIMITS`. Always finite:
        an unbounded voting fan-out is what trips provider rate limits.
        """
        client = self._try_resolve_client(model_name)
        if client is not None:
            limit = _positive_int(getattr(client, "max_concurrency", 0))
            if limit:
                return limit
        backend = self._backend_alias(client, model_name)
        field = (
            "cloud_max_concurrency"
            if backend == "cloud"
            else "local_max_concurrency"
        )
        limit = _positive_int(getattr(self._config, field, 0))
        if limit:
            return limit
        return _DEFAULT_FANOUT_LIMITS[backend]

    def backend_semaphore(self, model_name: str) -> "asyncio.Semaphore | None":
        """The per-backend fan-out reservation semaphore (``None`` if sync).

        Lazily created and cached per (backend, event loop). Voting callers
        hold it once per sample, so the aggregate fan-out of every concurrent
        solve stays within the backend's budget even when the underlying client
        is duck-typed and declares no ``max_concurrency`` of its own.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        limit = self.backend_limit(model_name)
        if limit <= 0:  # pragma: no cover - backend_limit is always positive
            return None
        key = self._backend_alias(self._try_resolve_client(model_name), model_name)
        cached = self._backend_sems.get(key)
        if cached is None or cached[0] is not loop:
            cached = (loop, asyncio.Semaphore(limit))
            self._backend_sems[key] = cached
        return cached[1]

    async def call_backend(
        self,
        model_name: str,
        messages: list,
        *,
        temperature: float = 0.0,
        max_tokens: int = 512,
    ) -> "tuple[str, ModelUsage]":
        """Call one specific backend (no routing / budget). Returns (content, usage).

        ``model_name`` may be the backend alias (``"local"`` / ``"cloud"``) or a
        concrete served model id — the grid plane's voting loop has already
        routed and budget-gated once and wants repeated samples on the same
        backend, which it identifies by the served id it got back.
        """
        client = self._resolve_client(model_name)
        timeout = float(getattr(self._config, "captcha_timeout", 30))
        return await client.chat(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
        )

    async def call_with_fallback(
        self,
        model_name: str,
        messages: list,
        *,
        client_key: "str | None",
        est_cost: float,
        temperature: float = 0.0,
        max_tokens: int = 512,
    ) -> "tuple[str, str, ModelUsage]":
        """Call ``model_name``, retrying the other backend on a connection error.

        The routed backend is tried first. If it fails with a *connection* error
        (service down / refused / timeout) — not a parse or bad-request error —
        the call retries once on the other backend: ``local → cloud`` (gated by
        ``vision_cloud_enabled`` + budget, since cloud is paid) or
        ``cloud → local`` (always, local is free). Returns
        ``(served_model_id, content, usage)`` where ``served_model_id`` is the
        concrete ``ModelClient.model`` that served the call (the backend alias is
        returned separately by :meth:`invoke` as ``ModelCallResult.backend``).
        """
        _, served, content, usage = await self._call_with_fallback(
            model_name,
            messages,
            client_key=client_key,
            est_cost=est_cost,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return served, content, usage

    async def _call_with_fallback(
        self,
        model_name: str,
        messages: list,
        *,
        client_key: "str | None",
        est_cost: float,
        temperature: float = 0.0,
        max_tokens: int = 512,
    ) -> "tuple[str, str, str, ModelUsage]":
        """Inner call returning ``(backend_alias, served_model, content, usage)``."""
        try:
            content, usage = await self.call_backend(
                model_name, messages, temperature=temperature, max_tokens=max_tokens
            )
            backend, served = self._served_identity(
                self._resolve_client(model_name), model_name
            )
            return backend, served, content, usage
        except Exception as exc:  # noqa: BLE001
            fallback_enabled = bool(
                getattr(self._config, "model_connection_fallback", True)
            )
            if not fallback_enabled or not is_connection_error(exc):
                raise
            alt = await self._fallback_backend(model_name, est_cost, client_key)
            if alt is None:
                raise
            log.warning(
                "model backend %r failed (%s: %s); falling back to %r",
                model_name,
                type(exc).__name__,
                exc,
                alt,
            )
            content, usage = await self.call_backend(
                alt, messages, temperature=temperature, max_tokens=max_tokens
            )
            backend, served = self._served_identity(
                self._resolve_client(alt), alt
            )
            return backend, served, content, usage

    async def _fallback_backend(
        self, model_name: str, est_cost: float, client_key: "str | None"
    ) -> "str | None":
        """Pick the alternate backend for a connection-error retry, or None."""
        if model_name == "cloud":
            return "local"  # local is free + self-hosted; always a valid fallback
        # local → cloud only when cloud is enabled and the budget allows it.
        if not getattr(self._config, "vision_cloud_enabled", False):
            return None
        if await self.budget_allows_cloud(est_cost, client_key):
            return "cloud"
        return None

    # -- high-level single call -------------------------------------------

    async def invoke(
        self, req: ModelCallRequest, *, client_key: "str | None" = None
    ) -> ModelCallResult:
        """Route, budget-gate, build messages and call — the whole single-call path.

        Everything a single-call caller (the free-form vision plane) needs:
        the returned :class:`ModelCallResult` carries the raw content, the token
        usage, the concrete model that actually served the call (post-fallback)
        and, separately, the backend alias that served it.
        """
        model_name = self.route(req.tier)
        model_name = await self.guard_budget(model_name, req.est_cost, client_key)
        messages = self.build_messages(
            req.system_prompt, req.user_text, req.image_urls, req.detail
        )
        backend, served, content, usage = await self._call_with_fallback(
            model_name,
            messages,
            client_key=client_key,
            est_cost=req.est_cost,
            temperature=req.temperature,
            max_tokens=req.max_tokens,
        )
        return ModelCallResult(
            content=content, usage=usage, model=served, backend=backend
        )
