"""Tests for the asset-management layer (fingerprint / proxy / token / session).

Follows the repo's existing style: plain synchronous ``def test_*`` functions
that drive coroutines with ``asyncio.run(...)`` (this project does not use
pytest-asyncio). No network calls and no real browser are involved: session
tests use a ``FakeContext`` and a fake async ``context_factory``.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    _ = sys.path.insert(0, str(PROJECT_ROOT))

from src.assets.fingerprint import (
    FingerprintProfile,
    build_stealth_js,
    context_kwargs,
    generate_fingerprint,
)
from src.assets.proxy_pool import ProxyAsset, ProxyPool, proxy_from_params
from src.assets.session_pool import BrowserSession, SessionPool
from src.assets.token_cache import TokenCache


# --------------------------------------------------------------------------- #
# fingerprint
# --------------------------------------------------------------------------- #


def test_fingerprint_deterministic_with_seed() -> None:
    a = generate_fingerprint(seed="sticky-1")
    b = generate_fingerprint(seed="sticky-1")
    assert a == b
    c = generate_fingerprint(seed="sticky-2")
    # Different seed should (almost certainly) yield a different profile.
    assert (a.user_agent, a.screen_width, a.locale) != (
        c.user_agent,
        c.screen_width,
        c.locale,
    ) or a != c


def test_fingerprint_is_coherent_ua_platform_webgl() -> None:
    for seed in [str(i) for i in range(40)]:
        fp = generate_fingerprint(seed=seed)
        if fp.platform == "Win32":
            assert "Windows NT" in fp.user_agent
            assert "Direct3D11" in fp.webgl_renderer
        elif fp.platform == "MacIntel":
            assert "Macintosh" in fp.user_agent
            assert "Apple" in fp.webgl_vendor or "Intel" in fp.webgl_vendor
        assert fp.languages[0].startswith(fp.locale.split("-")[0])


def test_fingerprint_overrides_timezone_and_locale() -> None:
    fp = generate_fingerprint(
        seed="x", timezone_id="Asia/Tokyo", locale="en-GB"
    )
    assert fp.timezone_id == "Asia/Tokyo"
    assert fp.locale == "en-GB"


def test_build_stealth_js_contains_fp_values() -> None:
    fp = generate_fingerprint(seed="stealth")
    js = build_stealth_js(fp)
    assert "webdriver" in js
    assert "37445" in js and "37446" in js
    assert fp.webgl_vendor in js
    assert fp.webgl_renderer in js
    assert str(fp.hardware_concurrency) in js
    assert str(fp.device_memory) in js
    assert fp.languages[0] in js


def test_context_kwargs_includes_timezone_and_proxy() -> None:
    fp = generate_fingerprint(seed="ck")
    proxy = {"server": "http://host:8080", "username": "u", "password": "p"}
    kwargs = context_kwargs(fp, proxy=proxy)
    assert kwargs["timezone_id"] == fp.timezone_id
    assert kwargs["locale"] == fp.locale
    assert kwargs["user_agent"] == fp.user_agent
    assert kwargs["viewport"] == {
        "width": fp.screen_width,
        "height": fp.screen_height,
    }
    assert kwargs["proxy"] == proxy

    no_proxy = context_kwargs(fp)
    assert "proxy" not in no_proxy


# --------------------------------------------------------------------------- #
# proxy_pool
# --------------------------------------------------------------------------- #


def test_proxy_from_params_single_string() -> None:
    p = proxy_from_params({"proxy": "http://user:pass@1.2.3.4:8080"})
    assert p is not None
    assert p.server == "http://1.2.3.4:8080"
    assert p.username == "user"
    assert p.password == "pass"
    pw = p.playwright_proxy()
    assert pw == {
        "server": "http://1.2.3.4:8080",
        "username": "user",
        "password": "pass",
    }


def test_proxy_from_params_split_fields() -> None:
    p = proxy_from_params(
        {
            "proxyType": "socks5",
            "proxyAddress": "9.9.9.9",
            "proxyPort": 1080,
            "proxyLogin": "u",
            "proxyPassword": "p",
        }
    )
    assert p is not None
    assert p.server == "socks5://9.9.9.9:1080"
    assert p.username == "u"
    assert p.password == "p"


def test_proxy_from_params_none_when_absent() -> None:
    assert proxy_from_params({}) is None
    assert proxy_from_params({"proxyAddress": "1.2.3.4"}) is None


def test_empty_pool_checkout_returns_none() -> None:
    pool = ProxyPool()
    assert asyncio.run(pool.checkout()) is None


def test_checkout_skips_burned() -> None:
    async def scenario() -> ProxyAsset:
        pool = ProxyPool()
        burned = ProxyAsset(id="a", server="http://a:1", state="burned")
        healthy = ProxyAsset(id="b", server="http://b:1")
        pool.add(burned)
        pool.add(healthy)
        chosen = await pool.checkout()
        assert chosen is not None
        return chosen

    chosen = asyncio.run(scenario())
    assert chosen.id == "b"


def test_report_triggers_cooldown_and_resets_on_success() -> None:
    async def scenario():
        pool = ProxyPool(cooldown_seconds=60, max_consecutive_fails=3)
        proxy = ProxyAsset(id="p", server="http://p:1")
        pool.add(proxy)

        await pool.report("p", success=False)
        await pool.report("p", success=False)
        assert proxy.state == "healthy"  # not yet at threshold
        assert proxy.consecutive_fails == 2

        await pool.report("p", success=False)
        assert proxy.state == "cooldown"
        assert proxy.cooldown_until > time.monotonic()

        # A cooling proxy is skipped by checkout.
        assert await pool.checkout() is None

        # Success rehabilitates and resets the streak.
        await pool.report("p", success=True)
        assert proxy.state == "healthy"
        assert proxy.consecutive_fails == 0
        chosen = await pool.checkout()
        assert chosen is not None and chosen.id == "p"

    asyncio.run(scenario())


def test_sitekey_ranking_prefers_proven_proxy() -> None:
    async def scenario():
        pool = ProxyPool()
        good = ProxyAsset(id="good", server="http://good:1")
        other = ProxyAsset(id="other", server="http://other:1")
        pool.add(good)
        pool.add(other)
        # "good" has a strong record for this sitekey.
        await pool.report_sitekey("good", "site-1", success=True)
        await pool.report_sitekey("good", "site-1", success=True)
        await pool.report_sitekey("other", "site-1", success=False)
        chosen = await pool.checkout(sitekey="site-1")
        assert chosen is not None and chosen.id == "good"

    asyncio.run(scenario())


def test_pool_snapshot_shape() -> None:
    pool = ProxyPool()
    pool.add(ProxyAsset(id="p", server="http://p:1", kind="residential"))
    snap = pool.snapshot()
    assert len(snap) == 1
    row = snap[0]
    assert row["id"] == "p"
    assert row["kind"] == "residential"
    assert "success_rate" in row and "state" in row


# --------------------------------------------------------------------------- #
# token_cache
# --------------------------------------------------------------------------- #


def test_token_cache_put_get_hit() -> None:
    async def scenario():
        cache = TokenCache(ttl_seconds=60)
        await cache.put("sk", "1.2.3.4", "UA", "tok-123")
        got = await cache.get("sk", "1.2.3.4", "UA")
        assert got == "tok-123"
        # Different UA / IP is a different bucket -> miss.
        assert await cache.get("sk", "1.2.3.4", "OTHER") is None
        assert await cache.get("sk", "9.9.9.9", "UA") is None
        # Proxyless bucket is distinct and stable.
        await cache.put("sk", None, "UA", "tok-none")
        assert await cache.get("sk", None, "UA") == "tok-none"

    asyncio.run(scenario())


def test_token_cache_expiry_and_purge() -> None:
    async def scenario():
        cache = TokenCache(ttl_seconds=0)  # everything expires immediately
        await cache.put("sk", "1.2.3.4", "UA", "tok")
        time.sleep(0.01)
        assert await cache.get("sk", "1.2.3.4", "UA") is None

        cache2 = TokenCache(ttl_seconds=0)
        await cache2.put("sk", "1.2.3.4", "UA", "tok")
        await cache2.put("sk2", "1.2.3.4", "UA", "tok2")
        time.sleep(0.01)
        removed = await cache2.purge_expired()
        assert removed == 2
        assert len(cache2) == 0

    asyncio.run(scenario())


def test_token_cache_lru_eviction() -> None:
    async def scenario():
        cache = TokenCache(ttl_seconds=60, max_size=2)
        await cache.put("a", None, "UA", "1")
        await cache.put("b", None, "UA", "2")
        await cache.put("c", None, "UA", "3")  # evicts "a" (LRU)
        assert await cache.get("a", None, "UA") is None
        assert await cache.get("b", None, "UA") == "2"
        assert await cache.get("c", None, "UA") == "3"

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# session_pool
# --------------------------------------------------------------------------- #


class FakeContext:
    """Stand-in for a Playwright BrowserContext that records close()."""

    def __init__(self, index: int, *, async_close: bool = True) -> None:
        self.index = index
        self.closed = False
        self._async_close = async_close

    def close(self):
        if self._async_close:
            async def _c():
                self.closed = True

            return _c()
        self.closed = True
        return None


def _make_factory(*, async_close: bool = True):
    """Return (factory, state) where state tracks call count and contexts."""
    state = {"calls": 0, "contexts": []}

    async def factory(fingerprint: FingerprintProfile, proxy):
        idx = state["calls"]
        state["calls"] += 1
        ctx = FakeContext(idx, async_close=async_close)
        state["contexts"].append(ctx)
        return ctx, fingerprint.user_agent

    return factory, state


def test_session_checkout_creates_via_factory() -> None:
    async def scenario():
        factory, state = _make_factory()
        pool = SessionPool(factory, size=2, max_solves=8)
        session = await pool.checkout(sitekey="site")
        assert isinstance(session, BrowserSession)
        assert state["calls"] == 1
        assert session.warm is True
        assert session.user_agent == session.fingerprint.user_agent
        await pool.close_all()

    asyncio.run(scenario())


def test_session_reuse_idle_warm_session() -> None:
    async def scenario():
        factory, state = _make_factory()
        pool = SessionPool(factory, size=2, max_solves=8)
        s1 = await pool.checkout()
        await pool.release(s1, success=True)
        s2 = await pool.checkout()
        # Reused the idle session; factory not called again.
        assert state["calls"] == 1
        assert s2.id == s1.id
        await pool.close_all()

    asyncio.run(scenario())


def test_session_retire_on_max_solves_closes_context() -> None:
    async def scenario():
        factory, state = _make_factory()
        pool = SessionPool(factory, size=2, max_solves=1)
        s1 = await pool.checkout()
        ctx = s1.context
        await pool.release(s1, success=True)  # solves reaches max -> retire
        assert ctx.closed is True
        # Next checkout must build a fresh session.
        s2 = await pool.checkout()
        assert state["calls"] == 2
        assert s2.id != s1.id
        await pool.close_all()

    asyncio.run(scenario())


def test_session_retire_on_burned_closes_fake_context() -> None:
    async def scenario():
        factory, state = _make_factory(async_close=False)
        pool = SessionPool(factory, size=2, max_solves=8)
        s1 = await pool.checkout()
        ctx = s1.context
        await pool.release(s1, success=False, burned=True)
        assert ctx.closed is True
        assert s1.reputation < 1.0

    asyncio.run(scenario())


def test_session_size_bound_respected_with_reuse() -> None:
    async def scenario():
        factory, state = _make_factory()
        pool = SessionPool(factory, size=2, max_solves=8)
        a = await pool.checkout()
        b = await pool.checkout()
        assert state["calls"] == 2
        # Return both to the warm idle set.
        await pool.release(a, success=True)
        await pool.release(b, success=True)
        # Two more checkouts reuse the idle sessions: no new contexts created,
        # so the live-session count never exceeds `size`.
        c = await pool.checkout()
        d = await pool.checkout()
        assert state["calls"] == 2
        assert {c.id, d.id} == {a.id, b.id}
        assert len(pool.snapshot()) == 2
        await pool.close_all()

    asyncio.run(scenario())


def test_session_close_all_closes_everything() -> None:
    async def scenario():
        factory, state = _make_factory()
        pool = SessionPool(factory, size=3, max_solves=8)
        s1 = await pool.checkout()
        s2 = await pool.checkout()
        await pool.release(s2, success=True)  # s2 idle, s1 in use
        await pool.close_all()
        assert all(ctx.closed for ctx in state["contexts"])
        assert pool.snapshot() == []

    asyncio.run(scenario())
