"""Smoke tests for the reCAPTCHA v2/v3 migration to BaseBrowserSolver.

Verifies the solvers route through ``_acquire_context`` / ``_release_context``
and thread the ``solved`` flag correctly. Uses a fake Playwright page/context
and monkeypatches ``_acquire_context`` / ``_release_context`` so no real
browser or network is involved.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.services.browser_solver import ProxyKind, SolveContext  # noqa: E402
from src.services.recaptcha_v2 import RecaptchaV2Solver  # noqa: E402
from src.services.recaptcha_v3 import RecaptchaV3Solver  # noqa: E402


class FakeContext:
    def __init__(self, page):
        self._page = page
        self.closed = False

    async def new_page(self):
        return self._page

    async def close(self):
        self.closed = True


class FakeManager:
    def __init__(self, context):
        self._context = context

    async def new_context(self, params):
        return self._context, "UA-FAKE"


class FakePage:
    """Minimal Playwright Page stand-in: token evaluate, no-op mouse/goto."""

    def __init__(self, *, token="tok-" + "x" * 40):
        self._token = token
        self.goto_wait_untils: list = []
        self.mouse = SimpleNamespace(move=self._noop)

    async def _noop(self, *a, **k):
        return None

    async def goto(self, url, wait_until=None, timeout=None):
        self.goto_wait_untils.append(wait_until)
        return None

    async def wait_for_selector(self, selector, timeout=None):
        # Smoke test has no challenge bframe — simulate timeout so the v2
        # checkbox path falls through to the token check.
        raise asyncio.TimeoutError()

    async def wait_for_function(self, script, timeout=None):
        return None

    async def evaluate(self, script, *args):
        return self._token


def _config():
    return SimpleNamespace(
        captcha_retries=1,
        browser_timeout=5,
        human_mouse_enabled=False,
        human_mouse_jitter_ms=0,
    )


def test_recaptcha_v2_uses_acquire_and_release_context() -> None:
    """v2 solve routes through BaseBrowserSolver._acquire/_release_context."""
    async def run() -> None:
        page = FakePage()
        context = FakeContext(page)
        manager = FakeManager(context)
        solver = RecaptchaV2Solver(_config(), manager=manager, services=None)

        acquired: list = []
        released: list = []

        async def fake_acquire(params):
            acquired.append(dict(params))
            return SolveContext(
                context=context,
                user_agent="UA-FAKE",
                proxy_kind=ProxyKind.PROXYLESS,
            )

        async def fake_release(solve_ctx, solved, params):
            released.append((solve_ctx, solved))

        solver._acquire_context = fake_acquire  # type: ignore[assignment]
        solver._release_context = fake_release  # type: ignore[assignment]

        async def fake_checkbox(p, params=None):
            return "tok-" + "x" * 40

        solver._solve_checkbox = fake_checkbox  # type: ignore[assignment]

        result = await solver.solve(
            {
                "websiteURL": "https://example.com",
                "websiteKey": "sitekey-1",
                "type": "RecaptchaV2TaskProxyless",
            }
        )

        assert result["gRecaptchaResponse"].startswith("tok-")
        assert result["userAgent"] == "UA-FAKE"
        assert len(acquired) == 1
        assert len(released) == 1
        # Solved=True must be threaded to release so the warm session is
        # returned to the idle bucket rather than burned.
        assert released[0][1] is True

    asyncio.run(run())


def test_recaptcha_v2_release_called_with_solved_false_on_failure() -> None:
    """When the solve raises, _release_context is called with solved=False."""
    async def run() -> None:
        page = FakePage(token=None)  # evaluate returns None → no token
        context = FakeContext(page)
        manager = FakeManager(context)
        solver = RecaptchaV2Solver(_config(), manager=manager, services=None)

        released: list = []

        async def fake_acquire(params):
            return SolveContext(
                context=context,
                user_agent="UA-FAKE",
                proxy_kind=ProxyKind.PROXYLESS,
            )

        async def fake_release(solve_ctx, solved, params):
            released.append((solve_ctx, solved))

        solver._acquire_context = fake_acquire  # type: ignore[assignment]
        solver._release_context = fake_release  # type: ignore[assignment]

        async def fake_checkbox(p, params=None):
            return None  # no token → _solve_once raises "Invalid token"

        solver._solve_checkbox = fake_checkbox  # type: ignore[assignment]

        try:
            await solver.solve(
                {
                    "websiteURL": "https://example.com",
                    "websiteKey": "sitekey-1",
                    "type": "RecaptchaV2TaskProxyless",
                }
            )
            assert False, "expected solve to raise"
        except RuntimeError as exc:
            assert "Invalid reCAPTCHA v2 token" in str(exc)

        assert released
        assert released[0][1] is False

    asyncio.run(run())


def test_recaptcha_v2_goto_uses_domcontentloaded() -> None:
    """v2 _solve_once navigates with wait_until='domcontentloaded' (not networkidle)."""
    async def run() -> None:
        page = FakePage()
        context = FakeContext(page)
        manager = FakeManager(context)
        solver = RecaptchaV2Solver(_config(), manager=manager, services=None)

        async def fake_acquire(params):
            return SolveContext(
                context=context, user_agent="UA-FAKE", proxy_kind=ProxyKind.PROXYLESS
            )

        async def fake_release(solve_ctx, solved, params):
            return None

        solver._acquire_context = fake_acquire  # type: ignore[assignment]
        solver._release_context = fake_release  # type: ignore[assignment]

        async def fake_checkbox(p, params=None):
            return "tok-" + "x" * 40

        solver._solve_checkbox = fake_checkbox  # type: ignore[assignment]

        await solver.solve(
            {
                "websiteURL": "https://example.com",
                "websiteKey": "sitekey-1",
                "type": "RecaptchaV2TaskProxyless",
            }
        )
        assert page.goto_wait_untils == ["domcontentloaded"]

    asyncio.run(run())


def test_recaptcha_v3_uses_acquire_and_release_context() -> None:
    """v3 solve routes through BaseBrowserSolver._acquire/_release_context."""
    async def run() -> None:
        page = FakePage()
        context = FakeContext(page)
        manager = FakeManager(context)
        solver = RecaptchaV3Solver(_config(), manager=manager, services=None)

        acquired: list = []
        released: list = []

        async def fake_acquire(params):
            acquired.append(dict(params))
            return SolveContext(
                context=context,
                user_agent="UA-FAKE",
                proxy_kind=ProxyKind.PROXYLESS,
            )

        async def fake_release(solve_ctx, solved, params):
            released.append((solve_ctx, solved))

        solver._acquire_context = fake_acquire  # type: ignore[assignment]
        solver._release_context = fake_release  # type: ignore[assignment]

        result = await solver.solve(
            {
                "websiteURL": "https://example.com",
                "websiteKey": "sitekey-1",
                "pageAction": "verify",
                "type": "RecaptchaV3TaskProxyless",
            }
        )

        assert result["gRecaptchaResponse"].startswith("tok-")
        assert result["userAgent"] == "UA-FAKE"
        assert len(acquired) == 1
        assert len(released) == 1
        assert released[0][1] is True

    asyncio.run(run())


def test_recaptcha_v3_goto_uses_domcontentloaded() -> None:
    """v3 _solve_once navigates with wait_until='domcontentloaded' (not networkidle)."""
    async def run() -> None:
        page = FakePage()
        context = FakeContext(page)
        manager = FakeManager(context)
        solver = RecaptchaV3Solver(_config(), manager=manager, services=None)

        async def fake_acquire(params):
            return SolveContext(
                context=context, user_agent="UA-FAKE", proxy_kind=ProxyKind.PROXYLESS
            )

        async def fake_release(solve_ctx, solved, params):
            return None

        solver._acquire_context = fake_acquire  # type: ignore[assignment]
        solver._release_context = fake_release  # type: ignore[assignment]

        await solver.solve(
            {
                "websiteURL": "https://example.com",
                "websiteKey": "sitekey-1",
                "pageAction": "verify",
                "type": "RecaptchaV3TaskProxyless",
            }
        )
        assert page.goto_wait_untils == ["domcontentloaded"]

    asyncio.run(run())


def test_recaptcha_v3_release_called_with_solved_false_on_failure() -> None:
    """When v3 evaluate returns no token, _release_context is called with solved=False."""
    async def run() -> None:
        page = FakePage(token=None)
        context = FakeContext(page)
        manager = FakeManager(context)
        solver = RecaptchaV3Solver(_config(), manager=manager, services=None)

        released: list = []

        async def fake_acquire(params):
            return SolveContext(
                context=context, user_agent="UA-FAKE", proxy_kind=ProxyKind.PROXYLESS
            )

        async def fake_release(solve_ctx, solved, params):
            released.append((solve_ctx, solved))

        solver._acquire_context = fake_acquire  # type: ignore[assignment]
        solver._release_context = fake_release  # type: ignore[assignment]

        try:
            await solver.solve(
                {
                    "websiteURL": "https://example.com",
                    "websiteKey": "sitekey-1",
                    "pageAction": "verify",
                    "type": "RecaptchaV3TaskProxyless",
                }
            )
            assert False, "expected solve to raise"
        except RuntimeError as exc:
            assert "Invalid token" in str(exc)

        assert released
        assert released[0][1] is False

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# Audio transcription routes through the ModelPool (metered + budget-gated)
# --------------------------------------------------------------------------- #


def test_v2_audio_transcription_routes_through_model_pool() -> None:
    """_transcribe_audio uses model_pool.cloud.transcribe_audio and meters it."""
    from src.assets.model_pool import ModelUsage

    async def run() -> None:
        calls = {"n": 0, "model": None}

        class _CloudClient:
            async def transcribe_audio(self, audio_bytes, *, model=None, filename=None, timeout=None):
                calls["n"] += 1
                calls["model"] = model
                return "3 1 4", ModelUsage(input_tokens=0, output_tokens=4)

        class _Budget:
            def __init__(self):
                self.checks = []

            async def check(self, client_key, est, *, model=None):
                self.checks.append((client_key, est, model))
                return SimpleNamespace(allowed=True)

        budget = _Budget()
        services = SimpleNamespace(
            model_pool=SimpleNamespace(cloud=_CloudClient()),
            budget=budget,
        )
        config = SimpleNamespace(
            captcha_retries=1,
            cloud_audio_model="whisper-1",
            cloud_base_url="http://cloud",
            cloud_api_key="k",
            captcha_timeout=10,
        )
        solver = RecaptchaV2Solver(config, manager=None, services=services)

        params = {"_clientKey": "acct-1"}
        text = await solver._transcribe_audio(b"FAKEMP3", params)
        assert text == "3 1 4"
        assert calls["n"] == 1
        assert calls["model"] == "whisper-1"
        # Budget was consulted for the paid cloud transcription.
        assert budget.checks and budget.checks[0][2] == "cloud"
        # Usage was stashed for the ledger recorder.
        meter = params["_vision"]
        assert meter.last_model == "cloud"
        assert meter.total_vision_calls == 1

    asyncio.run(run())


def test_v2_audio_transcription_denied_by_budget_raises() -> None:
    """A budget denial stops the (unmetered-in-the-past) cloud transcription."""
    async def run() -> None:
        class _CloudClient:
            async def transcribe_audio(self, *a, **k):
                raise AssertionError("must not call the model when budget denies")

        class _Budget:
            async def check(self, client_key, est, *, model=None):
                return SimpleNamespace(allowed=False, downgrade_to="local")

        services = SimpleNamespace(
            model_pool=SimpleNamespace(cloud=_CloudClient()),
            budget=_Budget(),
        )
        config = SimpleNamespace(
            cloud_audio_model="whisper-1", captcha_timeout=10
        )
        solver = RecaptchaV2Solver(config, manager=None, services=services)
        try:
            await solver._transcribe_audio(b"FAKEMP3", {"_clientKey": "acct-1"})
            assert False, "expected a budget denial to raise"
        except RuntimeError as exc:
            assert "budget" in str(exc).lower()

    asyncio.run(run())


if __name__ == "__main__":
    test_recaptcha_v2_uses_acquire_and_release_context()
    test_v2_audio_transcription_routes_through_model_pool()
    test_v2_audio_transcription_denied_by_budget_raises()
    test_recaptcha_v2_release_called_with_solved_false_on_failure()
    test_recaptcha_v2_goto_uses_domcontentloaded()
    test_recaptcha_v3_uses_acquire_and_release_context()
    test_recaptcha_v3_goto_uses_domcontentloaded()
    test_recaptcha_v3_release_called_with_solved_false_on_failure()
    print("ok")
