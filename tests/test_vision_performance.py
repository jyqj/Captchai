"""WP4 — performance tests for the vision router and the browser resource handler.

Three concerns are covered here:

* **Concurrent voting** — ``VisionRouter._vote`` runs vote samples via
  ``asyncio.gather`` when ``VISION_VOTE_CONCURRENT=true`` and serially when
  false. A scripted client records peak in-flight concurrency so we can
  assert the gather path actually overlaps the samples.
* **Inline tier-1 → tier-2 escalation** — when a tier-1 (local) call returns
  low confidence, the router retries on the cloud model in-process. The cloud
  result wins, both usages accumulate, and a budget denial downgrades back to
  the local result without ever calling the cloud client.
* **Resource interception** — ``BrowserManager._resource_handler`` aborts
  bandwidth-heavy resource types, allowlists challenge hosts (hcaptcha /
  cloudflare / google), blocklists known trackers, and lets documents through.
  This is the highest-risk regression surface: blocking challenge tile images
  would break solving.

No network: a fake client whose ``chat`` is an awaitable recorder drives the
vision router, and a fake route/request drives the resource handler.
``asyncio.run`` is used directly (this project has no pytest-asyncio).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.assets.model_pool import ModelUsage  # noqa: E402
from src.parsing.vision import VisionRequest, VisionRouter  # noqa: E402
from src.services.browser import BrowserManager  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class ScriptedConcurrencyClient:
    """A model client that records peak concurrency and scripts responses.

    Each ``chat`` call captures its script index *before* the first await so
    concurrent calls don't race on the counter. ``delay`` is a small sleep
    inside each call so the gather-launched coroutines overlap visibly.
    """

    def __init__(self, name, contents, *, delay=0.05, usage=None):
        self.name = name
        self._contents = list(contents)
        self._delay = delay
        self._usage = usage or ModelUsage(input_tokens=1, output_tokens=1)
        self.in_flight = 0
        self.max_in_flight = 0
        self.call_count = 0

    async def chat(self, *, messages, temperature, max_tokens, timeout):
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        idx = self.call_count
        self.call_count += 1
        content = self._contents[min(idx, len(self._contents) - 1)]
        try:
            await asyncio.sleep(self._delay)
            return content, self._usage
        finally:
            self.in_flight -= 1


class FakePool:
    """Stand-in for ModelPool: returns scripted clients by name."""

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
        self.calls: list = []

    async def check(self, client_key, est_cost, *, model=None):
        self.calls.append((client_key, est_cost, model))
        return SimpleNamespace(allowed=self._allowed, downgrade_to=self._downgrade_to)


class FakeRequest:
    def __init__(self, url, resource_type):
        self.url = url
        self.resource_type = resource_type


class FakeRoute:
    """Records the terminal action (continue/abort) chosen by the handler."""

    def __init__(self, url, resource_type):
        self.request = FakeRequest(url, resource_type)
        self.action = None

    async def continue_(self):
        self.action = "continue"

    async def abort(self):
        self.action = "abort"

    async def fulfill(self, **kwargs):
        self.action = "fulfill"


# ---------------------------------------------------------------------------
# Config / request helpers
# ---------------------------------------------------------------------------


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
        vision_vote_concurrent=True,
        vision_inline_escalate=True,
        captcha_timeout=30,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _req(**overrides):
    base = dict(prompt="select all buses", images=[b"\x89PNGfake"], task_tier=1)
    base.update(overrides)
    return VisionRequest(**base)


# ---------------------------------------------------------------------------
# Concurrent voting
# ---------------------------------------------------------------------------


def test_concurrent_voting_overlaps_samples():
    """VISION_VOTE_CONCURRENT=true → vote samples overlap (gather)."""
    contents = [
        '{"indices":[1],"confidence":0.2}',  # initial: low confidence → vote
        '{"indices":[1,2],"confidence":0.9}',
        '{"indices":[1,3],"confidence":0.9}',
        '{"indices":[1,2],"confidence":0.9}',
    ]
    config = _make_config(
        vision_vote_concurrent=True,
        vision_vote_samples=3,
        vision_confidence_threshold=0.6,
    )
    # tier-2 + cloud_enabled → initial + vote calls all hit the cloud client.
    cloud = ScriptedConcurrencyClient("cloud", contents, delay=0.05)
    local = ScriptedConcurrencyClient("local", contents, delay=0.05)
    pool = FakePool(local=local, cloud=cloud)
    router = VisionRouter(pool, config)

    asyncio.run(router.classify(_req(task_tier=2, grid_size=9)))

    # 1 initial + 3 vote samples = 4 calls, all on cloud.
    assert cloud.call_count == 4
    assert local.call_count == 0
    # The 3 vote samples overlapped (gather launched them together).
    assert cloud.max_in_flight >= 2


def test_serial_voting_does_not_overlap():
    """VISION_VOTE_CONCURRENT=false → vote samples run serially (no overlap)."""
    contents = [
        '{"indices":[1],"confidence":0.2}',
        '{"indices":[1,2],"confidence":0.9}',
        '{"indices":[1,3],"confidence":0.9}',
        '{"indices":[1,2],"confidence":0.9}',
    ]
    config = _make_config(
        vision_vote_concurrent=False,
        vision_vote_samples=3,
        vision_confidence_threshold=0.6,
    )
    cloud = ScriptedConcurrencyClient("cloud", contents, delay=0.05)
    local = ScriptedConcurrencyClient("local", contents, delay=0.05)
    pool = FakePool(local=local, cloud=cloud)
    router = VisionRouter(pool, config)

    asyncio.run(router.classify(_req(task_tier=2, grid_size=9)))

    assert cloud.call_count == 4
    # Serial loop: never more than one call in flight at a time.
    assert cloud.max_in_flight == 1


def test_concurrent_voting_accumulates_all_usage():
    """Concurrent gather still accumulates usage from every sample."""
    contents = [
        '{"indices":[1],"confidence":0.2}',
        '{"indices":[1,2],"confidence":0.9}',
        '{"indices":[1,2],"confidence":0.9}',
        '{"indices":[1,2],"confidence":0.9}',
    ]
    config = _make_config(
        vision_vote_concurrent=True,
        vision_vote_samples=3,
        vision_confidence_threshold=0.6,
    )
    cloud = ScriptedConcurrencyClient(
        "cloud", contents, delay=0.01, usage=ModelUsage(input_tokens=10, output_tokens=5)
    )
    local = ScriptedConcurrencyClient(
        "local", contents, delay=0.01, usage=ModelUsage(input_tokens=10, output_tokens=5)
    )
    pool = FakePool(local=local, cloud=cloud)
    router = VisionRouter(pool, config)

    result = asyncio.run(router.classify(_req(task_tier=2, grid_size=9)))

    # 4 calls × (10 in, 5 out).
    assert result.usage.input_tokens == 40
    assert result.usage.output_tokens == 20


# ---------------------------------------------------------------------------
# Inline tier-1 → tier-2 escalation
# ---------------------------------------------------------------------------


def test_inline_escalation_uses_cloud_when_local_low_confidence():
    """tier-1 low conf + cloud high conf → cloud result used, both usages summed."""
    config = _make_config(
        vision_cloud_enabled=True,
        vision_inline_escalate=True,
        vision_confidence_threshold=0.6,
        vision_vote_samples=1,  # disable voting; isolate the escalation step
    )
    local = ScriptedConcurrencyClient(
        "local",
        ['{"indices":[0],"confidence":0.2}'],
        delay=0.0,
        usage=ModelUsage(input_tokens=10, output_tokens=5),
    )
    cloud = ScriptedConcurrencyClient(
        "cloud",
        ['{"indices":[2,3],"confidence":0.95}'],
        delay=0.0,
        usage=ModelUsage(input_tokens=20, output_tokens=8),
    )
    pool = FakePool(local=local, cloud=cloud)
    router = VisionRouter(pool, config)

    result = asyncio.run(router.classify(_req(task_tier=1)))

    assert result.indices == [2, 3]
    assert result.confidence == 0.95
    assert result.model == "cloud"
    assert result.votes == 1
    # Both calls counted: local (10+5) + cloud (20+8) = 30 in, 13 out.
    assert result.usage.input_tokens == 30
    assert result.usage.output_tokens == 13
    assert local.call_count == 1
    assert cloud.call_count == 1


def test_inline_escalation_budget_denial_returns_local():
    """Budget denial on the escalation call → return local result, no cloud call."""
    config = _make_config(
        vision_cloud_enabled=True,
        vision_inline_escalate=True,
        vision_confidence_threshold=0.6,
        vision_vote_samples=1,
    )
    local = ScriptedConcurrencyClient(
        "local",
        ['{"indices":[0],"confidence":0.2}'],
        delay=0.0,
        usage=ModelUsage(input_tokens=10, output_tokens=5),
    )
    cloud = ScriptedConcurrencyClient(
        "cloud",
        ['{"indices":[2,3],"confidence":0.95}'],
        delay=0.0,
        usage=ModelUsage(input_tokens=20, output_tokens=8),
    )
    pool = FakePool(local=local, cloud=cloud)
    budget = FakeBudget(allowed=False, downgrade_to="local")
    router = VisionRouter(pool, config, budget=budget)

    result = asyncio.run(router.classify(_req(task_tier=1), client_key="acct-1"))

    # Local result kept (no escalation).
    assert result.indices == [0]
    assert result.confidence == 0.2
    assert result.model == "local"
    # Only local usage accumulated.
    assert result.usage.input_tokens == 10
    assert result.usage.output_tokens == 5
    # Cloud client was never called.
    assert cloud.call_count == 0
    # Budget was consulted for the would-be cloud escalation call.
    assert budget.calls
    assert budget.calls[0][0] == "acct-1"
    assert budget.calls[0][2] == "cloud"


def test_inline_escalation_skipped_when_local_confident():
    """tier-1 high confidence → no escalation (local result returned as-is)."""
    config = _make_config(
        vision_cloud_enabled=True,
        vision_inline_escalate=True,
        vision_confidence_threshold=0.6,
        vision_vote_samples=1,
    )
    local = ScriptedConcurrencyClient(
        "local",
        ['{"indices":[4],"confidence":0.95}'],
        delay=0.0,
        usage=ModelUsage(input_tokens=10, output_tokens=5),
    )
    cloud = ScriptedConcurrencyClient(
        "cloud",
        ['{"indices":[9],"confidence":0.99}'],
        delay=0.0,
        usage=ModelUsage(input_tokens=20, output_tokens=8),
    )
    pool = FakePool(local=local, cloud=cloud)
    router = VisionRouter(pool, config)

    result = asyncio.run(router.classify(_req(task_tier=1)))

    assert result.indices == [4]
    assert result.confidence == 0.95
    assert result.model == "local"
    assert cloud.call_count == 0
    assert result.usage.input_tokens == 10


def test_inline_escalation_skipped_when_flag_disabled():
    """vision_inline_escalate=false → low-confidence local result returned, no cloud."""
    config = _make_config(
        vision_cloud_enabled=True,
        vision_inline_escalate=False,
        vision_confidence_threshold=0.6,
        vision_vote_samples=1,
    )
    local = ScriptedConcurrencyClient(
        "local",
        ['{"indices":[0],"confidence":0.2}'],
        delay=0.0,
        usage=ModelUsage(input_tokens=10, output_tokens=5),
    )
    cloud = ScriptedConcurrencyClient(
        "cloud",
        ['{"indices":[2,3],"confidence":0.95}'],
        delay=0.0,
        usage=ModelUsage(input_tokens=20, output_tokens=8),
    )
    pool = FakePool(local=local, cloud=cloud)
    router = VisionRouter(pool, config)

    result = asyncio.run(router.classify(_req(task_tier=1)))

    assert result.model == "local"
    assert result.indices == [0]
    assert cloud.call_count == 0


def test_inline_escalation_engages_voting_when_cloud_also_low():
    """Cloud escalation low conf + samples>1 → cloud voting engages.

    Verifies the escalation path falls through to the existing voting path on
    the cloud client (not the local one).
    """
    local_contents = ['{"indices":[0],"confidence":0.2}']  # local initial: low
    cloud_contents = [
        '{"indices":[1,2],"confidence":0.2}',  # cloud escalation: still low
        '{"indices":[1,2],"confidence":0.9}',  # cloud vote 1
        '{"indices":[1,3],"confidence":0.9}',  # cloud vote 2
        '{"indices":[1,2],"confidence":0.9}',  # cloud vote 3
    ]
    config = _make_config(
        vision_cloud_enabled=True,
        vision_inline_escalate=True,
        vision_confidence_threshold=0.6,
        vision_vote_samples=3,
        vision_vote_concurrent=False,  # serial so the script indices line up
    )
    local = ScriptedConcurrencyClient(
        "local",
        local_contents,
        delay=0.0,
        usage=ModelUsage(input_tokens=10, output_tokens=5),
    )
    cloud = ScriptedConcurrencyClient(
        "cloud",
        cloud_contents,
        delay=0.0,
        usage=ModelUsage(input_tokens=10, output_tokens=5),
    )
    pool = FakePool(local=local, cloud=cloud)
    router = VisionRouter(pool, config)

    result = asyncio.run(router.classify(_req(task_tier=1, grid_size=9)))

    # tile 1 (3/3) and tile 2 (2/3) win the majority.
    assert result.indices == [1, 2]
    assert result.model == "cloud"
    assert result.votes == 3
    # 1 local initial + 1 cloud escalation + 3 cloud votes.
    assert local.call_count == 1
    assert cloud.call_count == 4
    # 5 × (10 in, 5 out).
    assert result.usage.input_tokens == 50
    assert result.usage.output_tokens == 25


# ---------------------------------------------------------------------------
# Resource interception
# ---------------------------------------------------------------------------


def _resource_manager(*, block_hosts=""):
    """Build a BrowserManager with the default resource policy pre-parsed."""
    config = SimpleNamespace(
        resource_block_enabled=True,
        resource_block_types="image,media,font,stylesheet",
        resource_allow_hosts=(
            "hcaptcha.com,challenges.cloudflare.com,google.com,"
            "recaptcha.net,gstatic.com,cloudflare.com"
        ),
        resource_block_hosts=block_hosts,
    )
    return BrowserManager(config)


def test_resource_handler_blocks_image_on_generic_host():
    """image resource on a non-allowlisted host → abort."""
    manager = _resource_manager()
    route = FakeRoute("https://example.com/banner.png", "image")
    asyncio.run(manager._resource_handler(route))
    assert route.action == "abort"


def test_resource_handler_allowlists_hcaptcha_challenge_assets():
    """image on hcaptcha.com → continue (challenge tile images must pass)."""
    manager = _resource_manager()
    route = FakeRoute("https://assets.hcaptcha.com/captcha/tile.png", "image")
    asyncio.run(manager._resource_handler(route))
    assert route.action == "continue"


def test_resource_handler_allowlists_cloudflare_turnstile():
    """script on challenges.cloudflare.com → continue (Turnstile widget assets)."""
    manager = _resource_manager()
    route = FakeRoute(
        "https://challenges.cloudflare.com/turnstile/v0/api.js", "script"
    )
    asyncio.run(manager._resource_handler(route))
    assert route.action == "continue"


def test_resource_handler_allowlists_recaptcha_assets():
    """image on google.com / recaptcha.net / gstatic.com → continue."""
    manager = _resource_manager()
    for url in (
        "https://www.google.com/recaptcha/api2/bimage?c=123",
        "https://www.gstatic.com/recaptcha/releases/api.js",
        "https://www.recaptcha.net/recaptcha/api2/logo.png",
    ):
        route = FakeRoute(url, "image")
        asyncio.run(manager._resource_handler(route))
        assert route.action == "continue", url


def test_resource_handler_blocklists_trackers_regardless_of_type():
    """google-analytics.com on the blocklist → abort even for script/fetch."""
    manager = _resource_manager(block_hosts="google-analytics.com,doubleclick.net")
    for url, rtype in (
        ("https://www.google-analytics.com/analytics.js", "script"),
        ("https://stats.g.doubleclick.net/pixel.gif", "image"),
        ("https://ad.doubleclick.net/ddm/trackimp", "xhr"),
    ):
        route = FakeRoute(url, rtype)
        asyncio.run(manager._resource_handler(route))
        assert route.action == "abort", (url, rtype)


def test_resource_handler_continues_documents_on_generic_host():
    """document resource on a non-allowlisted host → continue (synthetic page)."""
    manager = _resource_manager()
    route = FakeRoute("https://example.com/checkout", "document")
    asyncio.run(manager._resource_handler(route))
    assert route.action == "continue"


def test_resource_handler_continues_scripts_on_generic_host():
    """script on a generic host → continue (not in default block types)."""
    manager = _resource_manager()
    route = FakeRoute("https://example.com/app.js", "script")
    asyncio.run(manager._resource_handler(route))
    assert route.action == "continue"


def test_resource_handler_malformed_url_falls_through_to_continue():
    """A malformed URL never raises; the handler continues rather than breaking solving."""
    manager = _resource_manager()

    class BrokenRequest:
        @property
        def url(self):
            raise ValueError("malformed")

        resource_type = "image"

    class BrokenRoute:
        def __init__(self):
            self.request = BrokenRequest()
            self.action = None

        async def continue_(self):
            self.action = "continue"

        async def abort(self):
            self.action = "abort"

    route = BrokenRoute()
    asyncio.run(manager._resource_handler(route))
    assert route.action == "continue"


def test_resource_handler_blocks_font_and_stylesheet():
    """font and stylesheet resource types are in the default block set."""
    manager = _resource_manager()
    for url, rtype in (
        ("https://example.com/assets/font.woff2", "font"),
        ("https://example.com/assets/style.css", "stylesheet"),
        ("https://cdn.example.com/video.mp4", "media"),
    ):
        route = FakeRoute(url, rtype)
        asyncio.run(manager._resource_handler(route))
        assert route.action == "abort", (url, rtype)


if __name__ == "__main__":
    test_concurrent_voting_overlaps_samples()
    test_serial_voting_does_not_overlap()
    test_concurrent_voting_accumulates_all_usage()
    test_inline_escalation_uses_cloud_when_local_low_confidence()
    test_inline_escalation_budget_denial_returns_local()
    test_inline_escalation_skipped_when_local_confident()
    test_inline_escalation_skipped_when_flag_disabled()
    test_inline_escalation_engages_voting_when_cloud_also_low()
    test_resource_handler_blocks_image_on_generic_host()
    test_resource_handler_allowlists_hcaptcha_challenge_assets()
    test_resource_handler_allowlists_cloudflare_turnstile()
    test_resource_handler_allowlists_recaptcha_assets()
    test_resource_handler_blocklists_trackers_regardless_of_type()
    test_resource_handler_continues_documents_on_generic_host()
    test_resource_handler_continues_scripts_on_generic_host()
    test_resource_handler_malformed_url_falls_through_to_continue()
    test_resource_handler_blocks_font_and_stylesheet()
    print("ok")
