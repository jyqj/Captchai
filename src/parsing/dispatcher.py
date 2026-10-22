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

from ..services.captcha_errors import CaptchaError

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


# ---------------------------------------------------------------------------
# Shape selector vocabulary — the single source of truth
# ---------------------------------------------------------------------------
#
# The classifier decides *which shape* a frame is from cheap ``count()`` probes,
# and the dispatched solver then has to find (screenshot / click) the elements
# for that shape. Keeping those two selector sets in separate literal lists let
# them drift: the classifier could recognize a shape the solver had no selector
# for, wasting the whole attempt. Both sides now read the registry below, so a
# selector added for detection is visible to the solver and vice versa.
#
# ``detect`` groups hold the classifier's probe selectors; the named element
# groups (``tile`` / ``handle`` / ``background`` / ``puzzle`` / ``image``) are
# what the shape's solver operates on. Dynamic grids are a grid subtype — the
# classifier only reaches the dynamic markers after a grid signal matched — so
# their vocabulary also inherits the grid's (see ``_PARENT_SHAPE``).
SHAPE_SELECTORS: Dict[ChallengeShape, Dict[str, "tuple[str, ...]"]] = {
    ChallengeShape.GRID_SELECT: {
        "detect": (".task-grid", ".task-image", "table.rc-imageselect-table"),
        "tile": (".task-image",),
    },
    ChallengeShape.RECAPTCHA_DYNAMIC: {
        "detect": (".rc-imageselect-dynamic-selected", "[data-dynamic]", ".dynamic"),
        "tile": (".rc-imageselect-tile", ".task-image"),
    },
    ChallengeShape.CANVAS_SLIDE: {
        "detect": (
            ".slider",
            ".slide-handle",
            ".geetest_slider_button",
            "canvas.slideBg",
            "[class*='slider']",
        ),
        "handle": (".slide-handle", ".slider", ".geetest_slider_button"),
        "background": (
            "canvas.slideBg",
            ".geetest_canvas_bg",
            ".slider-bg",
            ".puzzle-image",
            ".captcha-image",
        ),
    },
    ChallengeShape.DRAG_DROP: {
        "detect": (
            ".drag-handle",
            ".draggable",
            "[draggable='true']",
            ".piece",
            # The drag area itself is what the solver screenshots; probing it
            # for detection keeps "classified as drag" and "solver can find the
            # puzzle" the same question.
            ".drag-area",
        ),
        "puzzle": (
            ".puzzle-image",
            ".captcha-image",
            ".drag-area",
            "canvas",
            ".task-image",
        ),
    },
    ChallengeShape.AREA_BBOX: {
        "detect": (".bbox-overlay", ".area-select", ".single-image"),
        "image": (".single-image", ".task-image", "canvas"),
    },
}

# Shapes whose detection vocabulary builds on another shape's (grid subtype).
_PARENT_SHAPE: Dict[ChallengeShape, ChallengeShape] = {
    ChallengeShape.RECAPTCHA_DYNAMIC: ChallengeShape.GRID_SELECT,
}


def shape_selectors(shape: ChallengeShape, group: str) -> "tuple[str, ...]":
    """Return one selector group from :data:`SHAPE_SELECTORS`."""
    try:
        return SHAPE_SELECTORS[shape][group]
    except KeyError as exc:  # pragma: no cover - programming error
        raise KeyError(
            f"no selector group {group!r} for shape {shape.value}"
        ) from exc


def selector_csv(shape: ChallengeShape, group: str) -> str:
    """Comma-joined selector group, ready for a Playwright locator string."""
    return ", ".join(shape_selectors(shape, group))


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

    # Selectors grouped by the signal they imply. Every set comes from
    # ``SHAPE_SELECTORS`` (the same registry the shape solvers read), so
    # detection vocabulary and solver selectors can't drift apart.
    GRID_SELECTORS = shape_selectors(ChallengeShape.GRID_SELECT, "detect")
    # The per-tile selector inside a grid. Counting it separates a real
    # multi-tile grid from a single-image "click the object" (area-select)
    # challenge, which hCaptcha also renders with ``.task-image``.
    TILE_SELECTOR = selector_csv(ChallengeShape.GRID_SELECT, "tile")
    DYNAMIC_MARKERS = shape_selectors(ChallengeShape.RECAPTCHA_DYNAMIC, "detect")
    SLIDER_SELECTORS = shape_selectors(ChallengeShape.CANVAS_SLIDE, "detect")
    DRAG_SELECTORS = shape_selectors(ChallengeShape.DRAG_DROP, "detect")
    BBOX_SELECTORS = shape_selectors(ChallengeShape.AREA_BBOX, "detect")
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
        except CaptchaError:
            # Classified errors (rate limit, non-retryable widget error, vision
            # parse failure) carry the reaction the solve loop must take —
            # fail fast or retry with backoff. Swallowing them here collapsed
            # every kind into an indistinguishable "no token".
            raise
        except Exception as exc:  # noqa: BLE001 - solver failures shouldn't crash dispatch
            log.warning("Solver for %s raised: %s", shape.value, exc)
            return None

        if token:
            log.info("Solver for %s produced a token (len=%d)", shape.value, len(token))
        return token


def assert_selector_vocabulary_matches() -> None:
    """Assert the classifier and every shape solver share one vocabulary.

    Module-level consistency hook (also callable from tests): a future edit that
    adds a selector to the classifier or to a solver without registering it in
    :data:`SHAPE_SELECTORS` — the drift that lets a challenge be classified as a
    shape whose solver can't find it — fails loudly here instead of silently
    wasting solve attempts. Raises ``AssertionError`` on any mismatch.
    """
    # Imported lazily: the shape modules import this module for the shared
    # request/context types, so a module-level import would be circular.
    from .shapes.area_bbox import AreaBBoxSolver
    from .shapes.drag_drop import DragDropSolver
    from .shapes.dynamic_grid import DynamicGridSolver
    from .shapes.grid_select import GridSelectSolver
    from .shapes.slide import CanvasSlideSolver

    solver_checks = (
        (ChallengeShape.GRID_SELECT, GridSelectSolver, "TILE_SELECTOR", "tile"),
        (
            ChallengeShape.RECAPTCHA_DYNAMIC,
            DynamicGridSolver,
            "TILE_SELECTOR",
            "tile",
        ),
        (ChallengeShape.CANVAS_SLIDE, CanvasSlideSolver, "HANDLE_SELECTOR", "handle"),
        (
            ChallengeShape.CANVAS_SLIDE,
            CanvasSlideSolver,
            "BG_SELECTOR",
            "background",
        ),
        (ChallengeShape.DRAG_DROP, DragDropSolver, "PUZZLE_SELECTOR", "puzzle"),
        (ChallengeShape.AREA_BBOX, AreaBBoxSolver, "IMAGE_SELECTOR", "image"),
    )
    for shape, solver_cls, attr, group in solver_checks:
        assert solver_cls.SHAPE is shape, (
            f"{solver_cls.__name__}.SHAPE is {solver_cls.SHAPE}, expected {shape}"
        )
        actual = tuple(
            part.strip() for part in str(getattr(solver_cls, attr)).split(",")
        )
        expected = shape_selectors(shape, group)
        assert actual == expected, (
            f"{solver_cls.__name__}.{attr} is {actual!r}, but "
            f"SHAPE_SELECTORS[{shape.value}][{group!r}] is {expected!r}"
        )

    # The classifier's own constants must be the registry values verbatim.
    assert ChallengeClassifier.GRID_SELECTORS == shape_selectors(
        ChallengeShape.GRID_SELECT, "detect"
    ), "ChallengeClassifier.GRID_SELECTORS drifted from SHAPE_SELECTORS"
    assert ChallengeClassifier.DYNAMIC_MARKERS == shape_selectors(
        ChallengeShape.RECAPTCHA_DYNAMIC, "detect"
    ), "ChallengeClassifier.DYNAMIC_MARKERS drifted from SHAPE_SELECTORS"
    assert ChallengeClassifier.SLIDER_SELECTORS == shape_selectors(
        ChallengeShape.CANVAS_SLIDE, "detect"
    ), "ChallengeClassifier.SLIDER_SELECTORS drifted from SHAPE_SELECTORS"
    assert ChallengeClassifier.DRAG_SELECTORS == shape_selectors(
        ChallengeShape.DRAG_DROP, "detect"
    ), "ChallengeClassifier.DRAG_SELECTORS drifted from SHAPE_SELECTORS"
    assert ChallengeClassifier.BBOX_SELECTORS == shape_selectors(
        ChallengeShape.AREA_BBOX, "detect"
    ), "ChallengeClassifier.BBOX_SELECTORS drifted from SHAPE_SELECTORS"

    # Every shape must expose some selector its classifier can detect AND some
    # element its solver can operate on, and the two sets must overlap (for
    # dynamic grids the grid vocabulary counts — it is a grid subtype).
    for shape, groups in SHAPE_SELECTORS.items():
        detect = set(groups.get("detect", ()))
        assert detect, f"{shape.value}: empty detection vocabulary"
        parent = _PARENT_SHAPE.get(shape)
        if parent is not None:
            detect |= set(shape_selectors(parent, "detect"))
        elements = set()
        for name, selectors in groups.items():
            if name != "detect":
                elements.update(selectors)
        assert elements, f"{shape.value}: no solver element selectors"
        assert elements & detect, (
            f"{shape.value}: classifier detects {sorted(detect)} but the solver "
            f"only operates on {sorted(elements)} — vocabularies are disjoint"
        )
