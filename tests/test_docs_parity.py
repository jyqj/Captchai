"""Tests for ``scripts/check_docs_parity.py`` (the CI bilingual-docs gate).

The script guards that every English doc has a Chinese translation and vice
versa. It runs in CI before ``mkdocs build --strict``; these tests exercise it
three ways: as the subprocess CI actually runs, via its importable ``main`` on
the real tree (exit 0), and against synthetic trees to prove it detects a
missing or extra translation.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_SCRIPT = PROJECT_ROOT / "scripts" / "check_docs_parity.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_docs_parity", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_parity_script_runs_clean_as_subprocess() -> None:
    """Exactly what CI runs: the script exits 0 on the real docs tree."""
    result = subprocess.run(
        [sys.executable, str(_SCRIPT)],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "docs parity OK" in result.stdout


def test_parity_main_returns_zero_on_real_tree() -> None:
    module = _load_script()
    assert module.main() == 0


def test_parity_main_detects_missing_translation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An English doc with no Chinese counterpart makes main() return 1."""
    module = _load_script()
    en = tmp_path / "docs"
    zh = en / "zh"
    zh.mkdir(parents=True)
    (en / "index.md").write_text("# hi", encoding="utf-8")
    (en / "orphan.md").write_text("# no translation", encoding="utf-8")
    (zh / "index.md").write_text("# 你好", encoding="utf-8")

    monkeypatch.setattr(module, "EN", en)
    monkeypatch.setattr(module, "ZH", zh)
    assert module.main() == 1


def test_parity_main_detects_extra_chinese_doc(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A Chinese doc with no English source also makes main() return 1."""
    module = _load_script()
    en = tmp_path / "docs"
    zh = en / "zh"
    zh.mkdir(parents=True)
    (en / "index.md").write_text("# hi", encoding="utf-8")
    (zh / "index.md").write_text("# 你好", encoding="utf-8")
    (zh / "extra.md").write_text("# 多余", encoding="utf-8")

    monkeypatch.setattr(module, "EN", en)
    monkeypatch.setattr(module, "ZH", zh)
    assert module.main() == 1


def test_parity_main_passes_when_trees_match(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _load_script()
    en = tmp_path / "docs"
    zh = en / "zh"
    (en / "usage").mkdir(parents=True)
    zh.mkdir(parents=True)
    (zh / "usage").mkdir(parents=True)
    (en / "index.md").write_text("# hi", encoding="utf-8")
    (en / "usage" / "hcaptcha.md").write_text("# h", encoding="utf-8")
    (zh / "index.md").write_text("# 你好", encoding="utf-8")
    (zh / "usage" / "hcaptcha.md").write_text("# h", encoding="utf-8")

    monkeypatch.setattr(module, "EN", en)
    monkeypatch.setattr(module, "ZH", zh)
    assert module.main() == 0
