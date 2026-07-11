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


def test_fingerprint_seed_none_produces_variety() -> None:
    """seed=None draws from entropy, so repeated calls should not all collide."""
    identities = set()
    for _ in range(20):
        fp = generate_fingerprint(seed=None)
        identities.add((fp.user_agent, fp.screen_width, fp.locale))
    assert len(identities) > 1


def test_session_pool_fingerprint_not_sitekey_determined() -> None:
    """Same sitekey must not produce identical fingerprints across sessions.

    Regression for the sitekey-seeded cluster bug: hCaptcha clusters identical
    fingerprints and flags them, so each proxyless session draws an independent
    entropy-backed fingerprint even when the sitekey is the same.
    """
    async def scenario():
        factory, _state = _make_factory()
        pool = SessionPool(factory, size=10, max_solves=8)
        sessions = await asyncio.gather(
            *[
                pool.checkout(key="proxyless", sitekey="same-sitekey")
                for _ in range(10)
            ]
        )
        identities = {
            (s.fingerprint.user_agent, s.fingerprint.screen_width, s.fingerprint.locale)
            for s in sessions
        }
        assert len(identities) > 1
        await pool.close_all()

    asyncio.run(scenario())


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


# --------------------------------------------------------------------------- #
# Fix #3: geo-derived locale coverage (ja-JP, pt-BR, ru-RU, hi-IN, en-CA)
# --------------------------------------------------------------------------- #


def test_fingerprint_ja_jp_languages_and_timezone() -> None:
    """ja-JP locale produces languages=[ja-JP, ja, en] and timezone=Asia/Tokyo."""
    fp = generate_fingerprint(seed="jp", locale="ja-JP")
    assert fp.languages == ["ja-JP", "ja", "en"]
    assert fp.timezone_id == "Asia/Tokyo"
    assert fp.locale == "ja-JP"


def test_fingerprint_pt_br_languages_and_timezone() -> None:
    """pt-BR locale produces languages=[pt-BR, pt, en] and timezone=America/Sao_Paulo."""
    fp = generate_fingerprint(seed="br", locale="pt-BR")
    assert fp.languages == ["pt-BR", "pt", "en"]
    assert fp.timezone_id == "America/Sao_Paulo"
    assert fp.locale == "pt-BR"


def test_fingerprint_ru_ru_languages_and_timezone() -> None:
    """ru-RU locale produces languages=[ru-RU, ru, en] and timezone=Europe/Moscow."""
    fp = generate_fingerprint(seed="ru", locale="ru-RU")
    assert fp.languages == ["ru-RU", "ru", "en"]
    assert fp.timezone_id == "Europe/Moscow"
    assert fp.locale == "ru-RU"


def test_fingerprint_hi_in_languages_and_timezone() -> None:
    """hi-IN locale produces languages=[hi-IN, hi, en-IN, en] and timezone=Asia/Kolkata."""
    fp = generate_fingerprint(seed="in", locale="hi-IN")
    assert fp.languages == ["hi-IN", "hi", "en-IN", "en"]
    assert fp.timezone_id == "Asia/Kolkata"
    assert fp.locale == "hi-IN"


def test_fingerprint_en_ca_languages_and_timezone() -> None:
    """en-CA locale produces languages=[en-CA, en, fr-CA] and timezone=America/Toronto."""
    fp = generate_fingerprint(seed="ca", locale="en-CA")
    assert fp.languages == ["en-CA", "en", "fr-CA"]
    assert fp.timezone_id == "America/Toronto"
    assert fp.locale == "en-CA"


def test_fingerprint_proxy_locale_threads_match_languages_first() -> None:
    """A proxy with locale=ja-JP threaded through generate_fingerprint produces
    a FingerprintProfile whose languages[0] == "ja-JP" (matches the locale),
    closing the mismatch that hCaptcha risk-models flag."""
    fp = generate_fingerprint(
        seed="p1", timezone_id="Asia/Tokyo", locale="ja-JP"
    )
    assert fp.locale == "ja-JP"
    assert fp.timezone_id == "Asia/Tokyo"
    assert fp.languages[0] == "ja-JP"
    # The language set is internally consistent with the locale.
    assert fp.languages[1] == "ja"


# --------------------------------------------------------------------------- #
# P1-4: mobile (Android Chrome) fingerprint for mobile-proxy egress
# --------------------------------------------------------------------------- #


def test_mobile_fingerprint_is_android_chrome() -> None:
    """mobile=True draws an Android Chrome profile (mobile UA, Android platform)."""
    fp = generate_fingerprint(seed="m1", mobile=True)
    assert fp.is_mobile is True
    assert "Android" in fp.user_agent
    assert "Mobile Safari" in fp.user_agent
    assert fp.platform == "Linux armv8l"
    # Mobile viewport is phone-sized, not a desktop resolution.
    assert fp.screen_width <= 480
    assert fp.device_scale_factor >= 2.0


def test_mobile_client_hints_signal_mobile_and_android() -> None:
    """Mobile fingerprint emits sec-ch-ua-mobile: ?1 and platform Android."""
    from src.assets.fingerprint import client_hint_headers

    fp = generate_fingerprint(seed="m2", mobile=True)
    headers = client_hint_headers(fp)
    assert headers["sec-ch-ua-mobile"] == "?1"
    assert headers["sec-ch-ua-platform"] == '"Android"'


def test_mobile_context_kwargs_have_touch_and_dpr() -> None:
    """Mobile context is touch-enabled, is_mobile, high-DPR."""
    from src.assets.fingerprint import context_kwargs

    fp = generate_fingerprint(seed="m3", mobile=True)
    kwargs = context_kwargs(fp)
    assert kwargs["is_mobile"] is True
    assert kwargs["has_touch"] is True
    assert kwargs["device_scale_factor"] >= 2.0


def test_mobile_stealth_js_sets_touch_and_mobile_uadata() -> None:
    """Mobile stealth JS spoofs maxTouchPoints>0 and userAgentData.mobile=true."""
    from src.assets.fingerprint import build_stealth_js

    fp = generate_fingerprint(seed="m4", mobile=True)
    js = build_stealth_js(fp)
    assert "maxTouchPoints" in js
    assert "mobile: true" in js
    # Desktop must remain ?0 / mobile: false.
    desktop = build_stealth_js(generate_fingerprint(seed="d4"))
    assert "mobile: false" in desktop


def test_mobile_fingerprint_deterministic_with_seed() -> None:
    """A seeded mobile fingerprint is stable (sticky mobile proxy identity)."""
    a = generate_fingerprint(seed="sticky-mobile", mobile=True)
    b = generate_fingerprint(seed="sticky-mobile", mobile=True)
    assert a == b
    # Desktop and mobile draws for the same seed differ (different pools).
    d = generate_fingerprint(seed="sticky-mobile", mobile=False)
    assert d.is_mobile is False
    assert a.user_agent != d.user_agent


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


def test_build_stealth_js_is_cached_by_identity() -> None:
    """Two builds for the same fingerprint identity reuse the cached script."""
    from src.assets.fingerprint import build_stealth_js

    fp1 = generate_fingerprint(seed="cache-a")
    fp2 = generate_fingerprint(seed="cache-a")  # identical identity
    js1 = build_stealth_js(fp1)
    js2 = build_stealth_js(fp2)
    # Same content and the same cached object (identity), not a re-render.
    assert js1 == js2
    assert js1 is js2


def test_build_stealth_js_plugins_are_realistic_objects() -> None:
    """WP5: navigator.plugins is a realistic Plugin-shaped array, not [1,2,3,4,5]."""
    fp = generate_fingerprint(seed="stealth-plugins")
    js = build_stealth_js(fp)
    # The bare-number detection signal is gone.
    assert "get: () => [1, 2, 3, 4, 5]" not in js
    # Plugin-shaped objects with name/filename/description/length are present.
    assert "PDF Viewer" in js
    assert "internal-pdf-viewer" in js
    assert "Portable Document Format" in js
    assert "length: 1" in js


def test_build_stealth_js_canvas_noise_is_sparse_and_per_fingerprint() -> None:
    """WP5: canvas noise uses a prime step + per-fingerprint UA-derived offset."""
    fp = generate_fingerprint(seed="stealth-canvas")
    js = build_stealth_js(fp)
    # The old fixed (hc*7+mem)%8 signature is gone.
    assert "* 7 +" not in js
    assert "% 8" not in js
    # The new sparse prime step (509) and per-fp offset are present.
    assert "509" in js
    # The offset is derived from the UA hash (a number in [0, 63]).
    import re
    m = re.search(r"const _offset = (\d+);", js)
    assert m is not None
    offset = int(m.group(1))
    assert 0 <= offset < 64


def test_build_stealth_js_canvas_offset_differs_per_fingerprint() -> None:
    """Different fingerprints get different canvas-noise offsets."""
    fp1 = generate_fingerprint(seed="fp-a")
    fp2 = generate_fingerprint(seed="fp-b")
    js1 = build_stealth_js(fp1)
    js2 = build_stealth_js(fp2)
    import re
    o1 = int(re.search(r"const _offset = (\d+);", js1).group(1))
    o2 = int(re.search(r"const _offset = (\d+);", js2).group(1))
    # Two different seeds very likely produce different UAs → different
    # offsets. Assert they differ (extremely high probability).
    assert o1 != o2 or fp1.user_agent != fp2.user_agent


def test_build_stealth_js_canvas_noise_covers_todataurl_and_toblob() -> None:
    """The canvas noise is applied on BOTH read paths, not just getImageData.

    The previous toDataURL override was a no-op (it called the original with no
    noise), so the most common canvas-fingerprint path (``toDataURL()``) saw an
    un-noised canvas. Both toDataURL and toBlob must now route through the shared
    noiser, and the perturbation must be idempotent LSB-forcing (stable across
    repeated reads) rather than add-and-wrap (which drifts).
    """
    js = build_stealth_js(generate_fingerprint(seed="canvas-paths"))
    assert "toDataURL = function" in js
    assert "toBlob = function" in js
    # Both overrides invoke the shared noiser before the original call.
    assert js.count("_applyCanvasNoise(this)") >= 2
    # Idempotent LSB force (not the old add-and-wrap that drifted on re-read).
    assert "& 0xfe" in js
    assert "+ _offset) & 0xff" not in js


def test_build_stealth_js_hardens_webgl_surface() -> None:
    """WebGL spoofing covers the whole capability surface, not just vendor/renderer.

    A GPU-less headless host falls back to SwiftShader whose params + extension
    list read as software rendering and contradict the discrete-GPU strings the
    layer spoofs. The stealth JS now also overrides ``getSupportedExtensions``
    and high-signal ``getParameter`` values (e.g. MAX_TEXTURE_SIZE 3379).
    """
    js = build_stealth_js(generate_fingerprint(seed="webgl-surface"))
    assert "getSupportedExtensions" in js
    assert "WEBGL_debug_renderer_info" in js  # a real extension in the list
    assert "3379" in js  # MAX_TEXTURE_SIZE param id
    assert "ALIASED_LINE_WIDTH_RANGE" in js


def test_build_stealth_js_adds_audio_screen_and_connection() -> None:
    """Deep hardening: AudioContext noise, coherent window.screen, connection."""
    fp = generate_fingerprint(seed="deep-hardening")
    js = build_stealth_js(fp)
    # AudioContext fingerprint perturbation (idempotent, guarded by a WeakSet).
    assert "getChannelData" in js
    assert "_audioSeen" in js
    # window.screen coherence (availWidth/colorDepth) so a headless host doesn't
    # report 0/odd screen dims contradicting the spoofed viewport.
    assert "window.screen" in js
    assert "colorDepth" in js
    # navigator.connection present (its absence is a headless tell).
    assert "'connection'" in js
    assert "effectiveType" in js


def test_stealth_screen_availheight_is_coherent_desktop_vs_mobile() -> None:
    """Desktop leaves room for a taskbar (availHeight < height); mobile is full."""
    import re

    desktop = build_stealth_js(generate_fingerprint(seed="scr-d"))
    mobile = build_stealth_js(generate_fingerprint(seed="scr-m", mobile=True))
    for js in (desktop, mobile):
        assert re.search(r"availHeight:\s*\d+", js) is not None


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


# --------------------------------------------------------------------------- #
# WP-fix#1: split sitekey stats into token-obtained vs real-outcome buckets
# --------------------------------------------------------------------------- #


def test_report_sitekey_and_report_sitekey_real_write_separate_buckets() -> None:
    async def scenario():
        pool = ProxyPool()
        proxy = ProxyAsset(id="p", server="http://p:1")
        pool.add(proxy)
        # Token-obtained bucket: solver reports success.
        await pool.report_sitekey("p", "sk-1", success=True)
        # Real-outcome bucket: report endpoint reports failure.
        await pool.report_sitekey_real("p", "sk-1", success=False)

        assert proxy.sitekey_stats == {"sk-1": [1, 0]}
        assert proxy.real_sitekey_stats == {"sk-1": [0, 1]}

    asyncio.run(scenario())


def test_checkout_prefers_real_sitekey_data_over_token_only() -> None:
    """When two proxies have the same token-obtained rate but different real
    outcomes, checkout prefers the one with real success data."""
    async def scenario():
        pool = ProxyPool()
        real_good = ProxyAsset(id="real-good", server="http://rg:1")
        real_bad = ProxyAsset(id="real-bad", server="http://rb:1")
        pool.add(real_good)
        pool.add(real_bad)
        # Both proxies have identical token-obtained records (1 success each).
        await pool.report_sitekey("real-good", "sk-1", success=True)
        await pool.report_sitekey("real-bad", "sk-1", success=True)
        # But real-outcome data diverges: real-good's token was accepted
        # downstream, real-bad's was rejected.
        await pool.report_sitekey_real("real-good", "sk-1", success=True)
        await pool.report_sitekey_real("real-bad", "sk-1", success=False)

        chosen = await pool.checkout(sitekey="sk-1")
        assert chosen is not None
        assert chosen.id == "real-good"

    asyncio.run(scenario())


def test_checkout_falls_back_to_token_rate_when_no_real_data() -> None:
    """When no proxy has real-outcome data, checkout falls back to the
    token-obtained rate (the pre-fix behavior)."""
    async def scenario():
        pool = ProxyPool()
        good = ProxyAsset(id="good", server="http://good:1")
        other = ProxyAsset(id="other", server="http://other:1")
        pool.add(good)
        pool.add(other)
        # Only token-obtained data; no real reports yet.
        await pool.report_sitekey("good", "sk-1", success=True)
        await pool.report_sitekey("other", "sk-1", success=False)

        chosen = await pool.checkout(sitekey="sk-1")
        assert chosen is not None
        assert chosen.id == "good"

    asyncio.run(scenario())


def test_checkout_real_data_wins_over_better_token_rate() -> None:
    """A proxy with real-outcome data ranks above one with a stronger
    token-obtained record but no real data yet — the real signal is the
    selection authority once any client has reported."""
    async def scenario():
        pool = ProxyPool()
        real_known = ProxyAsset(id="real-known", server="http://rk:1")
        token_strong = ProxyAsset(id="token-strong", server="http://ts:1")
        pool.add(real_known)
        pool.add(token_strong)
        # token-strong has a much better token-obtained record...
        await pool.report_sitekey("token-strong", "sk-1", success=True)
        await pool.report_sitekey("token-strong", "sk-1", success=True)
        await pool.report_sitekey("real-known", "sk-1", success=False)
        # ...but real-known has a real-outcome success report.
        await pool.report_sitekey_real("real-known", "sk-1", success=True)

        chosen = await pool.checkout(sitekey="sk-1")
        assert chosen is not None
        assert chosen.id == "real-known"

    asyncio.run(scenario())


def test_pool_snapshot_includes_real_sitekeys() -> None:
    async def scenario():
        pool = ProxyPool()
        proxy = ProxyAsset(id="p", server="http://p:1")
        pool.add(proxy)
        await pool.report_sitekey("p", "sk-1", success=True)
        await pool.report_sitekey_real("p", "sk-1", success=False)
        await pool.report_sitekey_real("p", "sk-2", success=True)

        snap = pool.snapshot()
        assert len(snap) == 1
        row = snap[0]
        assert row["sitekeys"] == {"sk-1": {"success": 1, "fail": 0}}
        assert row["real_sitekeys"] == {
            "sk-1": {"success": 0, "fail": 1},
            "sk-2": {"success": 1, "fail": 0},
        }

    asyncio.run(scenario())


def test_pool_snapshot_real_sitekeys_empty_when_only_token_data() -> None:
    """A proxy with only token-obtained data has an empty real_sitekeys snapshot."""
    async def scenario():
        pool = ProxyPool()
        proxy = ProxyAsset(id="p", server="http://p:1")
        pool.add(proxy)
        await pool.report_sitekey("p", "sk-1", success=True)

        snap = pool.snapshot()
        assert snap[0]["real_sitekeys"] == {}

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
# WP3: proxy geo metadata parsing (|country=DE|kind=residential suffix)
# --------------------------------------------------------------------------- #


def test_proxy_single_string_with_country_derives_timezone_and_locale() -> None:
    """``|country=DE`` derives timezone=Europe/Berlin, locale=de-DE."""
    p = proxy_from_params({"proxy": "http://user:pass@1.2.3.4:8080|country=DE"})
    assert p is not None
    assert p.country == "DE"
    assert p.timezone == "Europe/Berlin"
    assert p.locale == "de-DE"
    # Server / credentials are unaffected by the metadata suffix.
    assert p.server == "http://1.2.3.4:8080"
    assert p.username == "user"
    assert p.password == "pass"


def test_proxy_single_string_with_kind_metadata() -> None:
    """``|kind=residential`` sets ProxyAsset.kind (WP5)."""
    p = proxy_from_params(
        {"proxy": "http://user:pass@1.2.3.4:8080|country=DE|kind=residential"}
    )
    assert p is not None
    assert p.kind == "residential"
    assert p.country == "DE"


def test_proxy_country_only_unknown_leaves_tz_locale_none() -> None:
    """An unknown country (not in the table) leaves timezone/locale as None."""
    p = proxy_from_params({"proxy": "http://1.2.3.4:8080|country=ZZ"})
    assert p is not None
    assert p.country == "ZZ"
    assert p.timezone is None
    assert p.locale is None


def test_proxy_explicit_timezone_and_locale_override_derivation() -> None:
    """Explicit |timezone= / |locale= override the country-derived values."""
    p = proxy_from_params(
        {"proxy": "http://1.2.3.4:8080|country=DE|timezone=Europe/Vienna|locale=at-AT"}
    )
    assert p is not None
    assert p.country == "DE"
    assert p.timezone == "Europe/Vienna"
    assert p.locale == "at-AT"


def test_proxy_unknown_metadata_keys_ignored() -> None:
    """Unknown |key=value pairs are ignored, not stored."""
    p = proxy_from_params(
        {"proxy": "http://1.2.3.4:8080|country=DE|nonsense=ignored|foo=bar"}
    )
    assert p is not None
    assert p.country == "DE"
    assert p.timezone == "Europe/Berlin"


def test_proxy_no_geo_suffix_keeps_none() -> None:
    """A proxy without the |country= suffix has no geo (no regression)."""
    p = proxy_from_params({"proxy": "http://1.2.3.4:8080"})
    assert p is not None
    assert p.country is None
    assert p.timezone is None
    assert p.locale is None


def test_proxy_password_with_pipe_preserved_before_metadata() -> None:
    """A password containing ``|`` is preserved (creds split before metadata).

    The ``|key=value`` metadata suffix is parsed from the *post-credential*
    hostport, so a password with ``|`` survives as long as the hostport
    itself doesn't contain ``|`` (it never does for valid host:port).
    """
    p = proxy_from_params(
        {"proxy": "http://user:pa|ss@1.2.3.4:8080|country=DE|kind=residential"}
    )
    assert p is not None
    assert p.server == "http://1.2.3.4:8080"
    assert p.username == "user"
    assert p.password == "pa|ss"
    assert p.country == "DE"
    assert p.kind == "residential"


def test_proxy_no_creds_with_metadata() -> None:
    """A proxy with no creds but with metadata parses correctly."""
    p = proxy_from_params({"proxy": "http://1.2.3.4:8080|country=JP|kind=mobile"})
    assert p is not None
    assert p.server == "http://1.2.3.4:8080"
    assert p.username is None
    assert p.password is None
    assert p.country == "JP"
    assert p.timezone == "Asia/Tokyo"
    assert p.locale == "ja-JP"
    assert p.kind == "mobile"


# --------------------------------------------------------------------------- #
# P0-2a: sticky-session support (make sticky_session_id an actually-used field)
# --------------------------------------------------------------------------- #


def test_proxy_session_metadata_injects_username_placeholder() -> None:
    """``|session=abc`` substitutes the {session} placeholder in the username."""
    p = proxy_from_params(
        {"proxy": "http://user-{session}:pass@gw.example.com:8080|session=abc123"}
    )
    assert p is not None
    assert p.sticky_session_id == "abc123"
    pw = p.playwright_proxy()
    assert pw["username"] == "user-abc123"
    # The gateway host is untouched; only the credential token changes.
    assert pw["server"] == "http://gw.example.com:8080"


def test_proxy_sticky_true_autogenerates_stable_session() -> None:
    """``|sticky=true`` auto-generates a session id that is stable across calls."""
    p = proxy_from_params(
        {"proxy": "http://u-{session}:p@gw:8080|sticky=true|kind=residential"}
    )
    assert p is not None
    assert p.sticky_session_id  # auto-generated, non-empty
    first = p.playwright_proxy()["username"]
    second = p.playwright_proxy()["username"]
    # Same exit IP across calls: the substituted username is stable.
    assert first == second
    assert p.sticky_session_id in first


def test_proxy_placeholder_without_metadata_lazy_generates_once() -> None:
    """A {session} placeholder with no explicit session lazily pins one, stably."""
    p = proxy_from_params({"proxy": "http://u_{session}:p@gw:8080"})
    assert p is not None
    assert p.sticky_session_id is None  # not generated until first use
    first = p.playwright_proxy()["username"]
    assert p.sticky_session_id is not None  # generated on first playwright_proxy()
    second = p.playwright_proxy()["username"]
    assert first == second  # stable thereafter


def test_proxy_no_placeholder_leaves_username_untouched() -> None:
    """No {session} placeholder → username is unchanged (no regression)."""
    p = proxy_from_params({"proxy": "http://user:pass@gw:8080|sticky=true"})
    assert p is not None
    # sticky=true set an id, but with no placeholder the username is literal.
    assert p.playwright_proxy()["username"] == "user"


# --------------------------------------------------------------------------- #
# P0-2b: exit-IP geo probing (no manual annotation needed)
# --------------------------------------------------------------------------- #


def test_geo_probe_resolves_country_and_derives_tz_locale() -> None:
    """A probe that returns countryCode=DE derives Europe/Berlin + de-DE."""
    from src.assets.geo_probe import probe_proxy_geo

    async def fake_fetch(url, proxy_url, timeout):
        # The probe egresses through the proxy's credentials.
        assert proxy_url is not None
        return {"countryCode": "DE"}

    async def scenario():
        proxy = ProxyAsset(id="p", server="http://user:pass@gw:8080")
        applied = await probe_proxy_geo(
            proxy, url="http://ip-api.com/json", fetch=fake_fetch
        )
        assert applied is True
        assert proxy.country == "DE"
        assert proxy.timezone == "Europe/Berlin"
        assert proxy.locale == "de-DE"
        assert proxy.geo_probed is True

    asyncio.run(scenario())


def test_geo_probe_marks_probed_even_on_failure() -> None:
    """A failed/empty probe still sets geo_probed so it isn't retried."""
    from src.assets.geo_probe import probe_proxy_geo

    async def fake_fetch(url, proxy_url, timeout):
        return None  # simulate proxy down / non-200 / bad JSON

    async def scenario():
        proxy = ProxyAsset(id="p", server="http://gw:8080")
        applied = await probe_proxy_geo(
            proxy, url="http://ip-api.com/json", fetch=fake_fetch
        )
        assert applied is False
        assert proxy.country is None  # no regression: geo stays unset
        assert proxy.geo_probed is True  # but won't be re-probed

    asyncio.run(scenario())


def test_geo_probe_skips_manually_annotated_proxy() -> None:
    """A proxy with a manual |country= annotation is never probed."""
    from src.assets.geo_probe import probe_proxy_geo

    calls = {"n": 0}

    async def fake_fetch(url, proxy_url, timeout):
        calls["n"] += 1
        return {"countryCode": "US"}

    async def scenario():
        proxy = proxy_from_params({"proxy": "http://gw:8080|country=DE"})
        applied = await probe_proxy_geo(
            proxy, url="http://ip-api.com/json", fetch=fake_fetch
        )
        assert applied is False
        assert calls["n"] == 0  # manual annotation wins, no network probe
        assert proxy.country == "DE"

    asyncio.run(scenario())


def test_set_geo_persists_on_pool() -> None:
    """ProxyPool.set_geo writes probed geo back onto the stored proxy."""
    async def scenario():
        pool = ProxyPool()
        pool.add(ProxyAsset(id="p", server="http://gw:8080"))
        await pool.set_geo(
            "p", country="JP", timezone="Asia/Tokyo", locale="ja-JP"
        )
        row = pool.snapshot()[0]
        assert row["country"] == "JP"
        assert row["timezone"] == "Asia/Tokyo"
        assert row["locale"] == "ja-JP"
        assert row["geo_probed"] is True

    asyncio.run(scenario())


def test_pool_snapshot_includes_geo_fields() -> None:
    pool = ProxyPool()
    pool.add(
        ProxyAsset(
            id="p",
            server="http://p:1",
            country="DE",
            timezone="Europe/Berlin",
            locale="de-DE",
        )
    )
    row = pool.snapshot()[0]
    assert row["country"] == "DE"
    assert row["timezone"] == "Europe/Berlin"
    assert row["locale"] == "de-DE"


# --------------------------------------------------------------------------- #
# WP5: ProxyPool.checkout kind filtering
# --------------------------------------------------------------------------- #


def test_checkout_kind_filter_returns_none_when_only_datacenter_available() -> None:
    """A pool with only datacenter proxies returns None for kind=residential."""
    async def scenario():
        pool = ProxyPool()
        pool.add(ProxyAsset(id="dc-1", server="http://dc:1", kind="datacenter"))
        assert await pool.checkout(kind="residential") is None

    asyncio.run(scenario())


def test_checkout_kind_filter_returns_residential_when_available() -> None:
    """checkout(kind=residential) skips datacenter proxies and returns the residential one."""
    async def scenario():
        pool = ProxyPool()
        pool.add(ProxyAsset(id="dc-1", server="http://dc:1", kind="datacenter"))
        pool.add(ProxyAsset(id="res-1", server="http://res:1", kind="residential"))
        chosen = await pool.checkout(kind="residential")
        assert chosen is not None
        assert chosen.id == "res-1"
        assert chosen.kind == "residential"

    asyncio.run(scenario())


def test_checkout_kind_filter_mobile_distinct_from_residential() -> None:
    """kind=mobile doesn't match residential proxies (kind is exact-match)."""
    async def scenario():
        pool = ProxyPool()
        pool.add(ProxyAsset(id="res-1", server="http://res:1", kind="residential"))
        assert await pool.checkout(kind="mobile") is None

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# Fix #2: kind accepts a list/tuple (enterprise hCaptcha: residential OR mobile)
# --------------------------------------------------------------------------- #


def test_checkout_kind_list_selects_mobile_when_only_mobile_available() -> None:
    """checkout(kind=["residential","mobile"]) returns the mobile proxy when
    that's the only non-datacenter proxy in the pool."""
    async def scenario():
        pool = ProxyPool()
        pool.add(ProxyAsset(id="dc-1", server="http://dc:1", kind="datacenter"))
        pool.add(ProxyAsset(id="mob-1", server="http://mob:1", kind="mobile"))
        chosen = await pool.checkout(kind=["residential", "mobile"])
        assert chosen is not None
        assert chosen.id == "mob-1"
        assert chosen.kind == "mobile"

    asyncio.run(scenario())


def test_checkout_kind_list_selects_residential_when_both_available() -> None:
    """A pool with both residential and mobile proxies returns one of them
    (not datacenter) for kind=["residential","mobile"]."""
    async def scenario():
        pool = ProxyPool()
        pool.add(ProxyAsset(id="dc-1", server="http://dc:1", kind="datacenter"))
        pool.add(ProxyAsset(id="res-1", server="http://res:1", kind="residential"))
        pool.add(ProxyAsset(id="mob-1", server="http://mob:1", kind="mobile"))
        chosen = await pool.checkout(kind=["residential", "mobile"])
        assert chosen is not None
        assert chosen.kind in {"residential", "mobile"}

    asyncio.run(scenario())


def test_checkout_kind_list_returns_none_when_only_datacenter_available() -> None:
    """A pool with only datacenter proxies returns None for a residential+mobile
    kind list — the enterprise enforcement path raises on this."""
    async def scenario():
        pool = ProxyPool()
        pool.add(ProxyAsset(id="dc-1", server="http://dc:1", kind="datacenter"))
        assert (
            await pool.checkout(kind=["residential", "mobile"]) is None
        )

    asyncio.run(scenario())


def test_checkout_kind_list_accepts_tuple() -> None:
    """A tuple of kinds is accepted the same as a list."""
    async def scenario():
        pool = ProxyPool()
        pool.add(ProxyAsset(id="mob-1", server="http://mob:1", kind="mobile"))
        chosen = await pool.checkout(kind=("residential", "mobile"))
        assert chosen is not None
        assert chosen.id == "mob-1"

    asyncio.run(scenario())


def test_checkout_kind_single_str_still_works() -> None:
    """Backward compatibility: a single str kind still filters exactly."""
    async def scenario():
        pool = ProxyPool()
        pool.add(ProxyAsset(id="dc-1", server="http://dc:1", kind="datacenter"))
        pool.add(ProxyAsset(id="res-1", server="http://res:1", kind="residential"))
        chosen = await pool.checkout(kind="residential")
        assert chosen is not None
        assert chosen.id == "res-1"

    asyncio.run(scenario())


def test_checkout_kind_none_means_no_filter() -> None:
    """kind=None (the default) accepts any proxy kind."""
    async def scenario():
        pool = ProxyPool()
        pool.add(ProxyAsset(id="dc-1", server="http://dc:1", kind="datacenter"))
        pool.add(ProxyAsset(id="res-1", server="http://res:1", kind="residential"))
        chosen = await pool.checkout(kind=None)
        assert chosen is not None
        assert chosen.kind in {"datacenter", "residential"}

    asyncio.run(scenario())


def test_has_available_kind_list_matches_any() -> None:
    """has_available accepts a kind list and returns True when any matches."""
    pool = ProxyPool()
    pool.add(ProxyAsset(id="mob-1", server="http://mob:1", kind="mobile"))
    assert pool.has_available(kind=["residential", "mobile"]) is True
    assert pool.has_available(kind=["residential", "datacenter"]) is False


def test_has_available_kind_single_str_still_works() -> None:
    """Backward compatibility: has_available(kind=str) still filters exactly."""
    pool = ProxyPool()
    pool.add(ProxyAsset(id="res-1", server="http://res:1", kind="residential"))
    assert pool.has_available(kind="residential") is True
    assert pool.has_available(kind="mobile") is False


# --------------------------------------------------------------------------- #
# token_cache (removed in WP7 — dead code; no solver consulted the cache)
# --------------------------------------------------------------------------- #


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
        session = await pool.checkout(key="proxyless", sitekey="site")
        assert isinstance(session, BrowserSession)
        assert state["calls"] == 1
        assert session.warm is True
        assert session.user_agent == session.fingerprint.user_agent
        assert session.proxy is None
        await pool.close_all()

    asyncio.run(scenario())


def test_session_prewarm_creates_idle_sessions() -> None:
    async def scenario():
        factory, state = _make_factory()
        pool = SessionPool(factory, size=3, max_solves=8)
        count = await pool.prewarm(sitekey="site")
        assert count == 3
        assert state["calls"] == 3
        snap = pool.snapshot()
        assert len(snap) == 3
        assert all(row["warm"] is True and row["in_use"] is False for row in snap)
        await pool.close_all()

    asyncio.run(scenario())


def test_session_reuse_idle_warm_session() -> None:
    async def scenario():
        factory, state = _make_factory()
        pool = SessionPool(factory, size=2, max_solves=8)
        s1 = await pool.checkout(key="proxyless")
        await pool.release(s1, success=True)
        s2 = await pool.checkout(key="proxyless")
        # Reused the idle session; factory not called again.
        assert state["calls"] == 1
        assert s2.id == s1.id
        await pool.close_all()

    asyncio.run(scenario())


def test_session_retire_on_max_solves_closes_context() -> None:
    async def scenario():
        factory, state = _make_factory()
        pool = SessionPool(factory, size=2, max_solves=1)
        s1 = await pool.checkout(key="proxyless")
        ctx = s1.context
        await pool.release(s1, success=True)  # solves reaches max -> retire
        assert ctx.closed is True
        # Next checkout must build a fresh session.
        s2 = await pool.checkout(key="proxyless")
        assert state["calls"] == 2
        assert s2.id != s1.id
        await pool.close_all()

    asyncio.run(scenario())


def test_session_single_failure_does_not_retire() -> None:
    """A single solve failure no longer retires a warmed session.

    Two-strike policy: a fresh session (rep=1.0) survives one failure
    (1.0→0.6, above the 0.3 eviction threshold) and returns to the idle pool.
    """
    async def scenario():
        factory, _state = _make_factory(async_close=False)
        pool = SessionPool(factory, size=2, max_solves=8)
        s1 = await pool.checkout(key="proxyless")
        ctx = s1.context
        await pool.release(s1, success=False, burned=True)
        assert ctx.closed is False
        assert s1.reputation < 1.0
        assert s1.reputation >= 0.3
        # Session should still be live in the idle pool.
        snap = pool.snapshot()
        assert len(snap) == 1
        assert snap[0]["in_use"] is False
        await pool.close_all()

    asyncio.run(scenario())


def test_session_two_failures_retire() -> None:
    """Two consecutive failures drop reputation below 0.3 and retire the session."""
    async def scenario():
        factory, _state = _make_factory(async_close=False)
        pool = SessionPool(factory, size=2, max_solves=8)
        s1 = await pool.checkout(key="proxyless")
        ctx = s1.context
        # First failure: 1.0 -> 0.6 (still alive, returns to idle).
        await pool.release(s1, success=False, burned=True)
        assert not ctx.closed
        s2 = await pool.checkout(key="proxyless")
        assert s2.id == s1.id  # reused the idle session
        # Second failure: 0.6 -> 0.2 (below threshold, retired).
        await pool.release(s2, success=False, burned=True)
        assert ctx.closed is True
        assert s2.reputation < 0.3

    asyncio.run(scenario())


def test_session_size_bound_respected_with_reuse() -> None:
    async def scenario():
        factory, state = _make_factory()
        pool = SessionPool(factory, size=2, max_solves=8)
        a = await pool.checkout(key="proxyless")
        b = await pool.checkout(key="proxyless")
        assert state["calls"] == 2
        # Return both to the warm idle set.
        await pool.release(a, success=True)
        await pool.release(b, success=True)
        # Two more checkouts reuse the idle sessions: no new contexts created,
        # so the live-session count never exceeds `size`.
        c = await pool.checkout(key="proxyless")
        d = await pool.checkout(key="proxyless")
        assert state["calls"] == 2
        assert {c.id, d.id} == {a.id, b.id}
        assert len(pool.snapshot()) == 2
        await pool.close_all()

    asyncio.run(scenario())


def test_session_close_all_closes_everything() -> None:
    async def scenario():
        factory, state = _make_factory()
        pool = SessionPool(factory, size=3, max_solves=8)
        s1 = await pool.checkout(key="proxyless")
        s2 = await pool.checkout(key="proxyless")
        await pool.release(s2, success=True)  # s2 idle, s1 in use
        await pool.close_all()
        assert all(ctx.closed for ctx in state["contexts"])
        assert pool.snapshot() == []

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# session_pool bucketing (WP1)
# --------------------------------------------------------------------------- #


def test_session_buckets_isolate_proxyless_from_pool() -> None:
    """A proxyless checkout must not reuse a pool-proxy session, and vice versa."""
    async def scenario():
        factory, _state = _make_factory()
        pool = SessionPool(factory, size=4, max_solves=8)
        proxy = ProxyAsset(id="p-1", server="http://p:1")
        pool_sess = await pool.checkout(key="p-1", proxy=proxy)
        await pool.release(pool_sess, success=True)
        # Proxyless checkout must build its own session, not reuse pool_sess.
        proxyless_sess = await pool.checkout(key="proxyless", proxy=None)
        assert proxyless_sess.id != pool_sess.id
        assert proxyless_sess.proxy is None
        assert pool_sess.proxy is not None
        # Re-checking out the pool bucket reuses pool_sess.
        pool_sess2 = await pool.checkout(key="p-1", proxy=proxy)
        assert pool_sess2.id == pool_sess.id
        await pool.close_all()

    asyncio.run(scenario())


def test_session_sticky_proxy_reuses_same_session() -> None:
    """Two consecutive pool solves for the same proxy reuse the same session."""
    async def scenario():
        factory, state = _make_factory()
        pool = SessionPool(factory, size=2, max_solves=8)
        proxy = ProxyAsset(id="p-sticky", server="http://p:1")
        s1 = await pool.checkout(key=proxy.id, proxy=proxy)
        await pool.release(s1, success=True)
        s2 = await pool.checkout(key=proxy.id, proxy=proxy)
        assert s2.id == s1.id  # sticky reuse
        assert state["calls"] == 1  # factory only called once
        await pool.close_all()

    asyncio.run(scenario())


def test_session_sticky_proxy_fingerprint_seeded_by_proxy_id() -> None:
    """A pool session's fingerprint is deterministic in proxy.id across rebuilds."""
    async def scenario():
        factory, _state = _make_factory()
        pool = SessionPool(factory, size=1, max_solves=1)
        proxy = ProxyAsset(id="seed-proxy", server="http://p:1")
        s1 = await pool.checkout(key=proxy.id, proxy=proxy)
        fp1 = s1.fingerprint
        # Retire s1 (max_solves=1) so the next checkout builds a fresh session.
        await pool.release(s1, success=True)
        assert s1.context.closed is True
        s2 = await pool.checkout(key=proxy.id, proxy=proxy)
        # Same sticky proxy → same coherent fingerprint on rebuild.
        assert s2.fingerprint == fp1
        await pool.close_all()

    asyncio.run(scenario())


def test_session_prewarm_only_fills_proxyless_bucket() -> None:
    """prewarm() creates idle proxyless sessions only; no pool bucket exists."""
    async def scenario():
        factory, _state = _make_factory()
        pool = SessionPool(factory, size=3, max_solves=8)
        count = await pool.prewarm(sitekey="site")
        assert count == 3
        # All prewarmed sessions are proxyless — no pool bucket was created.
        snap = pool.snapshot()
        assert len(snap) == 3
        assert all(row["proxy_id"] is None for row in snap)
        await pool.close_all()

    asyncio.run(scenario())


def test_session_pool_bucket_lazy_created_on_checkout() -> None:
    """A pool bucket is created lazily on the first checkout for a proxy id."""
    async def scenario():
        factory, state = _make_factory()
        pool = SessionPool(factory, size=4, max_solves=8)
        # Prewarm 2 of 4 slots so a pool bucket can still claim a slot.
        s1 = await pool.checkout(key="proxyless")
        s2 = await pool.checkout(key="proxyless")
        await pool.release(s1, success=True)
        await pool.release(s2, success=True)
        assert state["calls"] == 2
        # First pool checkout builds a fresh session (lazy bucket creation).
        proxy = ProxyAsset(id="p-1", server="http://p:1")
        pool_sess = await pool.checkout(key=proxy.id, proxy=proxy)
        assert state["calls"] == 3
        assert pool_sess.proxy is not None
        assert pool_sess.proxy.id == "p-1"
        await pool.close_all()

    asyncio.run(scenario())


def test_session_snapshot_includes_proxy_id() -> None:
    async def scenario():
        factory, _state = _make_factory()
        pool = SessionPool(factory, size=2, max_solves=8)
        proxy = ProxyAsset(id="snap-proxy", server="http://p:1")
        await pool.checkout(key=proxy.id, proxy=proxy)
        await pool.checkout(key="proxyless", proxy=None)
        snap = pool.snapshot()
        proxy_ids = {row["proxy_id"] for row in snap}
        assert proxy_ids == {"snap-proxy", None}
        await pool.close_all()

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# WP3: pool-proxy geo → fingerprint alignment
# --------------------------------------------------------------------------- #


def test_session_pool_proxy_geo_drives_fingerprint_timezone_and_locale() -> None:
    """A pool session built with a German proxy gets Europe/Berlin + de-DE.

    The proxy's exit-IP geo is threaded into ``generate_fingerprint`` so a
    German residential IP presents Europe/Berlin + de-DE rather than
    en-US/New_York. Proxyless (server-IP) sessions keep a random coherent
    identity (current behavior).
    """
    async def scenario():
        factory, _state = _make_factory()
        pool = SessionPool(factory, size=2, max_solves=8)
        proxy = ProxyAsset(
            id="de-proxy",
            server="http://de:1",
            country="DE",
            timezone="Europe/Berlin",
            locale="de-DE",
        )
        session = await pool.checkout(key=proxy.id, proxy=proxy)
        assert session.fingerprint.timezone_id == "Europe/Berlin"
        assert session.fingerprint.locale == "de-DE"
        assert session.fingerprint.languages[0] == "de-DE"
        await pool.close_all()

    asyncio.run(scenario())


def test_session_pool_proxy_without_geo_keeps_random_fingerprint() -> None:
    """A proxy with no geo annotation falls back to a random coherent identity."""
    async def scenario():
        factory, _state = _make_factory()
        pool = SessionPool(factory, size=2, max_solves=8)
        proxy = ProxyAsset(id="no-geo", server="http://p:1")
        session = await pool.checkout(key=proxy.id, proxy=proxy)
        # No geo → fingerprint keeps some timezone/locale (random), but NOT
        # forced to None. The fingerprint is still coherent (locale matches
        # languages[0]).
        assert session.fingerprint.timezone_id is not None
        assert session.fingerprint.locale is not None
        assert session.fingerprint.languages[0].startswith(
            session.fingerprint.locale.split("-")[0]
        )
        await pool.close_all()

    asyncio.run(scenario())


def test_session_pool_geo_aligned_fingerprint_stable_across_rebuilds() -> None:
    """A sticky proxy's geo-aligned fingerprint is deterministic across rebuilds."""
    async def scenario():
        factory, _state = _make_factory()
        pool = SessionPool(factory, size=1, max_solves=1)
        proxy = ProxyAsset(
            id="jp-proxy",
            server="http://jp:1",
            country="JP",
            timezone="Asia/Tokyo",
            locale="ja-JP",
        )
        s1 = await pool.checkout(key=proxy.id, proxy=proxy)
        fp1 = s1.fingerprint
        assert fp1.timezone_id == "Asia/Tokyo"
        assert fp1.locale == "ja-JP"
        # Retire s1 (max_solves=1) so the next checkout rebuilds.
        await pool.release(s1, success=True)
        assert s1.context.closed is True
        s2 = await pool.checkout(key=proxy.id, proxy=proxy)
        # Same sticky proxy → same geo-aligned fingerprint on rebuild.
        assert s2.fingerprint == fp1
        await pool.close_all()

    asyncio.run(scenario())
