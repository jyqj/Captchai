"""Model pool: lazily-built local + cloud OpenAI-compatible clients.

Wraps ``openai.AsyncOpenAI`` behind a small ``ModelClient`` so the parsing /
routing layer never touches the SDK directly.  A ``client_factory`` seam lets
tests inject a fake ``AsyncOpenAI`` whose ``.chat.completions.create(...)`` is
awaitable, keeping the whole stack off the network.
"""

from __future__ import annotations

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
    _client: Optional[Any] = field(default=None, repr=False, compare=False)

    def _raw(self) -> Any:
        if self._client is None:
            self._client = self.client_factory(self.base_url, self.api_key)
        return self._client

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
        backend reports them; otherwise falls back to zeros.
        """
        kwargs: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if timeout is not None:
            kwargs["timeout"] = timeout

        response = await self._raw().chat.completions.create(**kwargs)

        content = _extract_content(response)
        usage = _extract_usage(response)
        return content, usage


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
            ),
            "cloud": ModelClient(
                name="cloud",
                model=config.cloud_model,
                base_url=config.cloud_base_url,
                api_key=config.cloud_api_key,
                client_factory=factory,
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
