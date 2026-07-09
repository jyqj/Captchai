"""Tests for shared browser-solver proxy categorisation."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.assets.proxy_pool import ProxyAsset, ProxyPool  # noqa: E402
from src.services.browser_solver import (  # noqa: E402
    BaseBrowserSolver,
    ProxyKind,
    SolveIdentity,
)


class FakeContext:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeManager:
    def __init__(self) -> None:
        self.calls = []
        self.contexts = []

    async def new_context(self, params):
        self.calls.append(dict(params))
        ctx = FakeContext()
        self.contexts.append(ctx)
        return ctx, params.get("userAgent") or "UA"


def _config():
    return SimpleNamespace(
        human_mouse_enabled=False,
        human_mouse_jitter_ms=0,
    )


def test_solve_identity_from_params_collects_resolved_fields() -> None:
    """SolveIdentity.from_params reads the resolved egress/session/geo keys."""
    params = {
        "_proxyKind": "pool_proxy",
        "_egress_server": "http://gw:8080",
        "_pool_proxy_id": "px-1",
        "_sessionId": "sess-9",
        "_used_timezone": "Europe/Berlin",
        "_used_languages": ["de-DE", "en-US"],
    }
    ident = SolveIdentity.from_params(params)
    assert ident.proxy_kind == "pool_proxy"
    assert ident.egress_server == "http://gw:8080"
    assert ident.proxy_id == "px-1"
    assert ident.session_id == "sess-9"
    assert ident.timezone_id == "Europe/Berlin"
    assert ident.languages == ("de-DE", "en-US")
    assert ident.accept_language == "de-DE, en-US"
    # The solution echo carries exactly the four IP/UA-binding fields.
    assert ident.solution_fields() == {
        "proxyKind": "pool_proxy",
        "egressServer": "http://gw:8080",
        "timezoneId": "Europe/Berlin",
        "acceptLanguage": "de-DE, en-US",
    }


def test_solve_identity_defaults_when_params_empty() -> None:
    """A proxyless / mocked solve yields an all-None identity (no KeyErrors)."""
    ident = SolveIdentity.from_params({})
    assert ident.proxy_kind is None
    assert ident.egress_server is None
    assert ident.proxy_id is None
    assert ident.session_id is None
    assert ident.timezone_id is None
    assert ident.languages == ()
    assert ident.accept_language is None
    assert ident.solution_fields() == {
        "proxyKind": None,
        "egressServer": None,
        "timezoneId": None,
        "acceptLanguage": None,
    }


def test_task_proxy_is_explicit_category() -> None:
    async def run() -> None:
        manager = FakeManager()
        solver = BaseBrowserSolver(_config(), manager=manager, services=None)
        params = {
            "websiteKey": "site",
            "proxyType": "http",
            "proxyAddress": "1.2.3.4",
            "proxyPort": 8080,
        }

        ctx = await solver._acquire_context(params)

        assert ctx.proxy_kind == ProxyKind.TASK_PROXY
        assert params["_proxyKind"] == "task_proxy"
        assert manager.calls[0]["proxyAddress"] == "1.2.3.4"
        await solver._release_context(ctx, True, params)
        assert ctx.context.closed is True

    asyncio.run(run())


def test_pool_proxy_is_final_category_when_inventory_selected() -> None:
    async def run() -> None:
        manager = FakeManager()
        pool = ProxyPool()
        pool.add(ProxyAsset(id="pool-1", server="http://pool:8080"))
        services = SimpleNamespace(session_pool=None, proxy_pool=pool)
        solver = BaseBrowserSolver(_config(), manager=manager, services=services)
        params = {"websiteKey": "site"}

        ctx = await solver._acquire_context(params)

        assert ctx.proxy_kind == ProxyKind.POOL_PROXY
        assert ctx.proxy_id == "pool-1"
        assert params["_proxyKind"] == "pool_proxy"
        assert manager.calls[0]["_proxy_override"] == {"server": "http://pool:8080"}
        await solver._release_context(ctx, True, params)
        assert pool.snapshot()[0]["success_count"] == 1

    asyncio.run(run())


def test_no_proxy_and_no_pool_stays_proxyless() -> None:
    async def run() -> None:
        manager = FakeManager()
        services = SimpleNamespace(session_pool=None)
        solver = BaseBrowserSolver(_config(), manager=manager, services=services)
        params = {"websiteKey": "site"}

        ctx = await solver._acquire_context(params)

        assert ctx.proxy_kind == ProxyKind.PROXYLESS
        assert params["_proxyKind"] == "proxyless"
        await solver._release_context(ctx, True, params)

    asyncio.run(run())


def test_egress_task_without_proxy_raises() -> None:
    async def run() -> None:
        manager = FakeManager()
        solver = BaseBrowserSolver(_config(), manager=manager, services=None)
        params = {"websiteKey": "site", "egress": "task"}

        try:
            await solver._acquire_context(params)
        except RuntimeError as exc:
            assert "egress=task" in str(exc)
        else:
            raise AssertionError("egress=task without proxy should raise RuntimeError")

    asyncio.run(run())


def test_egress_pool_with_empty_pool_raises() -> None:
    async def run() -> None:
        manager = FakeManager()
        pool = ProxyPool()  # empty: checkout returns None
        services = SimpleNamespace(session_pool=None, proxy_pool=pool)
        solver = BaseBrowserSolver(_config(), manager=manager, services=services)
        params = {"websiteKey": "site", "egress": "pool"}

        try:
            await solver._acquire_context(params)
        except RuntimeError as exc:
            assert "egress=pool" in str(exc)
        else:
            raise AssertionError("egress=pool with empty pool should raise RuntimeError")

    asyncio.run(run())


def test_egress_proxyless_skips_task_proxy() -> None:
    async def run() -> None:
        manager = FakeManager()
        services = SimpleNamespace(session_pool=None)
        solver = BaseBrowserSolver(_config(), manager=manager, services=services)
        params = {
            "websiteKey": "site",
            "egress": "proxyless",
            "proxyType": "http",
            "proxyAddress": "1.2.3.4",
            "proxyPort": 8080,
        }

        ctx = await solver._acquire_context(params)

        assert ctx.proxy_kind == ProxyKind.PROXYLESS
        assert params["_proxyKind"] == "proxyless"
        await solver._release_context(ctx, True, params)

    asyncio.run(run())


def test_egress_auto_legacy_behavior() -> None:
    async def run() -> None:
        # Subcase 1: no egress, has task proxy → TASK_PROXY.
        manager = FakeManager()
        solver = BaseBrowserSolver(_config(), manager=manager, services=None)
        params = {
            "websiteKey": "site",
            "proxyType": "http",
            "proxyAddress": "1.2.3.4",
            "proxyPort": 8080,
        }

        ctx = await solver._acquire_context(params)
        assert ctx.proxy_kind == ProxyKind.TASK_PROXY
        assert params["_proxyKind"] == "task_proxy"
        await solver._release_context(ctx, True, params)

        # Subcase 2: no egress, no proxy, no services → PROXYLESS (server egress).
        manager2 = FakeManager()
        solver2 = BaseBrowserSolver(_config(), manager=manager2, services=None)
        params2 = {"websiteKey": "site"}

        ctx2 = await solver2._acquire_context(params2)
        assert ctx2.proxy_kind == ProxyKind.PROXYLESS
        assert params2["_proxyKind"] == "proxyless"
        await solver2._release_context(ctx2, True, params2)

    asyncio.run(run())


def test_release_context_stashes_proxy_bytes() -> None:
    """Fresh-context release reads _omc_bytes_used into params for the recorder."""
    async def run() -> None:
        from src.assets.proxy_pool import ProxyPool

        manager = FakeManager()
        pool = ProxyPool()
        pool.add(ProxyAsset(id="pool-1", server="http://pool:8080"))
        services = SimpleNamespace(session_pool=None, proxy_pool=pool)
        solver = BaseBrowserSolver(_config(), manager=manager, services=services)
        params = {"websiteKey": "site"}

        ctx = await solver._acquire_context(params)
        # Simulate the response listener accumulating bytes on the context.
        ctx.context._omc_bytes_used = 42_613  # type: ignore[attr-defined]
        await solver._release_context(ctx, solved=True, params=params)

        assert params["_proxy_bytes"] == 42_613
        # Proxy pool report reflects the byte count too.
        snap = pool.snapshot()[0]
        assert snap["bytes_used"] == 42_613

    asyncio.run(run())


def test_release_context_session_skips_proxy_bytes() -> None:
    """Warm-session release must NOT stash proxy_bytes (context outlives the solve)."""
    async def run() -> None:
        from src.assets.session_pool import BrowserSession

        class FakeSession:
            id = "sess-1"

            def __init__(self) -> None:
                self.context = FakeContext()

        manager = FakeManager()

        class FakeSessionPool:
            async def release(self, session, *, success, burned=False) -> None:
                return None

        services = SimpleNamespace(session_pool=FakeSessionPool(), proxy_pool=None)
        solver = BaseBrowserSolver(_config(), manager=manager, services=services)
        params = {"websiteKey": "site"}

        from src.services.browser_solver import SolveContext

        session = FakeSession()
        ctx = SolveContext(
            context=session.context,
            user_agent="UA",
            proxy_kind=ProxyKind.PROXYLESS,
            session=session,
            session_id="sess-1",
        )
        await solver._release_context(ctx, solved=True, params=params)
        # Session release path must not set _proxy_bytes.
        assert "_proxy_bytes" not in params

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# WP1: new auto priority (task → pool → proxyless) with session_pool wired
# --------------------------------------------------------------------------- #


class FakeSession:
    """Minimal BrowserSession stand-in that pairs with a FakeContext."""

    def __init__(self, session_id: str, proxy_id: str | None = None) -> None:
        self.id = session_id
        self.context = FakeContext()
        self.proxy = SimpleNamespace(id=proxy_id) if proxy_id else None
        self.user_agent = "UA"


class FakeSessionPool:
    """SessionPool stand-in that bucket-checkouts and tracks releases."""

    def __init__(self) -> None:
        self.PROXYLESS_KEY = "proxyless"
        self._idle: dict[str, list] = {}
        self.released: list[tuple] = []
        self.next_id = 0

    async def checkout(self, *, key, proxy=None, sitekey=None):
        bucket = self._idle.get(key, [])
        if bucket:
            return bucket.pop()
        sid = f"sess-{self.next_id}"
        self.next_id += 1
        return FakeSession(sid, proxy_id=proxy.id if proxy else None)

    async def release(self, session, *, success, burned=False):
        self.released.append((session.id, success, burned))
        key = session.proxy.id if session.proxy else self.PROXYLESS_KEY
        self._idle.setdefault(key, []).append(session)


def test_auto_uses_pool_when_pool_has_proxies_and_session_pool_wired() -> None:
    """auto + no task proxy + pool available → POOL_PROXY via sticky warm session."""
    async def run() -> None:
        manager = FakeManager()
        pool = ProxyPool()
        pool.add(ProxyAsset(id="pool-1", server="http://pool:8080"))
        session_pool = FakeSessionPool()
        services = SimpleNamespace(session_pool=session_pool, proxy_pool=pool)
        solver = BaseBrowserSolver(_config(), manager=manager, services=services)
        params = {"websiteKey": "site"}

        ctx = await solver._acquire_context(params)

        assert ctx.proxy_kind == ProxyKind.POOL_PROXY
        assert ctx.proxy_id == "pool-1"
        assert ctx.session is not None
        assert ctx.session.proxy.id == "pool-1"
        assert params["_proxyKind"] == "pool_proxy"
        assert params["_pool_proxy_id"] == "pool-1"
        assert params["_sessionId"] == ctx.session.id
        # Pool path with session_pool must NOT call manager.new_context — the
        # warm session provides the context directly via the factory.
        assert manager.calls == []
        await solver._release_context(ctx, True, params)
        # Proxy pool was reported per solve.
        assert pool.snapshot()[0]["success_count"] == 1

    asyncio.run(run())


def test_auto_falls_to_proxyless_when_pool_empty_with_session_pool() -> None:
    """auto + no task proxy + empty pool → PROXYLESS via warm server-IP session."""
    async def run() -> None:
        manager = FakeManager()
        pool = ProxyPool()  # empty
        session_pool = FakeSessionPool()
        services = SimpleNamespace(session_pool=session_pool, proxy_pool=pool)
        solver = BaseBrowserSolver(_config(), manager=manager, services=services)
        params = {"websiteKey": "site"}

        ctx = await solver._acquire_context(params)

        assert ctx.proxy_kind == ProxyKind.PROXYLESS
        assert ctx.session is not None
        assert ctx.session.proxy is None
        assert params["_proxyKind"] == "proxyless"
        assert manager.calls == []
        await solver._release_context(ctx, True, params)

    asyncio.run(run())


def test_pool_path_reuses_sticky_session_for_same_proxy() -> None:
    """Two consecutive pool solves for the same proxy reuse the same session."""
    async def run() -> None:
        manager = FakeManager()
        pool = ProxyPool()
        pool.add(ProxyAsset(id="pool-1", server="http://pool:8080"))
        session_pool = FakeSessionPool()
        services = SimpleNamespace(session_pool=session_pool, proxy_pool=pool)
        solver = BaseBrowserSolver(_config(), manager=manager, services=services)

        params1 = {"websiteKey": "site"}
        ctx1 = await solver._acquire_context(params1)
        sess1_id = ctx1.session.id
        await solver._release_context(ctx1, True, params1)

        params2 = {"websiteKey": "site"}
        ctx2 = await solver._acquire_context(params2)
        # Same sticky session was reused (returned to the pool bucket between
        # calls).
        assert ctx2.session.id == sess1_id
        assert ctx2.proxy_id == "pool-1"
        await solver._release_context(ctx2, True, params2)

    asyncio.run(run())


def test_pool_session_release_reports_to_proxy_pool_without_bytes() -> None:
    """Warm pool-session release reports to proxy pool with bytes_used=0."""
    async def run() -> None:
        manager = FakeManager()
        pool = ProxyPool()
        pool.add(ProxyAsset(id="pool-1", server="http://pool:8080"))
        session_pool = FakeSessionPool()
        services = SimpleNamespace(session_pool=session_pool, proxy_pool=pool)
        solver = BaseBrowserSolver(_config(), manager=manager, services=services)
        params = {"websiteKey": "site-key"}

        ctx = await solver._acquire_context(params)
        await solver._release_context(ctx, solved=True, params=params)
        # Per-solve byte attribution is skipped for warm sessions.
        assert "_proxy_bytes" not in params
        snap = pool.snapshot()[0]
        assert snap["success_count"] == 1
        # Per-sitekey report was filed.
        assert "site-key" in snap["sitekeys"]
        assert snap["sitekeys"]["site-key"]["success"] == 1

    asyncio.run(run())


def test_egress_pool_with_session_pool_uses_warm_session() -> None:
    """egress=pool + session_pool wired → warm session, no manager.new_context."""
    async def run() -> None:
        manager = FakeManager()
        pool = ProxyPool()
        pool.add(ProxyAsset(id="pool-1", server="http://pool:8080"))
        session_pool = FakeSessionPool()
        services = SimpleNamespace(session_pool=session_pool, proxy_pool=pool)
        solver = BaseBrowserSolver(_config(), manager=manager, services=services)
        params = {"websiteKey": "site", "egress": "pool"}

        ctx = await solver._acquire_context(params)
        assert ctx.proxy_kind == ProxyKind.POOL_PROXY
        assert ctx.session is not None
        assert ctx.session.proxy.id == "pool-1"
        assert manager.calls == []  # warm session, no new_context
        await solver._release_context(ctx, True, params)

    asyncio.run(run())


def test_egress_proxyless_wins_over_pool_when_pool_has_proxies() -> None:
    """egress=proxyless + pool has proxies → still PROXYLESS (explicit wins)."""
    async def run() -> None:
        manager = FakeManager()
        pool = ProxyPool()
        pool.add(ProxyAsset(id="pool-1", server="http://pool:8080"))
        session_pool = FakeSessionPool()
        services = SimpleNamespace(session_pool=session_pool, proxy_pool=pool)
        solver = BaseBrowserSolver(_config(), manager=manager, services=services)
        params = {"websiteKey": "site", "egress": "proxyless"}

        ctx = await solver._acquire_context(params)
        assert ctx.proxy_kind == ProxyKind.PROXYLESS
        assert ctx.session is not None
        assert ctx.session.proxy is None
        assert params["_proxyKind"] == "proxyless"
        # Pool proxy was NOT checked out — the pool still has its single proxy
        # untouched (success_count == 0 after release).
        await solver._release_context(ctx, True, params)
        assert pool.snapshot()[0]["success_count"] == 0

    asyncio.run(run())


def test_task_proxy_wins_over_pool_when_pool_has_proxies() -> None:
    """auto + task proxy + pool has proxies → TASK_PROXY (task proxy wins)."""
    async def run() -> None:
        manager = FakeManager()
        pool = ProxyPool()
        pool.add(ProxyAsset(id="pool-1", server="http://pool:8080"))
        session_pool = FakeSessionPool()
        services = SimpleNamespace(session_pool=session_pool, proxy_pool=pool)
        solver = BaseBrowserSolver(_config(), manager=manager, services=services)
        params = {
            "websiteKey": "site",
            "proxyType": "http",
            "proxyAddress": "1.2.3.4",
            "proxyPort": 8080,
        }

        ctx = await solver._acquire_context(params)
        assert ctx.proxy_kind == ProxyKind.TASK_PROXY
        assert ctx.session is None  # fresh context, no warm session reuse
        assert ctx.proxy_id is None  # pool proxy not used
        assert params["_proxyKind"] == "task_proxy"
        # Pool proxy was NOT checked out.
        await solver._release_context(ctx, True, params)
        assert pool.snapshot()[0]["success_count"] == 0

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# WP3: _acquire_pool threads proxy geo + seed onto params
# WP5: _acquire_pool honours _required_proxy_kind and raises a specific error
# --------------------------------------------------------------------------- #


def test_acquire_pool_stashes_pool_geo_and_seed() -> None:
    """_acquire_pool stashes _pool_geo + _proxy_seed for fresh-context builds."""
    async def run() -> None:
        manager = FakeManager()
        pool = ProxyPool()
        de_proxy = ProxyAsset(
            id="de-1",
            server="http://de:8080",
            country="DE",
            timezone="Europe/Berlin",
            locale="de-DE",
        )
        pool.add(de_proxy)
        services = SimpleNamespace(session_pool=None, proxy_pool=pool)
        solver = BaseBrowserSolver(_config(), manager=manager, services=services)
        params = {"websiteKey": "site", "egress": "pool"}

        ctx = await solver._acquire_context(params)
        assert ctx.proxy_kind == ProxyKind.POOL_PROXY
        assert params["_pool_geo"] == {
            "timezone": "Europe/Berlin",
            "locale": "de-DE",
            "country": "DE",
        }
        assert params["_proxy_seed"] == "de-1"
        await solver._release_context(ctx, True, params)

    asyncio.run(run())


def test_acquire_pool_required_kind_filters_checkout() -> None:
    """_required_proxy_kind=residential filters proxy_pool.checkout by kind."""
    async def run() -> None:
        manager = FakeManager()
        pool = ProxyPool()
        pool.add(ProxyAsset(id="dc-1", server="http://dc:8080", kind="datacenter"))
        pool.add(
            ProxyAsset(id="res-1", server="http://res:8080", kind="residential")
        )
        services = SimpleNamespace(session_pool=None, proxy_pool=pool)
        solver = BaseBrowserSolver(_config(), manager=manager, services=services)
        params = {
            "websiteKey": "site",
            "egress": "pool",
            "_required_proxy_kind": "residential",
        }

        ctx = await solver._acquire_context(params)
        assert ctx.proxy_kind == ProxyKind.POOL_PROXY
        assert ctx.proxy_id == "res-1"
        await solver._release_context(ctx, True, params)

    asyncio.run(run())


def test_acquire_pool_required_kind_raises_when_none_available() -> None:
    """_required_proxy_kind=residential + only datacenter proxies → raises."""
    async def run() -> None:
        manager = FakeManager()
        pool = ProxyPool()
        pool.add(ProxyAsset(id="dc-1", server="http://dc:8080", kind="datacenter"))
        services = SimpleNamespace(session_pool=None, proxy_pool=pool)
        solver = BaseBrowserSolver(_config(), manager=manager, services=services)
        params = {
            "websiteKey": "site",
            "egress": "pool",
            "_required_proxy_kind": "residential",
        }

        try:
            await solver._acquire_context(params)
            raise AssertionError("expected residential requirement to raise")
        except RuntimeError as exc:
            assert "residential" in str(exc).lower()

    asyncio.run(run())


def test_force_fresh_context_bypasses_warm_session() -> None:
    """_force_fresh_context makes a pool solve build a fresh context, not reuse a session."""
    async def run() -> None:
        manager = FakeManager()
        pool = ProxyPool()
        pool.add(ProxyAsset(id="pool-1", server="http://pool:8080"))
        session_pool = FakeSessionPool()
        services = SimpleNamespace(session_pool=session_pool, proxy_pool=pool)
        solver = BaseBrowserSolver(_config(), manager=manager, services=services)
        params = {
            "websiteKey": "site",
            "egress": "pool",
            "_force_fresh_context": True,
        }

        ctx = await solver._acquire_context(params)
        assert ctx.proxy_kind == ProxyKind.POOL_PROXY
        # Fresh context: no warm session was checked out; manager built one
        # bound to the pool proxy via _proxy_override.
        assert ctx.session is None
        assert manager.calls and manager.calls[0]["_proxy_override"] == {
            "server": "http://pool:8080"
        }
        await solver._release_context(ctx, True, params)

    asyncio.run(run())


def test_acquire_pool_probes_geo_when_unannotated() -> None:
    """P0-2b: a pool proxy with no geo is probed on checkout; geo threads through."""
    async def run() -> None:
        import src.services.browser_solver as bs

        manager = FakeManager()
        pool = ProxyPool()
        pool.add(ProxyAsset(id="p-nogeo", server="http://user:pass@gw:8080"))
        services = SimpleNamespace(session_pool=None, proxy_pool=pool)
        # Enable the probe on the config and stub the network fetch.
        config = SimpleNamespace(
            human_mouse_enabled=False,
            human_mouse_jitter_ms=0,
            proxy_geo_probe=True,
            proxy_geo_probe_url="http://ip-api.com/json",
        )
        solver = BaseBrowserSolver(config, manager=manager, services=services)

        import src.assets.geo_probe as gp

        async def fake_fetch(url, proxy_url, timeout):
            return {"countryCode": "DE"}

        orig = gp._httpx_fetch_json
        gp._httpx_fetch_json = fake_fetch  # type: ignore[assignment]
        try:
            params = {"websiteKey": "site", "egress": "pool"}
            ctx = await solver._acquire_context(params)
        finally:
            gp._httpx_fetch_json = orig  # type: ignore[assignment]

        assert ctx.proxy_kind == ProxyKind.POOL_PROXY
        # The probed geo was threaded onto params for the fingerprint build.
        assert params["_pool_geo"]["country"] == "DE"
        assert params["_pool_geo"]["timezone"] == "Europe/Berlin"
        assert params["_pool_geo"]["locale"] == "de-DE"
        # And persisted back onto the pool so later checkouts skip the probe.
        assert pool.snapshot()[0]["country"] == "DE"
        assert pool.snapshot()[0]["geo_probed"] is True
        await solver._release_context(ctx, True, params)

    asyncio.run(run())


def test_stash_fingerprint_geo_reads_session_fingerprint() -> None:
    """_stash_fingerprint_geo reads session.fingerprint for warm-session solves."""
    async def run() -> None:
        from src.assets.fingerprint import generate_fingerprint
        from src.services.browser_solver import SolveContext

        manager = FakeManager()
        solver = BaseBrowserSolver(_config(), manager=manager, services=None)
        fp = generate_fingerprint(
            seed="de", timezone_id="Europe/Berlin", locale="de-DE"
        )

        class FakeSession:
            fingerprint = fp

        solve_ctx = SolveContext(
            context=FakeContext(),
            user_agent="UA",
            proxy_kind=ProxyKind.POOL_PROXY,
            session=FakeSession(),
        )
        params: dict = {}
        solver._stash_fingerprint_geo(solve_ctx, params)
        assert params["_used_timezone"] == "Europe/Berlin"
        assert params["_used_languages"][0] == "de-DE"

    asyncio.run(run())
