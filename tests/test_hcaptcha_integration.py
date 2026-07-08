"""Integration tests for the refactored hCaptcha solver.

These drive the solver with a fake Playwright page/context and a fake vision
router so the vision routing, challenge dispatch, proxyless-fallback refusal,
invisible-widget mouse skip, and ledger metering can be verified with no
browser and no network.
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
from src.consumption.ledger import CostLedger  # noqa: E402
from src.parsing.vision import VisionResult  # noqa: E402
from src.services.hcaptcha import HCaptchaSolver  # noqa: E402


# ── fakes ──────────────────────────────────────────────────────


class FakeVisionRouter:
    """Returns a fixed selection so the grid solver clicks deterministic tiles."""

    def __init__(self, indices):
        self._indices = indices
        self.calls = 0

    async def classify(self, req, *, client_key=None):
        self.calls += 1
        return VisionResult(
            indices=list(self._indices),
            confidence=0.95,
            model="cloud",
            usage=ModelUsage(input_tokens=100, output_tokens=10),
            votes=1,
        )


class FakeLocator:
    def __init__(self, page, selector):
        self._page = page
        self._selector = selector

    def first(self):
        return self

    @property
    def first_prop(self):
        return self

    def nth(self, i):
        return self

    async def count(self):
        if ".task-image" in self._selector:
            return self._page.tile_count
        return 0

    async def click(self, timeout=None):
        self._page.clicks.append(self._selector)

    async def inner_text(self, timeout=None):
        return "click all buses"

    async def screenshot(self, timeout=None):
        return b"\x89PNG-fake"

    async def wait_for(self, timeout=None):
        return None


# FakeLocator.first is used as an attribute in shape base (locator(sel).first)
FakeLocator.first = property(lambda self: self)  # type: ignore[assignment]


class FakeFrameLocator:
    def __init__(self, page):
        self._page = page

    def locator(self, selector):
        return FakeLocator(self._page, selector)

    def frame_locator(self, selector):
        return FakeFrameLocator(self._page)


class FakePage:
    def __init__(self, *, token_after=1, tile_count=9):
        self.clicks = []
        self.tile_count = tile_count
        self._token_after = token_after
        self._poll_calls = 0
        self.mouse = SimpleNamespace(move=self._noop)
        self.routed = False

    async def _noop(self, *a, **k):
        return None

    def frame_locator(self, selector):
        return FakeFrameLocator(self)

    def locator(self, selector):
        return FakeLocator(self, selector)

    async def goto(self, url, wait_until=None, timeout=None):
        return None

    async def evaluate(self, script, *args):
        has_token = "__omcToken" in script
        has_error = "__omcError" in script
        if has_error and not has_token:
            # error-only probe (e.g. _solve_challenge's surfacing check)
            return None
        # token extractor (combined {token, error} or token-only): only
        # resolve after a grid tile has been clicked, so the solve must go
        # through the vision-driven challenge dispatch.
        self._poll_calls += 1
        tile_clicks = [c for c in self.clicks if ".task-image" in c]
        ready = bool(tile_clicks) and self._poll_calls >= self._token_after
        token = "P1_" + "x" * 40 if ready else None
        if has_error:
            return {"token": token, "error": None}
        return token

    async def wait_for_function(self, script, timeout=None):
        return None


class FakeContext:
    def __init__(self, page):
        self._page = page
        self.closed = False
        self.init_scripts = []

    async def route(self, url, handler):
        return None

    async def add_init_script(self, script):
        self.init_scripts.append(script)

    async def new_page(self):
        return self._page

    async def close(self):
        self.closed = True


class FakeManager:
    def __init__(self, context):
        self._context = context

    async def new_context(self, params):
        return self._context, "UA-FAKE"


def _config():
    return SimpleNamespace(
        captcha_retries=1,
        browser_timeout=5,
        poll_budget=2,
        poll_interval=0.01,
        vision_cloud_enabled=True,
        vision_vote_samples=1,
        vision_confidence_threshold=0.6,
        vision_tier2_detail="high",
        captcha_timeout=10,
    )


def _services(vision_router):
    ledger = CostLedger()

    class Acc:
        def __init__(self):
            self.records = []

        async def record(self, sitekey, outcome, *, proxy_kind=None, model=None):
            self.records.append((sitekey, outcome, model))

    class TokenCache:
        def __init__(self):
            self.store = {}

        async def get(self, sitekey, ip, ua):
            return self.store.get((sitekey, ip, ua))

        async def put(self, sitekey, ip, ua, token):
            self.store[(sitekey, ip, ua)] = token

    return SimpleNamespace(
        vision_router=vision_router,
        ledger=ledger,
        accounting=Acc(),
        token_cache=TokenCache(),
        session_pool=None,
    )


# ── tests ──────────────────────────────────────────────────────


def test_grid_challenge_solved_via_vision_and_recorded() -> None:
    async def run() -> None:
        page = FakePage(token_after=1, tile_count=9)
        context = FakeContext(page)
        manager = FakeManager(context)
        vision = FakeVisionRouter(indices=[0, 3, 5])
        services = _services(vision)

        solver = HCaptchaSolver(_config(), manager=manager, services=services)
        result = await solver.solve(
            {
                "websiteURL": "https://example.com",
                "websiteKey": "sitekey-1",
                "type": "HCaptchaTaskProxyless",
                "userAgent": "UA-FAKE",
            }
        )

        assert result["gRecaptchaResponse"].startswith("P1_")
        assert result["userAgent"] == "UA-FAKE"
        # vision was consulted and tiles were clicked
        assert vision.calls >= 1
        assert any(".task-image" in c for c in page.clicks)
        # ledger recorded a successful, cloud-attributed solve
        summary = await services.ledger.summary()
        assert summary["count"] == 1
        assert summary["by_outcome"].get("ready") == 1
        assert context.closed is True

    asyncio.run(run())


def test_enterprise_fields_forwarded() -> None:
    async def run() -> None:
        page = FakePage(token_after=1, tile_count=0)

        # passive: token present on first poll (no clicks needed)
        async def eval_token(script, *args):
            has_token = "__omcToken" in script
            has_error = "__omcError" in script
            if has_error and not has_token:
                return None
            token = "P1_" + "y" * 40
            if has_error:
                return {"token": token, "error": None}
            return token

        page.evaluate = eval_token  # type: ignore[assignment]
        context = FakeContext(page)
        manager = FakeManager(context)
        solver = HCaptchaSolver(_config(), manager=manager, services=None)
        # A task proxy is supplied so the enterprise proxyless-fallback guard
        # does not fire — this test is about enterprise field forwarding, not
        # proxy enforcement.
        await solver.solve(
            {
                "websiteURL": "https://example.com",
                "websiteKey": "sitekey-ent",
                "type": "HCaptchaTaskProxyless",
                "rqdata": "RQ-DATA-XYZ",
                "enterprisePayload": {"sentry": "value"},
                "proxy": "http://user:pass@proxy.example.com:8080",
            }
        )

        assert context.init_scripts
        script = context.init_scripts[0]
        assert "RQ-DATA-XYZ" in script
        assert '"enterprise": {"sentry": "value"}' in script

    asyncio.run(run())


def test_token_cache_not_consulted() -> None:
    async def run() -> None:
        page = FakePage(token_after=1, tile_count=9)
        context = FakeContext(page)
        manager = FakeManager(context)
        vision = FakeVisionRouter(indices=[0, 3, 5])
        services = _services(vision)
        # Pre-seed the cache for (sitekey, no-proxy, UA). With cross-request
        # caching removed, solve MUST proceed through the browser and mint a
        # fresh token rather than serving the cached one.
        await services.token_cache.put(
            "sitekey-c", None, "UA-CACHED", "P1_cached_token_value_xxxxx"
        )

        solver = HCaptchaSolver(_config(), manager=manager, services=services)
        result = await solver.solve(
            {
                "websiteURL": "https://example.com",
                "websiteKey": "sitekey-c",
                "type": "HCaptchaTaskProxyless",
                "userAgent": "UA-CACHED",
            }
        )
        # A fresh token was minted via vision-driven dispatch, not the cache.
        assert result["gRecaptchaResponse"] != "P1_cached_token_value_xxxxx"
        assert result["gRecaptchaResponse"].startswith("P1_")
        assert vision.calls >= 1
        assert any(".task-image" in c for c in page.clicks)

    asyncio.run(run())


def test_enterprise_refuses_proxyless_fallback() -> None:
    async def run() -> None:
        from src.services.browser_solver import ProxyKind, SolveContext

        page = FakePage(token_after=1, tile_count=9)
        context = FakeContext(page)
        manager = FakeManager(context)
        solver = HCaptchaSolver(_config(), manager=manager, services=None)

        async def fake_acquire(params):
            return SolveContext(
                context=context,
                user_agent="UA-FAKE",
                proxy_kind=ProxyKind.PROXYLESS,
            )

        released = []

        async def fake_release(solve_ctx, solved, params):
            released.append((solve_ctx, solved))

        solver._acquire_context = fake_acquire  # type: ignore[assignment]
        solver._release_context = fake_release  # type: ignore[assignment]

        try:
            await solver.solve(
                {
                    "websiteURL": "https://example.com",
                    "websiteKey": "sitekey-ent",
                    "type": "HCaptchaTaskProxyless",
                    "rqdata": "RQ-DATA-XYZ",
                    "enterprisePayload": {"sentry": "value"},
                }
            )
            assert False, "expected enterprise proxyless fallback to raise"
        except RuntimeError as exc:
            assert "Enterprise hCaptcha requires" in str(exc)
        # Context was still released with solved=False even though we raised.
        assert released
        assert released[0][1] is False

    asyncio.run(run())


def test_invisible_skips_human_mouse() -> None:
    async def run() -> None:
        page = FakePage(token_after=1, tile_count=0)

        # passive: token present on first poll (no clicks needed)
        async def eval_token(script, *args):
            has_token = "__omcToken" in script
            has_error = "__omcError" in script
            if has_error and not has_token:
                return None
            token = "P1_" + "z" * 40
            if has_error:
                return {"token": token, "error": None}
            return token

        page.evaluate = eval_token  # type: ignore[assignment]
        context = FakeContext(page)
        manager = FakeManager(context)
        solver = HCaptchaSolver(_config(), manager=manager, services=None)

        mouse_calls = []

        async def fake_mouse(p):
            mouse_calls.append(p)

        solver._human_mouse = fake_mouse  # type: ignore[assignment]

        await solver.solve(
            {
                "websiteURL": "https://example.com",
                "websiteKey": "sitekey-inv",
                "type": "HCaptchaTaskProxyless",
                "isInvisible": True,
            }
        )
        assert mouse_calls == []

    asyncio.run(run())


def test_widget_error_surfaced() -> None:
    async def run() -> None:
        page = FakePage(tile_count=0)

        async def eval_err(script, *args):
            has_token = "__omcToken" in script
            has_error = "__omcError" in script
            if has_error and not has_token:
                return "network-error"
            if has_error and has_token:
                # combined {token, error} extractor — surface the error inline
                return {"token": None, "error": "network-error"}
            return None

        page.evaluate = eval_err  # type: ignore[assignment]
        context = FakeContext(page)
        manager = FakeManager(context)
        solver = HCaptchaSolver(_config(), manager=manager, services=None)
        try:
            await solver.solve(
                {
                    "websiteURL": "https://example.com",
                    "websiteKey": "sitekey-e",
                    "type": "HCaptchaTaskProxyless",
                }
            )
            assert False, "expected widget error to propagate"
        except RuntimeError as exc:
            assert "widget error" in str(exc)

    asyncio.run(run())


if __name__ == "__main__":
    test_grid_challenge_solved_via_vision_and_recorded()
    test_enterprise_fields_forwarded()
    test_token_cache_not_consulted()
    test_enterprise_refuses_proxyless_fallback()
    test_invisible_skips_human_mouse()
    test_widget_error_surfaced()
    print("ok")
