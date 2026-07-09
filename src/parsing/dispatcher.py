"""Challenge-parsing dispatcher.

Generalizes the single hardcoded hCaptcha image-grid path (see
``src/services/hcaptcha.py``) into a small taxonomy of *challenge shapes* plus
a classifier and dispatcher that route a live frame to the right shape solver.

Design goals:

  * **Vision-layer decoupling.** Neither the classifier nor the dispatcher nor
    the shape solvers know anything about the concrete vision router. They are
    handed a ``vision`` object via dependency injection whose only contract is
    an awaitable ``classify(req) -> result`` where ``result.indices`` is a
    ``list[int]`` and ``result.confidence`` is a ``float``. See
    ``VisionClient`` / ``ClassifyResult`` below.
  * **Cheap DOM signals first.** The classifier inspects the DOM (tile grids,
    slider handles, drag handles, bbox overlays) before ever paying for a
    vision round-trip. Vision is only a last-resort "what kind is this?" hint.
  * **Testability.** Every DOM interaction goes through a small awaitable helper
    guarded by ``try/except`` so unit tests can drive the whole flow with fakes
    and no real browser.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Optional, Protocol, runtime_checkable

log = logging.getLogger(__name__)


class ChallengeShape(str, Enum):
    """The taxonomy of challenge interaction shapes we know how to solve."""

    GRID_SELECT = "grid_select"
    AREA_BBOX = "area_bbox"
    DRAG_DROP = "drag_drop"
    ONE_OF_N = "one_of_n"
    ENTITY_COUNT = "entity_count"
    CANVAS_SLIDE = "canvas_slide"
    RECAPTCHA_DYNAMIC = "recaptcha_dynamic"
    UNKNOWN = "unknown"


@dataclass
class ChallengeContext:
    """Everything a solver needs to know about the current challenge attempt."""

    prompt: str = ""
    task_id: Optional[str] = None
    sitekey: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Vision contract (duck-typed / injected)
# ---------------------------------------------------------------------------


@runtime_checkable
class ClassifyResult(Protocol):
    """Minimal shape of a vision classification result."""

    indices: "list[int]"
    confidence: float


@runtime_checkable
class VisionClient(Protocol):
    """Minimal shape of the injected vision object.

    The concrete implementation lives in the vision layer and is adapted by the
    integrator; here we only rely on this awaitable method.
    """

    async def classify(self, req: Any) -> ClassifyResult: ...


# A token poll is any awaitable callable that returns the solved token or None.
TokenPoll = "Optional[Any]"  # documented alias; runtime type is Callable[[], Awaitable[Optional[str]]]


@runtime_checkable
class ShapeSolver(Protocol):
    """Anything that can attempt a challenge of one shape and return a token."""

    async def run(self, frame: Any, ctx: ChallengeContext) -> Optional[str]: ...


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


class ChallengeClassifier:
    """Detect the challenge shape from cheap DOM signals first, vision fallback.

    ``frame`` is duck-typed as a Playwright ``FrameLocator``/``Page``: the only
    thing we require is ``frame.locator(selector).count()`` returning an
    awaitable int. Every access is guarded so a missing/odd frame degrades to
    ``UNKNOWN`` rather than raising.
    """

    # Selectors grouped by the signal they imply.
    GRID_SELECTORS = (".task-grid", ".task-image", "table.rc-imageselect-table")
    DYNAMIC_MARKERS = (
        ".rc-imageselect-dynamic-selected",
        "[data-dynamic]",
        ".dynamic",
    )
    SLIDER_SELECTORS = (
        ".slider",
        ".slide-handle",
        ".geetest_slider_button",
        "canvas.slideBg",
        "[class*='slider']",
    )
    DRAG_SELECTORS = (
        ".drag-handle",
        ".draggable",
        "[draggable='true']",
        ".piece",
    )
    BBOX_SELECTORS = (".bbox-overlay", ".area-select", ".single-image")

    def __init__(self, vision: Optional[VisionClient] = None) -> None:
        self._vision = vision

    async def _count(self, frame: Any, selector: str) -> int:
        """Guarded ``frame.locator(selector).count()`` -> int (0 on any error)."""
        try:
            locator = frame.locator(selector)
            count = await locator.count()
            return int(count)
        except Exception:  # noqa: BLE001 - any duck-typed failure means "not present"
            return 0

    async def _any(self, frame: Any, selectors: "tuple[str, ...]") -> bool:
        for sel in selectors:
            if await self._count(frame, sel) > 0:
                return True
        return False

    async def detect(self, frame: Any, ctx: ChallengeContext) -> ChallengeShape:
        # 1. A grid of tiles is the most common shape.
        if await self._any(frame, self.GRID_SELECTORS):
            if await self._any(frame, self.DYNAMIC_MARKERS):
                return ChallengeShape.RECAPTCHA_DYNAMIC
            return ChallengeShape.GRID_SELECT

        # 2. Slider / canvas puzzles.
        if await self._any(frame, self.SLIDER_SELECTORS):
            return ChallengeShape.CANVAS_SLIDE

        # 3. Drag-a-piece-to-a-slot puzzles.
        if await self._any(frame, self.DRAG_SELECTORS):
            return ChallengeShape.DRAG_DROP

        # 4. A single image with a click/area overlay.
        if await self._any(frame, self.BBOX_SELECTORS):
            return ChallengeShape.AREA_BBOX

        # 5. Nothing recognizable via DOM. Optionally ask vision "what kind?".
        if self._vision is not None:
            hinted = await self._vision_hint(frame, ctx)
            if hinted is not None:
                return hinted

        return ChallengeShape.UNKNOWN

    async def _vision_hint(
        self, frame: Any, ctx: ChallengeContext
    ) -> Optional[ChallengeShape]:
        """Best-effort vision fallback. Never raises."""
        try:
            req = {"kind": "what_shape", "prompt": ctx.prompt, "extra": ctx.extra}
            result = await self._vision.classify(req)  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            return None

        # A vision router may return a shape name in a variety of ways; be
        # liberal about what we accept, without importing its concretes.
        candidate = getattr(result, "shape", None) or getattr(result, "kind", None)
        if isinstance(candidate, ChallengeShape):
            return candidate
        if isinstance(candidate, str):
            try:
                return ChallengeShape(candidate)
            except ValueError:
                return None
        return None


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


class ChallengeDispatcher:
    """Detect the shape, look up the registered solver, and run it."""

    def __init__(
        self,
        classifier: ChallengeClassifier,
        registry: Optional[Dict[ChallengeShape, ShapeSolver]] = None,
    ) -> None:
        self._classifier = classifier
        self._registry: Dict[ChallengeShape, ShapeSolver] = dict(registry or {})

    def register(self, shape: ChallengeShape, solver: ShapeSolver) -> None:
        self._registry[shape] = solver

    async def solve(
        self,
        frame: Any,
        ctx: ChallengeContext,
        *,
        on_detected: "Optional[Callable[[ChallengeShape], None]]" = None,
    ) -> Optional[str]:
        """Detect the challenge shape once, then run its solver.

        ``on_detected`` is a synchronous observer invoked with the detected
        shape *before* dispatch — the hCaptcha solver uses it to record the
        shape on the ledger without re-running :meth:`ChallengeClassifier.detect`
        (calling ``detect`` and then ``solve`` separately previously classified
        the same challenge twice, doubling the cost on the vision-fallback path).
        """
        shape = await self._classifier.detect(frame, ctx)
        log.info("Challenge classified as %s (task_id=%s)", shape.value, ctx.task_id)
        if on_detected is not None:
            try:
                on_detected(shape)
            except Exception:  # noqa: BLE001 - an observer must never break dispatch
                log.debug("on_detected observer raised", exc_info=True)

        solver = self._registry.get(shape)
        if solver is None:
            log.warning("No solver registered for shape %s; skipping", shape.value)
            return None

        try:
            token = await solver.run(frame, ctx)
        except Exception as exc:  # noqa: BLE001 - solver failures shouldn't crash dispatch
            log.warning("Solver for %s raised: %s", shape.value, exc)
            return None

        if token:
            log.info("Solver for %s produced a token (len=%d)", shape.value, len(token))
        return token
