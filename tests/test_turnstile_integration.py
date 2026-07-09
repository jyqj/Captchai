"""Integration tests for the Turnstile solver.

These drive ``TurnstileSolver.solve`` end-to-end with a fake Playwright
page/context/manager so the real ``_solve_once`` → ``_poll_token`` path runs
with no browser and no network. This is the runtime coverage the solver was
missing: the only existing Turnstile tests (``tests/test_api.py``) replace the
solver with an ``AsyncMock`` and never execute a single line of
``turnstile.py`` — so a runtime fault on the sole success path (e.g. a missing
``import asyncio`` used by ``_poll_token``) sailed through green.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.consumption.ledger import CostLedger  # noqa: E402
from src.services.turnstile import TurnstileSolver  # noqa: E402


# ── fakes ──────────────────────────────────────────────────────


class FakeLocator:
    def __init__(self, page, selector):
        self._page = page
        self._selector = selector
        self.clicked = False

    @property
    def first(self):
        return self

    async def click(self, timeout=None):
        self.clicked = True
        self._page.clicks.append(self._selector)


class FakeFrameLocator:
    def __init__(self, page):
        self._page = page

    def locator(self, selector):
        return FakeLocator(self._page, selector)


class FakeRoute:
    """Minimal Playwright Route so a captured route handler can be exercised."""

    def __init__(self, resource_type="document"):
        self.request = SimpleNamespace(resource_type=resource_type)
        self.fulfilled = None
        self.continued = False

    async def fulfill(self, *, status=200, content_type=None, body=None):
        self.fulfilled = {"status": status, "content_type": content_type, "body": body}

    async def continue_(self):
        self.continued = True


class FakePage:
    def __init__(self, *, token="cf_" + "t" * 40, error=None, token_after=0):
        self.clicks = []
        self._token = token
        self._error = error
        self._token_after = token_after
        self._poll_calls = 0
        self.mouse = SimpleNamespace(move=self._noop)

    async def _noop(self, *a, **k):
        return None

    def frame_locator(self, selector):
        return FakeFrameLocator(self)

    async def goto(self, url, wait_until=None, timeout=None):
        return None

    async def evaluate(self, script, *args):
        self._poll_calls += 1
        ready = self._poll_calls > self._token_after
        return {
            "token": self._token if (ready and self._error is None) else None,
            "error": self._error,
        }

    async def wait_for_function(self, script, timeout=None):
        return None


class FakeContext:
    def __init__(self, page):
        self._page = page
        self.closed = False
        self.routes = []
        self.init_scripts = []

    async def route(self, url, handler):
        self.routes.append((url, handler))

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
        return self._context, "UA-TS"


def _config():
    return SimpleNamespace(
        captcha_retries=1,
        browser_timeout=5,
        poll_budget=2,
        poll_interval=0.01,
        retry_backoff_base=0.0,
        retry_backoff_max=0.0,
        human_mouse_enabled=False,
        human_mouse_jitter_ms=0,
    )


def _services():
    ledger = CostLedger()

    class Acc:
        def __init__(self):
            self.records = []

        async def record(self, sitekey, outcome, *, proxy_kind=None, model=None):
            self.records.append((sitekey, outcome, model))

    return SimpleNamespace(
        ledger=ledger,
        accounting=Acc(),
        session_pool=None,
        proxy_pool=None,
    )


# ── tests ──────────────────────────────────────────────────────


def test_turnstile_solve_returns_token_and_records() -> None:
    """The core regression: a full solve exercises _poll_token and returns a token.

    Before the ``import asyncio`` fix, ``_poll_token`` raised
    ``NameError: name 'asyncio' is not defined`` on its first line, so every
    attempt failed and ``solve`` raised "Turnstile failed after N attempts".
    """
    async def run() -> None:
        page = FakePage(token="cf_" + "t" * 40)
        context = FakeContext(page)
        manager = FakeManager(context)
        services = _services()

        solver = TurnstileSolver(_config(), manager=manager, services=services)
        result = await solver.solve(
            {
                "websiteURL": "https://example.com",
                "websiteKey": "0x4AAAAAAA",
                "type": "TurnstileTaskProxyless",
            }
        )

        assert result["token"].startswith("cf_")
        assert result["userAgent"] == "UA-TS"
        # The checkbox inside the challenges.cloudflare.com iframe was clicked.
        assert any("checkbox" in c for c in page.clicks)
        # A successful solve was metered in the ledger.
        summary = await services.ledger.summary()
        assert summary["count"] == 1
        assert summary["by_outcome"].get("ready") == 1
        assert context.closed is True

    asyncio.run(run())


def test_turnstile_poll_waits_then_returns_token() -> None:
    """A token that only appears on a later poll still resolves (event-driven wait)."""
    async def run() -> None:
        page = FakePage(token="cf_" + "z" * 40, token_after=2)
        context = FakeContext(page)
        manager = FakeManager(context)
        solver = TurnstileSolver(_config(), manager=manager, services=None)

        result = await solver.solve(
            {
                "websiteURL": "https://example.com",
                "websiteKey": "0x4AAAAAAA",
                "type": "TurnstileTaskProxyless",
            }
        )
        assert result["token"].startswith("cf_")
        assert page._poll_calls >= 3

    asyncio.run(run())


def test_turnstile_widget_error_surfaced() -> None:
    """A widget error-callback is surfaced as a classified (retryable) error."""
    async def run() -> None:
        page = FakePage(token=None, error="network-error")
        context = FakeContext(page)
        manager = FakeManager(context)
        solver = TurnstileSolver(_config(), manager=manager, services=None)

        try:
            await solver.solve(
                {
                    "websiteURL": "https://example.com",
                    "websiteKey": "0x4AAAAAAA",
                    "type": "TurnstileTaskProxyless",
                }
            )
            assert False, "expected widget error to propagate"
        except RuntimeError as exc:
            assert "widget error" in str(exc)

    asyncio.run(run())


def test_turnstile_forwards_render_options_to_injected_page() -> None:
    """action / cData / chlPageData are forwarded into the injected widget page."""
    async def run() -> None:
        page = FakePage(token="cf_" + "t" * 40)
        context = FakeContext(page)
        manager = FakeManager(context)
        solver = TurnstileSolver(_config(), manager=manager, services=None)

        await solver.solve(
            {
                "websiteURL": "https://example.com",
                "websiteKey": "0x4AAAAAAA",
                "type": "TurnstileTaskProxyless",
                "action": "login",
                "cData": "session-123",
                "chlPageData": "chl-abc",
            }
        )

        # Exercise the captured document route handler to inspect the HTML body.
        assert context.routes
        _url, handler = context.routes[0]
        route = FakeRoute(resource_type="document")
        await handler(route)
        body = route.fulfilled["body"]
        assert '"action": "login"' in body
        assert '"cData": "session-123"' in body
        assert '"chlPageData": "chl-abc"' in body
        assert "0x4AAAAAAA" in body

        # Non-document requests are passed through untouched.
        other = FakeRoute(resource_type="script")
        await handler(other)
        assert other.continued is True

    asyncio.run(run())


def test_turnstile_real_page_hooks_render_via_init_script() -> None:
    """Real-page parity: TURNSTILE_REAL_PAGE hooks turnstile.render, no route.

    The injected path route-fulfils the document; real-page mode instead adds an
    init script that wraps the real page's own ``turnstile.render`` and disables
    resource blocking (loading the real page like a human browser).
    """
    async def run() -> None:
        page = FakePage(token="cf_" + "t" * 40)
        context = FakeContext(page)
        manager = FakeManager(context)
        config = _config()
        config.turnstile_real_page = True
        solver = TurnstileSolver(config, manager=manager, services=None)

        result = await solver.solve(
            {
                "websiteURL": "https://example.com",
                "websiteKey": "0x4AAAAAAA",
                "type": "TurnstileTaskProxyless",
                "action": "login",
            }
        )
        assert result["token"].startswith("cf_")
        # Real-page mode: an init script hooking turnstile.render was added and
        # forwards the render options; NO synthetic document route was set.
        assert context.init_scripts
        script = context.init_scripts[0]
        assert "turnstile" in script
        assert '"action": "login"' in script
        assert context.routes == []
        # Resource blocking was turned off for the real page.
        assert getattr(context, "_omc_block_resources", True) is False

    asyncio.run(run())


def test_turnstile_release_solved_false_on_timeout() -> None:
    """When no token is ever produced, the context is released with solved=False."""
    async def run() -> None:
        page = FakePage(token=None, error=None)
        context = FakeContext(page)
        manager = FakeManager(context)
        solver = TurnstileSolver(_config(), manager=manager, services=None)

        released: list = []
        orig_release = solver._release_context

        async def spy_release(solve_ctx, solved, params):
            released.append(solved)
            await orig_release(solve_ctx, solved, params)

        solver._release_context = spy_release  # type: ignore[assignment]

        try:
            await solver.solve(
                {
                    "websiteURL": "https://example.com",
                    "websiteKey": "0x4AAAAAAA",
                    "type": "TurnstileTaskProxyless",
                }
            )
            assert False, "expected solve to raise after timeout"
        except RuntimeError:
            pass

        assert released and released[0] is False

    asyncio.run(run())


if __name__ == "__main__":
    test_turnstile_solve_returns_token_and_records()
    test_turnstile_poll_waits_then_returns_token()
    test_turnstile_widget_error_surfaced()
    test_turnstile_forwards_render_options_to_injected_page()
    test_turnstile_real_page_hooks_render_via_init_script()
    test_turnstile_release_solved_false_on_timeout()
    print("ok")
