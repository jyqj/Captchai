"""Model pool: lazily-built local + cloud OpenAI-compatible clients.

Wraps ``openai.AsyncOpenAI`` behind a small ``ModelClient`` so the parsing /
routing layer never touches the SDK directly.  A ``client_factory`` seam lets
tests inject a fake ``AsyncOpenAI`` whose ``.chat.completions.create(...)`` is
awaitable, keeping the whole stack off the network.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

log = logging.getLogger(__name__)

# Per-backend circuit breaker defaults: three consecutive failures open the
# circuit for 30s; the next call after cooldown is a single half-open probe.
DEFAULT_FAILURE_THRESHOLD = 3
DEFAULT_COOLDOWN_SECONDS = 30.0
# Short client-level timeout (connect/probe); per-call timeouts still override
# it (ModelInvoker passes ``captcha_timeout`` on every request).
DEFAULT_CONNECT_TIMEOUT_SECONDS = 10.0


class ModelBackendUnavailable(ConnectionError):
    """Raised when a backend's circuit breaker is open.

    Subclasses ``ConnectionError`` on purpose: ``ModelInvoker`` already treats
    connection errors as "try the other backend", so an open circuit degrades
    to the same local↔cloud fallback path as a dead socket.
    """


@dataclass
class ModelUsage:
    """Token accounting for a single model call (or an accumulated total)."""

    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, other: "ModelUsage") -> "ModelUsage":
        return ModelUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )


# A client factory takes (base_url, api_key) and returns an object exposing
# ``.chat.completions.create(...)`` as an awaitable — i.e. openai.AsyncOpenAI.
ClientFactory = Callable[[str, str], Any]


def _default_client_factory(base_url: str, api_key: str) -> Any:
    # Imported lazily so importing this module (e.g. in tests) never requires
    # the openai package or valid credentials.
    from openai import AsyncOpenAI

    # Short connect/probe timeout and no hidden SDK retries: a dead backend
    # should fail fast and let the circuit breaker / invoker fallback decide
    # (per-call ``timeout=`` still overrides the read default). Older SDKs
    # without these kwargs fall back to the plain constructor.
    try:
        return AsyncOpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=DEFAULT_CONNECT_TIMEOUT_SECONDS,
            max_retries=0,
        )
    except TypeError:  # pragma: no cover - older openai SDK signature
        return AsyncOpenAI(base_url=base_url, api_key=api_key)


@dataclass
class ModelClient:
    """A single logical backend (``local`` or ``cloud``).

    The underlying AsyncOpenAI-compatible client is created lazily on first use
    via ``client_factory`` and cached on the instance.
    """

    name: str
    model: str
    base_url: str
    api_key: str
    client_factory: ClientFactory = field(default=_default_client_factory, repr=False)
    # Max concurrent in-flight calls to this backend (0 = unlimited). Bounds the
    # ``browser_concurrency × vote_samples`` fan-out so a burst of solves (each
    # voting several samples concurrently) can't overrun a rate-limited cloud
    # provider. The semaphore is created lazily so a ModelClient can be built
    # outside a running event loop (Python 3.9 binds Semaphore to the loop at
    # construction).
    max_concurrency: int = 0
    # Per-backend circuit breaker (closed → open → half-open). Kept separate
    # from the routing layer so a flapping local model doesn't take the cloud
    # path down with it.
    failure_threshold: int = DEFAULT_FAILURE_THRESHOLD
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS
    _client: Optional[Any] = field(default=None, repr=False, compare=False)
    _sem: Optional[asyncio.Semaphore] = field(
        default=None, repr=False, compare=False
    )
    _success_count: int = field(default=0, repr=False, compare=False)
    _failure_count: int = field(default=0, repr=False, compare=False)
    _consecutive_failures: int = field(default=0, repr=False, compare=False)
    _cooldown_until: float = field(default=0.0, repr=False, compare=False)
    _circuit_state: str = field(default="closed", repr=False, compare=False)
    _half_open_in_flight: bool = field(default=False, repr=False, compare=False)
    _last_error: Optional[str] = field(default=None, repr=False, compare=False)

    def _raw(self) -> Any:
        if self._client is None:
            self._client = self.client_factory(self.base_url, self.api_key)
        return self._client

    def _semaphore(self) -> "asyncio.Semaphore | None":
        if self.max_concurrency <= 0:
            return None
        if self._sem is None:
            self._sem = asyncio.Semaphore(self.max_concurrency)
        return self._sem

    # -- circuit breaker --------------------------------------------------

    def _before_call(self) -> None:
        """Gate a call through the breaker; raise when the backend is out.

        ``closed`` → allow. ``open`` → reject until the cooldown lapses, then
        transition to ``half_open`` and allow exactly one probe. While that
        probe is in flight further calls are rejected (rather than piling more
        traffic onto a backend that is probably still down).
        """
        if self._circuit_state == "closed":
            return
        now = time.monotonic()
        if self._circuit_state == "open":
            if now < self._cooldown_until:
                raise ModelBackendUnavailable(
                    "model backend {!r} circuit open ({:.1f}s cooldown left)".format(
                        self.name, self._cooldown_until - now
                    )
                )
            self._circuit_state = "half_open"
            self._half_open_in_flight = False
        if self._circuit_state == "half_open":
            if self._half_open_in_flight:
                raise ModelBackendUnavailable(
                    "model backend {!r} circuit half-open (probe in flight)".format(
                        self.name
                    )
                )
            self._half_open_in_flight = True

    def _record_success(self) -> None:
        self._success_count += 1
        self._consecutive_failures = 0
        self._cooldown_until = 0.0
        self._half_open_in_flight = False
        self._circuit_state = "closed"

    def _record_failure(self, exc: BaseException) -> None:
        self._failure_count += 1
        self._consecutive_failures += 1
        self._last_error = "{}: {}".format(type(exc).__name__, exc)
        was_half_open = self._circuit_state == "half_open"
        self._half_open_in_flight = False
        if was_half_open or self._consecutive_failures >= max(
            1, self.failure_threshold
        ):
            self._circuit_state = "open"
            self._cooldown_until = time.monotonic() + self.cooldown_seconds
            log.warning(
                "model backend %r circuit opened after %d consecutive failure(s): %s",
                self.name,
                self._consecutive_failures,
                self._last_error,
            )

    def health(self) -> Dict[str, Any]:
        """Serialisable per-backend health for readiness reporting."""
        now = time.monotonic()
        state = self._circuit_state
        if state == "open" and now >= self._cooldown_until:
            # Cooldown lapsed: the next call gets a half-open probe.
            state = "half_open"
        total = self._success_count + self._failure_count
        return {
            "name": self.name,
            "model": self.model,
            "state": state,
            "healthy": state == "closed",
            "success_count": self._success_count,
            "failure_count": self._failure_count,
            "consecutive_failures": self._consecutive_failures,
            "success_rate": (
                round(self._success_count / total, 4) if total else None
            ),
            "cooldown_remaining": (
                max(0.0, self._cooldown_until - now)
                if self._circuit_state == "open"
                else 0.0
            ),
            "last_error": self._last_error,
            "client_created": self._client is not None,
        }

    async def aclose(self) -> None:
        """Close the lazily-built SDK client (releases its httpx pool)."""
        client = self._client
        self._client = None
        if client is None:
            return
        for closer_name in ("aclose", "close"):
            closer = getattr(client, closer_name, None)
            if closer is None:
                continue
            try:
                result = closer()
                if asyncio.iscoroutine(result):
                    await result
                return
            except Exception:  # noqa: BLE001 - teardown is best-effort
                log.debug(
                    "closing model backend %r via %s failed",
                    self.name,
                    closer_name,
                    exc_info=True,
                )
                continue

    async def chat(
        self,
        *,
        messages: list,
        temperature: float = 0.0,
        max_tokens: int = 512,
        timeout: "float | None" = None,
    ) -> "tuple[str, ModelUsage]":
        """Run a chat completion and return ``(content_text, usage)``.

        Reads ``response.usage.prompt_tokens`` / ``completion_tokens`` when the
        backend reports them; otherwise falls back to zeros. Concurrency is
        bounded by ``max_concurrency`` (via a lazily-created semaphore) so a
        voting fan-out can't overrun a rate-limited backend.
        """
        kwargs: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if timeout is not None:
            kwargs["timeout"] = timeout

        self._before_call()
        # True when this call is the single half-open probe admitted above. A
        # cancelled probe must actively re-open the breaker below: leaving the
        # state at ``half_open`` with the in-flight flag set would reject every
        # later call forever (the half-open branch never consults the cooldown).
        probe_owner = self._circuit_state == "half_open"
        try:
            sem = self._semaphore()
            if sem is not None:
                async with sem:
                    response = await self._raw().chat.completions.create(**kwargs)
            else:
                response = await self._raw().chat.completions.create(**kwargs)

            content = _extract_content(response)
            usage = _extract_usage(response)
        except asyncio.CancelledError as exc:
            # A cancelled probe is inconclusive about backend health: re-open
            # the circuit with a fresh cooldown instead of latching half-open.
            if probe_owner:
                self._record_failure(exc)
            raise
        except Exception as exc:  # noqa: BLE001 - any failure trips the breaker
            self._record_failure(exc)
            raise
        finally:
            # Whatever happened, an admitted probe is no longer outstanding.
            # (An exception path already cleared it; success clears it in
            # ``_record_success``, so this is the cancellation/BaseException
            # safety net.) Only the owning call may clear it.
            if probe_owner:
                self._half_open_in_flight = False
        self._record_success()
        return content, usage

    async def transcribe_audio(
        self,
        audio_bytes: bytes,
        *,
        model: "str | None" = None,
        filename: str = "audio.mp3",
        timeout: "float | None" = None,
    ) -> "tuple[str, ModelUsage]":
        """Transcribe audio via the OpenAI-compatible ``audio.transcriptions`` API.

        Uses the dedicated speech-to-text endpoint (Whisper-family), NOT a chat
        completion with the mp3 stuffed into an ``image_url`` (which the prior
        reCAPTCHA path did — a text/vision model can't decode audio that way).
        ``model`` overrides the client's default so a chat backend can still be
        pointed at a transcription model. Bounded by the same concurrency
        semaphore as ``chat``. Returns ``(text, usage)``.
        """
        kwargs: dict = {
            "model": model or self.model,
            "file": (filename, audio_bytes),
        }
        if timeout is not None:
            kwargs["timeout"] = timeout

        self._before_call()
        # See ``chat``: a cancelled half-open probe must not latch the breaker
        # in ``half_open`` with ``_half_open_in_flight`` stuck True.
        probe_owner = self._circuit_state == "half_open"
        try:
            sem = self._semaphore()
            if sem is not None:
                async with sem:
                    response = await self._raw().audio.transcriptions.create(**kwargs)
            else:
                response = await self._raw().audio.transcriptions.create(**kwargs)

            text = getattr(response, "text", None)
            if text is None and isinstance(response, dict):
                text = response.get("text")
            usage = _extract_usage(response)
        except asyncio.CancelledError as exc:
            if probe_owner:
                self._record_failure(exc)
            raise
        except Exception as exc:  # noqa: BLE001 - any failure trips the breaker
            self._record_failure(exc)
            raise
        finally:
            if probe_owner:
                self._half_open_in_flight = False
        self._record_success()
        return (text or "").strip(), usage


def _extract_content(response: Any) -> str:
    try:
        choice = response.choices[0]
    except (AttributeError, IndexError, TypeError):
        return ""
    message = getattr(choice, "message", None)
    content = getattr(message, "content", None) if message is not None else None
    return content or ""


def _extract_usage(response: Any) -> ModelUsage:
    usage = getattr(response, "usage", None)
    if usage is None:
        return ModelUsage()
    prompt = getattr(usage, "prompt_tokens", None)
    completion = getattr(usage, "completion_tokens", None)
    # Some backends expose usage as a mapping.
    if prompt is None and isinstance(usage, dict):
        prompt = usage.get("prompt_tokens")
    if completion is None and isinstance(usage, dict):
        completion = usage.get("completion_tokens")
    return ModelUsage(
        input_tokens=int(prompt or 0),
        output_tokens=int(completion or 0),
    )


class ModelPool:
    """Holds the local + cloud ``ModelClient`` instances built from ``Config``."""

    def __init__(self, config, client_factory: "ClientFactory | None" = None) -> None:
        self._config = config
        factory = client_factory if client_factory is not None else _default_client_factory
        failure_threshold = int(
            getattr(config, "model_failure_threshold", DEFAULT_FAILURE_THRESHOLD)
            or DEFAULT_FAILURE_THRESHOLD
        )
        cooldown_seconds = float(
            getattr(config, "model_cooldown_seconds", DEFAULT_COOLDOWN_SECONDS)
            or DEFAULT_COOLDOWN_SECONDS
        )
        self._clients: dict = {
            "local": ModelClient(
                name="local",
                model=config.local_model,
                base_url=config.local_base_url,
                api_key=config.local_api_key,
                client_factory=factory,
                max_concurrency=int(getattr(config, "local_max_concurrency", 0) or 0),
                failure_threshold=failure_threshold,
                cooldown_seconds=cooldown_seconds,
            ),
            "cloud": ModelClient(
                name="cloud",
                model=config.cloud_model,
                base_url=config.cloud_base_url,
                api_key=config.cloud_api_key,
                client_factory=factory,
                max_concurrency=int(getattr(config, "cloud_max_concurrency", 0) or 0),
                failure_threshold=failure_threshold,
                cooldown_seconds=cooldown_seconds,
            ),
        }

    def get(self, name: str) -> ModelClient:
        try:
            return self._clients[name]
        except KeyError:
            raise KeyError(
                "unknown model client {!r}; expected 'local' or 'cloud'".format(name)
            )

    @property
    def local(self) -> ModelClient:
        return self._clients["local"]

    @property
    def cloud(self) -> ModelClient:
        return self._clients["cloud"]

    def health_snapshot(self) -> Dict[str, Any]:
        """Per-backend health for readiness / admin endpoints.

        Shape: ``{"local": {...}, "cloud": {...}}`` with each backend's
        circuit state, counters and cooldown remaining. Never raises.
        """
        snapshot: Dict[str, Any] = {}
        for name, client in self._clients.items():
            try:
                snapshot[name] = client.health()
            except Exception:  # noqa: BLE001 - readiness must not raise
                snapshot[name] = {"name": name, "state": "unknown"}
        return snapshot

    async def aclose(self) -> None:
        """Close every backend's lazy SDK client (releases httpx pools)."""
        for client in self._clients.values():
            await client.aclose()
