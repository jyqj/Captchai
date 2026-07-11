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


@dataclass(frozen=True)
class ClassifierSelectors:
    """Provider-scoped selector overrides for challenge-shape detection.

    The classifier ships generic multi-provider defaults (grid + GeeTest slider
    + reCAPTCHA dynamic markers + …). A single provider's challenge DOM only
    uses a subset, and feeding it the other providers' class names invites
    misclassification — e.g. hCaptcha has no GeeTest sliders, so carrying
    ``.geetest_slider_button`` in its detection set is pure noise. A provider
    passes only the sets it wants to pin; ``None`` fields fall back to the
    classifier's generic class constants (so the default classifier — and every
    existing test — is unchanged).
    """

    grid: "tuple[str, ...] | None" = None
    tile: "str | None" = None
    dynamic_markers: "tuple[str, ...] | None" = None
    slider: "tuple[str, ...] | None" = None
    drag: "tuple[str, ...] | None" = None
    bbox: "tuple[str, ...] | None" = None
    prompt: "tuple[str, ...] | None" = None


class ChallengeClassifier:
    """Detect the challenge shape from cheap DOM signals first, vision fallback.

    ``frame`` is duck-typed as a Playwright ``FrameLocator``/``Page``: the only
    thing we require is ``frame.locator(selector).count()`` returning an
    awaitable int. Every access is guarded so a missing/odd frame degrades to
    ``UNKNOWN`` rather than raising.
    """

    # Selectors grouped by the signal they imply.
    GRID_SELECTORS = (".task-grid", ".task-image", "table.rc-imageselect-table")
    # The per-tile selector inside a grid. Counting it separates a real
    # multi-tile grid from a single-image "click the object" (area-select)
    # challenge, which hCaptcha also renders with ``.task-image``.
    TILE_SELECTOR = ".task-image"
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
    # Prompt containers — present on every rendered hCaptcha challenge (grid,
    # area-select, etc.) regardless of interaction shape.
    PROMPT_SELECTORS = (".prompt-text", ".challenge-prompt")
    # Any signal that the challenge iframe DOM has actually populated. Used by
    # the bounded pre-dispatch readiness wait so a slow (1–3s) iframe is given
    # time to render instead of being classified UNKNOWN / zero-tile — which
    # reads as "no challenge" and wastes the whole solve attempt + a retry.
    READY_SELECTORS = (
        GRID_SELECTORS
        + SLIDER_SELECTORS
        + DRAG_SELECTORS
        + BBOX_SELECTORS
        + DYNAMIC_MARKERS
        + PROMPT_SELECTORS
    )

    def __init__(
        self,
        vision: Optional[VisionClient] = None,
        *,
        selectors: "ClassifierSelectors | None" = None,
        vision_hint: bool = True,
    ) -> None:
        self._vision = vision
        # Whether to ask the vision client "what shape is this?" when DOM
        # detection fails. Only useful when the injected vision object can
        # actually return a shape name; a plain grid ``VisionRouter`` cannot, so
        # hCaptcha disables it and relies on the dispatcher's UNKNOWN fallback
        # instead of paying for a model call that can never yield a shape.
        self._vision_hint = vision_hint
        sel = selectors or ClassifierSelectors()
        # Instance selector sets: a provider override wins, else the generic
        # class constant. Detection reads these instance attributes so a
        # provider (hCaptcha) isn't muddied by other providers' class names.
        self._grid = sel.grid if sel.grid is not None else self.GRID_SELECTORS
        self._tile = sel.tile if sel.tile is not None else self.TILE_SELECTOR
        self._dynamic = (
            sel.dynamic_markers
            if sel.dynamic_markers is not None
            else self.DYNAMIC_MARKERS
        )
        self._slider = sel.slider if sel.slider is not None else self.SLIDER_SELECTORS
        self._drag = sel.drag if sel.drag is not None else self.DRAG_SELECTORS
        self._bbox = sel.bbox if sel.bbox is not None else self.BBOX_SELECTORS
        self._prompt = sel.prompt if sel.prompt is not None else self.PROMPT_SELECTORS
        self._ready = (
            tuple(self._grid)
            + tuple(self._slider)
            + tuple(self._drag)
            + tuple(self._bbox)
            + tuple(self._dynamic)
            + tuple(self._prompt)
        )

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

    async def ready(self, frame: Any) -> bool:
        """True once the challenge iframe DOM shows any recognizable signal.

        A cheap DOM-only probe (no vision) over the union of every shape's
        selectors plus the prompt containers. Used by the pre-dispatch
        readiness wait so classification runs against a rendered challenge
        rather than an empty iframe.
        """
        return await self._any(frame, self._ready)

    async def detect(self, frame: Any, ctx: ChallengeContext) -> ChallengeShape:
        # 1. A grid of tiles is the most common shape.
        if await self._any(frame, self._grid):
            # A single ``.task-image`` with no multi-tile grid is hCaptcha's
            # area-select ("click on the X"), NOT a grid — routing it to the
            # grid solver asks the model for tile indices on a one-image
            # coordinate task and always answers wrong. A tile count of exactly
            # one (language-independent, one cheap DOM read) disambiguates.
            if await self._count(frame, self._tile) == 1:
                return ChallengeShape.AREA_BBOX
            if self._dynamic and await self._any(frame, self._dynamic):
                return ChallengeShape.RECAPTCHA_DYNAMIC
            return ChallengeShape.GRID_SELECT

        # 2. Slider / canvas puzzles.
        if self._slider and await self._any(frame, self._slider):
            return ChallengeShape.CANVAS_SLIDE

        # 3. Drag-a-piece-to-a-slot puzzles.
        if self._drag and await self._any(frame, self._drag):
            return ChallengeShape.DRAG_DROP

        # 4. A single image with a click/area overlay.
        if self._bbox and await self._any(frame, self._bbox):
            return ChallengeShape.AREA_BBOX

        # 5. Nothing recognizable via DOM. Optionally ask vision "what kind?".
        #    Skipped when the injected vision object can't return a shape (the
        #    grid VisionRouter): the dispatcher's UNKNOWN fallback handles it
        #    without a wasted, image-less model call.
        if self._vision is not None and self._vision_hint:
            hinted = await self._vision_hint_shape(frame, ctx)
            if hinted is not None:
                return hinted

        return ChallengeShape.UNKNOWN

    async def _vision_hint_shape(
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
        *,
        unknown_fallback: Optional[ChallengeShape] = None,
    ) -> None:
        self._classifier = classifier
        self._registry: Dict[ChallengeShape, ShapeSolver] = dict(registry or {})
        # When detection yields UNKNOWN (a provider class-name change, an
        # unfamiliar challenge layout), fall back to this shape's solver rather
        # than giving up the whole attempt. hCaptcha sets GRID_SELECT — its
        # dominant shape — so a renamed ``.task-image`` degrades to "attempt the
        # grid" instead of a hard miss + retry.
        self._unknown_fallback = unknown_fallback

    @property
    def classifier(self) -> ChallengeClassifier:
        """The classifier used for detection (and the readiness probe)."""
        return self._classifier

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
        # UNKNOWN → optional fallback shape (e.g. hCaptcha's grid) so a DOM
        # signature change or an unrecognised layout still gets one real solve
        # attempt instead of an immediate miss.
        if shape is ChallengeShape.UNKNOWN and self._unknown_fallback is not None:
            log.info(
                "Challenge UNKNOWN; falling back to %s (task_id=%s)",
                self._unknown_fallback.value,
                ctx.task_id,
            )
            shape = self._unknown_fallback
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
