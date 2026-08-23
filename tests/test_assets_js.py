"""Tests for the JS asset loader (``src/assets/js_loader.py``).

Covers the three properties the rest of the system relies on:

* ``load_js`` reads a bundled asset, normalises a bare name to ``.js``, and is
  cached (stable across calls) — the solvers load their scripts once at import.
* ``js_bundle_version`` is a stable, well-formed fingerprint surfaced by
  ``/api/v1/health`` for deploy verification.
* ``EXPECTED_JS_ASSETS`` stays in sync with the directory, and ``verify_assets``
  reports a missing asset — the startup packaging check in ``src/main.py``.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.assets import js_loader  # noqa: E402
from src.assets.js_loader import (  # noqa: E402
    EXPECTED_JS_ASSETS,
    js_bundle_version,
    load_js,
    verify_assets,
)

_JS_DIR = Path(js_loader.__file__).resolve().parent / "js"

# A short 12-char hex fingerprint (sha256 digest truncated).
_VERSION_RE = re.compile(r"^[0-9a-f]{12}$")


def test_load_js_reads_asset_and_is_cached() -> None:
    a = load_js("omc_dom_read.js")
    b = load_js("omc_dom_read.js")
    assert a  # non-empty
    # lru_cache returns the identical cached object, not a re-read.
    assert a is b


def test_load_js_normalises_bare_name() -> None:
    """A name without the ``.js`` suffix resolves to the same asset."""
    assert load_js("omc_dom_read") == load_js("omc_dom_read.js")


def test_load_js_content_is_stable() -> None:
    """Repeated loads return byte-identical content (no stateful mutation)."""
    first = load_js("turnstile_token_extract.js")
    second = load_js("turnstile_token_extract.js")
    assert first == second
    assert "cf-turnstile-response" in first


def test_js_bundle_version_is_well_formed_and_stable() -> None:
    v1 = js_bundle_version()
    v2 = js_bundle_version()
    assert _VERSION_RE.match(v1), v1
    assert v1 == v2  # deterministic across calls


def test_js_bundle_version_covers_all_bundled_assets() -> None:
    """The fingerprint hashes every ``.js`` file, so it changes if any changes.

    Recomputing over the same inputs reproduces the value; recomputing while
    pretending one asset is absent must differ — proving no asset is ignored.
    """
    import hashlib

    paths = sorted(_JS_DIR.glob("*.js"))
    assert paths, "no JS assets found"

    full = hashlib.sha256()
    for p in paths:
        full.update(p.name.encode())
        full.update(p.read_bytes())
    assert full.hexdigest()[:12] == js_bundle_version()

    partial = hashlib.sha256()
    for p in paths[:-1]:
        partial.update(p.name.encode())
        partial.update(p.read_bytes())
    assert partial.hexdigest()[:12] != js_bundle_version()


def test_expected_manifest_matches_directory() -> None:
    """EXPECTED_JS_ASSETS is the exact set of ``.js`` files on disk.

    Adding an asset without registering it here (or vice versa) fails this test,
    keeping the startup packaging check honest.
    """
    on_disk = {p.name for p in _JS_DIR.glob("*.js")}
    assert set(EXPECTED_JS_ASSETS) == on_disk


def test_verify_assets_passes_for_bundled_assets() -> None:
    assert verify_assets() == []


def test_verify_assets_reports_missing_asset() -> None:
    problems = verify_assets(["does_not_exist.js"])
    assert len(problems) == 1
    assert "does_not_exist.js" in problems[0]


def test_verify_assets_reports_all_missing() -> None:
    problems = verify_assets(["missing_a.js", "missing_b.js", "omc_dom_read.js"])
    # Two missing, one present → exactly two problems reported.
    assert len(problems) == 2
    joined = "\n".join(problems)
    assert "missing_a.js" in joined and "missing_b.js" in joined
    assert "omc_dom_read.js" not in joined
