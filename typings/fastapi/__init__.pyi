from typing import Any, Callable, Generic, TypeVar

T = TypeVar("T")


def Header(
    default: Any = ...,
    *,
    alias: str | None = ...,
    **kwargs: Any,
) -> Any: ...


class HTTPException(Exception):
    status_code: int
    detail: Any
    def __init__(self, status_code: int, detail: Any = ...) -> None: ...


class APIRouter:
    def __init__(self, *, prefix: str = ...) -> None: ...
    def get(self, path: str, *, response_model: Any = ...) -> Callable[[Callable[..., T]], Callable[..., T]]: ...
    def post(self, path: str, *, response_model: Any = ...) -> Callable[[Callable[..., T]], Callable[..., T]]: ...


class _Client:
    host: str


class _Headers:
    def get(self, key: str, default: str = ...) -> str: ...


class Request:
    headers: _Headers
    client: _Client | None


class FastAPI:
    def __init__(self, *, title: str = ..., version: str = ..., description: str = ..., lifespan: Any = ...) -> None: ...
    def include_router(self, router: APIRouter) -> None: ...
    def get(self, path: str) -> Callable[[Callable[..., T]], Callable[..., T]]: ...
