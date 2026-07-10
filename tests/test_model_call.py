"""Tests for the shared model-call seam and the free-form vision adapters.

Candidate 2 of the architecture review: ``ClassificationSolver`` and
``CaptchaRecognizer`` used to build their own model client, retry loop and JSON
parser, and — the bug — never metered their spend to the cost ledger nor passed
the budget gate. These tests pin the new behaviour: both planes now route
through one :class:`~src.parsing.model_call.ModelInvoker`, so their spend reaches
the ledger and a cloud fallback is budget-gated.

Repo convention: plain ``def test_*`` driving coroutines with ``asyncio.run``
(no pytest-asyncio); fakes injected instead of a real client/network.
"""

from __future__ import annotations

import asyncio
import base64
import io
import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    _ = sys.path.insert(0, str(PROJECT_ROOT))

from src.assets.model_pool import ModelUsage  # noqa: E402
from src.parsing.model_call import (  # noqa: E402
    ModelCallRequest,
    ModelInvoker,
    encode_png_data_urls,
    is_connection_error,
)
from src.services.classification import ClassificationSolver  # noqa: E402
from src.services.recognition import CaptchaRecognizer  # noqa: E402
from src.services.vision_solver import parse_json_object  # noqa: E402


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class FakeClient:
    def __init__(self, name, content="{}", *, usage=(1, 1), error=None):
        self.name = name
        self._content = content
        self._usage = usage
        self._error = error
        self.calls = 0

    async def chat(self, *, messages, temperature, max_tokens, timeout=None):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._content, ModelUsage(*self._usage)


class FakePool:
    def __init__(self, local, cloud=None):
        self._clients = {"local": local}
        if cloud is not None:
            self._clients["cloud"] = cloud

    def get(self, name):
        return self._clients[name]


class FakeBudget:
    def __init__(self, allowed=True, downgrade_to=None):
        self._allowed = allowed
        self._downgrade_to = downgrade_to
        self.calls = []

    async def check(self, client_key, est_cost, *, model=None):
        self.calls.append((client_key, est_cost, model))
        return SimpleNamespace(allowed=self._allowed, downgrade_to=self._downgrade_to)


class FakeLedger:
    def __init__(self):
        self.records = []

    async def record(self, rec):
        self.records.append(rec)


def _cfg(**over):
    base = dict(
        captcha_retries=3,
        captcha_timeout=30,
        vision_cloud_enabled=True,
        model_connection_fallback=True,
        local_model="qwen-local",
        cloud_model="gpt-cloud",
        local_base_url="http://local",
        local_api_key="k",
        cloud_base_url="http://cloud",
        cloud_api_key="k",
    )
    base.update(over)
    return SimpleNamespace(**base)


def _png_b64() -> str:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (120, 80, 40)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _req(**over):
    base = dict(
        system_prompt="sys",
        user_text="hi",
        image_urls=["data:image/png;base64,AAAA"],
        tier=1,
        est_cost=0.002,
    )
    base.update(over)
    return ModelCallRequest(**base)


# --------------------------------------------------------------------------- #
# ModelInvoker: routing + budget + fallback
# --------------------------------------------------------------------------- #


def test_route_maps_tier_to_backend():
    inv = ModelInvoker(FakePool(FakeClient("local")), _cfg(vision_cloud_enabled=True))
    assert inv.route(1) == "local"
    assert inv.route(2) == "cloud"
    inv2 = ModelInvoker(FakePool(FakeClient("local")), _cfg(vision_cloud_enabled=False))
    assert inv2.route(2) == "local"  # cloud disabled → stay local


def test_invoke_returns_content_usage_and_served_model():
    pool = FakePool(FakeClient("local", '{"ok":1}', usage=(9, 3)))
    inv = ModelInvoker(pool, _cfg())
    result = asyncio.run(inv.invoke(_req(tier=1)))
    assert result.content == '{"ok":1}'
    assert result.model == "local"
    assert result.usage == ModelUsage(9, 3)


def test_invoke_connection_error_falls_back_to_cloud():
    local = FakeClient("local", error=ConnectionError("local down"))
    cloud = FakeClient("cloud", '{"via":"cloud"}', usage=(4, 2))
    inv = ModelInvoker(FakePool(local, cloud), _cfg())
    result = asyncio.run(inv.invoke(_req(tier=1)))
    assert result.model == "cloud"
    assert result.content == '{"via":"cloud"}'
    assert cloud.calls == 1


def test_invoke_cloud_fallback_blocked_by_budget_denial():
    local = FakeClient("local", error=ConnectionError("down"))
    cloud = FakeClient("cloud", '{"via":"cloud"}')
    budget = FakeBudget(allowed=False)  # deny the local→cloud fallback
    inv = ModelInvoker(FakePool(local, cloud), _cfg(), budget=budget)
    try:
        asyncio.run(inv.invoke(_req(tier=1)))
        raise AssertionError("expected the connection error to propagate")
    except ConnectionError:
        pass
    assert cloud.calls == 0  # budget refused the paid fallback


def test_guard_budget_downgrades_cloud_to_local():
    budget = FakeBudget(allowed=False, downgrade_to="local")
    inv = ModelInvoker(FakePool(FakeClient("local")), _cfg(), budget=budget)
    chosen = asyncio.run(inv.guard_budget("cloud", 0.01, "ck"))
    assert chosen == "local"
    assert budget.calls[0][2] == "cloud"


def test_guard_budget_ignores_local_calls():
    budget = FakeBudget(allowed=False)
    inv = ModelInvoker(FakePool(FakeClient("local")), _cfg(), budget=budget)
    # A local (free) call must never consult the budget.
    assert asyncio.run(inv.guard_budget("local", 0.01, "ck")) == "local"
    assert budget.calls == []


def test_is_connection_error_matches_by_class_name():
    assert is_connection_error(ConnectionError("x"))
    assert is_connection_error(TimeoutError("x"))
    assert not is_connection_error(ValueError("x"))


def test_encode_png_data_urls_roundtrip():
    urls = encode_png_data_urls([b"\x89PNG..."])
    assert urls[0].startswith("data:image/png;base64,")


# --------------------------------------------------------------------------- #
# Shared JSON parser (deletes two byte-identical copies)
# --------------------------------------------------------------------------- #


def test_parse_json_object_handles_fence():
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_object('{"b": 2}') == {"b": 2}


def test_parse_json_object_rejects_non_object():
    try:
        parse_json_object("[1, 2, 3]")
        raise AssertionError("expected ValueError for a non-object reply")
    except ValueError:
        pass


# --------------------------------------------------------------------------- #
# ClassificationSolver as an adapter over the seam
# --------------------------------------------------------------------------- #


def test_classification_spend_reaches_ledger():
    """The bug the seam closes: pure-vision spend is now metered to the ledger."""
    ledger = FakeLedger()
    local = FakeClient("local", '{"objects":[0,3]}', usage=(12, 4))
    solver = ClassificationSolver(
        _cfg(), model_pool=FakePool(local), ledger=ledger
    )
    params = {
        "type": "ReCaptchaV2Classification",
        "image": _png_b64(),
        "question": "select traffic lights",
        "_taskId": "task-1",
        "_clientKey": "acct-9",
    }
    result = asyncio.run(solver.solve(params))
    assert result == {"objects": [0, 3]}
    assert len(ledger.records) == 1
    rec = ledger.records[0]
    assert rec.task_id == "task-1"
    assert rec.task_type == "ReCaptchaV2Classification"
    assert rec.model == "local"
    assert rec.input_tokens == 12 and rec.output_tokens == 4
    assert rec.outcome == "ready"
    assert rec.client_key == "acct-9"
    assert rec.challenge_shape == "classification"


def test_classification_cloud_fallback_is_budget_gated():
    """Local down + budget denies cloud → no cloud call, a failed record is kept."""
    ledger = FakeLedger()
    local = FakeClient("local", error=ConnectionError("local model down"))
    cloud = FakeClient("cloud", '{"objects":[1]}')
    budget = FakeBudget(allowed=False)
    solver = ClassificationSolver(
        _cfg(captcha_retries=1),
        model_pool=FakePool(local, cloud),
        ledger=ledger,
        budget=budget,
    )
    params = {"type": "HCaptchaClassification", "image": _png_b64(), "_taskId": "t"}
    try:
        asyncio.run(solver.solve(params))
        raise AssertionError("expected the solve to fail when cloud is denied")
    except RuntimeError:
        pass
    assert cloud.calls == 0  # the paid fallback was refused by the budget
    assert ledger.records[-1].outcome == "failed"


def test_classification_without_ledger_still_solves():
    """A bare-config solver (no services) skips metering but still works."""
    local = FakeClient("local", '{"answer": true}', usage=(2, 1))
    solver = ClassificationSolver(_cfg(), model_pool=FakePool(local))
    result = asyncio.run(
        solver.solve({"type": "HCaptchaClassification", "image": _png_b64()})
    )
    assert result == {"answer": True}


# --------------------------------------------------------------------------- #
# CaptchaRecognizer as an adapter over the seam
# --------------------------------------------------------------------------- #


def test_recognition_spend_reaches_ledger_and_returns_text():
    ledger = FakeLedger()
    payload = '{"captcha_type":"click","clicks":[{"x":1,"y":2}]}'
    local = FakeClient("local", payload, usage=(20, 6))
    solver = CaptchaRecognizer(_cfg(), model_pool=FakePool(local), ledger=ledger)
    params = {"type": "ImageToTextTask", "body": _png_b64(), "_taskId": "img-1"}
    result = asyncio.run(solver.solve(params))
    # solve wraps the parsed JSON back into a {"text": <json string>} envelope.
    assert parse_json_object(result["text"])["captcha_type"] == "click"
    assert len(ledger.records) == 1
    rec = ledger.records[0]
    assert rec.model == "local"
    assert rec.input_tokens == 20 and rec.output_tokens == 6
    assert rec.challenge_shape == "image_to_text"


def test_recognition_falls_back_to_cloud_and_meters_it():
    """Local down + cloud allowed → recognition transparently solves on cloud."""
    ledger = FakeLedger()
    local = FakeClient("local", error=TimeoutError("local timed out"))
    cloud = FakeClient("cloud", '{"captcha_type":"slide"}', usage=(30, 9))
    solver = CaptchaRecognizer(
        _cfg(), model_pool=FakePool(local, cloud), ledger=ledger, budget=FakeBudget(True)
    )
    params = {"type": "ImageToTextTask", "body": _png_b64(), "_taskId": "img-2"}
    result = asyncio.run(solver.solve(params))
    assert parse_json_object(result["text"])["captcha_type"] == "slide"
    assert cloud.calls == 1
    rec = ledger.records[0]
    assert rec.model == "cloud"  # metered against the backend that served it
    assert rec.input_tokens == 30 and rec.output_tokens == 9


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok")
