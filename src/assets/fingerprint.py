"""Browser fingerprint generation and stealth injection.

Replaces the single hard-coded stealth script in the old ``browser.py`` (which
used identical navigator/WebGL values for every context) with per-session
*coherent* fingerprints: the User-Agent, ``navigator.platform`` and the WebGL
vendor/renderer are drawn from the same profile so they never contradict each
other. Generation is deterministic when a ``seed`` is supplied, which lets tests
assert stable output and lets callers pin a fingerprint to a sticky proxy.
"""

from __future__ import annotations

import hashlib
import random
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class FingerprintProfile:
    user_agent: str
    platform: str
    languages: List[str]
    hardware_concurrency: int
    device_memory: int
    screen_width: int
    screen_height: int
    webgl_vendor: str
    webgl_renderer: str
    timezone_id: str
    locale: str
    # P1-4: mobile (Android Chrome) profile. A carrier / mobile proxy egress
    # paired with a desktop Chrome fingerprint (sec-ch-ua-mobile: ?0, no touch)
    # is an obvious contradiction; when the pool proxy is ``kind="mobile"`` the
    # solver draws a mobile fingerprint so the UA, client hints (?1 + Android),
    # touch capability, and viewport all agree with the carrier IP.
    is_mobile: bool = False
    device_scale_factor: float = 1.0


# Chrome major version presented by every profile. This MUST track the major
# version of the Chromium that Playwright bundles (playwright==1.61.0 ships
# Chromium 149.0.7827.55). Pinning the whole pool to the engine's real major
# version is deliberate: a UA / client-hint version that disagrees with the
# actual browser build is a stronger detection signal than a uniform,
# engine-matched version. Bump this in lockstep with the Playwright upgrade in
# requirements.txt; ``BrowserManager`` validates the running engine's major
# against this value at startup and warns (or fails, in strict mode) on drift
# so a stale pin can't silently rot into a "Chrome/131 in 2026" signal. GPU /
# OS / screen / locale still vary per profile for diversity.
_CHROME_MAJOR = "149"
# A recent, plausible full build for the pinned major (surfaced only via
# userAgentData high-entropy values; the UA string keeps the ``.0.0.0`` form
# Chrome uses in its reduced User-Agent).
_CHROME_FULL_VERSION = "149.0.7827.55"


def chrome_major() -> str:
    """The Chrome major version the fingerprint pool presents (see ``_CHROME_MAJOR``)."""
    return _CHROME_MAJOR
# GREASE brand + version Chrome emits in sec-ch-ua / userAgentData.brands.
_UA_GREASE = ("Not_A Brand", "24")


def _win_ua(major: str) -> str:
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{major}.0.0.0 Safari/537.36"
    )


def _mac_ua(major: str) -> str:
    return (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{major}.0.0.0 Safari/537.36"
    )


def _android_ua(major: str, device: str) -> str:
    return (
        f"Mozilla/5.0 (Linux; Android 14; {device}) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{major}.0.0.0 Mobile Safari/537.36"
    )


# Each entry is a self-consistent desktop profile. The UA family, platform token
# and WebGL vendor/renderer are picked together so detectors cannot flag a
# mismatch (e.g. a "Win32" platform reporting an Apple GPU). All UAs share
# ``_CHROME_MAJOR`` so the version never contradicts the bundled engine.
_WINDOWS_PROFILES = [
    {
        "user_agent": _win_ua(_CHROME_MAJOR),
        "platform": "Win32",
        "webgl_vendor": "Google Inc. (NVIDIA)",
        "webgl_renderer": (
            "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"
        ),
    },
    {
        "user_agent": _win_ua(_CHROME_MAJOR),
        "platform": "Win32",
        "webgl_vendor": "Google Inc. (Intel)",
        "webgl_renderer": (
            "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"
        ),
    },
    {
        "user_agent": _win_ua(_CHROME_MAJOR),
        "platform": "Win32",
        "webgl_vendor": "Google Inc. (AMD)",
        "webgl_renderer": (
            "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)"
        ),
    },
]

_MAC_PROFILES = [
    {
        "user_agent": _mac_ua(_CHROME_MAJOR),
        "platform": "MacIntel",
        "webgl_vendor": "Google Inc. (Apple)",
        "webgl_renderer": "ANGLE (Apple, Apple M1, OpenGL 4.1)",
    },
    {
        "user_agent": _mac_ua(_CHROME_MAJOR),
        "platform": "MacIntel",
        "webgl_vendor": "Google Inc. (Intel)",
        "webgl_renderer": (
            "ANGLE (Intel, Intel(R) Iris(TM) Plus Graphics OpenGL Engine, OpenGL 4.1)"
        ),
    },
]

_ALL_PROFILES = _WINDOWS_PROFILES + _MAC_PROFILES

# Coherent Android Chrome profiles. navigator.platform on Android Chrome is
# "Linux armv8l"; the GPU is a mobile SoC reported through ANGLE's OpenGL ES
# backend (Adreno / Mali / Xclipse), matching the ``Mobile Safari`` UA. Each
# entry pairs a plausible device model with its GPU + CSS viewport + DPR.
_ANDROID_PROFILES = [
    {
        "device": "Pixel 8",
        "webgl_vendor": "Google Inc. (Qualcomm)",
        "webgl_renderer": "ANGLE (Qualcomm, Adreno (TM) 740, OpenGL ES 3.2)",
        "screen": (412, 915),
        "dpr": 2.625,
    },
    {
        "device": "SM-S918B",  # Galaxy S23 Ultra
        "webgl_vendor": "Google Inc. (Qualcomm)",
        "webgl_renderer": "ANGLE (Qualcomm, Adreno (TM) 740, OpenGL ES 3.2)",
        "screen": (384, 824),
        "dpr": 3.0,
    },
    {
        "device": "SM-A546B",  # Galaxy A54 (Exynos / Mali)
        "webgl_vendor": "Google Inc. (ARM)",
        "webgl_renderer": "ANGLE (ARM, Mali-G68 MC4, OpenGL ES 3.2)",
        "screen": (360, 780),
        "dpr": 3.0,
    },
    {
        "device": "Pixel 7a",
        "webgl_vendor": "Google Inc. (ARM)",
        "webgl_renderer": "ANGLE (ARM, Mali-G710, OpenGL ES 3.2)",
        "screen": (412, 892),
        "dpr": 2.625,
    },
]

_SCREEN_SIZES = [
    (1920, 1080),
    (2560, 1440),
    (1536, 864),
    (1440, 900),
    (1680, 1050),
]

_HARDWARE_CONCURRENCY = [4, 8, 12, 16]
_DEVICE_MEMORY = [4, 8, 16]
# Mobile SoCs report fewer logical cores / less device memory than desktops.
_MOBILE_HARDWARE_CONCURRENCY = [6, 8]
_MOBILE_DEVICE_MEMORY = [4, 6, 8]

_LANGUAGE_SETS = {
    "en-US": ["en-US", "en"],
    "en-GB": ["en-GB", "en"],
    "de-DE": ["de-DE", "de", "en-US", "en"],
    "fr-FR": ["fr-FR", "fr", "en-US", "en"],
    "es-ES": ["es-ES", "es", "en"],
    # Geo-derived locales (proxy_pool._COUNTRY_GEO derives these for RU/BR/JP/IN/CA
    # exit IPs). Without explicit entries, generate_fingerprint fell back to
    # ["en-US","en"], producing Playwright locale=ja-JP + timezone=Asia/Tokyo but
    # navigator.language=en-US — an inconsistent fingerprint hCaptcha risk-models.
    "ja-JP": ["ja-JP", "ja", "en"],
    "pt-BR": ["pt-BR", "pt", "en"],
    "ru-RU": ["ru-RU", "ru", "en"],
    "hi-IN": ["hi-IN", "hi", "en-IN", "en"],
    "en-CA": ["en-CA", "en", "fr-CA"],
}

_LOCALE_TIMEZONES = {
    "en-US": "America/New_York",
    "en-GB": "Europe/London",
    "de-DE": "Europe/Berlin",
    "fr-FR": "Europe/Paris",
    "es-ES": "Europe/Madrid",
    # Mirror the geo-derived locales so a random draw (no proxy) still picks a
    # coherent timezone for these locales.
    "ja-JP": "Asia/Tokyo",
    "pt-BR": "America/Sao_Paulo",
    "ru-RU": "Europe/Moscow",
    "hi-IN": "Asia/Kolkata",
    "en-CA": "America/Toronto",
}


def _rng(seed: Optional[str]) -> random.Random:
    """Return a deterministic RNG when seeded, else a fresh entropy-backed one."""
    if seed is None:
        return random.Random()
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return random.Random(int(digest, 16))


def generate_fingerprint(
    *,
    seed: Optional[str] = None,
    timezone_id: Optional[str] = None,
    locale: Optional[str] = None,
    mobile: bool = False,
) -> FingerprintProfile:
    """Pick a coherent, realistic fingerprint.

    The UA family, ``navigator.platform`` and WebGL vendor/renderer come from a
    single profile so they stay mutually consistent. When ``seed`` is given the
    result is fully deterministic (used by tests and for pinning a fingerprint
    to a sticky proxy). ``timezone_id`` / ``locale`` override the derived values.

    ``mobile=True`` draws from the Android Chrome profile pool instead of the
    desktop pool, so a carrier / mobile-proxy egress gets a mobile UA, an
    Android platform, a mobile GPU, a phone-sized viewport, and (via
    ``client_hint_headers`` / ``build_stealth_js``) ``sec-ch-ua-mobile: ?1`` +
    touch capability — closing the desktop-fingerprint-on-a-mobile-IP
    contradiction.
    """
    rng = _rng(seed)

    resolved_locale = locale or rng.choice(list(_LANGUAGE_SETS.keys()))
    languages = _LANGUAGE_SETS.get(resolved_locale, ["en-US", "en"])
    resolved_tz = timezone_id or _LOCALE_TIMEZONES.get(
        resolved_locale, "America/New_York"
    )

    if mobile:
        profile = rng.choice(_ANDROID_PROFILES)
        width, height = profile["screen"]
        return FingerprintProfile(
            user_agent=_android_ua(_CHROME_MAJOR, profile["device"]),
            platform="Linux armv8l",
            languages=list(languages),
            hardware_concurrency=rng.choice(_MOBILE_HARDWARE_CONCURRENCY),
            device_memory=rng.choice(_MOBILE_DEVICE_MEMORY),
            screen_width=width,
            screen_height=height,
            webgl_vendor=profile["webgl_vendor"],
            webgl_renderer=profile["webgl_renderer"],
            timezone_id=resolved_tz,
            locale=resolved_locale,
            is_mobile=True,
            device_scale_factor=float(profile["dpr"]),
        )

    profile = rng.choice(_ALL_PROFILES)
    width, height = rng.choice(_SCREEN_SIZES)

    return FingerprintProfile(
        user_agent=profile["user_agent"],
        platform=profile["platform"],
        languages=list(languages),
        hardware_concurrency=rng.choice(_HARDWARE_CONCURRENCY),
        device_memory=rng.choice(_DEVICE_MEMORY),
        screen_width=width,
        screen_height=height,
        webgl_vendor=profile["webgl_vendor"],
        webgl_renderer=profile["webgl_renderer"],
        timezone_id=resolved_tz,
        locale=resolved_locale,
    )


def _js_str(value: str) -> str:
    """Serialise a Python string as a JS string literal (JSON is a subset)."""
    import json

    return json.dumps(value)


# ── User-Agent Client Hints (Sec-CH-UA) ─────────────────────────────
#
# Modern Chrome sends its version primarily via the User-Agent Client Hints
# (``Sec-CH-UA*`` headers + ``navigator.userAgentData``), NOT just the UA
# string. Playwright's ``user_agent`` context option overrides the UA string
# but does NOT rewrite the client hints, so a spoofed ``Chrome/131`` UA would
# otherwise ship alongside the *bundled* engine's client hints — an obvious
# contradiction that hCaptcha / Cloudflare fingerprinting flags. These helpers
# derive coherent client hints from the fingerprint's UA so the header, the
# JS ``navigator.userAgentData``, and the UA string all agree.


def _chrome_major_from_ua(user_agent: str) -> str:
    m = re.search(r"Chrome/(\d+)", user_agent or "")
    return m.group(1) if m else _CHROME_MAJOR


def _android_model_from_ua(user_agent: str) -> str:
    """Extract the Android device model token from a mobile UA (best-effort).

    ``... (Linux; Android 14; Pixel 8) ...`` → ``"Pixel 8"``. Returns an empty
    string when the UA isn't an Android UA.
    """
    m = re.search(r"Android [\d.]+; ([^)]+?)\)", user_agent or "")
    return m.group(1).strip() if m else ""


def _ch_platform(platform: str, *, is_mobile: bool = False) -> str:
    """Map ``navigator.platform`` to the Sec-CH-UA-Platform token."""
    if is_mobile:
        return "Android"
    if platform == "MacIntel":
        return "macOS"
    if platform.startswith("Linux"):
        return "Linux"
    return "Windows"


def _ch_platform_version(platform: str, *, is_mobile: bool = False) -> str:
    """A plausible platformVersion for getHighEntropyValues (best-effort).

    Windows 11 reports platformVersion ``15.0.0`` (13.0.0+ = Win11) via UA-CH;
    macOS reports a current major; Android reports its release ("14.0.0").
    Kept roughly aligned with the UA so a high-entropy read doesn't pair a
    modern Chrome with a stale OS.
    """
    if is_mobile:
        return "14.0.0"
    if platform == "MacIntel":
        return "14.6.0"
    return "15.0.0"


def sec_ch_ua(user_agent: str) -> str:
    """Build the ``Sec-CH-UA`` brand list for a given UA's Chrome version."""
    major = _chrome_major_from_ua(user_agent)
    grease_brand, grease_ver = _UA_GREASE
    return (
        f'"Google Chrome";v="{major}", '
        f'"Chromium";v="{major}", '
        f'"{grease_brand}";v="{grease_ver}"'
    )


def client_hint_headers(fp: FingerprintProfile) -> Dict[str, str]:
    """Coherent ``Sec-CH-UA*`` + ``Accept-Language`` headers for ``fp``.

    Applied via ``context.extra_http_headers`` so requests carry client hints
    that match the spoofed UA and locale instead of the bundled engine's.
    """
    return {
        "sec-ch-ua": sec_ch_ua(fp.user_agent),
        "sec-ch-ua-mobile": "?1" if fp.is_mobile else "?0",
        "sec-ch-ua-platform": f'"{_ch_platform(fp.platform, is_mobile=fp.is_mobile)}"',
        "accept-language": _accept_language(fp.languages),
    }


def _accept_language(languages: List[str]) -> str:
    """Build an ``Accept-Language`` value with descending q-weights."""
    if not languages:
        return "en-US,en;q=0.9"
    parts: List[str] = [languages[0]]
    for i, lang in enumerate(languages[1:], start=1):
        q = max(0.1, 1.0 - i * 0.1)
        parts.append(f"{lang};q={q:.1f}")
    return ",".join(parts)


# Bounded cache of built stealth scripts keyed by the fingerprint identity that
# affects the JS. The script is a large f-string rebuilt on every fresh context
# (once per task-proxy solve); caching by identity means two solves that draw
# the same coherent fingerprint reuse the string instead of re-rendering it.
_STEALTH_JS_CACHE: Dict[tuple, str] = {}
_STEALTH_JS_CACHE_MAX = 256


def _stealth_identity(fp: FingerprintProfile) -> tuple:
    """Hashable key of the fingerprint fields that influence the stealth JS."""
    return (
        fp.user_agent,
        fp.platform,
        tuple(fp.languages),
        fp.hardware_concurrency,
        fp.device_memory,
        fp.screen_width,
        fp.screen_height,
        fp.webgl_vendor,
        fp.webgl_renderer,
        fp.locale,
        fp.is_mobile,
    )


def build_stealth_js(fp: FingerprintProfile) -> str:
    """Build (and cache) a Playwright init-script projecting ``fp`` onto the page.

    Results are memoised by :func:`_stealth_identity` so repeated solves with
    the same coherent fingerprint don't re-render the ~2KB script each time.
    """
    key = _stealth_identity(fp)
    cached = _STEALTH_JS_CACHE.get(key)
    if cached is not None:
        return cached
    script = _build_stealth_js_uncached(fp)
    if len(_STEALTH_JS_CACHE) >= _STEALTH_JS_CACHE_MAX:
        # Simple bound: drop an arbitrary entry (FIFO-ish) to cap memory.
        _STEALTH_JS_CACHE.pop(next(iter(_STEALTH_JS_CACHE)), None)
    _STEALTH_JS_CACHE[key] = script
    return script


def _build_stealth_js_uncached(fp: FingerprintProfile) -> str:
    """Build a Playwright init-script that projects ``fp`` onto the page.

    Patches the signals detectors commonly read (``navigator.webdriver``,
    ``languages``, ``plugins``, ``hardwareConcurrency``, ``deviceMemory``,
    ``window.chrome``, ``permissions.query``, ``navigator.userAgentData``) and
    rewrites the WebGL ``UNMASKED_VENDOR_WEBGL`` (37445) /
    ``UNMASKED_RENDERER_WEBGL`` (37446) parameters to this fingerprint's GPU
    strings. Also injects a sparse, per-fingerprint canvas noise so the canvas
    hash differs from the un-patched default without visibly corrupting
    rendered content.

    Stealth hardening:
    * ``navigator.plugins`` is a small array of realistic ``Plugin``-shaped
      objects (name/filename/description/length) instead of the bare
      ``[1,2,3,4,5]`` array (a trivial detection signal).
    * ``navigator.userAgentData`` (brands / mobile / platform +
      ``getHighEntropyValues``) is spoofed to match the Sec-CH-UA headers and
      the UA string, so JS-side client hints don't contradict the header set
      or reveal the bundled engine's real version.
    * The canvas noise step is a larger prime (509) that isn't a divisor of
      common image widths, and the per-pixel offset is derived from the whole
      coherent identity (UA + GPU + screen + locale) so the same fingerprint
      produces a stable hash but different fingerprints produce uncorrelated
      noise — defeating naive canvas-fingerprint reuse without the previous
      fixed ``(hc*7+mem)%8`` signature that was itself detectable across solves.
    """
    languages_array = "[" + ",".join(_js_str(lang) for lang in fp.languages) + "]"
    # Coherent navigator.userAgentData (JS-side client hints). Must agree with
    # the Sec-CH-UA headers and the UA string, else the low/high-entropy hints
    # contradict the header set — a detection signal on their own.
    ua_major = _chrome_major_from_ua(fp.user_agent)
    grease_brand, grease_ver = _UA_GREASE
    brands_js = (
        f'[{{"brand":"Google Chrome","version":"{ua_major}"}},'
        f'{{"brand":"Chromium","version":"{ua_major}"}},'
        f'{{"brand":{_js_str(grease_brand)},"version":"{grease_ver}"}}]'
    )
    full_brands_js = (
        f'[{{"brand":"Google Chrome","version":{_js_str(_CHROME_FULL_VERSION)}}},'
        f'{{"brand":"Chromium","version":{_js_str(_CHROME_FULL_VERSION)}}},'
        f'{{"brand":{_js_str(grease_brand)},"version":"{grease_ver}.0.0.0"}}]'
    )
    ch_platform = _ch_platform(fp.platform, is_mobile=fp.is_mobile)
    ch_platform_version = _ch_platform_version(fp.platform, is_mobile=fp.is_mobile)
    # Mobile UA-CH report an empty architecture/bitness; desktop reports arm/x86.
    if fp.is_mobile:
        ua_arch = ""
    else:
        ua_arch = "arm" if "Apple M" in fp.webgl_renderer else "x86"
    ua_bitness = "" if fp.is_mobile else "64"
    ua_mobile_js = "true" if fp.is_mobile else "false"
    # Real Android Chrome reports a device model in high-entropy values and a
    # non-zero maxTouchPoints; desktop reports "" / 0.
    ua_model_js = _js_str(_android_model_from_ua(fp.user_agent)) if fp.is_mobile else '""'
    max_touch_points = 5 if fp.is_mobile else 0
    # Per-fingerprint canvas noise offset: deterministic from the full coherent
    # identity so the same fingerprint always produces the same canvas hash,
    # but different fingerprints produce different (uncorrelated) noise. Derived
    # from the whole identity (not just the UA) because the UA is now pinned to
    # a single Chrome major — seeding off the UA alone would give every Windows
    # (or Mac) identity the same canvas offset, itself a cluster signal.
    _identity = "|".join(
        [
            fp.user_agent,
            fp.webgl_vendor,
            fp.webgl_renderer,
            f"{fp.screen_width}x{fp.screen_height}",
            str(fp.hardware_concurrency),
            str(fp.device_memory),
            fp.locale,
        ]
    )
    fp_offset = int(hashlib.sha256(_identity.encode("utf-8")).hexdigest(), 16) % 64
    # Sparse prime step — 509 is prime and not a divisor of common image
    # widths (1920/1080/1440/900), so the noise doesn't align to a visible
    # column pattern. Larger than the previous 251 so fewer pixels are
    # touched (sparser) while still perturbing the canvas hash.
    canvas_step = 509

    return f"""
(() => {{
  Object.defineProperty(navigator, 'webdriver', {{get: () => undefined}});
  Object.defineProperty(navigator, 'languages', {{get: () => {languages_array}}});
  Object.defineProperty(navigator, 'language', {{get: () => {_js_str(fp.languages[0])}}});
  Object.defineProperty(navigator, 'platform', {{get: () => {_js_str(fp.platform)}}});
  // Realistic PluginArray-shaped objects. Bare numbers like [1,2,3,4,5]
  // are a trivial detection signal; real Chrome exposes ~5 PDF-related
  // plugins, each with the standard (name/filename/description/length) shape.
  const _plugins = [
    {{name: "PDF Viewer", filename: "internal-pdf-viewer", description: "Portable Document Format", length: 1}},
    {{name: "Chrome PDF Viewer", filename: "internal-pdf-viewer", description: "Portable Document Format", length: 1}},
    {{name: "Chromium PDF Viewer", filename: "internal-pdf-viewer", description: "Portable Document Format", length: 1}},
    {{name: "Microsoft Edge PDF Viewer", filename: "internal-pdf-viewer", description: "Portable Document Format", length: 1}},
    {{name: "WebKit built-in PDF", filename: "internal-pdf-viewer", description: "Portable Document Format", length: 1}}
  ];
  Object.defineProperty(navigator, 'plugins', {{get: () => _plugins}});
  Object.defineProperty(navigator, 'hardwareConcurrency', {{get: () => {fp.hardware_concurrency}}});
  Object.defineProperty(navigator, 'deviceMemory', {{get: () => {fp.device_memory}}});
  // maxTouchPoints: >0 on mobile so a touch-capability probe matches the
  // Android UA / sec-ch-ua-mobile: ?1; 0 on desktop.
  try {{ Object.defineProperty(navigator, 'maxTouchPoints', {{get: () => {max_touch_points}}}); }} catch (e) {{}}
  window.chrome = {{runtime: {{}}, app: {{}}, loadTimes: () => {{}}, csi: () => {{}}}};

  // navigator.userAgentData — JS-side client hints, kept consistent with the
  // Sec-CH-UA headers + UA string so low/high-entropy reads never contradict.
  try {{
    const _brands = {brands_js};
    const _fullVersionList = {full_brands_js};
    const _highEntropy = {{
      architecture: {_js_str(ua_arch)},
      bitness: {_js_str(ua_bitness)},
      brands: _brands,
      fullVersionList: _fullVersionList,
      mobile: {ua_mobile_js},
      model: {ua_model_js},
      platform: {_js_str(ch_platform)},
      platformVersion: {_js_str(ch_platform_version)},
      uaFullVersion: {_js_str(_CHROME_FULL_VERSION)},
      wow64: false,
    }};
    const _uaData = {{
      brands: _brands,
      mobile: {ua_mobile_js},
      platform: {_js_str(ch_platform)},
      getHighEntropyValues: (hints) => Promise.resolve(_highEntropy),
      toJSON: () => ({{brands: _brands, mobile: {ua_mobile_js}, platform: {_js_str(ch_platform)}}}),
    }};
    Object.defineProperty(navigator, 'userAgentData', {{get: () => _uaData}});
  }} catch (e) {{}}

  const _origQuery = window.navigator.permissions && window.navigator.permissions.query;
  if (_origQuery) {{
    window.navigator.permissions.query = (params) => (
      params && params.name === 'notifications'
        ? Promise.resolve({{state: Notification.permission}})
        : _origQuery(params)
    );
  }}

  const _WEBGL_VENDOR = {_js_str(fp.webgl_vendor)};
  const _WEBGL_RENDERER = {_js_str(fp.webgl_renderer)};
  const _patchGL = (proto) => {{
    if (!proto) return;
    const getParameter = proto.getParameter;
    proto.getParameter = function (p) {{
      if (p === 37445) return _WEBGL_VENDOR;   // UNMASKED_VENDOR_WEBGL
      if (p === 37446) return _WEBGL_RENDERER; // UNMASKED_RENDERER_WEBGL
      return getParameter.call(this, p);
    }};
  }};
  try {{ _patchGL(window.WebGLRenderingContext && WebGLRenderingContext.prototype); }} catch (e) {{}}
  try {{ _patchGL(window.WebGL2RenderingContext && WebGL2RenderingContext.prototype); }} catch (e) {{}}

  // Sparse, per-fingerprint canvas noise: nudges the pixel data hash
  // without visible artefacts. The step ({canvas_step}) is prime and not a
  // divisor of common image widths, so the noise doesn't align to a column
  // pattern; the offset ({fp_offset}) is derived from the UA so the same
  // fingerprint produces a stable hash but different fingerprints produce
  // uncorrelated noise — defeating naive canvas-fingerprint reuse without
  // the previous fixed (hc*7+mem)%8 signature.
  try {{
    const _toDataURL = HTMLCanvasElement.prototype.toDataURL;
    const _getImageData = CanvasRenderingContext2D.prototype.getImageData;
    const _step = {canvas_step};
    const _offset = {fp_offset};
    CanvasRenderingContext2D.prototype.getImageData = function (x, y, w, h) {{
      const data = _getImageData.call(this, x, y, w, h);
      for (let i = 0; i < data.data.length; i += _step) {{
        data.data[i] = (data.data[i] + _offset) & 0xff;
      }}
      return data;
    }};
    HTMLCanvasElement.prototype.toDataURL = function (...args) {{
      return _toDataURL.apply(this, args);
    }};
  }} catch (e) {{}}
}})();
"""


def context_kwargs(
    fp: FingerprintProfile, proxy: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Build kwargs for ``browser.new_context`` from a fingerprint.

    Returns ``user_agent``, ``viewport``, ``locale``, ``timezone_id`` and
    ``extra_http_headers`` (coherent ``Sec-CH-UA*`` + ``Accept-Language``), plus
    ``proxy`` when one is supplied. Geolocation is intentionally omitted (opt-in
    per site) to avoid a permission mismatch on sites that never request it.
    """
    kwargs: Dict[str, Any] = {
        "user_agent": fp.user_agent,
        "viewport": {"width": fp.screen_width, "height": fp.screen_height},
        "locale": fp.locale,
        "timezone_id": fp.timezone_id,
        # Sec-CH-UA* + Accept-Language derived from this fingerprint so the
        # client hints match the spoofed UA / locale rather than the bundled
        # engine's defaults.
        "extra_http_headers": client_hint_headers(fp),
    }
    # P1-4: a mobile fingerprint gets a touch-enabled, high-DPR context so the
    # viewport, device_scale_factor, is_mobile, and touch capability all agree
    # with the Android UA + sec-ch-ua-mobile: ?1 (a desktop-shaped context on a
    # mobile UA is itself a contradiction).
    if fp.is_mobile:
        kwargs["is_mobile"] = True
        kwargs["has_touch"] = True
        kwargs["device_scale_factor"] = fp.device_scale_factor or 2.0
    if proxy:
        kwargs["proxy"] = proxy
    return kwargs
