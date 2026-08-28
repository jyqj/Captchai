"""Application lifespan ownership and failure-path cleanup tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import src.main as main_module


def test_lifespan_stops_browser_when_start_fails(monkeypatch):
    events: list[str] = []

    class _Browser:
        def __init__(self, config) -> None:
            events.append("browser.init")

        async def start(self) -> None:
            events.append("browser.start")
            raise RuntimeError("launch failed")

        async def stop(self) -> None:
            events.append("browser.stop")

    monkeypatch.setattr(main_module, "BrowserManager", _Browser)

    async def scenario():
        with pytest.raises(RuntimeError, match="launch failed"):
            async with main_module.lifespan(SimpleNamespace()):
                raise AssertionError("lifespan should not yield")

    asyncio.run(scenario())
    assert events == ["browser.init", "browser.start", "browser.stop"]


def test_lifespan_stops_browser_when_services_creation_fails(monkeypatch):
    events: list[str] = []

    class _Browser:
        def __init__(self, config) -> None:
            return None

        async def start(self) -> None:
            events.append("browser.start")

        async def stop(self) -> None:
            events.append("browser.stop")

    class _Services:
        @classmethod
        async def create(cls, config):
            events.append("services.create")
            raise RuntimeError("redis unavailable")

    monkeypatch.setattr(main_module, "BrowserManager", _Browser)
    monkeypatch.setattr(main_module, "SolverServices", _Services)

    async def scenario():
        with pytest.raises(RuntimeError, match="redis unavailable"):
            async with main_module.lifespan(SimpleNamespace()):
                raise AssertionError("lifespan should not yield")

    asyncio.run(scenario())
    assert events == ["browser.start", "services.create", "browser.stop"]


def test_lifespan_closes_services_when_session_prewarm_fails(monkeypatch):
    events: list[str] = []

    class _Browser:
        def __init__(self, config) -> None:
            return None

        async def start(self) -> None:
            events.append("browser.start")

        async def stop(self) -> None:
            events.append("browser.stop")

    class _Services:
        @classmethod
        async def create(cls, config):
            events.append("services.create")
            return cls()

        def attach_browser(self, browser) -> None:
            events.append("services.attach")

        async def prewarm_sessions(self) -> int:
            events.append("services.prewarm")
            raise RuntimeError("prewarm failed")

        async def close(self) -> None:
            events.append("services.close")

    monkeypatch.setattr(main_module, "BrowserManager", _Browser)
    monkeypatch.setattr(main_module, "SolverServices", _Services)

    async def scenario():
        with pytest.raises(RuntimeError, match="prewarm failed"):
            async with main_module.lifespan(SimpleNamespace()):
                raise AssertionError("lifespan should not yield")

    asyncio.run(scenario())
    assert events == [
        "browser.start",
        "services.create",
        "services.attach",
        "services.prewarm",
        "services.close",
        "browser.stop",
    ]


def test_lifespan_normal_shutdown_is_dependency_ordered(monkeypatch):
    events: list[str] = []

    class _Browser:
        def __init__(self, config) -> None:
            return None

        async def start(self) -> None:
            events.append("browser.start")

        async def stop(self) -> None:
            events.append("browser.stop")

    class _Services:
        proxy_pool = object()

        @classmethod
        async def create(cls, config):
            events.append("services.create")
            return cls()

        def attach_browser(self, browser) -> None:
            events.append("services.attach")

        async def prewarm_sessions(self) -> int:
            events.append("services.prewarm")
            return 0

        async def close(self) -> None:
            events.append("services.close")

    class _TaskManager:
        def configure(self, *args, **kwargs) -> None:
            events.append("tasks.configure")

        def register_solver(self, task_type, solver, category) -> None:
            return None

        async def shutdown(self) -> None:
            events.append("tasks.shutdown")

    class _Solver:
        def __init__(self, *args, **kwargs) -> None:
            return None

        async def solve(self, params):
            return {}

    def _set_services(value) -> None:
        events.append(
            "services.set" if value is not None else "services.clear"
        )

    monkeypatch.setattr(main_module, "BrowserManager", _Browser)
    monkeypatch.setattr(main_module, "SolverServices", _Services)
    monkeypatch.setattr(main_module, "task_manager", _TaskManager())
    monkeypatch.setattr(main_module, "set_services", _set_services)
    for name in (
        "RecaptchaV3Solver",
        "RecaptchaV2Solver",
        "HCaptchaSolver",
        "TurnstileSolver",
        "CaptchaRecognizer",
        "ClassificationSolver",
    ):
        monkeypatch.setattr(main_module, name, _Solver)
    for name in (
        "_RECAPTCHA_V3_TYPES",
        "_RECAPTCHA_V2_TYPES",
        "_HCAPTCHA_TYPES",
        "_TURNSTILE_TYPES",
        "_CLASSIFICATION_TYPES",
        "_IMAGE_TEXT_TYPES",
    ):
        monkeypatch.setattr(main_module, name, ())

    async def scenario():
        async with main_module.lifespan(SimpleNamespace()):
            events.append("running")

    asyncio.run(scenario())
    assert events == [
        "browser.start",
        "services.create",
        "services.attach",
        "services.prewarm",
        "services.set",
        "tasks.configure",
        "running",
        "tasks.shutdown",
        "services.clear",
        "services.close",
        "browser.stop",
    ]
