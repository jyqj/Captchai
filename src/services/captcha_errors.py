"""Classified captcha widget errors so the solve loop reacts per failure kind.

A widget ``error-callback`` was previously collapsed into a generic
``RuntimeError`` and the solver simply retried ``captcha_retries`` times with a
fixed 2s sleep — the worst possible response to a *rate-limit*, where hammering
the same egress deepens the block, and wasted effort for a *non-retryable*
error like malformed ``rqdata``.

This module maps the provider's error string to one of three reactions:

* :class:`RateLimitedError` — the egress is throttled. Fail this attempt fast
  (the proxy is already cooled down by ``_release_context(solved=False)``), so
  the next task picks a different egress instead of burning retries here.
* :class:`NonRetryableWidgetError` — a request-level fault (bad rqdata / sitekey
  / unsupported). Retrying is pointless; surface it immediately.
* :class:`RetryableWidgetError` — transient (network / expired / internal).
  Keep the existing retry-with-backoff behaviour.

``outcome`` is threaded into the cost ledger so a rate-limit shows up as its own
bucket in ``/admin/metrics`` rather than being indistinguishable from a solve
miss.
"""

from __future__ import annotations


class CaptchaError(RuntimeError):
    """Base for classified widget errors. Retryable + generic by default."""

    retryable: bool = True
    outcome: str = "failed"


class RetryableWidgetError(CaptchaError):
    retryable = True
    outcome = "widget_error"


class NonRetryableWidgetError(CaptchaError):
    retryable = False
    outcome = "widget_error"


class RateLimitedError(CaptchaError):
    retryable = False
    outcome = "rate_limited"


# Substring signals (matched case-insensitively) grouped by reaction. Order
# matters: rate-limit is checked first because a "rate-limited" string also
# contains no other keyword, and non-retryable is checked before the retryable
# default so "invalid-data" isn't swallowed by a generic retry.
_RATE_LIMIT_SIGNALS = ("rate-limit", "rate_limit", "rate limit", "too-many", "429")
_NON_RETRYABLE_SIGNALS = (
    "invalid-data",
    "invalid-render",
    "invalid-sitekey",
    "invalid sitekey",
    "bad-request",
    "unsupported",
    "invalid-captcha-id",
    "sitekey-secret-mismatch",
)
_RETRYABLE_SIGNALS = (
    "network",
    "timeout",
    "timed out",
    "expired",
    "challenge-error",
    "challenge error",
    "crash",
    "internal",
    "500",
    "502",
    "503",
)


def classify_widget_error(raw: object, *, provider: str = "captcha") -> CaptchaError:
    """Map a widget ``error-callback`` payload to a classified error.

    Unknown / boolean-only errors (e.g. Turnstile's bare ``true``) default to
    retryable so behaviour is unchanged from the pre-taxonomy code for anything
    not explicitly recognised.
    """
    text = str(raw if raw is not None else "").strip().lower()
    msg = f"{provider} widget error: {raw}"

    if any(sig in text for sig in _RATE_LIMIT_SIGNALS):
        return RateLimitedError(msg)
    if any(sig in text for sig in _NON_RETRYABLE_SIGNALS):
        return NonRetryableWidgetError(msg)
    if any(sig in text for sig in _RETRYABLE_SIGNALS):
        return RetryableWidgetError(msg)
    return RetryableWidgetError(msg)
