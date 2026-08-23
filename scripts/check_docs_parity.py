#!/usr/bin/env python3
"""Verify ``docs/`` and ``docs/zh/`` have matching relative Markdown paths."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EN = ROOT / "docs"
ZH = ROOT / "docs" / "zh"


def _md_paths(base: Path, *, exclude: Path | None = None) -> set[str]:
    paths: set[str] = set()
    for p in base.rglob("*.md"):
        if not p.is_file():
            continue
        if exclude is not None and exclude in p.parents:
            continue
        paths.add(str(p.relative_to(base)).replace("\\", "/"))
    return paths


def main() -> int:
    en_paths = _md_paths(EN, exclude=ZH)
    zh_paths = _md_paths(ZH)

    missing_in_zh = sorted(en_paths - zh_paths)
    missing_in_en = sorted(zh_paths - en_paths)

    ok = True
    if missing_in_zh:
        ok = False
        print("Missing Chinese translations (present in docs/, absent in docs/zh/):")
        for p in missing_in_zh:
            print(f"  - {p}")
    if missing_in_en:
        ok = False
        print("Extra Chinese docs (present in docs/zh/, absent in docs/):")
        for p in missing_in_en:
            print(f"  - {p}")

    if ok:
        print(f"docs parity OK ({len(en_paths)} en, {len(zh_paths)} zh markdown files)")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
