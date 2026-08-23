"""YesCaptcha / AntiCaptcha protocol error codes.

Centralises the ``errorCode`` strings returned by the HTTP API so routes and
tests share one source of truth. Values are kept byte-identical to the
YesCaptcha-compatible protocol — do not rename or reformat them.
"""

from __future__ import annotations

from enum import StrEnum


class ApiErrorCode(StrEnum):
    KEY_DOES_NOT_EXIST = "ERROR_KEY_DOES_NOT_EXIST"
    TASK_NOT_SUPPORTED = "ERROR_TASK_NOT_SUPPORTED"
    TASK_PROPERTY_EMPTY = "ERROR_TASK_PROPERTY_EMPTY"
    NO_SLOT_AVAILABLE = "ERROR_NO_SLOT_AVAILABLE"
    NO_SUCH_CAPCHA_ID = "ERROR_NO_SUCH_CAPCHA_ID"
    CAPTCHA_UNSOLVABLE = "ERROR_CAPTCHA_UNSOLVABLE"
