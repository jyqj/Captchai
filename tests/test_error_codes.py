"""Tests pinning ``ApiErrorCode`` to the YesCaptcha / AntiCaptcha protocol.

The API returns these ``errorCode`` strings verbatim to clients that parse them
against the YesCaptcha-compatible protocol, so the values are a wire contract:
a rename or a reformat (even fixing the deliberate ``CAPCHA`` misspelling) would
silently break every conforming client. This test fails on any drift.
"""

from __future__ import annotations

import sys
from enum import StrEnum
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.api.error_codes import ApiErrorCode  # noqa: E402

# The exact protocol strings, spelled out here independently of the enum so a
# change to ``error_codes.py`` must also be made (and reviewed) here. The
# ``CAPCHA`` misspelling in NO_SUCH_CAPCHA_ID is intentional — it is the real
# AntiCaptcha protocol string.
_PROTOCOL_STRINGS = {
    "KEY_DOES_NOT_EXIST": "ERROR_KEY_DOES_NOT_EXIST",
    "TASK_NOT_SUPPORTED": "ERROR_TASK_NOT_SUPPORTED",
    "TASK_PROPERTY_EMPTY": "ERROR_TASK_PROPERTY_EMPTY",
    "NO_SLOT_AVAILABLE": "ERROR_NO_SLOT_AVAILABLE",
    "NO_SUCH_CAPCHA_ID": "ERROR_NO_SUCH_CAPCHA_ID",
    "CAPTCHA_UNSOLVABLE": "ERROR_CAPTCHA_UNSOLVABLE",
}


def test_error_code_values_match_protocol_strings() -> None:
    for member_name, wire_value in _PROTOCOL_STRINGS.items():
        member = ApiErrorCode[member_name]
        assert member.value == wire_value


def test_error_code_enum_has_no_undocumented_members() -> None:
    """Every enum member is pinned above (no member escapes the wire contract)."""
    assert {m.name for m in ApiErrorCode} == set(_PROTOCOL_STRINGS)


def test_error_code_is_str_enum_and_compares_as_string() -> None:
    """StrEnum so ``errorCode == "ERROR_..."`` works for clients and routes."""
    assert issubclass(ApiErrorCode, StrEnum)
    assert ApiErrorCode.KEY_DOES_NOT_EXIST == "ERROR_KEY_DOES_NOT_EXIST"
    # It also serialises to the bare protocol string (FastAPI JSON encoding).
    assert str(ApiErrorCode.NO_SUCH_CAPCHA_ID) == "ERROR_NO_SUCH_CAPCHA_ID"


def test_error_codes_are_unique() -> None:
    values = [m.value for m in ApiErrorCode]
    assert len(values) == len(set(values))
