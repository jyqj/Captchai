"""Load browser-side JavaScript assets from ``src/assets/js/``.

Injected scripts were previously embedded as Python string literals, which made
them hard to lint, diff, and version. Assets are read once at first use and
cached; :func:`js_bundle_version` exposes a stable hash for the health endpoint.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path

_JS_DIR = Path(__file__).resolve().parent / "js"

# Manifest of every browser-side asset the solvers load. Kept explicit so a
# packaging error (a ``.js`` file dropped from the build / wheel) is caught at
# startup by :func:`verify_assets` rather than surfacing as a confusing
# ``FileNotFoundError`` mid-solve. ``tests/test_assets_js.py`` asserts this
# manifest stays in sync with the directory contents so adding an asset without
# registering it here fails CI.
EXPECTED_JS_ASSETS: frozenset[str] = frozenset(
    {
        "defer_execute.js",
        "hardening.js",
        "hcaptcha_token_extract.js",
        "hcaptcha_widget_render.js",
        "omc_bridge.js",
        "omc_dom_read.js",
        "recaptcha_v2_extract.js",
        "recaptcha_v2_invisible_execute.js",
        "recaptcha_v3_execute.js",
        "turnstile_token_extract.js",
    }
)


@lru_cache(maxsize=None)
def load_js(name: str) -> str:
    """Return the contents of ``js/<name>`` (must end with ``.js``)."""
    if not name.endswith(".js"):
        name = f"{name}.js"
    path = _JS_DIR / name
    return path.read_text(encoding="utf-8")


def verify_assets(expected: Iterable[str] = EXPECTED_JS_ASSETS) -> list[str]:
    """Return human-readable problems with the bundled JS assets.

    An empty list means the ``js/`` directory exists and every expected asset
    is present. Called once at startup (see ``src/main.py``) so a packaging
    error fails loudly and immediately instead of at first solve.
    """
    if not _JS_DIR.is_dir():
        return [f"JS assets directory is missing: {_JS_DIR}"]
    present = {p.name for p in _JS_DIR.glob("*.js")}
    return [
        f"expected JS asset '{name}' is missing from {_JS_DIR}"
        for name in sorted(expected)
        if name not in present
    ]


def js_bundle_version() -> str:
    """Short SHA-256 fingerprint of all bundled ``.js`` files (sorted by name)."""
    digest = hashlib.sha256()
    for path in sorted(_JS_DIR.glob("*.js")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:12]
