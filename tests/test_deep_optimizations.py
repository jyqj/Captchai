"""Tests for the deep-optimization pass (behavioral realism, client-hint
coherence, hardened-runtime honesty, egress echo, real-outcome proxy burn).

Follows the repo convention: plain ``def test_*`` functions driving coroutines
with ``asyncio.run`` (no pytest-asyncio), fakes instead of a real browser.
"""

from __future__ import annotations

import asyncio
import random
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    _ = sys.path.insert(0, str(PROJECT_ROOT))

from src.assets.fingerprint import (  # noqa: E402
    client_hint_headers,
    context_kwargs,
    generate_fingerprint,
    sec_ch_ua,
)
from src.parsing.dispatcher import ChallengeContext  # noqa: E402
from src.parsing.shapes.grid_select import GridSelectSolver  # noqa: E402
from src.parsing.shapes.human_cursor import ease_path, human_click, point_in_box  # noqa: E402
from src.services.browser import BrowserManager  # noqa: E402
from src.services.browser_solver import egress_from_params  # noqa: E402


# --------------------------------------------------------------------------- #
# human_cursor: pure path math
# --------------------------------------------------------------------------- #


def test_ease_path_starts_and_ends_correctly() -> None:
    rng = random.Random(1)
    path = ease_path((0.0, 0.0), (100.0, 50.0), steps=24, rng=rng)
    assert len(path) == 24
    # Last point lands exactly on the target so the click hits the intended px.
    assert path[-1] == (100.0, 50.0)
    # Monotonic-ish progression in x (eased, so strictly increasing overall).
    assert path[0][0] < path[-1][0]
    # All points finite.
    assert all(isinstance(x, float) and isinstance(y, float) for x, y in path)


def test_point_in_box_is_inside_and_off_center() -> None:
    box = {"x": 100.0, "y": 200.0, "width": 40.0, "height": 40.0}
    rng = random.Random(7)
    for _ in range(50):
        x, y = point_in_box(box, rng=rng)
        # Well inside the element (never on the border / adjacent tile).
        assert 100.0 + 40.0 * 0.30 <= x <= 100.0 + 40.0 * 0.70
        assert 200.0 + 40.0 * 0.30 <= y <= 200.0 + 40.0 * 0.70


class _RecordingMouse:
    def __init__(self) -> None:
        self.moves: List[Tuple[float, float]] = []
        self.downs = 0
        self.ups = 0

    async def move(self, x: float, y: float) -> None:
        self.moves.append((x, y))

    async def down(self) -> None:
        self.downs += 1

    async def up(self) -> None:
        self.ups += 1


class _RecordingPage:
    def __init__(self) -> None:
        self.mouse = _RecordingMouse()


def test_human_click_traces_path_and_presses() -> None:
    async def run() -> None:
        page = _RecordingPage()
        box = {"x": 10.0, "y": 20.0, "width": 40.0, "height": 40.0}
        # jitter_ms 0 keeps the test fast (no real sleeps of note).
        end = await human_click(page, box, rng=random.Random(3), jitter_ms=0.0)
        assert end is not None
        # The pointer travelled (multiple moves), not a single teleport.
        assert len(page.mouse.moves) > 5
        # A real press (down+up), not a zero-duration synthetic click.
        assert page.mouse.downs == 1 and page.mouse.ups == 1
        # Final move lands on the returned click point.
        assert page.mouse.moves[-1] == end

    asyncio.run(run())


def test_human_click_returns_none_without_mouse() -> None:
    async def run() -> None:
        page = SimpleNamespace()  # no .mouse
        box = {"x": 0.0, "y": 0.0, "width": 10.0, "height": 10.0}
        assert await human_click(page, box) is None

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# grid solver uses the human path when a page is threaded on ctx
# --------------------------------------------------------------------------- #


class _FakeLoc:
    def __init__(self, frame: "_FakeFrame", selector: str, index: int | None = None):
        self._frame = frame
        self._selector = selector
        self._index = index

    @property
    def first(self) -> "_FakeLoc":
        return _FakeLoc(self._frame, self._selector, 0)

    def nth(self, i: int) -> "_FakeLoc":
        return _FakeLoc(self._frame, self._selector, i)

    async def count(self) -> int:
        return self._frame.counts.get(self._selector, 0)

    async def inner_text(self) -> str:
        return self._frame.texts.get(self._selector, "")

    async def screenshot(self) -> bytes:
        return b"png"

    async def click(self, **kwargs: Any) -> None:
        self._frame.clicks.append((self._selector, self._index))

    async def bounding_box(self) -> dict:
        return {"x": 5.0 + (self._index or 0) * 50, "y": 5.0, "width": 40.0, "height": 40.0}


class _FakeFrame:
    def __init__(self, counts=None, texts=None):
        self.counts = counts or {}
        self.texts = texts or {}
        self.clicks: List[Any] = []

    def locator(self, selector: str) -> _FakeLoc:
        return _FakeLoc(self, selector)


class _FakeVision:
    def __init__(self, indices):
        self.indices = indices

    async def classify(self, req: Any):
        return SimpleNamespace(indices=list(self.indices), confidence=0.95)


def test_grid_select_uses_human_path_when_page_present() -> None:
    async def run() -> None:
        frame = _FakeFrame(counts={".task-image": 4}, texts={".prompt-text": "x"})
        vision = _FakeVision(indices=[1, 2])
        seq = iter([None, None, "TOKEN"])

        async def poll():
            return next(seq, None)

        solver = GridSelectSolver(vision=vision, token_poll=poll)
        page = _RecordingPage()
        ctx = ChallengeContext(extra={"page": page, "humanize": True, "humanize_jitter_ms": 0})
        token = await solver.run(frame, ctx)
        assert token == "TOKEN"
        # Human path used page.mouse instead of locator.click → no tile clicks
        # recorded on the frame, but real mouse presses happened.
        assert not any(sel == ".task-image" for sel, _ in frame.clicks)
        assert page.mouse.downs >= 2  # two tiles clicked via the human path

    asyncio.run(run())


def test_grid_select_falls_back_to_locator_click_without_page() -> None:
    async def run() -> None:
        frame = _FakeFrame(counts={".task-image": 4}, texts={".prompt-text": "x"})
        vision = _FakeVision(indices=[0, 3])
        seq = iter([None, None, "TOKEN"])

        async def poll():
            return next(seq, None)

        solver = GridSelectSolver(vision=vision, token_poll=poll)
        # No page on ctx → must fall back to locator.click (recorded on frame).
        token = await solver.run(frame, ChallengeContext())
        assert token == "TOKEN"
        tile_clicks = [i for sel, i in frame.clicks if sel == ".task-image"]
        assert tile_clicks == [0, 3]

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# P1-5: checkbox click uses the human-like pointer path (not a teleport click)
# --------------------------------------------------------------------------- #


def test_checkbox_click_uses_human_path_when_enabled() -> None:
    """_human_click_in_frame moves page.mouse along a path + presses (no teleport)."""
    async def run() -> None:
        from src.services.browser_solver import BaseBrowserSolver

        class _Loc:
            async def bounding_box(self):
                return {"x": 30.0, "y": 40.0, "width": 24.0, "height": 24.0}

            @property
            def first(self):
                return self

            async def click(self, timeout=None):
                raise AssertionError("should use human path, not locator.click")

        class _Frame:
            def locator(self, selector):
                return _Loc()

        page = _RecordingPage()
        config = SimpleNamespace(human_mouse_enabled=True, human_mouse_jitter_ms=0)
        solver = BaseBrowserSolver(config, manager=SimpleNamespace(), services=None)
        await solver._human_click_in_frame(page, _Frame(), "#checkbox")
        # Real pointer travel + a real press, exactly like the tile clicks.
        assert len(page.mouse.moves) > 5
        assert page.mouse.downs == 1 and page.mouse.ups == 1

    asyncio.run(run())


def test_checkbox_click_falls_back_to_locator_click_when_disabled() -> None:
    """With humanisation off, the checkbox uses a plain locator.click()."""
    async def run() -> None:
        from src.services.browser_solver import BaseBrowserSolver

        clicked = {"n": 0}

        class _Loc:
            @property
            def first(self):
                return self

            async def bounding_box(self):
                raise AssertionError("must not read geometry when disabled")

            async def click(self, timeout=None):
                clicked["n"] += 1

        class _Frame:
            def locator(self, selector):
                return _Loc()

        page = _RecordingPage()
        config = SimpleNamespace(human_mouse_enabled=False, human_mouse_jitter_ms=0)
        solver = BaseBrowserSolver(config, manager=SimpleNamespace(), services=None)
        await solver._human_click_in_frame(page, _Frame(), "#checkbox")
        assert clicked["n"] == 1
        assert page.mouse.downs == 0

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# client-hint coherence
# --------------------------------------------------------------------------- #


def test_client_hints_match_ua_version_and_platform() -> None:
    for seed in [str(i) for i in range(20)]:
        fp = generate_fingerprint(seed=seed)
        major = fp.user_agent.split("Chrome/")[1].split(".")[0]
        headers = client_hint_headers(fp)
        assert f'"Google Chrome";v="{major}"' in headers["sec-ch-ua"]
        assert f'"Chromium";v="{major}"' in headers["sec-ch-ua"]
        assert headers["sec-ch-ua-mobile"] == "?0"
        expected_platform = '"macOS"' if fp.platform == "MacIntel" else '"Windows"'
        assert headers["sec-ch-ua-platform"] == expected_platform
        # Accept-Language leads with the fingerprint's primary language.
        assert headers["accept-language"].startswith(fp.languages[0])


def test_context_kwargs_carries_client_hints() -> None:
    fp = generate_fingerprint(seed="ch-kwargs")
    kwargs = context_kwargs(fp)
    assert "extra_http_headers" in kwargs
    assert kwargs["extra_http_headers"]["sec-ch-ua"] == sec_ch_ua(fp.user_agent)


def test_stealth_js_useragentdata_matches_ua() -> None:
    from src.assets.fingerprint import build_stealth_js

    fp = generate_fingerprint(seed="uad")
    major = fp.user_agent.split("Chrome/")[1].split(".")[0]
    js = build_stealth_js(fp)
    assert "navigator, 'userAgentData'" in js
    assert f'"version":"{major}"' in js
    assert "getHighEntropyValues" in js


# --------------------------------------------------------------------------- #
# egress echo
# --------------------------------------------------------------------------- #


def test_egress_from_params_surfaces_kind_and_server() -> None:
    assert egress_from_params(
        {"_proxyKind": "pool_proxy", "_egress_server": "http://gw:8080"}
    ) == {"proxyKind": "pool_proxy", "egressServer": "http://gw:8080"}
    # Proxyless / unknown → both None (backward-compatible omission).
    assert egress_from_params({"_proxyKind": "proxyless"}) == {
        "proxyKind": "proxyless",
        "egressServer": None,
    }
    assert egress_from_params({}) == {"proxyKind": None, "egressServer": None}


# --------------------------------------------------------------------------- #
# hardened-runtime honesty
# --------------------------------------------------------------------------- #


def _runtime_config(runtime: str, strict: bool) -> SimpleNamespace:
    return SimpleNamespace(
        browser_headless=True,
        browser_runtime=runtime,
        browser_runtime_strict=strict,
        resource_block_enabled=False,
        resource_block_types="",
        resource_allow_hosts="",
        resource_block_hosts="",
    )


def test_strict_runtime_raises_when_hardened_runtime_unavailable() -> None:
    async def run() -> None:
        mgr = BrowserManager(_runtime_config("camoufox", strict=True))
        # Force "not installed" so the test is independent of the environment.
        mgr._hardened_runtime_available = lambda rt: False  # type: ignore[assignment]
        mgr._playwright = SimpleNamespace(chromium=SimpleNamespace())
        try:
            await mgr._launch()
            raise AssertionError("strict mode should raise on unavailable runtime")
        except RuntimeError as exc:
            assert "camoufox" in str(exc)
            assert "STRICT" in str(exc).upper()

    asyncio.run(run())


def test_lenient_runtime_degrades_to_chromium() -> None:
    async def run() -> None:
        launched = {"chromium": False}

        class _Launcher:
            async def launch(self, **kwargs):
                launched["chromium"] = True
                return SimpleNamespace()

        mgr = BrowserManager(_runtime_config("camoufox", strict=False))
        mgr._hardened_runtime_available = lambda rt: False  # type: ignore[assignment]
        mgr._playwright = SimpleNamespace(chromium=_Launcher())
        await mgr._launch()
        # Degraded to stock chromium and recorded the divergence.
        assert launched["chromium"] is True
        assert mgr.runtime == "chromium"
        assert mgr.requested_runtime == "camoufox"

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# real-outcome feeds proxy health (downstream reject cools the proxy down)
# --------------------------------------------------------------------------- #


def _burn_services(max_consecutive_fails: int = 3):
    from src.assets.proxy_pool import ProxyAsset, ProxyPool
    from src.consumption.accounting import SuccessAccounting
    from src.consumption.ledger import CostLedger, SolveRecord

    ledger = CostLedger()
    proxy_pool = ProxyPool(max_consecutive_fails=max_consecutive_fails)
    proxy = ProxyAsset(id="px-burn", server="http://px:1")
    proxy_pool.add(proxy)
    services = SimpleNamespace(
        ledger=ledger,
        accounting=SuccessAccounting(),
        proxy_pool=proxy_pool,
        session_pool=None,
    )
    return services, proxy, SolveRecord


def test_report_incorrect_burns_proxy_health() -> None:
    async def run() -> None:
        from src.api import routes as routes_mod
        from src.core.services import set_services
        from src.models.task import ReportTaskRequest

        services, proxy, SolveRecord = _burn_services()
        await services.ledger.record(
            SolveRecord(
                task_id="t-burn",
                sitekey="sk",
                task_type="HCaptchaTaskProxyless",
                proxy_id="px-burn",
                proxy_kind="pool_proxy",
                outcome="ready",
                client_key=None,
            )
        )
        set_services(services)  # type: ignore[arg-type]
        try:
            ck = routes_mod.config.client_key or ""
            resp = await routes_mod._report_outcome(
                ReportTaskRequest(clientKey=ck, taskId="t-burn"), correct=False
            )
            assert resp.errorId == 0
            # Downstream reject now hits proxy *health*, not just ranking.
            assert proxy.fail_count == 1
            assert proxy.consecutive_fails == 1
        finally:
            set_services(None)

    asyncio.run(run())


def test_report_correct_reinforces_proxy_health() -> None:
    async def run() -> None:
        from src.api import routes as routes_mod
        from src.core.services import set_services
        from src.models.task import ReportTaskRequest

        services, proxy, SolveRecord = _burn_services()
        await services.ledger.record(
            SolveRecord(
                task_id="t-ok",
                sitekey="sk",
                task_type="HCaptchaTaskProxyless",
                proxy_id="px-burn",
                proxy_kind="pool_proxy",
                outcome="ready",
                client_key=None,
            )
        )
        set_services(services)  # type: ignore[arg-type]
        try:
            ck = routes_mod.config.client_key or ""
            resp = await routes_mod._report_outcome(
                ReportTaskRequest(clientKey=ck, taskId="t-ok"), correct=True
            )
            assert resp.errorId == 0
            assert proxy.success_count == 1
            assert proxy.consecutive_fails == 0
        finally:
            set_services(None)

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# vision cost: grid tiles composed into a single image
# --------------------------------------------------------------------------- #


def _png(color: Tuple[int, int, int]) -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (48, 48), color).save(buf, format="PNG")
    return buf.getvalue()


class _RecClient:
    name = "local"

    def __init__(self, pool: "_RecPool") -> None:
        self._pool = pool

    async def chat(self, *, messages, temperature, max_tokens, timeout):
        from src.assets.model_pool import ModelUsage

        self._pool.calls += 1
        self._pool.last_messages = messages
        return '{"indices":[0,2],"confidence":0.95}', ModelUsage(1, 1)


class _RecPool:
    def __init__(self) -> None:
        self.calls = 0
        self.last_messages = None

    def get(self, name: str) -> _RecClient:
        return _RecClient(self)


def _vision_config(**over):
    base = dict(
        vision_cloud_enabled=False,
        vision_vote_samples=1,
        vision_confidence_threshold=0.6,
        vision_tier2_detail="high",
        vision_stitch_grid=True,
        captcha_timeout=30,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _count_images(messages) -> int:
    user = messages[1]["content"]
    return sum(1 for part in user if isinstance(part, dict) and part.get("type") == "image_url")


def test_vision_stitches_grid_into_single_image() -> None:
    from src.parsing.vision import VisionRequest, VisionRouter

    pool = _RecPool()
    router = VisionRouter(pool, _vision_config(vision_stitch_grid=True))
    req = VisionRequest(
        prompt="select all buses",
        images=[_png((i * 30 % 255, 80, 120)) for i in range(4)],
        task_tier=1,
        shape="grid_select",
    )
    result = asyncio.run(router.classify(req))
    assert result.indices == [0, 2]
    # 4 tiles collapsed into 1 montage image.
    assert _count_images(pool.last_messages) == 1


def test_vision_stitch_disabled_sends_per_tile() -> None:
    from src.parsing.vision import VisionRequest, VisionRouter

    pool = _RecPool()
    router = VisionRouter(pool, _vision_config(vision_stitch_grid=False))
    req = VisionRequest(
        prompt="select all buses",
        images=[_png((i * 30 % 255, 80, 120)) for i in range(4)],
        task_tier=1,
        shape="grid_select",
    )
    asyncio.run(router.classify(req))
    assert _count_images(pool.last_messages) == 4


def test_vision_does_not_stitch_coordinate_shapes() -> None:
    from src.parsing.vision import VisionRequest, VisionRouter

    pool = _RecPool()
    router = VisionRouter(pool, _vision_config(vision_stitch_grid=True))
    # area_bbox normally sends one image; even with 2 it must NOT be stitched
    # (stitching would corrupt the pixel-coordinate answer).
    req = VisionRequest(
        prompt="click the center",
        images=[_png((10, 10, 10)), _png((20, 20, 20))],
        task_tier=1,
        shape="area_bbox",
    )
    asyncio.run(router.classify(req))
    assert _count_images(pool.last_messages) == 2


# --------------------------------------------------------------------------- #
# widget error taxonomy
# --------------------------------------------------------------------------- #


def test_widget_error_classification() -> None:
    from src.services.captcha_errors import (
        NonRetryableWidgetError,
        RateLimitedError,
        RetryableWidgetError,
        classify_widget_error,
    )

    assert isinstance(classify_widget_error("rate-limited"), RateLimitedError)
    assert classify_widget_error("rate-limited").retryable is False
    assert classify_widget_error("rate-limited").outcome == "rate_limited"

    assert isinstance(classify_widget_error("invalid-data"), NonRetryableWidgetError)
    assert classify_widget_error("invalid-data").retryable is False

    assert isinstance(classify_widget_error("network-error"), RetryableWidgetError)
    assert classify_widget_error("network-error").retryable is True

    # Unknown / bare boolean (Turnstile) → retryable default (no behaviour change).
    assert classify_widget_error(True).retryable is True
    assert isinstance(classify_widget_error("challenge-expired"), RetryableWidgetError)
    # The message still contains "widget error" for existing assertions.
    assert "widget error" in str(classify_widget_error("boom", provider="hCaptcha"))


def test_hcaptcha_rate_limit_fails_fast_without_retry() -> None:
    """A rate-limited widget error stops retrying immediately (don't hammer)."""
    from src.services.hcaptcha import HCaptchaSolver

    class _Ctx:
        def __init__(self) -> None:
            self.closed = False

        async def add_init_script(self, script):
            return None

        async def route(self, url, handler):
            return None

        async def new_page(self):
            return _Page()

        async def close(self):
            self.closed = True

    class _Page:
        def __init__(self) -> None:
            self.mouse = SimpleNamespace(move=self._noop)

        async def _noop(self, *a, **k):
            return None

        async def goto(self, *a, **k):
            return None

        async def evaluate(self, script, *a):
            # error-only probe returns rate-limited; combined extractor too.
            if "__omcToken" in script and "__omcError" in script:
                return {"token": None, "error": "rate-limited"}
            return "rate-limited"

        async def wait_for_function(self, *a, **k):
            return None

        def frame_locator(self, sel):
            return self

        def locator(self, sel):
            return self

    class _Manager:
        def __init__(self, ctx) -> None:
            self._ctx = ctx

        async def new_context(self, params):
            return self._ctx, "UA"

    config = SimpleNamespace(
        captcha_retries=3,
        browser_timeout=5,
        poll_budget=1,
        poll_interval=0.01,
        poll_budget_passive=0.05,
        poll_budget_challenge=0.05,
        vision_cloud_enabled=False,
        vision_vote_samples=1,
        vision_confidence_threshold=0.6,
        vision_tier2_detail="high",
        captcha_timeout=5,
        enterprise_require_residential=True,
        human_mouse_enabled=False,
        human_mouse_jitter_ms=0,
        hcaptcha_real_page=False,
    )

    async def run() -> None:
        ctx = _Ctx()
        solver = HCaptchaSolver(config, manager=_Manager(ctx), services=None)
        from src.services.captcha_errors import RateLimitedError

        try:
            await solver.solve(
                {
                    "websiteURL": "https://example.com",
                    "websiteKey": "sk",
                    "type": "HCaptchaTaskProxyless",
                }
            )
            raise AssertionError("expected a RateLimitedError")
        except RateLimitedError:
            pass  # failed fast on the first attempt, no 3× retry loop

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# session pool: reputation-aware eviction
# --------------------------------------------------------------------------- #


def test_session_pool_evicts_lowest_reputation() -> None:
    from src.assets.proxy_pool import ProxyAsset
    from src.assets.session_pool import SessionPool

    class _Ctx:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    async def factory(fp, proxy):
        return _Ctx(), fp.user_agent

    async def run() -> None:
        # Distinct proxies so each session lands in its own egress bucket
        # (sessions are bucketed by proxy identity, not the checkout key).
        pa = ProxyAsset(id="A", server="http://a:1")
        pb = ProxyAsset(id="B", server="http://b:1")
        pc = ProxyAsset(id="C", server="http://c:1")
        pool = SessionPool(factory, size=2, max_solves=8)
        a = await pool.checkout(key="A", proxy=pa)
        b = await pool.checkout(key="B", proxy=pb)
        await pool.release(a, success=True)
        await pool.release(b, success=True)
        # Make A clearly the least valuable idle session.
        a.reputation = 0.1
        b.reputation = 0.9
        a_ctx = a.context
        # A new egress identity needs a slot → the worst idle (A) is evicted.
        c = await pool.checkout(key="C", proxy=pc)
        assert a_ctx.closed is True  # A retired (lowest reputation)
        assert c.id != a.id
        # B (high reputation) survived and is still reusable.
        b2 = await pool.checkout(key="B", proxy=pb)
        assert b2.id == b.id
        await pool.close_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# proxy bandwidth budget
# --------------------------------------------------------------------------- #


def test_proxy_burns_when_bandwidth_cap_exceeded() -> None:
    from src.assets.proxy_pool import ProxyAsset, ProxyPool

    async def run() -> None:
        pool = ProxyPool(max_bytes_per_proxy=100)
        proxy = ProxyAsset(id="p", server="http://p:1")
        pool.add(proxy)
        # A successful solve that blows the quota still burns the proxy.
        await pool.report("p", success=True, bytes_used=150)
        assert proxy.state == "burned"
        assert await pool.checkout() is None

    asyncio.run(run())


def test_proxy_bandwidth_unlimited_by_default() -> None:
    from src.assets.proxy_pool import ProxyAsset, ProxyPool

    async def run() -> None:
        pool = ProxyPool()  # max_bytes_per_proxy=0 → unlimited
        proxy = ProxyAsset(id="p", server="http://p:1")
        pool.add(proxy)
        await pool.report("p", success=True, bytes_used=10**12)
        assert proxy.state != "burned"

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# cross-worker idempotency
# --------------------------------------------------------------------------- #


def test_store_claim_idempotency_first_wins() -> None:
    from src.orchestration.store import InMemoryTaskStore

    async def run() -> None:
        store = InMemoryTaskStore(ttl_seconds=60)
        assert await store.claim_idempotency("k", "t1", 60) == "t1"
        # A later claim for the same key returns the original owner.
        assert await store.claim_idempotency("k", "t2", 60) == "t1"

    asyncio.run(run())


def test_acreate_task_returns_cross_worker_owner_without_spawning() -> None:
    import src.services.task_manager as tm

    class _FakeStore:
        def __init__(self) -> None:
            self.puts = 0

        async def put(self, *a, **k):
            self.puts += 1

        async def claim_idempotency(self, key, task_id, ttl):
            return "owner-on-another-worker"

    async def run() -> None:
        mgr = tm.TaskManager()
        store = _FakeStore()
        mgr.configure(
            SimpleNamespace(
                browser_concurrency=2,
                vision_concurrency=2,
                queue_max_size=10,
                solve_timeout=5,
            ),
            store=store,
        )
        tid = await mgr.acreate_task("T", {}, idempotency_key="dup")
        # Returned the other worker's task id; no local task/spawn/persist.
        assert tid == "owner-on-another-worker"
        assert mgr.get_task(tid) is None
        assert store.puts == 0

    asyncio.run(run())


def test_acreate_task_admits_when_claim_won() -> None:
    import src.services.task_manager as tm

    async def run() -> None:
        mgr = tm.TaskManager()
        mgr.configure(
            SimpleNamespace(
                browser_concurrency=2,
                vision_concurrency=2,
                queue_max_size=10,
                solve_timeout=5,
            )
        )  # default InMemory store: claim returns our own id (won)
        release = asyncio.Event()

        class _Solver:
            async def solve(self, params):
                await release.wait()
                return {"token": "x"}

        mgr.register_solver("T", _Solver(), tm.TaskCategory.BROWSER)
        a = await mgr.acreate_task("T", {}, idempotency_key="k1")
        b = await mgr.acreate_task("T", {}, idempotency_key="k1")
        assert a == b  # same-worker coalesce via in-memory fast path
        assert mgr.get_task(a) is not None
        release.set()
        await asyncio.sleep(0.05)

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# camoufox runtime: context is built without Chromium spoofing
# --------------------------------------------------------------------------- #


class _CamouFakeContext:
    def __init__(self) -> None:
        self.init_scripts: List[str] = []
        self.routes: List[Tuple] = []
        self.listeners: List[Tuple] = []
        self._omc_bytes_used = 0

    async def add_init_script(self, script) -> None:
        self.init_scripts.append(script)

    async def route(self, url, handler) -> None:
        self.routes.append((url, handler))

    def on(self, event, handler) -> None:
        self.listeners.append((event, handler))


class _CamouFakeBrowser:
    def __init__(self) -> None:
        self.new_context_kwargs: List[dict] = []

    async def new_context(self, **kwargs):
        self.new_context_kwargs.append(kwargs)
        return _CamouFakeContext()


def _ctx_config(runtime: str) -> SimpleNamespace:
    return SimpleNamespace(
        browser_headless=True,
        browser_runtime=runtime,
        browser_runtime_strict=False,
        resource_block_enabled=True,
        resource_block_types="image,media,font,stylesheet",
        resource_allow_hosts="hcaptcha.com",
        resource_block_hosts="",
        camoufox_humanize=True,
        camoufox_block_webrtc=True,
        camoufox_os="",
    )


def test_camoufox_context_skips_chromium_spoofing() -> None:
    mgr = BrowserManager(_ctx_config("camoufox"))
    mgr._runtime = "camoufox"
    mgr._camoufox_user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:146.0) "
        "Gecko/20100101 Firefox/146.0"
    )
    mgr._browser = _CamouFakeBrowser()  # type: ignore[assignment]

    fp = generate_fingerprint(seed="cf", timezone_id="Europe/Berlin", locale="de-DE")
    ctx = asyncio.run(
        mgr._build_context(fp, {"server": "http://p:1"}, forced_ua=None)
    )
    kw = mgr._browser.new_context_kwargs[0]  # type: ignore[attr-defined]
    # Geo + proxy are threaded, but NO Chrome client hints and NO forced UA.
    assert kw["locale"] == "de-DE"
    assert kw["timezone_id"] == "Europe/Berlin"
    assert kw["no_viewport"] is True
    assert kw["proxy"] == {"server": "http://p:1"}
    assert "extra_http_headers" not in kw
    assert "user_agent" not in kw
    # Camoufox owns the fingerprint → no Chromium stealth JS injected.
    assert ctx.init_scripts == []
    # Byte accounting + resource interception are engine-agnostic and still wired.
    assert any(event == "response" for event, _ in ctx.listeners)
    assert any(url == "**/*" for url, _ in ctx.routes)


def test_camoufox_context_honours_forced_ua() -> None:
    mgr = BrowserManager(_ctx_config("camoufox"))
    mgr._runtime = "camoufox"
    mgr._camoufox_user_agent = "Firefox/146.0"
    mgr._browser = _CamouFakeBrowser()  # type: ignore[assignment]
    fp = generate_fingerprint(seed="cf2")
    asyncio.run(mgr._build_context(fp, None, forced_ua="ForcedUA/9.9"))
    kw = mgr._browser.new_context_kwargs[0]  # type: ignore[attr-defined]
    assert kw["user_agent"] == "ForcedUA/9.9"


def test_effective_user_agent_resolution() -> None:
    cam = BrowserManager(_ctx_config("camoufox"))
    cam._runtime = "camoufox"
    cam._camoufox_user_agent = "Firefox/146.0"
    # camoufox: forced wins, else engine UA.
    assert cam._effective_user_agent("ChromeUA", "Forced") == "Forced"
    assert cam._effective_user_agent("ChromeUA", None) == "Firefox/146.0"
    # non-camoufox: the Chromium fingerprint UA is authoritative.
    chrome = BrowserManager(_ctx_config("chromium"))
    assert chrome._effective_user_agent("ChromeUA", None) == "ChromeUA"


# --------------------------------------------------------------------------- #
# P1-3: startup engine-version validation (stale _CHROME_MAJOR can't rot silently)
# --------------------------------------------------------------------------- #


def test_engine_version_match_is_silent_ok() -> None:
    """A live engine major equal to the pinned Chrome major passes validation."""
    from src.assets.fingerprint import chrome_major

    async def run() -> None:
        mgr = BrowserManager(_runtime_config("chromium", strict=True))
        mgr._browser = SimpleNamespace(version=f"{chrome_major()}.0.7827.55")
        # Strict mode + matching version → no raise.
        await mgr._validate_engine_version()

    asyncio.run(run())


def test_engine_version_mismatch_raises_in_strict_mode() -> None:
    """A drifted engine major fails fast under BROWSER_RUNTIME_STRICT."""
    async def run() -> None:
        mgr = BrowserManager(_runtime_config("chromium", strict=True))
        mgr._browser = SimpleNamespace(version="131.0.6778.86")  # stale
        try:
            await mgr._validate_engine_version()
            raise AssertionError("expected a version-mismatch RuntimeError")
        except RuntimeError as exc:
            assert "131" in str(exc)
            assert "STRICT" in str(exc).upper()

    asyncio.run(run())


def test_engine_version_mismatch_warns_in_lenient_mode() -> None:
    """A drifted engine major only warns (doesn't raise) when strict is off."""
    async def run() -> None:
        mgr = BrowserManager(_runtime_config("chromium", strict=False))
        mgr._browser = SimpleNamespace(version="131.0.6778.86")
        # Lenient mode → no raise even on mismatch.
        await mgr._validate_engine_version()

    asyncio.run(run())


def test_chromium_context_still_injects_stealth_and_client_hints() -> None:
    """Regression: the camoufox branch must not change the Chromium path."""
    mgr = BrowserManager(_ctx_config("chromium"))
    mgr._browser = _CamouFakeBrowser()  # type: ignore[assignment]
    fp = generate_fingerprint(seed="chrome-path")
    ctx = asyncio.run(mgr._build_context(fp, None))
    kw = mgr._browser.new_context_kwargs[0]  # type: ignore[attr-defined]
    assert "extra_http_headers" in kw  # Chrome client hints present
    assert kw["user_agent"] == fp.user_agent
    assert ctx.init_scripts  # Chromium stealth JS injected


if __name__ == "__main__":
    test_ease_path_starts_and_ends_correctly()
    test_point_in_box_is_inside_and_off_center()
    test_human_click_traces_path_and_presses()
    test_human_click_returns_none_without_mouse()
    test_grid_select_uses_human_path_when_page_present()
    test_grid_select_falls_back_to_locator_click_without_page()
    test_checkbox_click_uses_human_path_when_enabled()
    test_checkbox_click_falls_back_to_locator_click_when_disabled()
    test_client_hints_match_ua_version_and_platform()
    test_context_kwargs_carries_client_hints()
    test_stealth_js_useragentdata_matches_ua()
    test_egress_from_params_surfaces_kind_and_server()
    test_strict_runtime_raises_when_hardened_runtime_unavailable()
    test_lenient_runtime_degrades_to_chromium()
    test_report_incorrect_burns_proxy_health()
    test_report_correct_reinforces_proxy_health()
    test_vision_stitches_grid_into_single_image()
    test_vision_stitch_disabled_sends_per_tile()
    test_vision_does_not_stitch_coordinate_shapes()
    test_widget_error_classification()
    test_hcaptcha_rate_limit_fails_fast_without_retry()
    test_session_pool_evicts_lowest_reputation()
    test_proxy_burns_when_bandwidth_cap_exceeded()
    test_proxy_bandwidth_unlimited_by_default()
    test_store_claim_idempotency_first_wins()
    test_acreate_task_returns_cross_worker_owner_without_spawning()
    test_acreate_task_admits_when_claim_won()
    test_camoufox_context_skips_chromium_spoofing()
    test_camoufox_context_honours_forced_ua()
    test_effective_user_agent_resolution()
    test_engine_version_match_is_silent_ok()
    test_engine_version_mismatch_raises_in_strict_mode()
    test_engine_version_mismatch_warns_in_lenient_mode()
    test_chromium_context_still_injects_stealth_and_client_hints()
    print("ok")
