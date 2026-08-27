"""Lazily-built local and cloud OpenAI-compatible model clients.

The pool owns the SDK clients and their underlying HTTP connection pools. It is
therefore also responsible for closing them during application shutdown; model
callers only depend on the small ``ModelClient`` surface.
"""

from __future__ import annotations

import asyncio
import inspect
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


ClientFactory = Callable[[str, str], Any]


def _default_client_factory(base_url: str, api_key: str) -> Any:
    # Imported lazily so importing this module in tests does not require a live
    # endpoint or valid credentials.
    from openai import AsyncOpenAI

    return AsyncOpenAI(base_url=base_url, api_key=api_key)


async def _close_client(client: Any) -> None:
    """Close an async/sync SDK client using its supported close convention."""
    closer = getattr(client, "aclose", None)
    if closer is None:
        closer = getattr(client, "close", None)
    if closer is None:
        return
    result = closer()
    if inspect.isawaitable(result):
        await result


@dataclass
class ModelClient:
    """One logical model backend (``local`` or ``cloud``)."""

    name: str
    model: str
    base_url: str
    api_key: str
    client_factory: ClientFactory = field(default=_default_client_factory, repr=False)
    max_concurrency: int = 0
    _client: Optional[Any] = field(default=None, repr=False, compare=False)
    _sem: Optional[asyncio.Semaphore] = field(
        default=None, repr=False, compare=False
    )
    _closed: bool = field(default=False, repr=False, compare=False)

    def _raw(self) -> Any:
        if self._closed:
            raise RuntimeError(f"model client {self.name!r} is closed")
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
        """Run a chat completion and return ``(content_text, usage)``."""
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

        return _extract_content(response), _extract_usage(response)

    async def transcribe_audio(
        self,
        audio_bytes: bytes,
        *,
        model: "str | None" = None,
        filename: str = "audio.mp3",
        timeout: "float | None" = None,
    ) -> "tuple[str, ModelUsage]":
        """Transcribe audio via the OpenAI-compatible transcription endpoint."""
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

    async def close(self) -> None:
        """Close the underlying SDK/HTTP client exactly once."""
        if self._closed:
            return
        self._closed = True
        client, self._client = self._client, None
        self._sem = None
        if client is not None:
            await _close_client(client)


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
    if prompt is None and isinstance(usage, dict):
        prompt = usage.get("prompt_tokens")
    if completion is None and isinstance(usage, dict):
        completion = usage.get("completion_tokens")
    return ModelUsage(
        input_tokens=int(prompt or 0),
        output_tokens=int(completion or 0),
    )


class ModelPool:
    """Own the local and cloud ``ModelClient`` instances built from ``Config``."""

    def __init__(self, config, client_factory: "ClientFactory | None" = None) -> None:
        self._config = config
        factory = client_factory if client_factory is not None else _default_client_factory
        self._clients: dict[str, ModelClient] = {
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

    async def close(self) -> None:
        """Close both backends, attempting every client even if one fails."""
        results = await asyncio.gather(
            *(client.close() for client in self._clients.values()),
            return_exceptions=True,
        )
        failures = [result for result in results if isinstance(result, BaseException)]
        if failures:
            raise RuntimeError(
                "one or more model clients failed to close: "
                + "; ".join(str(exc) for exc in failures)
            )
