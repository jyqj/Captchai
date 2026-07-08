"""Tests for BrowserManager context-option resolution.

Covers two cross-cutting correctness properties that span the browser solver
and the browser manager:

* ``egress=proxyless`` must produce a context with NO proxy even when the
  caller supplied task-proxy fields — the solver has already classified the
  solve as proxyless, so the manager must not silently rebind the task proxy.
* Fingerprints must NOT be sitekey-deterministic — every context gets a fresh
  random coherent identity so detectors cannot cluster solves of a target.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.services.browser import resolve_context_options  # noqa: E402


def _config() -> SimpleNamespace:
    return SimpleNamespace()


def test_egress_proxyless_strips_task_proxy() -> None:
    """egress=proxyless + task proxy fields → proxy is None, not the task proxy."""
    params = {
        "websiteKey": "site",
        "egress": "proxyless",
        "_proxyKind": "proxyless",
        "proxyType": "http",
        "proxyAddress": "1.2.3.4",
        "proxyPort": 8080,
    }
    opts = resolve_context_options(_config(), params)
    assert opts.proxy is None


def test_auto_with_task_proxy_binds_task_proxy() -> None:
    """auto + task proxy (no _proxyKind override) → proxy dict comes from params."""
    params = {
        "websiteKey": "site",
        "proxyType": "http",
        "proxyAddress": "1.2.3.4",
        "proxyPort": 8080,
    }
    opts = resolve_context_options(_config(), params)
    assert opts.proxy == {"server": "http://1.2.3.4:8080"}


def test_proxy_override_wins() -> None:
    """_proxy_override (pool proxy) takes precedence over task proxy fields."""
    params = {
        "websiteKey": "site",
        "_proxyKind": "pool_proxy",
        "_proxy_override": {"server": "http://pool:8080"},
        "proxyType": "http",
        "proxyAddress": "1.2.3.4",
        "proxyPort": 8080,
    }
    opts = resolve_context_options(_config(), params)
    assert opts.proxy == {"server": "http://pool:8080"}


def test_fingerprint_not_sitekey_deterministic() -> None:
    """Two resolves with the same sitekey must produce different fingerprints."""
    params = {"websiteKey": "site"}
    fp1 = resolve_context_options(_config(), dict(params)).fingerprint
    fp2 = resolve_context_options(_config(), dict(params)).fingerprint
    # With 5 profiles + random extras, collisions are possible but unlikely
    # across 2 draws. Assert at least one distinguishing field differs.
    assert (
        (fp1.user_agent, fp1.screen_width, fp1.locale, fp1.hardware_concurrency)
        != (fp2.user_agent, fp2.screen_width, fp2.locale, fp2.hardware_concurrency)
    )


def test_fingerprint_varies_across_many_draws() -> None:
    """Across 20 resolves, more than 2 distinct identities appear."""
    params = {"websiteKey": "site"}
    identities = set()
    for _ in range(20):
        fp = resolve_context_options(_config(), dict(params)).fingerprint
        identities.add((fp.user_agent, fp.screen_width, fp.locale))
    assert len(identities) > 2


def test_caller_user_agent_wins_over_fingerprint() -> None:
    """A caller-supplied userAgent is honoured so the token binds to it."""
    params = {"websiteKey": "site", "userAgent": "UA-CALLER"}
    opts = resolve_context_options(_config(), params)
    assert opts.user_agent == "UA-CALLER"


def test_count_response_bytes_accumulates_content_length() -> None:
    """The response listener sums Content-Length onto the owning context."""
    from src.services.browser import _count_response_bytes

    class FakeContext:
        def __init__(self) -> None:
            self._omc_bytes_used = 0

    class FakePage:
        def __init__(self, ctx) -> None:
            self.context = ctx

    class FakeFrame:
        def __init__(self, page) -> None:
            self.page = page

    class FakeResponse:
        def __init__(self, ctx, headers) -> None:
            self.headers = headers
            self.frame = FakeFrame(FakePage(ctx))

    ctx = FakeContext()
    # Three responses with Content-Length headers.
    for cl in (1024, 2048, 512):
        _count_response_bytes(FakeResponse(ctx, {"content-length": str(cl)}))
    assert ctx._omc_bytes_used == 3584

    # A response without Content-Length is skipped, not counted as 0.
    _count_response_bytes(FakeResponse(ctx, {}))
    assert ctx._omc_bytes_used == 3584

    # Malformed Content-Length is swallowed, doesn't raise.
    _count_response_bytes(FakeResponse(ctx, {"content-length": "not-a-number"}))
    assert ctx._omc_bytes_used == 3584


if __name__ == "__main__":
    test_egress_proxyless_strips_task_proxy()
    test_auto_with_task_proxy_binds_task_proxy()
    test_proxy_override_wins()
    test_fingerprint_not_sitekey_deterministic()
    test_fingerprint_varies_across_many_draws()
    test_caller_user_agent_wins_over_fingerprint()
    test_count_response_bytes_accumulates_content_length()
    print("ok")
