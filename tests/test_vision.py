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

def test_hard_grid_votes_even_at_high_self_reported_confidence():
    """A tier-2 grid is voted on regardless of the model's self-reported
    confidence — a single call's own number isn't trusted to finalise the grid.

    (Was ``test_single_shot_high_confidence_no_voting``: the old behaviour
    returned after one call when the model self-reported ≥ threshold. That is
    exactly the "confidently wrong" hole the agreement gate closes.)
    """
    config = _make_config()
    pool, recorder = _make_pool(
        config, _const_script('{"indices":[1,3],"confidence":0.95}')
    )
    router = VisionRouter(pool, config)
    result = asyncio.run(router.classify(_req(task_tier=2, grid_size=9)))
    assert result.indices == [1, 3]
    # 1 initial + 3 vote samples (self-consistency), not a single call.
    assert result.votes == 3
    assert len(recorder) == 4
    # Confidence is cross-sample agreement (all samples agreed → 1.0), not 0.95.
    assert result.confidence == 1.0


def test_hard_grid_agreement_overrides_confidently_wrong_initial():
    """When samples DISAGREE, the majority (agreement) answer wins over the
    initial single call's confidently-self-reported answer."""
    contents = [
        '{"indices":[0],"confidence":0.99}',  # initial (old gate would trust this)
        '{"indices":[5],"confidence":0.99}',  # vote 1
        '{"indices":[5],"confidence":0.99}',  # vote 2
        '{"indices":[7],"confidence":0.99}',  # vote 3
    ]
    config = _make_config(vision_vote_samples=3, vision_confidence_threshold=0.6)
    pool, recorder = _make_pool(config, _seq_script(contents))
    router = VisionRouter(pool, config)
    result = asyncio.run(router.classify(_req(task_tier=2, grid_size=9)))
    # Majority of the 3 votes picked tile 5 (2/3); the initial [0] is discarded.
    assert result.indices == [5]
    assert result.votes == 3
    assert len(recorder) == 4


def test_trust_self_confidence_restores_single_call_gate():
    """VISION_TRUST_SELF_CONFIDENCE=true restores the legacy single-call gate:
    a high self-reported confidence short-circuits voting even on tier 2."""
    config = _make_config(vision_trust_self_confidence=True)
    pool, recorder = _make_pool(
        config, _const_script('{"indices":[1,3],"confidence":0.95}')
    )
    router = VisionRouter(pool, config)
    result = asyncio.run(router.classify(_req(task_tier=2, grid_size=9)))
    assert result.indices == [1, 3]
    assert result.votes == 1
    assert result.confidence == 0.95
    # Exactly one call: self-report ≥ threshold short-circuits voting.
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


# ---------------------------------------------------------------------------
# Model concurrency semaphore + connection-error backend fallback
# ---------------------------------------------------------------------------


def test_model_client_bounds_concurrency():
    """max_concurrency caps simultaneous in-flight chat() calls."""
    import asyncio as _asyncio

    from src.assets.model_pool import ModelClient, ModelUsage

    state = {"in_flight": 0, "peak": 0}

    class _SlowCompletions:
        async def create(self, **kwargs):
            state["in_flight"] += 1
            state["peak"] = max(state["peak"], state["in_flight"])
            await _asyncio.sleep(0.02)
            state["in_flight"] -= 1
            return _fake_response('{"indices":[],"confidence":1.0}')

    def factory(base_url, api_key):
        return SimpleNamespace(chat=SimpleNamespace(completions=_SlowCompletions()))

    async def run():
        client = ModelClient(
            name="cloud",
            model="m",
            base_url="u",
            api_key="k",
            client_factory=factory,
            max_concurrency=2,
        )
        await _asyncio.gather(
            *(client.chat(messages=[{"role": "user", "content": "x"}]) for _ in range(8))
        )

    asyncio.run(run())
    # Never more than 2 concurrent calls despite 8 being launched at once.
    assert state["peak"] <= 2


def test_local_connection_error_falls_back_to_cloud():
    """A dead local backend transparently falls back to cloud (tier-1)."""
    from src.assets.model_pool import ModelUsage

    class _DeadLocalClient:
        name = "local"

        async def chat(self, **kwargs):
            raise ConnectionError("local model service down")

    class _CloudClient:
        name = "cloud"

        async def chat(self, **kwargs):
            return '{"indices":[2,4],"confidence":0.9}', ModelUsage(5, 2)

    class _Pool:
        def get(self, name):
            return _DeadLocalClient() if name == "local" else _CloudClient()

    config = _make_config(
        vision_cloud_enabled=True,
        vision_stitch_grid=False,
        model_connection_fallback=True,
    )
    router = VisionRouter(_Pool(), config)
    result = asyncio.run(router.classify(_req(task_tier=1)))
    assert result.model == "cloud"  # fell back to cloud
    assert result.indices == [2, 4]


def test_cloud_connection_error_falls_back_to_local():
    """A dead cloud backend falls back to the free local model (tier-2)."""
    from src.assets.model_pool import ModelUsage

    class _DeadCloudClient:
        name = "cloud"

        async def chat(self, **kwargs):
            raise TimeoutError("cloud timed out")

    class _LocalClient:
        name = "local"

        async def chat(self, **kwargs):
            return '{"indices":[1],"confidence":0.8}', ModelUsage(3, 1)

    class _Pool:
        def get(self, name):
            return _DeadCloudClient() if name == "cloud" else _LocalClient()

    config = _make_config(
        vision_cloud_enabled=True,
        vision_stitch_grid=False,
        model_connection_fallback=True,
    )
    router = VisionRouter(_Pool(), config)
    result = asyncio.run(router.classify(_req(task_tier=2, grid_size=9)))
    assert result.model == "local"
    assert result.indices == [1]


def test_connection_fallback_disabled_reraises():
    """With fallback disabled, a connection error propagates (no silent swap)."""
    class _DeadLocalClient:
        name = "local"

        async def chat(self, **kwargs):
            raise ConnectionError("down")

    class _Pool:
        def get(self, name):
            return _DeadLocalClient()

    config = _make_config(
        vision_cloud_enabled=True,
        vision_stitch_grid=False,
        model_connection_fallback=False,
    )
    router = VisionRouter(_Pool(), config)
    try:
        asyncio.run(router.classify(_req(task_tier=1)))
        raise AssertionError("expected the connection error to propagate")
    except ConnectionError:
        pass


def test_non_connection_error_does_not_fall_back():
    """A non-connection error (e.g. ValueError) is not retried on the other backend."""
    class _BadLocalClient:
        name = "local"

        async def chat(self, **kwargs):
            raise ValueError("malformed request")

    class _Pool:
        def __init__(self):
            self.cloud_calls = 0

        def get(self, name):
            if name == "cloud":
                self.cloud_calls += 1
            return _BadLocalClient()

    pool = _Pool()
    config = _make_config(
        vision_cloud_enabled=True,
        vision_stitch_grid=False,
        model_connection_fallback=True,
    )
    router = VisionRouter(pool, config)
    try:
        asyncio.run(router.classify(_req(task_tier=1)))
        raise AssertionError("expected the ValueError to propagate")
    except ValueError:
        pass
    assert pool.cloud_calls == 0  # no fallback attempted for a non-connection error


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
