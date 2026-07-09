"""Single source of truth for supported task types and how each is handled.

Solver registration (``main.py``), request field validation (``api/routes.py``),
and the API tests each previously kept a *parallel, hand-maintained* list of
task-type strings. They agreed today, but any type added to one and forgotten in
another drifts silently — and ``routes.py`` keys its required-field validation
off set membership, so a new browser type missing from ``_BROWSER_TASK_TYPES``
would skip the ``websiteURL`` / ``websiteKey`` check entirely.

This module is the one list. Each :class:`TaskType` declares:

  * ``provider`` — which solver handles it (``main.py`` wiring),
  * ``category`` — the concurrency pool it draws from (``"browser"`` /
    ``"vision"``; the string values match ``task_manager.TaskCategory`` so it can
    be passed straight to ``register_solver``), and
  * ``validation`` — which required-field check ``routes.py`` applies.

Everything else derives from :data:`TASK_TYPES` via the accessors below, so a new
type is added in exactly one place.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Provider(str, Enum):
    """The solver family that handles a task type."""

    RECAPTCHA_V3 = "recaptcha_v3"
    RECAPTCHA_V2 = "recaptcha_v2"
    HCAPTCHA = "hcaptcha"
    TURNSTILE = "turnstile"
    IMAGE_TO_TEXT = "image_to_text"
    CLASSIFICATION = "classification"


class ValidationKind(str, Enum):
    """Which required-field validation the API applies to a task type."""

    BROWSER = "browser"  # requires websiteURL + websiteKey
    IMAGE = "image"  # requires body (base64 image)
    CLASSIFICATION = "classification"  # requires image / images / body / queries


# Concurrency category string values (mirror ``task_manager.TaskCategory`` so
# they can be handed to ``register_solver`` directly without importing it here
# — keeping this a dependency-free, pure-data module).
CATEGORY_BROWSER = "browser"
CATEGORY_VISION = "vision"


@dataclass(frozen=True)
class TaskType:
    name: str
    provider: Provider
    category: str
    validation: ValidationKind


TASK_TYPES: tuple[TaskType, ...] = (
    # ── reCAPTCHA v3 (browser) ──
    TaskType("RecaptchaV3TaskProxyless", Provider.RECAPTCHA_V3, CATEGORY_BROWSER, ValidationKind.BROWSER),
    TaskType("RecaptchaV3TaskProxylessM1", Provider.RECAPTCHA_V3, CATEGORY_BROWSER, ValidationKind.BROWSER),
    TaskType("RecaptchaV3TaskProxylessM1S7", Provider.RECAPTCHA_V3, CATEGORY_BROWSER, ValidationKind.BROWSER),
    TaskType("RecaptchaV3TaskProxylessM1S9", Provider.RECAPTCHA_V3, CATEGORY_BROWSER, ValidationKind.BROWSER),
    TaskType("RecaptchaV3EnterpriseTask", Provider.RECAPTCHA_V3, CATEGORY_BROWSER, ValidationKind.BROWSER),
    TaskType("RecaptchaV3EnterpriseTaskM1", Provider.RECAPTCHA_V3, CATEGORY_BROWSER, ValidationKind.BROWSER),
    # ── reCAPTCHA v2 (browser) ──
    TaskType("NoCaptchaTaskProxyless", Provider.RECAPTCHA_V2, CATEGORY_BROWSER, ValidationKind.BROWSER),
    TaskType("RecaptchaV2TaskProxyless", Provider.RECAPTCHA_V2, CATEGORY_BROWSER, ValidationKind.BROWSER),
    TaskType("RecaptchaV2EnterpriseTaskProxyless", Provider.RECAPTCHA_V2, CATEGORY_BROWSER, ValidationKind.BROWSER),
    # ── hCaptcha (browser) ──
    TaskType("HCaptchaTaskProxyless", Provider.HCAPTCHA, CATEGORY_BROWSER, ValidationKind.BROWSER),
    # ── Turnstile (browser) ──
    TaskType("TurnstileTaskProxyless", Provider.TURNSTILE, CATEGORY_BROWSER, ValidationKind.BROWSER),
    TaskType("TurnstileTaskProxylessM1", Provider.TURNSTILE, CATEGORY_BROWSER, ValidationKind.BROWSER),
    # ── Image-to-text (vision) ──
    TaskType("ImageToTextTask", Provider.IMAGE_TO_TEXT, CATEGORY_VISION, ValidationKind.IMAGE),
    TaskType("ImageToTextTaskMuggle", Provider.IMAGE_TO_TEXT, CATEGORY_VISION, ValidationKind.IMAGE),
    TaskType("ImageToTextTaskM1", Provider.IMAGE_TO_TEXT, CATEGORY_VISION, ValidationKind.IMAGE),
    # ── Classification (vision) ──
    TaskType("HCaptchaClassification", Provider.CLASSIFICATION, CATEGORY_VISION, ValidationKind.CLASSIFICATION),
    TaskType("ReCaptchaV2Classification", Provider.CLASSIFICATION, CATEGORY_VISION, ValidationKind.CLASSIFICATION),
    TaskType("FunCaptchaClassification", Provider.CLASSIFICATION, CATEGORY_VISION, ValidationKind.CLASSIFICATION),
    TaskType("AwsClassification", Provider.CLASSIFICATION, CATEGORY_VISION, ValidationKind.CLASSIFICATION),
)


def all_task_type_names() -> list[str]:
    """Every supported task-type string, in declaration order."""
    return [t.name for t in TASK_TYPES]


def types_for_provider(provider: Provider) -> list[str]:
    """Task-type names handled by one provider, in declaration order."""
    return [t.name for t in TASK_TYPES if t.provider == provider]


def names_for_validation(kind: ValidationKind) -> set[str]:
    """Task-type names that get one required-field validation kind."""
    return {t.name for t in TASK_TYPES if t.validation == kind}
