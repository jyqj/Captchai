"""Tests for the YesCaptcha-compatible captcha solver API."""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    _ = sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient


def _load_app(*, client_key: str | None = None) -> TestClient:
    """Reload modules with fresh env vars and return a test client."""
    os.environ.pop("CLIENT_KEY", None)
    os.environ.setdefault("CAPTCHA_BASE_URL", "https://example.com/v1")
    os.environ.setdefault("CAPTCHA_API_KEY", "test-key")
    os.environ.setdefault("CAPTCHA_MODEL", "gpt-5.4")
    os.environ.setdefault("CAPTCHA_MULTIMODAL_MODEL", "qwen3.5-2b")
    os.environ.setdefault("BROWSER_HEADLESS", "true")
    if client_key is not None:
        os.environ["CLIENT_KEY"] = client_key

    config_mod = importlib.import_module("src.core.config")
    routes_mod = importlib.import_module("src.api.routes")
    task_mgr_mod = importlib.import_module("src.services.task_manager")
    main_mod = importlib.import_module("src.main")

    _ = importlib.reload(config_mod)
    _ = importlib.reload(task_mgr_mod)
    _ = importlib.reload(routes_mod)
    main_mod = importlib.reload(main_mod)

    return TestClient(getattr(main_mod, "app"))


from src.core.task_types import all_task_type_names  # noqa: E402

# The task roster derives from the single registry, so this list can't drift
# from the solvers main.py registers or the validation sets routes.py uses.
ALL_TASK_TYPES = all_task_type_names()


def test_task_type_registry_is_internally_consistent() -> None:
    """Every registered task type has exactly one validation bucket.

    Guards the drift the single registry was introduced to prevent: routes.py
    keys required-field validation off set membership, so a type present in the
    roster but missing from every validation bucket would silently skip its
    ``websiteURL`` / image / classification checks.
    """
    from src.api import routes
    from src.core.task_types import (
        Provider,
        all_task_type_names,
        types_for_provider,
    )

    names = all_task_type_names()
    # No duplicate task-type strings.
    assert len(names) == len(set(names))

    # The three validation buckets partition the full roster (union == all,
    # pairwise disjoint) so each type gets one and only one field check.
    buckets = [
        routes._BROWSER_TASK_TYPES,
        routes._IMAGE_TASK_TYPES,
        routes._CLASSIFICATION_TASK_TYPES,
    ]
    union: set[str] = set()
    for bucket in buckets:
        assert union.isdisjoint(bucket)
        union |= bucket
    assert union == set(names)

    # Every type maps to exactly one provider (the main.py wiring).
    provider_union: set[str] = set()
    for provider in Provider:
        provider_union |= set(types_for_provider(provider))
    assert provider_union == set(names)


def test_health_endpoint() -> None:
    client = _load_app()
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "cloud_model" in body
    assert "local_model" in body


def test_root_endpoint() -> None:
    client = _load_app()
    response = client.get("/")
    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "captchai"
    assert body["version"] == "3.0.0"
    assert "createTask" in body["endpoints"]
    assert isinstance(body["supported_task_types"], list)


def test_root_endpoint_reports_all_supported_types() -> None:
    client = _load_app()
    task_mgr_mod = importlib.import_module("src.services.task_manager")
    mgr = getattr(task_mgr_mod, "task_manager")
    for task_type in ALL_TASK_TYPES:
        mgr.register_solver(task_type, AsyncMock())
    response = client.get("/")
    body = response.json()
    assert set(body["supported_task_types"]) == set(ALL_TASK_TYPES)


def test_get_balance() -> None:
    client = _load_app()
    response = client.post("/getBalance", json={"clientKey": "any"})
    assert response.status_code == 200
    body = response.json()
    assert body["errorId"] == 0
    assert body["balance"] > 0


def test_get_balance_requires_client_key() -> None:
    client = _load_app(client_key="secret")
    bad = client.post("/getBalance", json={"clientKey": "wrong"})
    good = client.post("/getBalance", json={"clientKey": "secret"})
    assert bad.json()["errorId"] == 1
    assert good.json()["errorId"] == 0


def test_create_task_unsupported_type() -> None:
    client = _load_app()
    response = client.post(
        "/createTask",
        json={
            "clientKey": "any",
            "task": {"type": "UnsupportedType", "websiteURL": "https://example.com"},
        },
    )
    body = response.json()
    assert body["errorId"] == 1
    assert body["errorCode"] == "ERROR_TASK_NOT_SUPPORTED"


def test_create_task_missing_fields_recaptcha_v3() -> None:
    client = _load_app()
    task_mgr_mod = importlib.import_module("src.services.task_manager")
    mgr = getattr(task_mgr_mod, "task_manager")
    mgr.register_solver("RecaptchaV3TaskProxyless", AsyncMock())
    try:
        response = client.post(
            "/createTask",
            json={"clientKey": "any", "task": {"type": "RecaptchaV3TaskProxyless"}},
        )
        body = response.json()
        assert body["errorId"] == 1
        assert body["errorCode"] == "ERROR_TASK_PROPERTY_EMPTY"
    finally:
        mgr._solvers.pop("RecaptchaV3TaskProxyless", None)


def test_create_task_missing_fields_recaptcha_v2() -> None:
    client = _load_app()
    task_mgr_mod = importlib.import_module("src.services.task_manager")
    mgr = getattr(task_mgr_mod, "task_manager")
    mgr.register_solver("NoCaptchaTaskProxyless", AsyncMock())
    try:
        response = client.post(
            "/createTask",
            json={"clientKey": "any", "task": {"type": "NoCaptchaTaskProxyless"}},
        )
        body = response.json()
        assert body["errorId"] == 1
        assert body["errorCode"] == "ERROR_TASK_PROPERTY_EMPTY"
    finally:
        mgr._solvers.pop("NoCaptchaTaskProxyless", None)


def test_create_task_missing_fields_hcaptcha() -> None:
    client = _load_app()
    task_mgr_mod = importlib.import_module("src.services.task_manager")
    mgr = getattr(task_mgr_mod, "task_manager")
    mgr.register_solver("HCaptchaTaskProxyless", AsyncMock())
    try:
        response = client.post(
            "/createTask",
            json={"clientKey": "any", "task": {"type": "HCaptchaTaskProxyless"}},
        )
        body = response.json()
        assert body["errorId"] == 1
        assert body["errorCode"] == "ERROR_TASK_PROPERTY_EMPTY"
    finally:
        mgr._solvers.pop("HCaptchaTaskProxyless", None)


def test_create_task_missing_fields_turnstile() -> None:
    client = _load_app()
    task_mgr_mod = importlib.import_module("src.services.task_manager")
    mgr = getattr(task_mgr_mod, "task_manager")
    mgr.register_solver("TurnstileTaskProxyless", AsyncMock())
    try:
        response = client.post(
            "/createTask",
            json={"clientKey": "any", "task": {"type": "TurnstileTaskProxyless"}},
        )
        body = response.json()
        assert body["errorId"] == 1
        assert body["errorCode"] == "ERROR_TASK_PROPERTY_EMPTY"
    finally:
        mgr._solvers.pop("TurnstileTaskProxyless", None)


def test_create_task_missing_fields_image() -> None:
    client = _load_app()
    task_mgr_mod = importlib.import_module("src.services.task_manager")
    mgr = getattr(task_mgr_mod, "task_manager")
    mgr.register_solver("ImageToTextTask", AsyncMock())
    try:
        response = client.post(
            "/createTask",
            json={"clientKey": "any", "task": {"type": "ImageToTextTask"}},
        )
        body = response.json()
        assert body["errorId"] == 1
        assert body["errorCode"] == "ERROR_TASK_PROPERTY_EMPTY"
    finally:
        mgr._solvers.pop("ImageToTextTask", None)


def test_create_task_missing_fields_classification() -> None:
    client = _load_app()
    task_mgr_mod = importlib.import_module("src.services.task_manager")
    mgr = getattr(task_mgr_mod, "task_manager")
    mgr.register_solver("HCaptchaClassification", AsyncMock())
    try:
        response = client.post(
            "/createTask",
            json={"clientKey": "any", "task": {"type": "HCaptchaClassification"}},
        )
        body = response.json()
        assert body["errorId"] == 1
        assert body["errorCode"] == "ERROR_TASK_PROPERTY_EMPTY"
    finally:
        mgr._solvers.pop("HCaptchaClassification", None)


def test_create_task_invalid_client_key() -> None:
    client = _load_app(client_key="correct-key")
    response = client.post(
        "/createTask",
        json={
            "clientKey": "wrong-key",
            "task": {
                "type": "RecaptchaV3TaskProxyless",
                "websiteURL": "https://example.com",
                "websiteKey": "key123",
            },
        },
    )
    body = response.json()
    assert body["errorId"] == 1
    assert body["errorCode"] == "ERROR_KEY_DOES_NOT_EXIST"


def test_get_task_result_not_found() -> None:
    client = _load_app()
    response = client.post(
        "/getTaskResult",
        json={"clientKey": "any", "taskId": "nonexistent-id"},
    )
    body = response.json()
    assert body["errorId"] == 1
    assert body["errorCode"] == "ERROR_NO_SUCH_CAPCHA_ID"


def test_create_recaptcha_v3_task_accepted() -> None:
    client = _load_app()
    task_mgr_mod = importlib.import_module("src.services.task_manager")
    mgr = getattr(task_mgr_mod, "task_manager")
    mock_solver = AsyncMock(return_value={"gRecaptchaResponse": "tok"})
    mock_solver.solve = mock_solver
    mgr.register_solver("RecaptchaV3TaskProxyless", mock_solver)
    try:
        resp = client.post(
            "/createTask",
            json={
                "clientKey": "any",
                "task": {
                    "type": "RecaptchaV3TaskProxyless",
                    "websiteURL": "https://example.com",
                    "websiteKey": "test-key",
                },
            },
        )
        body = resp.json()
        assert body["errorId"] == 0
        assert body["taskId"] is not None
    finally:
        mgr._solvers.pop("RecaptchaV3TaskProxyless", None)


def test_create_turnstile_task_accepted() -> None:
    client = _load_app()
    task_mgr_mod = importlib.import_module("src.services.task_manager")
    mgr = getattr(task_mgr_mod, "task_manager")
    mock_solver = AsyncMock(return_value={"token": "cf-tok"})
    mock_solver.solve = mock_solver
    mgr.register_solver("TurnstileTaskProxyless", mock_solver)
    try:
        resp = client.post(
            "/createTask",
            json={
                "clientKey": "any",
                "task": {
                    "type": "TurnstileTaskProxyless",
                    "websiteURL": "https://example.com",
                    "websiteKey": "1x000",
                },
            },
        )
        body = resp.json()
        assert body["errorId"] == 0
        assert body["taskId"] is not None
    finally:
        mgr._solvers.pop("TurnstileTaskProxyless", None)


def test_create_turnstile_task_accepts_proxy_and_advanced_fields() -> None:
    """Proxy / action / cData / userAgent must be accepted and forwarded."""
    client = _load_app()
    task_mgr_mod = importlib.import_module("src.services.task_manager")
    mgr = getattr(task_mgr_mod, "task_manager")
    mock_solver = AsyncMock(return_value={"token": "cf-tok", "userAgent": "UA"})
    mock_solver.solve = mock_solver
    mgr.register_solver("TurnstileTaskProxyless", mock_solver)
    try:
        resp = client.post(
            "/createTask",
            json={
                "clientKey": "any",
                "task": {
                    "type": "TurnstileTaskProxyless",
                    "websiteURL": "https://example.com",
                    "websiteKey": "0x4AAA",
                    "action": "login",
                    "cData": "session-123",
                    "userAgent": "Mozilla/5.0 Custom",
                    "proxyType": "http",
                    "proxyAddress": "1.2.3.4",
                    "proxyPort": 8080,
                    "proxyLogin": "u",
                    "proxyPassword": "p",
                },
            },
        )
        body = resp.json()
        assert body["errorId"] == 0
        assert body["taskId"] is not None
        forwarded = mock_solver.call_args.args[0]
        assert forwarded["action"] == "login"
        assert forwarded["cData"] == "session-123"
        assert forwarded["proxyAddress"] == "1.2.3.4"
        assert forwarded["userAgent"] == "Mozilla/5.0 Custom"
    finally:
        mgr._solvers.pop("TurnstileTaskProxyless", None)


def test_get_task_result_returns_user_agent_in_solution() -> None:
    """solution.userAgent must survive serialization for token/UA binding."""
    import time

    client = _load_app()
    task_mgr_mod = importlib.import_module("src.services.task_manager")
    mgr = getattr(task_mgr_mod, "task_manager")
    mock_solver = AsyncMock(return_value={"token": "cf-tok", "userAgent": "UA-XYZ"})
    mock_solver.solve = mock_solver
    mgr.register_solver("TurnstileTaskProxyless", mock_solver)
    try:
        created = client.post(
            "/createTask",
            json={
                "clientKey": "any",
                "task": {
                    "type": "TurnstileTaskProxyless",
                    "websiteURL": "https://example.com",
                    "websiteKey": "0x4AAA",
                },
            },
        ).json()
        task_id = created["taskId"]
        for _ in range(20):
            result = client.post(
                "/getTaskResult",
                json={"clientKey": "any", "taskId": task_id},
            ).json()
            if result.get("status") == "ready":
                break
            time.sleep(0.05)
        assert result["status"] == "ready"
        assert result["solution"]["token"] == "cf-tok"
        assert result["solution"]["userAgent"] == "UA-XYZ"
    finally:
        mgr._solvers.pop("TurnstileTaskProxyless", None)


def test_create_classification_task_accepted() -> None:
    client = _load_app()
    task_mgr_mod = importlib.import_module("src.services.task_manager")
    mgr = getattr(task_mgr_mod, "task_manager")
    mock_solver = AsyncMock(return_value={"objects": [0, 3]})
    mock_solver.solve = mock_solver
    mgr.register_solver("ReCaptchaV2Classification", mock_solver)
    try:
        resp = client.post(
            "/createTask",
            json={
                "clientKey": "any",
                "task": {
                    "type": "ReCaptchaV2Classification",
                    "image": "aGVsbG8=",
                    "question": "Select traffic lights",
                },
            },
        )
        body = resp.json()
        assert body["errorId"] == 0
        assert body["taskId"] is not None
    finally:
        mgr._solvers.pop("ReCaptchaV2Classification", None)
