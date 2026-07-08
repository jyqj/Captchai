"""Model pool: lazily-built local + cloud OpenAI-compatible clients.

Wraps ``openai.AsyncOpenAI`` behind a small ``ModelClient`` so the parsing /
routing layer never touches the SDK directly.  A ``client_factory`` seam lets
tests inject a fake ``AsyncOpenAI`` whose ``.chat.completions.create(...)`` is
awaitable, keeping the whole stack off the network.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


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
    _client: Optional[Any] = field(default=None, repr=False, compare=False)
    _sem: Optional[asyncio.Semaphore] = field(
        default=None, repr=False, compare=False
    )

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

        sem = self._semaphore()
        if sem is not None:
            async with sem:
                response = await self._raw().chat.completions.create(**kwargs)
        else:
            response = await self._raw().chat.completions.create(**kwargs)

        content = _extract_content(response)
        usage = _extract_usage(response)
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

        sem = self._semaphore()
        if sem is not None:
            async with sem:
                response = await self._raw().audio.transcriptions.create(**kwargs)
        else:
            response = await self._raw().audio.transcriptions.create(**kwargs)

        text = getattr(response, "text", None)
        if text is None and isinstance(response, dict):
            text = response.get("text")
        return (text or "").strip(), _extract_usage(response)


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
        self._clients: dict = {
            "local": ModelClient(
                name="local",
                model=config.local_model,
                base_url=config.local_base_url,
                api_key=config.local_api_key,
                client_factory=factory,
                max_concurrency=int(getattr(config, "local_max_concurrency", 0) or 0),
            ),
            "cloud": ModelClient(
                name="cloud",
                model=config.cloud_model,
                base_url=config.cloud_base_url,
                api_key=config.cloud_api_key,
                client_factory=factory,
                max_concurrency=int(getattr(config, "cloud_max_concurrency", 0) or 0),
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
