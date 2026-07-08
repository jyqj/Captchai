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


# Chrome major version presented by every profile. This MUST track the major
# version of the Chromium that Playwright bundles (playwright==1.49.1 ships
# Chromium build 1148 == Chrome 131). Pinning the whole pool to the engine's
# real major version is deliberate: a UA / client-hint version that disagrees
# with the actual browser build is a stronger detection signal than a uniform,
# engine-matched version. Bump this in lockstep with the Playwright upgrade in
# requirements.txt; GPU / OS / screen / locale still vary per profile for
# diversity.
_CHROME_MAJOR = "131"
# A recent, plausible full build for the pinned major (surfaced only via
# userAgentData high-entropy values; the UA string keeps the ``.0.0.0`` form
# Chrome uses in its reduced User-Agent).
_CHROME_FULL_VERSION = f"{_CHROME_MAJOR}.0.6778.86"
# GREASE brand + version Chrome 131 emits in sec-ch-ua / userAgentData.brands.
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

_SCREEN_SIZES = [
    (1920, 1080),
    (2560, 1440),
    (1536, 864),
    (1440, 900),
    (1680, 1050),
]

_HARDWARE_CONCURRENCY = [4, 8, 12, 16]
_DEVICE_MEMORY = [4, 8, 16]

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
) -> FingerprintProfile:
    """Pick a coherent, realistic fingerprint.

    The UA family, ``navigator.platform`` and WebGL vendor/renderer come from a
    single profile so they stay mutually consistent. When ``seed`` is given the
    result is fully deterministic (used by tests and for pinning a fingerprint
    to a sticky proxy). ``timezone_id`` / ``locale`` override the derived values.
    """
    rng = _rng(seed)

    profile = rng.choice(_ALL_PROFILES)
    resolved_locale = locale or rng.choice(list(_LANGUAGE_SETS.keys()))
    languages = _LANGUAGE_SETS.get(resolved_locale, ["en-US", "en"])
    resolved_tz = timezone_id or _LOCALE_TIMEZONES.get(
        resolved_locale, "America/New_York"
    )
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


def _ch_platform(platform: str) -> str:
    """Map ``navigator.platform`` to the Sec-CH-UA-Platform token."""
    if platform == "MacIntel":
        return "macOS"
    if platform.startswith("Linux"):
        return "Linux"
    return "Windows"


def _ch_platform_version(platform: str) -> str:
    """A plausible platformVersion for getHighEntropyValues (best-effort)."""
    if platform == "MacIntel":
        return "13.5.0"
    return "10.0.0"


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
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": f'"{_ch_platform(fp.platform)}"',
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


def build_stealth_js(fp: FingerprintProfile) -> str:
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
    ch_platform = _ch_platform(fp.platform)
    ch_platform_version = _ch_platform_version(fp.platform)
    ua_arch = "arm" if "Apple M" in fp.webgl_renderer else "x86"
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
  window.chrome = {{runtime: {{}}, app: {{}}, loadTimes: () => {{}}, csi: () => {{}}}};

  // navigator.userAgentData — JS-side client hints, kept consistent with the
  // Sec-CH-UA headers + UA string so low/high-entropy reads never contradict.
  try {{
    const _brands = {brands_js};
    const _fullVersionList = {full_brands_js};
    const _highEntropy = {{
      architecture: {_js_str(ua_arch)},
      bitness: "64",
      brands: _brands,
      fullVersionList: _fullVersionList,
      mobile: false,
      model: "",
      platform: {_js_str(ch_platform)},
      platformVersion: {_js_str(ch_platform_version)},
      uaFullVersion: {_js_str(_CHROME_FULL_VERSION)},
      wow64: false,
    }};
    const _uaData = {{
      brands: _brands,
      mobile: false,
      platform: {_js_str(ch_platform)},
      getHighEntropyValues: (hints) => Promise.resolve(_highEntropy),
      toJSON: () => ({{brands: _brands, mobile: false, platform: {_js_str(ch_platform)}}}),
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
    if proxy:
        kwargs["proxy"] = proxy
    return kwargs
