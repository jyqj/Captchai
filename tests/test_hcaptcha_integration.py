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
        # WP5: enterprise residential-proxy enforcement (default on).
        enterprise_require_residential=True,
        human_mouse_enabled=False,
        human_mouse_jitter_ms=0,
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
        proxy_pool=None,
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


def test_solve_records_phase_timing() -> None:
    """The ledger record breaks wall_ms into page-load / challenge / vision phases."""
    async def run() -> None:
        page = FakePage(token_after=1, tile_count=9)
        context = FakeContext(page)
        manager = FakeManager(context)
        vision = FakeVisionRouter(indices=[0, 3, 5])
        services = _services(vision)

        solver = HCaptchaSolver(_config(), manager=manager, services=services)
        await solver.solve(
            {
                "websiteURL": "https://example.com",
                "websiteKey": "sitekey-phase",
                "type": "HCaptchaTaskProxyless",
                "userAgent": "UA-FAKE",
            }
        )
        recs = await services.ledger.records()
        assert recs
        rec = recs[-1]
        # Phase fields are populated (page-load + challenge instrumented on the
        # injected-page path; all are non-negative and bounded by wall_ms).
        assert rec.page_load_ms >= 0
        assert rec.challenge_ms >= 0
        assert rec.vision_ms >= 0
        assert rec.challenge_ms <= rec.wall_ms + 5

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


# --------------------------------------------------------------------------- #
# WP5: enterprise residential-proxy enforcement + WP3: solution geo surfacing
# --------------------------------------------------------------------------- #


def test_enterprise_with_explicit_proxyless_egress_raises() -> None:
    """Enterprise + egress=proxyless is refused by _enforce_enterprise_egress."""
    async def run() -> None:
        page = FakePage(tile_count=0)
        context = FakeContext(page)
        manager = FakeManager(context)
        solver = HCaptchaSolver(_config(), manager=manager, services=None)
        try:
            await solver.solve(
                {
                    "websiteURL": "https://example.com",
                    "websiteKey": "sitekey-ent",
                    "type": "HCaptchaTaskProxyless",
                    "rqdata": "RQ-DATA-XYZ",
                    "enterprisePayload": {"sentry": "value"},
                    "egress": "proxyless",
                }
            )
            assert False, "expected enterprise + egress=proxyless to raise"
        except RuntimeError as exc:
            assert "Enterprise hCaptcha requires" in str(exc)
            assert "egress=proxyless is not allowed" in str(exc)

    asyncio.run(run())


def test_enterprise_with_no_residential_proxy_in_pool_raises() -> None:
    """Enterprise + pool with only datacenter proxies raises a residential-specific error."""
    async def run() -> None:
        from src.assets.proxy_pool import ProxyAsset, ProxyPool
        from src.assets.session_pool import SessionPool

        page = FakePage(tile_count=0)

        async def factory(fingerprint, proxy):
            return FakeContext(page), fingerprint.user_agent

        session_pool = SessionPool(factory, size=2, max_solves=8)
        proxy_pool = ProxyPool()
        proxy_pool.add(
            ProxyAsset(id="dc-1", server="http://dc:1", kind="datacenter")
        )

        vision = FakeVisionRouter(indices=[0])
        services = _services(vision)
        services.session_pool = session_pool
        services.proxy_pool = proxy_pool

        solver = HCaptchaSolver(
            _config(), manager=FakeManager(FakeContext(page)), services=services
        )
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
            assert False, "expected enterprise + no residential proxy to raise"
        except RuntimeError as exc:
            assert "residential" in str(exc).lower()
        finally:
            await session_pool.close_all()

    asyncio.run(run())


def test_enterprise_with_residential_proxy_succeeds_and_includes_geo() -> None:
    """Enterprise + residential pool proxy: solve succeeds and solution carries geo.

    Verifies the full WP3 + WP5 path: a German residential pool proxy is
    checked out (kind filter), paired with a warm session whose fingerprint
    is geo-aligned (Europe/Berlin + de-DE), the solve succeeds via
    vision-driven challenge dispatch, and the returned solution includes
    ``timezoneId`` / ``acceptLanguage`` from the fingerprint actually used.
    """
    async def run() -> None:
        from src.assets.proxy_pool import ProxyAsset, ProxyPool
        from src.assets.session_pool import SessionPool

        page = FakePage(token_after=1, tile_count=9)

        async def factory(fingerprint, proxy):
            return FakeContext(page), fingerprint.user_agent

        session_pool = SessionPool(factory, size=2, max_solves=8)
        proxy_pool = ProxyPool()
        proxy_pool.add(
            ProxyAsset(
                id="res-de",
                server="http://res-de:1",
                kind="residential",
                country="DE",
                timezone="Europe/Berlin",
                locale="de-DE",
            )
        )

        vision = FakeVisionRouter(indices=[0, 3, 5])
        services = _services(vision)
        services.session_pool = session_pool
        services.proxy_pool = proxy_pool

        solver = HCaptchaSolver(
            _config(), manager=FakeManager(FakeContext(page)), services=services
        )
        result = await solver.solve(
            {
                "websiteURL": "https://example.com",
                "websiteKey": "sitekey-ent",
                "type": "HCaptchaTaskProxyless",
                "rqdata": "RQ-DATA-XYZ",
                "enterprisePayload": {"sentry": "value"},
            }
        )

        assert result["gRecaptchaResponse"].startswith("P1_")
        # WP3: solution surfaces the fingerprint geo actually used.
        assert result["timezoneId"] == "Europe/Berlin"
        assert result["acceptLanguage"] is not None
        assert "de-DE" in result["acceptLanguage"]
        # The residential proxy was checked out and reported per solve.
        snap = proxy_pool.snapshot()[0]
        assert snap["success_count"] == 1
        assert snap["kind"] == "residential"
        # The enterprise enforcement forced egress=pool and required residential.
        # (Implicit: the solve succeeded, so the residential proxy was used.)
        await session_pool.close_all()

    asyncio.run(run())


def test_enterprise_with_mobile_proxy_succeeds() -> None:
    """Fix #2: enterprise hCaptcha accepts a mobile pool proxy, not just residential.

    The enterprise enforcement now sets ``_required_proxy_kinds=("residential",
    "mobile")`` so a pool that has only a ``kind="mobile"`` proxy is accepted.
    A JP mobile proxy is used so the geo-alignment path is also exercised
    (Asia/Tokyo + ja-JP — the locale added in Fix #3).
    """
    async def run() -> None:
        from src.assets.proxy_pool import ProxyAsset, ProxyPool
        from src.assets.session_pool import SessionPool

        page = FakePage(token_after=1, tile_count=9)

        async def factory(fingerprint, proxy):
            return FakeContext(page), fingerprint.user_agent

        session_pool = SessionPool(factory, size=2, max_solves=8)
        proxy_pool = ProxyPool()
        proxy_pool.add(
            ProxyAsset(
                id="mob-jp",
                server="http://mob-jp:1",
                kind="mobile",
                country="JP",
                timezone="Asia/Tokyo",
                locale="ja-JP",
            )
        )

        vision = FakeVisionRouter(indices=[0, 3, 5])
        services = _services(vision)
        services.session_pool = session_pool
        services.proxy_pool = proxy_pool

        solver = HCaptchaSolver(
            _config(), manager=FakeManager(FakeContext(page)), services=services
        )
        result = await solver.solve(
            {
                "websiteURL": "https://example.com",
                "websiteKey": "sitekey-ent",
                "type": "HCaptchaTaskProxyless",
                "rqdata": "RQ-DATA-XYZ",
                "enterprisePayload": {"sentry": "value"},
            }
        )

        assert result["gRecaptchaResponse"].startswith("P1_")
        # The mobile proxy was checked out and reported per solve — no
        # "no residential proxy" error was raised.
        snap = proxy_pool.snapshot()[0]
        assert snap["success_count"] == 1
        assert snap["kind"] == "mobile"
        # Geo alignment: the JP mobile proxy's fingerprint carries ja-JP.
        assert result["timezoneId"] == "Asia/Tokyo"
        assert result["acceptLanguage"] is not None
        assert "ja-JP" in result["acceptLanguage"]
        await session_pool.close_all()

    asyncio.run(run())


def test_enterprise_with_only_datacenter_proxy_raises_mentions_kinds() -> None:
    """Fix #2: enterprise + pool with only datacenter proxies raises an error
    that mentions both residential and mobile (the requested kinds)."""
    async def run() -> None:
        from src.assets.proxy_pool import ProxyAsset, ProxyPool
        from src.assets.session_pool import SessionPool

        page = FakePage(tile_count=0)

        async def factory(fingerprint, proxy):
            return FakeContext(page), fingerprint.user_agent

        session_pool = SessionPool(factory, size=2, max_solves=8)
        proxy_pool = ProxyPool()
        proxy_pool.add(
            ProxyAsset(id="dc-1", server="http://dc:1", kind="datacenter")
        )

        vision = FakeVisionRouter(indices=[0])
        services = _services(vision)
        services.session_pool = session_pool
        services.proxy_pool = proxy_pool

        solver = HCaptchaSolver(
            _config(), manager=FakeManager(FakeContext(page)), services=services
        )
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
            assert False, "expected enterprise + no residential/mobile proxy to raise"
        except RuntimeError as exc:
            msg = str(exc).lower()
            assert "residential" in msg
            assert "mobile" in msg
        finally:
            await session_pool.close_all()

    asyncio.run(run())


def test_enterprise_with_task_proxy_allowed_without_pool() -> None:
    """Enterprise + caller task proxy → egress=task, no pool required.

    A caller-supplied task proxy is the caller's responsibility; the
    enterprise enforcement honours it (egress=task) and does NOT force the
    pool path. Verifies the existing test_enterprise_fields_forwarded
    behaviour is preserved under the new enforcement helper.
    """
    async def run() -> None:
        page = FakePage(token_after=1, tile_count=0)

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
        result = await solver.solve(
            {
                "websiteURL": "https://example.com",
                "websiteKey": "sitekey-ent",
                "type": "HCaptchaTaskProxyless",
                "rqdata": "RQ-DATA-XYZ",
                "enterprisePayload": {"sentry": "value"},
                "proxy": "http://user:pass@proxy.example.com:8080",
            }
        )
        assert result["gRecaptchaResponse"].startswith("P1_")
        # Enterprise fields were forwarded to the real-page init script.
        assert context.init_scripts
        script = context.init_scripts[0]
        assert "RQ-DATA-XYZ" in script
        assert '"enterprise": {"sentry": "value"}' in script

    asyncio.run(run())


if __name__ == "__main__":
    test_grid_challenge_solved_via_vision_and_recorded()
    test_solve_records_phase_timing()
    test_enterprise_fields_forwarded()
    test_token_cache_not_consulted()
    test_enterprise_refuses_proxyless_fallback()
    test_invisible_skips_human_mouse()
    test_widget_error_surfaced()
    test_enterprise_with_explicit_proxyless_egress_raises()
    test_enterprise_with_no_residential_proxy_in_pool_raises()
    test_enterprise_with_residential_proxy_succeeds_and_includes_geo()
    test_enterprise_with_task_proxy_allowed_without_pool()
    test_enterprise_with_mobile_proxy_succeeds()
    test_enterprise_with_only_datacenter_proxy_raises_mentions_kinds()
    print("ok")
