"""Load browser-side JavaScript assets from ``src/assets/js/``.

Injected scripts were previously embedded as Python string literals, which made
them hard to lint, diff, and version. Assets are read once at first use and
cached; :func:`js_bundle_version` exposes a stable hash for the health endpoint.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

_JS_DIR = Path(__file__).resolve().parent / "js"


@lru_cache(maxsize=None)
def load_js(name: str) -> str:
    """Return the contents of ``js/<name>`` (must end with ``.js``)."""
    if not name.endswith(".js"):
        name = f"{name}.js"
    path = _JS_DIR / name
    return path.read_text(encoding="utf-8")


def js_bundle_version() -> str:
    """Short SHA-256 fingerprint of all bundled ``.js`` files (sorted by name)."""
    digest = hashlib.sha256()
    for path in sorted(_JS_DIR.glob("*.js")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:12]
