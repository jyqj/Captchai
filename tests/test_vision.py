"""Tests for the vision routing / model pool layer.

No network: a fake AsyncOpenAI-compatible client is injected into ``ModelPool``
via ``client_factory``. Coroutines are driven with ``asyncio.run`` (this project
has no pytest-asyncio).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    _ = sys.path.insert(0, str(PROJECT_ROOT))

from src.assets.model_pool import ModelClient, ModelPool, ModelUsage
from src.parsing.vision import VisionRequest, VisionResult, VisionRouter


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

def _fake_response(content: str, prompt_tokens: int = 11, completion_tokens: int = 7):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
        ),
    )


class FakeCompletions:
    """Stands in for ``client.chat.completions`` with a scripted responder."""

    def __init__(self, base_url, script, recorder):
        self._base_url = base_url
        self._script = script
        self._recorder = recorder

    async def create(self, **kwargs):
        call_index = len(self._recorder)
        self._recorder.append(
            {
                "base_url": self._base_url,
                "model": kwargs.get("model"),
                "temperature": kwargs.get("temperature"),
                "messages": kwargs.get("messages"),
            }
        )
        content, prompt_tokens, completion_tokens = self._script(kwargs, call_index)
        return _fake_response(content, prompt_tokens, completion_tokens)


class FakeClient:
    def __init__(self, base_url, script, recorder):
        self.base_url = base_url
        self.chat = SimpleNamespace(
            completions=FakeCompletions(base_url, script, recorder)
        )


def _make_pool(config, script):
    """Build a ModelPool whose clients draw from a scripted, recorded fake."""
    recorder: list = []

    def factory(base_url, api_key):
        return FakeClient(base_url, script, recorder)

    return ModelPool(config, client_factory=factory), recorder


def _const_script(content, prompt_tokens=11, completion_tokens=7):
    def script(kwargs, call_index):
        return content, prompt_tokens, completion_tokens

    return script


def _seq_script(contents, prompt_tokens=10, completion_tokens=5):
    def script(kwargs, call_index):
        content = contents[min(call_index, len(contents) - 1)]
        return content, prompt_tokens, completion_tokens

    return script


def _make_config(**overrides):
    base = dict(
        cloud_base_url="http://cloud",
        cloud_api_key="cloud-key",
        cloud_model="gpt-cloud",
        local_base_url="http://local",
        local_api_key="local-key",
        local_model="qwen-local",
        vision_cloud_enabled=True,
        vision_vote_samples=3,
        vision_confidence_threshold=0.6,
        vision_tier2_detail="high",
        captcha_timeout=30,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _req(**overrides):
    base = dict(prompt="select all buses", images=[b"\x89PNGfake"], task_tier=1)
    base.update(overrides)
    return VisionRequest(**base)


class FakeBudget:
    def __init__(self, allowed=True, downgrade_to=None):
        self._allowed = allowed
        self._downgrade_to = downgrade_to
        self.calls: list = []

    async def check(self, client_key, est_cost, *, model=None):
        self.calls.append((client_key, est_cost, model))
        return SimpleNamespace(
            allowed=self._allowed, downgrade_to=self._downgrade_to
        )


# ---------------------------------------------------------------------------
# ModelPool
# ---------------------------------------------------------------------------

def test_pool_exposes_local_and_cloud():
    config = _make_config()
    pool, _ = _make_pool(config, _const_script('{"indices":[],"confidence":1.0}'))
    assert isinstance(pool.local, ModelClient)
    assert isinstance(pool.cloud, ModelClient)
    assert pool.local.model == "qwen-local"
    assert pool.cloud.model == "gpt-cloud"
    assert pool.get("local") is pool.local
    assert pool.get("cloud") is pool.cloud


def test_model_client_chat_returns_content_and_usage():
    config = _make_config()
    pool, _ = _make_pool(
        config, _const_script('{"indices":[1],"confidence":0.9}', 21, 8)
    )

    async def run():
        return await pool.local.chat(messages=[{"role": "user", "content": "hi"}])

    content, usage = asyncio.run(run())
    assert content == '{"indices":[1],"confidence":0.9}'
    assert usage == ModelUsage(input_tokens=21, output_tokens=8)


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def test_route_tier1_uses_local():
    config = _make_config()
    pool, recorder = _make_pool(
        config, _const_script('{"indices":[0],"confidence":0.95}')
    )
    router = VisionRouter(pool, config)
    result = asyncio.run(router.classify(_req(task_tier=1)))
    assert result.model == "local"
    assert recorder[0]["model"] == "qwen-local"


def test_route_tier2_cloud_enabled_uses_cloud():
    config = _make_config(vision_cloud_enabled=True)
    pool, recorder = _make_pool(
        config, _const_script('{"indices":[0],"confidence":0.95}')
    )
    router = VisionRouter(pool, config)
    result = asyncio.run(router.classify(_req(task_tier=2, grid_size=9)))
    assert result.model == "cloud"
    assert recorder[0]["model"] == "gpt-cloud"


def test_route_tier2_cloud_disabled_uses_local():
    config = _make_config(vision_cloud_enabled=False)
    pool, recorder = _make_pool(
        config, _const_script('{"indices":[0],"confidence":0.95}')
    )
    router = VisionRouter(pool, config)
    result = asyncio.run(router.classify(_req(task_tier=2, grid_size=9)))
    assert result.model == "local"
    assert recorder[0]["model"] == "qwen-local"


# ---------------------------------------------------------------------------
# Single-shot & voting
# ---------------------------------------------------------------------------

def test_single_shot_high_confidence_no_voting():
    config = _make_config()
    pool, recorder = _make_pool(
        config, _const_script('{"indices":[1,3],"confidence":0.95}')
    )
    router = VisionRouter(pool, config)
    result = asyncio.run(router.classify(_req(task_tier=2, grid_size=9)))
    assert result.indices == [1, 3]
    assert result.votes == 1
    assert result.confidence == 0.95
    # Exactly one call: no escalation.
    assert len(recorder) == 1


def test_low_confidence_tier2_triggers_majority_voting():
    config = _make_config(vision_vote_samples=3, vision_confidence_threshold=0.6)
    contents = [
        '{"indices":[1],"confidence":0.2}',  # first pass: low confidence
        '{"indices":[1,2],"confidence":0.9}',  # vote sample 1
        '{"indices":[1,3],"confidence":0.9}',  # vote sample 2
        '{"indices":[1,2],"confidence":0.9}',  # vote sample 3
    ]
    pool, recorder = _make_pool(config, _seq_script(contents))
    router = VisionRouter(pool, config)
    result = asyncio.run(router.classify(_req(task_tier=2, grid_size=9)))

    # tile 1 (3/3) and tile 2 (2/3) win the majority; tile 3 (1/3) loses.
    assert result.indices == [1, 2]
    assert result.votes == 3
    # 1 initial call + 3 vote samples.
    assert len(recorder) == 4
    # Vote samples run at a temperature > 0.
    assert recorder[1]["temperature"] > 0
    # Confidence is the mean agreement ratio across candidate tiles.
    assert 0.0 <= result.confidence <= 1.0


def test_low_confidence_tier1_does_not_vote():
    config = _make_config(vision_vote_samples=3, vision_confidence_threshold=0.6)
    pool, recorder = _make_pool(
        config, _const_script('{"indices":[0],"confidence":0.1}')
    )
    router = VisionRouter(pool, config)
    result = asyncio.run(router.classify(_req(task_tier=1)))
    assert result.votes == 1
    assert len(recorder) == 1


# ---------------------------------------------------------------------------
# Budget downgrade
# ---------------------------------------------------------------------------

def test_budget_denial_downgrades_to_local():
    config = _make_config(vision_cloud_enabled=True)
    pool, recorder = _make_pool(
        config, _const_script('{"indices":[0],"confidence":0.95}')
    )
    budget = FakeBudget(allowed=False, downgrade_to="local")
    router = VisionRouter(pool, config, budget=budget)
    result = asyncio.run(
        router.classify(_req(task_tier=2, grid_size=9), client_key="acct-1")
    )
    assert result.model == "local"
    assert recorder[0]["model"] == "qwen-local"
    # Budget was consulted for the (would-be) cloud call.
    assert budget.calls
    assert budget.calls[0][0] == "acct-1"
    assert budget.calls[0][2] == "cloud"


def test_budget_allowed_keeps_cloud():
    config = _make_config(vision_cloud_enabled=True)
    pool, recorder = _make_pool(
        config, _const_script('{"indices":[2],"confidence":0.95}')
    )
    budget = FakeBudget(allowed=True)
    router = VisionRouter(pool, config, budget=budget)
    result = asyncio.run(router.classify(_req(task_tier=2, grid_size=9)))
    assert result.model == "cloud"
    assert recorder[0]["model"] == "gpt-cloud"


# ---------------------------------------------------------------------------
# Parsing robustness
# ---------------------------------------------------------------------------

def test_parse_fenced_json():
    config = _make_config()
    content = '```json\n{"indices": [0, 4], "confidence": 0.8}\n```'
    pool, _ = _make_pool(config, _const_script(content))
    router = VisionRouter(pool, config)
    result = asyncio.run(router.classify(_req(task_tier=1)))
    assert result.indices == [0, 4]
    assert result.confidence == 0.8


def test_parse_missing_confidence_defaults():
    config = _make_config()
    pool, _ = _make_pool(config, _const_script('{"indices":[2]}'))
    router = VisionRouter(pool, config)
    result = asyncio.run(router.classify(_req(task_tier=1)))
    assert result.indices == [2]
    assert result.confidence == 0.5


def test_parse_regex_fallback_non_json():
    config = _make_config()
    content = "The matching indices are [3, 5, 7] with confidence 0.42."
    pool, _ = _make_pool(config, _const_script(content))
    router = VisionRouter(pool, config)
    result = asyncio.run(router.classify(_req(task_tier=1)))
    assert result.indices == [3, 5, 7]
    assert result.confidence == 0.42


# ---------------------------------------------------------------------------
# Usage capture
# ---------------------------------------------------------------------------

def test_usage_tokens_captured_into_result():
    config = _make_config()
    pool, _ = _make_pool(
        config, _const_script('{"indices":[1],"confidence":0.95}', 33, 12)
    )
    router = VisionRouter(pool, config)
    result = asyncio.run(router.classify(_req(task_tier=1)))
    assert isinstance(result.usage, ModelUsage)
    assert result.usage.input_tokens == 33
    assert result.usage.output_tokens == 12


def test_usage_accumulates_across_votes():
    config = _make_config(vision_vote_samples=3, vision_confidence_threshold=0.6)
    contents = [
        '{"indices":[1],"confidence":0.2}',
        '{"indices":[1,2],"confidence":0.9}',
        '{"indices":[1,2],"confidence":0.9}',
        '{"indices":[1,2],"confidence":0.9}',
    ]
    pool, _ = _make_pool(config, _seq_script(contents, prompt_tokens=10, completion_tokens=5))
    router = VisionRouter(pool, config)
    result = asyncio.run(router.classify(_req(task_tier=2, grid_size=9)))
    # 4 calls total => 4 * (10 in, 5 out).
    assert result.usage.input_tokens == 40
    assert result.usage.output_tokens == 20
