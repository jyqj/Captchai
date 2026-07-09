"""Tests for the opt-in token-trust verification closure (WP6).

Covers the pure config/parsing helpers and the solver-level closure: a wired
verifier turns a siteverify verdict into automatic real-outcome accounting and,
on rejection, retries on a fresh egress instead of returning a dead token. With
no verifier (the default) solving is unchanged.
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
from src.consumption.token_verify import (  # noqa: E402
    HttpTokenVerifier,
    build_token_verifier,
    parse_secret_map,
)
from src.services.turnstile import TurnstileSolver  # noqa: E402


# ── pure helpers ───────────────────────────────────────────────


def test_parse_secret_map_parses_pairs_and_skips_junk() -> None:
    parsed = parse_secret_map("sk1:secret1, sk2:sec:ret2 ,bad, :nope, ok:")
    # Secrets keep colons after the first; blank/half entries dropped.
    assert parsed == {"sk1": "secret1", "sk2": "sec:ret2"}


def test_build_token_verifier_off_by_default() -> None:
    assert build_token_verifier(SimpleNamespace(token_verify_enabled=False)) is None
    # Enabled but no secrets → still None.
    assert (
        build_token_verifier(
            SimpleNamespace(token_verify_enabled=True, token_verify_secrets="")
        )
        is None
    )


def test_build_token_verifier_when_enabled_with_secrets() -> None:
    verifier = build_token_verifier(
        SimpleNamespace(
            token_verify_enabled=True,
            token_verify_secrets="sk1:secret1",
            token_verify_timeout=5.0,
        )
    )
    assert isinstance(verifier, HttpTokenVerifier)
    assert verifier.has_secret("sk1")
    assert not verifier.has_secret("other")


def test_http_verifier_unknown_without_secret() -> None:
    async def run() -> None:
        verifier = HttpTokenVerifier({"sk1": "secret1"})
        # No secret for this sitekey → verdict is unknown (None), no network.
        verdict = await verifier.verify(
            "tok", provider="turnstile", sitekey="other"
        )
        assert verdict is None

    asyncio.run(run())


# ── solver closure ─────────────────────────────────────────────


class _FakePage:
    def __init__(self, token: str) -> None:
        self._token = token
        self.mouse = SimpleNamespace(move=self._noop)

    async def _noop(self, *a, **k):
        return None

    def frame_locator(self, selector):
        return _FakeFrame()

    async def goto(self, url, wait_until=None, timeout=None):
        return None

    async def evaluate(self, script, *args):
        return {"token": self._token, "error": None}

    async def wait_for_function(self, script, timeout=None):
        return None


class _FakeFrame:
    def locator(self, selector):
        return self

    @property
    def first(self):
        return self

    async def click(self, timeout=None):
        return None


class _FakeContext:
    def __init__(self, page) -> None:
        self._page = page
        self.closed = False

    async def route(self, url, handler):
        return None

    async def new_page(self):
        return self._page

    async def close(self):
        self.closed = True


class _FakeManager:
    def __init__(self, context) -> None:
        self._context = context

    async def new_context(self, params):
        return self._context, "UA"


class _Accounting:
    def __init__(self) -> None:
        self.opt: list = []
        self.real: list = []

    async def record(self, sitekey, outcome, *, proxy_kind=None, model=None):
        self.opt.append((sitekey, outcome))

    async def record_real_outcome(self, sitekey, *, success, proxy_kind=None, model=None):
        self.real.append((sitekey, success))


class _FakeVerifier:
    def __init__(self, verdict) -> None:
        self.verdict = verdict
        self.calls: list = []

    async def verify(self, token, *, provider, sitekey, remote_ip=None):
        self.calls.append((token, provider, sitekey))
        return self.verdict


def _config(retries: int = 1):
    return SimpleNamespace(
        captcha_retries=retries,
        browser_timeout=5,
        poll_budget=1,
        poll_interval=0.01,
        retry_backoff_base=0.0,
        retry_backoff_max=0.0,
        human_mouse_enabled=False,
        human_mouse_jitter_ms=0,
    )


def _services(verifier):
    return SimpleNamespace(
        ledger=CostLedger(),
        accounting=_Accounting(),
        token_verifier=verifier,
        session_pool=None,
        proxy_pool=None,
    )


def _run_turnstile(verifier, retries=1):
    async def run():
        page = _FakePage("cf_" + "t" * 40)
        context = _FakeContext(page)
        services = _services(verifier)
        solver = TurnstileSolver(
            _config(retries), manager=_FakeManager(context), services=services
        )
        result = None
        error = None
        try:
            result = await solver.solve(
                {
                    "websiteURL": "https://example.com",
                    "websiteKey": "sk1",
                    "type": "TurnstileTaskProxyless",
                }
            )
        except RuntimeError as exc:
            error = exc
        return result, error, services

    return asyncio.run(run())


def test_verified_token_returns_and_records_real_success() -> None:
    verifier = _FakeVerifier(verdict=True)
    result, error, services = _run_turnstile(verifier)
    assert error is None
    assert result["token"].startswith("cf_")
    # Verifier was consulted with the provider + sitekey.
    assert verifier.calls and verifier.calls[0][1] == "turnstile"
    assert verifier.calls[0][2] == "sk1"
    # Real-outcome accounting closed automatically (a success).
    assert services.accounting.real == [("sk1", True)]


def test_rejected_token_retries_then_fails_and_records_real_failure() -> None:
    verifier = _FakeVerifier(verdict=False)
    result, error, services = _run_turnstile(verifier, retries=2)
    # A siteverify rejection is not returned to the caller; retries exhausted.
    assert result is None
    assert error is not None and "Turnstile failed after 2 attempts" in str(error)
    # Verified (and closed the loop as a real failure) on every attempt.
    assert len(verifier.calls) == 2
    assert services.accounting.real == [("sk1", False), ("sk1", False)]


def test_unknown_verdict_returns_token_without_real_record() -> None:
    verifier = _FakeVerifier(verdict=None)
    result, error, services = _run_turnstile(verifier)
    assert error is None
    assert result["token"].startswith("cf_")
    # Unknown verdict → caller-driven loop preserved, no real outcome recorded.
    assert services.accounting.real == []


def test_no_verifier_is_unchanged_behaviour() -> None:
    result, error, services = _run_turnstile(verifier=None)
    assert error is None
    assert result["token"].startswith("cf_")
    assert services.accounting.real == []


if __name__ == "__main__":
    test_parse_secret_map_parses_pairs_and_skips_junk()
    test_build_token_verifier_off_by_default()
    test_build_token_verifier_when_enabled_with_secrets()
    test_http_verifier_unknown_without_secret()
    test_verified_token_returns_and_records_real_success()
    test_rejected_token_retries_then_fails_and_records_real_failure()
    test_unknown_verdict_returns_token_without_real_record()
    test_no_verifier_is_unchanged_behaviour()
    print("ok")
