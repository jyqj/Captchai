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


def test_pool_geo_drives_fingerprint_timezone_and_locale() -> None:
    """WP3: _pool_geo on params threads proxy geo into the fingerprint."""
    params = {
        "websiteKey": "site",
        "_pool_geo": {
            "timezone": "Europe/Berlin",
            "locale": "de-DE",
            "country": "DE",
        },
        "_proxy_seed": "de-proxy-id",
    }
    opts = resolve_context_options(_config(), params)
    assert opts.fingerprint.timezone_id == "Europe/Berlin"
    assert opts.fingerprint.locale == "de-DE"
    assert opts.fingerprint.languages[0] == "de-DE"
    # The actually-used geo is stashed back onto params for the solver.
    assert params["_used_timezone"] == "Europe/Berlin"
    assert params["_used_languages"][0] == "de-DE"


def test_pool_geo_seed_makes_fingerprint_deterministic() -> None:
    """WP3: _proxy_seed makes the fingerprint deterministic per proxy."""
    params = {
        "websiteKey": "site",
        "_pool_geo": {"timezone": "Europe/Berlin", "locale": "de-DE", "country": "DE"},
        "_proxy_seed": "sticky-proxy-1",
    }
    fp1 = resolve_context_options(_config(), dict(params)).fingerprint
    fp2 = resolve_context_options(_config(), dict(params)).fingerprint
    assert fp1 == fp2  # same seed → same coherent fingerprint


def test_no_pool_geo_keeps_random_fingerprint() -> None:
    """WP3: without _pool_geo, the fingerprint stays random (no regression)."""
    params = {"websiteKey": "site"}
    opts = resolve_context_options(_config(), params)
    # Some timezone / locale is always set (coherent fingerprint), but it's
    # NOT forced to a proxy geo.
    assert opts.fingerprint.timezone_id is not None
    assert opts.fingerprint.locale is not None


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


# --------------------------------------------------------------------------- #
# WP4: _build_context registers the per-context resource interception route
# --------------------------------------------------------------------------- #


class _RecordingFakeBrowser:
    """Stands in for the playwright Browser; returns a recording FakeContext."""

    def __init__(self) -> None:
        self.last_kwargs: dict | None = None

    async def new_context(self, **kwargs):
        self.last_kwargs = kwargs
        return _RecordingFakeContext()


class _RecordingFakeContext:
    """Records route registrations, init scripts, and event listeners."""

    def __init__(self) -> None:
        self.routes: list[tuple] = []  # (url_pattern, handler)
        self.init_scripts: list[str] = []
        self.listeners: list[tuple] = []  # (event, handler)
        self._omc_bytes_used = 0

    async def add_init_script(self, script: str) -> None:
        self.init_scripts.append(script)

    async def route(self, url: str, handler) -> None:
        self.routes.append((url, handler))

    def on(self, event: str, handler) -> None:
        self.listeners.append((event, handler))


def _resource_block_config(enabled: bool) -> SimpleNamespace:
    return SimpleNamespace(
        browser_headless=True,
        browser_runtime="chromium",
        resource_block_enabled=enabled,
        resource_block_types="image,media,font,stylesheet",
        resource_allow_hosts=(
            "hcaptcha.com,challenges.cloudflare.com,google.com,"
            "recaptcha.net,gstatic.com,cloudflare.com"
        ),
        resource_block_hosts="",
    )


def test_build_context_registers_resource_route_when_enabled() -> None:
    """WP4: RESOURCE_BLOCK_ENABLED=true → _build_context registers **/* route."""
    import asyncio

    from src.assets.fingerprint import generate_fingerprint
    from src.services.browser import BrowserManager

    manager = BrowserManager(_resource_block_config(True))
    manager._browser = _RecordingFakeBrowser()

    fp = generate_fingerprint(seed="wp4-enabled")
    ctx = asyncio.run(manager._build_context(fp, None))

    # The resource route is registered with the catch-all pattern.
    assert any(url == "**/*" for url, _ in ctx.routes)
    # The response listener is still registered (no regression from WP1).
    assert any(event == "response" for event, _ in ctx.listeners)
    # The init script (stealth) is still applied.
    assert ctx.init_scripts


def test_build_context_does_not_register_resource_route_when_disabled() -> None:
    """WP4: RESOURCE_BLOCK_ENABLED=false → no **/* route registered."""
    import asyncio

    from src.assets.fingerprint import generate_fingerprint
    from src.services.browser import BrowserManager

    manager = BrowserManager(_resource_block_config(False))
    manager._browser = _RecordingFakeBrowser()

    fp = generate_fingerprint(seed="wp4-disabled")
    ctx = asyncio.run(manager._build_context(fp, None))

    # No catch-all resource route.
    assert not any(url == "**/*" for url, _ in ctx.routes)
    # The response listener is registered independently of resource blocking.
    assert any(event == "response" for event, _ in ctx.listeners)


def test_build_context_resource_route_ordered_before_solver_fulfill() -> None:
    """WP4: resource handler is registered first; solver fulfill handler second.

    Playwright runs route handlers in registration order. _build_context
    registers the resource handler before returning; hcaptcha/turnstile
    register ``context.route(website_url, _fulfill_document)`` afterwards.
    The synthetic document request has resource_type "document" (not in the
    block set), so the resource handler continues it through to the fulfill
    handler — verify the ordering recorded by the fake context.
    """
    import asyncio

    from src.assets.fingerprint import generate_fingerprint
    from src.services.browser import BrowserManager

    manager = BrowserManager(_resource_block_config(True))
    fake_browser = _RecordingFakeBrowser()
    manager._browser = fake_browser

    fp = generate_fingerprint(seed="wp4-order")
    ctx = asyncio.run(manager._build_context(fp, None))

    # Simulate the solver registering its fulfill handler after _build_context.
    async def _solver_fulfill(route) -> None:
        await route.continue_()

    asyncio.run(ctx.route("https://example.com/checkout", _solver_fulfill))

    # The resource handler (**) was registered before the solver's fulfill
    # handler (specific URL) — order matters for Playwright's dispatch.
    assert ctx.routes[0][0] == "**/*"
    assert ctx.routes[1][0] == "https://example.com/checkout"


# --------------------------------------------------------------------------- #
# P0-1: per-context resource-blocking toggle (real-page / enterprise safety)
# --------------------------------------------------------------------------- #


class _FakeRoute:
    """Minimal Route stand-in exposing request.frame.page.context + resource_type."""

    def __init__(self, context, url: str, resource_type: str) -> None:
        page = SimpleNamespace(context=context)
        frame = SimpleNamespace(page=page)
        self.request = SimpleNamespace(
            url=url, resource_type=resource_type, frame=frame
        )
        self.continued = False
        self.aborted = False

    async def continue_(self) -> None:
        self.continued = True

    async def abort(self) -> None:
        self.aborted = True


def test_resource_handler_aborts_blockable_type_by_default() -> None:
    """Default (blocking on): an image on a non-allowlisted host is aborted."""
    import asyncio

    from src.assets.fingerprint import generate_fingerprint
    from src.services.browser import BrowserManager

    manager = BrowserManager(_resource_block_config(True))
    manager._browser = _RecordingFakeBrowser()
    ctx = asyncio.run(manager._build_context(generate_fingerprint(seed="rb1"), None))

    route = _FakeRoute(ctx, "https://cdn.example.com/x.png", "image")
    asyncio.run(manager._resource_handler(route))
    assert route.aborted is True
    assert route.continued is False


def test_resource_handler_passes_everything_when_context_blocking_off() -> None:
    """P0-1: set_context_resource_blocking(ctx, False) → blockable types pass through."""
    import asyncio

    from src.assets.fingerprint import generate_fingerprint
    from src.services.browser import BrowserManager, set_context_resource_blocking

    manager = BrowserManager(_resource_block_config(True))
    manager._browser = _RecordingFakeBrowser()
    ctx = asyncio.run(manager._build_context(generate_fingerprint(seed="rb2"), None))

    # Real-page / enterprise solve disables blocking on the context.
    set_context_resource_blocking(ctx, False)

    route = _FakeRoute(ctx, "https://cdn.example.com/style.css", "stylesheet")
    asyncio.run(manager._resource_handler(route))
    # The real page's own CSS is NOT aborted — it loads like a human browser.
    assert route.continued is True
    assert route.aborted is False


if __name__ == "__main__":
    test_egress_proxyless_strips_task_proxy()
    test_auto_with_task_proxy_binds_task_proxy()
    test_proxy_override_wins()
    test_fingerprint_not_sitekey_deterministic()
    test_fingerprint_varies_across_many_draws()
    test_caller_user_agent_wins_over_fingerprint()
    test_count_response_bytes_accumulates_content_length()
    test_pool_geo_drives_fingerprint_timezone_and_locale()
    test_pool_geo_seed_makes_fingerprint_deterministic()
    test_no_pool_geo_keeps_random_fingerprint()
    test_build_context_registers_resource_route_when_enabled()
    test_build_context_does_not_register_resource_route_when_disabled()
    test_build_context_resource_route_ordered_before_solver_fulfill()
    test_resource_handler_aborts_blockable_type_by_default()
    test_resource_handler_passes_everything_when_context_blocking_off()
    print("ok")
