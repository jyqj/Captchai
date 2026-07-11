"""Tests for the challenge-parsing dispatcher and shape solvers.

Everything runs with fakes and NO browser. Plain ``def test_*`` functions drive
async code with ``asyncio.run`` (no pytest-asyncio).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    _ = sys.path.insert(0, str(PROJECT_ROOT))

from src.parsing.dispatcher import (
    ChallengeClassifier,
    ChallengeContext,
    ChallengeDispatcher,
    ChallengeShape,
    ClassifierSelectors,
)
from src.parsing.shapes.area_bbox import AreaBBoxSolver
from src.parsing.shapes.base import BaseShapeSolver
from src.parsing.shapes.drag_drop import DragDropSolver
from src.parsing.shapes.dynamic_grid import DynamicGridSolver
from src.parsing.shapes.grid_select import GridSelectSolver
from src.parsing.shapes.slide import CanvasSlideSolver


def _png_bytes(width: int, height: int) -> bytes:
    """A minimal PNG header (signature + IHDR) with the given pixel dimensions.

    ``png_pixel_size`` reads the IHDR width/height at bytes 16:24, so a real
    encoder isn't needed — this is enough to exercise the screenshot→CSS scale
    without pulling in Pillow.
    """
    import struct

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr_len = struct.pack(">I", 13)
    ihdr_type = b"IHDR"
    dims = struct.pack(">II", width, height)
    rest = b"\x08\x06\x00\x00\x00"  # bit depth / colour type / etc.
    return sig + ihdr_len + ihdr_type + dims + rest


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeLocator:
    """A locator scoped to one selector on a FakeFrame; records interactions."""

    def __init__(self, frame: "FakeFrame", selector: str, index: Optional[int] = None):
        self._frame = frame
        self._selector = selector
        self._index = index

    @property
    def first(self) -> "FakeLocator":
        return FakeLocator(self._frame, self._selector, 0)

    def nth(self, index: int) -> "FakeLocator":
        return FakeLocator(self._frame, self._selector, index)

    async def count(self) -> int:
        return self._frame.counts.get(self._selector, 0)

    async def inner_text(self) -> str:
        text = self._frame.texts.get(self._selector)
        if text is None:
            raise RuntimeError(f"no text for {self._selector}")
        return text

    async def screenshot(self) -> bytes:
        self._frame.screenshots.append((self._selector, self._index))
        shot = self._frame.shots.get(self._selector)
        if shot is not None:
            return shot
        return f"shot:{self._selector}:{self._index}".encode()

    async def click(self, **kwargs: Any) -> None:
        self._frame.clicks.append((self._selector, self._index, kwargs))

    async def bounding_box(self) -> dict:
        return self._frame.boxes.get(
            self._selector, {"x": 10.0, "y": 20.0, "width": 40.0, "height": 40.0}
        )


class FakeFrame:
    """Duck-typed FrameLocator/Page that returns canned data and records calls."""

    def __init__(self, counts=None, texts=None, boxes=None, shots=None):
        self.counts = counts or {}
        self.texts = texts or {}
        self.boxes = boxes or {}
        self.shots = shots or {}
        self.clicks: List[Any] = []
        self.screenshots: List[Any] = []

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, selector)


class FakeVision:
    """Injected vision object with ``async classify(req) -> result``."""

    def __init__(self, indices: List[int], confidence: float = 0.9):
        self.indices = indices
        self.confidence = confidence
        self.calls: List[Any] = []

    async def classify(self, req: Any):
        self.calls.append(req)
        return SimpleNamespace(indices=list(self.indices), confidence=self.confidence)


def make_token_poll(sequence: List[Optional[str]]):
    """A token poll that yields values from ``sequence`` then None forever."""
    state = {"i": 0}

    async def poll() -> Optional[str]:
        i = state["i"]
        state["i"] += 1
        if i < len(sequence):
            return sequence[i]
        return None

    return poll, state


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


def test_classifier_detects_grid_select() -> None:
    async def run() -> None:
        frame = FakeFrame(counts={".task-image": 9})
        clf = ChallengeClassifier()
        shape = await clf.detect(frame, ChallengeContext(prompt="click buses"))
        assert shape is ChallengeShape.GRID_SELECT

    asyncio.run(run())


def test_classifier_detects_dynamic_grid() -> None:
    async def run() -> None:
        frame = FakeFrame(counts={".task-grid": 1, "[data-dynamic]": 1})
        clf = ChallengeClassifier()
        shape = await clf.detect(frame, ChallengeContext())
        assert shape is ChallengeShape.RECAPTCHA_DYNAMIC

    asyncio.run(run())


def test_classifier_single_task_image_is_area_bbox() -> None:
    """A single .task-image is hCaptcha area-select ("click the X"), not a grid.

    hCaptcha renders the coordinate ("click on the largest animal") challenge
    with the same .task-image class as a grid, so tile count disambiguates:
    exactly one tile is a single-image coordinate task, not a multi-tile grid.
    """
    async def run() -> None:
        frame = FakeFrame(counts={".task-image": 1})
        clf = ChallengeClassifier()
        shape = await clf.detect(frame, ChallengeContext(prompt="click the bus"))
        assert shape is ChallengeShape.AREA_BBOX

    asyncio.run(run())


def test_classifier_multi_task_image_is_grid_select() -> None:
    """Two or more .task-image tiles remain a grid-select challenge."""
    async def run() -> None:
        frame = FakeFrame(counts={".task-image": 9})
        clf = ChallengeClassifier()
        shape = await clf.detect(frame, ChallengeContext())
        assert shape is ChallengeShape.GRID_SELECT

    asyncio.run(run())


def test_classifier_ready_true_on_any_signal() -> None:
    """ready() is True for grid tiles, a prompt, or any shape marker."""
    async def run() -> None:
        clf = ChallengeClassifier()
        assert await clf.ready(FakeFrame(counts={".task-image": 9})) is True
        assert await clf.ready(FakeFrame(counts={".prompt-text": 1})) is True
        assert await clf.ready(FakeFrame(counts={".slide-handle": 1})) is True

    asyncio.run(run())


def test_classifier_ready_false_on_empty_iframe() -> None:
    """ready() is False for a not-yet-rendered (empty) challenge iframe."""
    async def run() -> None:
        clf = ChallengeClassifier()
        assert await clf.ready(FakeFrame(counts={})) is False
        assert await clf.ready(FakeFrame(counts={".task-image": 0})) is False

    asyncio.run(run())


def test_classifier_detects_canvas_slide() -> None:
    async def run() -> None:
        frame = FakeFrame(counts={".slide-handle": 1})
        clf = ChallengeClassifier()
        shape = await clf.detect(frame, ChallengeContext())
        assert shape is ChallengeShape.CANVAS_SLIDE

    asyncio.run(run())


def test_classifier_unknown_when_nothing_matches() -> None:
    async def run() -> None:
        frame = FakeFrame(counts={})
        clf = ChallengeClassifier()
        shape = await clf.detect(frame, ChallengeContext())
        assert shape is ChallengeShape.UNKNOWN

    asyncio.run(run())


def test_classifier_vision_fallback_hint() -> None:
    async def run() -> None:
        class HintVision:
            async def classify(self, req: Any):
                return SimpleNamespace(shape="drag_drop", indices=[], confidence=0.5)

        frame = FakeFrame(counts={})
        clf = ChallengeClassifier(vision=HintVision())
        shape = await clf.detect(frame, ChallengeContext())
        assert shape is ChallengeShape.DRAG_DROP

    asyncio.run(run())


def test_classifier_vision_hint_disabled_skips_model_call() -> None:
    """``vision_hint=False`` never calls the vision client for shape detection.

    hCaptcha sets this because its VisionAdapter classifies tile IMAGES, not
    "what shape is this frame" — an image-less hint would be a wasted model
    call that can't return a shape. Detection must go straight to UNKNOWN.
    """
    async def run() -> None:
        class BoomVision:
            def __init__(self) -> None:
                self.calls = 0

            async def classify(self, req: Any):
                self.calls += 1
                raise AssertionError("vision must not be consulted for the hint")

        vision = BoomVision()
        clf = ChallengeClassifier(vision=vision, vision_hint=False)
        shape = await clf.detect(FakeFrame(counts={}), ChallengeContext())
        assert shape is ChallengeShape.UNKNOWN
        assert vision.calls == 0

    asyncio.run(run())


def test_dispatcher_unknown_fallback_runs_fallback_solver() -> None:
    """UNKNOWN detection routes to the configured fallback shape's solver.

    A DOM class-name change (hCaptcha renames a tile class) yields UNKNOWN; with
    ``unknown_fallback=GRID_SELECT`` the dispatcher still attempts the grid
    solver instead of giving up the whole attempt.
    """
    async def run() -> None:
        class StubSolver:
            def __init__(self) -> None:
                self.ran = False

            async def run(self, frame: Any, ctx: ChallengeContext) -> Optional[str]:
                self.ran = True
                return "FALLBACK-TOK"

        stub = StubSolver()
        dispatcher = ChallengeDispatcher(
            ChallengeClassifier(), unknown_fallback=ChallengeShape.GRID_SELECT
        )
        dispatcher.register(ChallengeShape.GRID_SELECT, stub)
        seen: List[ChallengeShape] = []
        # Empty frame → UNKNOWN → fallback → grid solver.
        token = await dispatcher.solve(
            FakeFrame(counts={}), ChallengeContext(), on_detected=seen.append
        )
        assert token == "FALLBACK-TOK"
        assert stub.ran is True
        # on_detected observes the resolved (fallback) shape, not UNKNOWN.
        assert seen == [ChallengeShape.GRID_SELECT]

    asyncio.run(run())


def test_dispatcher_no_unknown_fallback_returns_none() -> None:
    """Without a fallback, UNKNOWN with no registered solver still returns None."""
    async def run() -> None:
        dispatcher = ChallengeDispatcher(ChallengeClassifier())
        token = await dispatcher.solve(FakeFrame(counts={}), ChallengeContext())
        assert token is None

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Provider-scoped selectors (hCaptcha profile drops foreign class names)
# ---------------------------------------------------------------------------


_HCAPTCHA_SELECTORS = ClassifierSelectors(
    grid=(".task-grid", ".task-image"),
    tile=".task-image",
    dynamic_markers=(),
    slider=(),
    drag=("[draggable='true']", ".draggable"),
    bbox=(".challenge-example",),
    prompt=(".prompt-text", ".challenge-prompt"),
)


def test_hcaptcha_profile_ignores_geetest_slider() -> None:
    """A GeeTest slider class is NOT a slide challenge under the hCaptcha
    profile (hCaptcha uses no sliders), so it degrades to UNKNOWN instead of
    misrouting a page to the slide solver."""
    async def run() -> None:
        frame = FakeFrame(counts={".slide-handle": 1, ".geetest_slider_button": 1})
        clf = ChallengeClassifier(selectors=_HCAPTCHA_SELECTORS)
        shape = await clf.detect(frame, ChallengeContext())
        assert shape is ChallengeShape.UNKNOWN

    asyncio.run(run())


def test_hcaptcha_profile_grid_and_area_select_still_detected() -> None:
    """The hCaptcha profile still classifies its real shapes: a multi-tile
    ``.task-image`` grid, and a single ``.task-image`` as area-select."""
    async def run() -> None:
        clf = ChallengeClassifier(selectors=_HCAPTCHA_SELECTORS)
        grid = await clf.detect(
            FakeFrame(counts={".task-image": 9}), ChallengeContext()
        )
        assert grid is ChallengeShape.GRID_SELECT
        area = await clf.detect(
            FakeFrame(counts={".task-image": 1}), ChallengeContext()
        )
        assert area is ChallengeShape.AREA_BBOX

    asyncio.run(run())


def test_hcaptcha_profile_no_dynamic_grid_misroute() -> None:
    """With empty dynamic markers, a plain hCaptcha grid that happens to carry a
    reCAPTCHA-style ``[data-dynamic]`` attr is still a plain GRID_SELECT."""
    async def run() -> None:
        frame = FakeFrame(counts={".task-image": 9, "[data-dynamic]": 1})
        clf = ChallengeClassifier(selectors=_HCAPTCHA_SELECTORS)
        shape = await clf.detect(frame, ChallengeContext())
        assert shape is ChallengeShape.GRID_SELECT

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def test_dispatcher_routes_to_registered_solver() -> None:
    async def run() -> None:
        class StubSolver:
            async def run(self, frame: Any, ctx: ChallengeContext) -> Optional[str]:
                return "TOKEN-123"

        frame = FakeFrame(counts={".task-image": 4})
        dispatcher = ChallengeDispatcher(ChallengeClassifier(), {})
        dispatcher.register(ChallengeShape.GRID_SELECT, StubSolver())
        token = await dispatcher.solve(frame, ChallengeContext())
        assert token == "TOKEN-123"

    asyncio.run(run())


def test_dispatcher_returns_none_when_no_solver() -> None:
    async def run() -> None:
        frame = FakeFrame(counts={".task-image": 4})
        dispatcher = ChallengeDispatcher(ChallengeClassifier(), {})
        token = await dispatcher.solve(frame, ChallengeContext())
        assert token is None

    asyncio.run(run())


def test_dispatcher_swallows_solver_errors() -> None:
    async def run() -> None:
        class BoomSolver:
            async def run(self, frame: Any, ctx: ChallengeContext) -> Optional[str]:
                raise RuntimeError("boom")

        frame = FakeFrame(counts={".task-image": 4})
        dispatcher = ChallengeDispatcher(ChallengeClassifier(), {})
        dispatcher.register(ChallengeShape.GRID_SELECT, BoomSolver())
        token = await dispatcher.solve(frame, ChallengeContext())
        assert token is None

    asyncio.run(run())


# ---------------------------------------------------------------------------
# GridSelectSolver
# ---------------------------------------------------------------------------


def test_grid_select_clicks_indices_submits_and_returns_token() -> None:
    async def run() -> None:
        frame = FakeFrame(
            counts={".task-image": 6},
            texts={".prompt-text": "  click all buses  "},
        )
        vision = FakeVision(indices=[0, 2, 5])
        # No token until after we submit (index 0 poll -> None, later -> token).
        poll, _state = make_token_poll([None, None, "SOLVED"])
        solver = GridSelectSolver(vision=vision, token_poll=poll)

        token = await solver.run(frame, ChallengeContext())
        assert token == "SOLVED"

        # Clicked exactly the returned tile indices.
        tile_clicks = [c for c in frame.clicks if c[0] == ".task-image"]
        assert [c[1] for c in tile_clicks] == [0, 2, 5]

        # Submit was clicked.
        assert any(c[0] == ".button-submit, .submit" for c in frame.clicks)

        # Parallel screenshot path exercised: one screenshot per tile.
        assert len(frame.screenshots) == 6

        # Vision received the trimmed prompt and the tile images.
        req = vision.calls[0]
        assert req.prompt == "click all buses"
        assert len(req.images) == 6

    asyncio.run(run())


def test_grid_select_stops_when_no_indices() -> None:
    async def run() -> None:
        frame = FakeFrame(counts={".task-image": 4}, texts={".prompt-text": "x"})
        vision = FakeVision(indices=[])
        poll, _state = make_token_poll([None])  # never yields a token
        solver = GridSelectSolver(vision=vision, token_poll=poll)

        token = await solver.run(frame, ChallengeContext())
        assert token is None
        # Vision consulted once; no tiles clicked because indices was empty.
        assert len(vision.calls) == 1
        assert not any(c[0] == ".task-image" for c in frame.clicks)

    asyncio.run(run())


class _FakeTouchscreen:
    def __init__(self) -> None:
        self.taps: List[tuple] = []

    async def tap(self, x: float, y: float) -> None:
        self.taps.append((x, y))


class _FakeTouchPage:
    """A page exposing a touchscreen (mobile) and a mouse that must NOT be used."""

    def __init__(self) -> None:
        self.touchscreen = _FakeTouchscreen()
        self.mouse_moves: List[tuple] = []
        self.mouse = SimpleNamespace(
            move=self._move, down=self._noop, up=self._noop
        )

    async def _move(self, x: float, y: float) -> None:
        self.mouse_moves.append((x, y))

    async def _noop(self, *a: Any, **k: Any) -> None:
        return None


def test_grid_select_taps_via_touchscreen_on_mobile() -> None:
    """A mobile context (ctx.extra['touch']) selects tiles with trusted touch
    taps, not mouse events — a mouse click on a phone fingerprint is a modality
    contradiction hCaptcha mobile scores."""
    async def run() -> None:
        frame = FakeFrame(counts={".task-image": 4}, texts={".prompt-text": "buses"})
        vision = FakeVision(indices=[1, 3])
        poll, _state = make_token_poll([None, "TAP-TOK"])
        solver = GridSelectSolver(vision=vision, token_poll=poll)
        page = _FakeTouchPage()
        ctx = ChallengeContext(
            extra={
                "page": page,
                "humanize": True,
                "humanize_jitter_ms": 0,
                "touch": True,
            }
        )
        token = await solver.run(frame, ctx)
        assert token == "TAP-TOK"
        # Tiles + submit were tapped via the touchscreen (2 tiles + 1 submit).
        assert len(page.touchscreen.taps) >= 3
        # The mouse was never moved (no desktop pointer path on a phone).
        assert page.mouse_moves == []
        # And no fallback locator.click() teleport either.
        assert not any(c[0] == ".task-image" for c in frame.clicks)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# DynamicGridSolver
# ---------------------------------------------------------------------------


def test_dynamic_grid_terminates_when_token_appears() -> None:
    async def run() -> None:
        frame = FakeFrame(
            counts={".rc-imageselect-tile, .task-image": 9},
            texts={".prompt-text": "select cars"},
        )
        vision = FakeVision(indices=[1, 3])
        # Token appears on the 2nd poll of the loop.
        poll, _state = make_token_poll([None, "DYN-TOKEN"])
        solver = DynamicGridSolver(vision=vision, token_poll=poll)

        token = await solver.run(frame, ChallengeContext())
        assert token == "DYN-TOKEN"

    asyncio.run(run())


def test_dynamic_grid_is_bounded_no_infinite_loop() -> None:
    async def run() -> None:
        frame = FakeFrame(
            counts={".rc-imageselect-tile, .task-image": 9},
            texts={".prompt-text": "select cars"},
        )
        # Vision always returns matches and token never appears; must still stop.
        vision = FakeVision(indices=[0])
        poll, _state = make_token_poll([])  # always None
        solver = DynamicGridSolver(vision=vision, token_poll=poll)

        token = await solver.run(frame, ChallengeContext())
        assert token is None
        # Bounded by MAX_ROUNDS: vision called at most that many times.
        assert len(vision.calls) <= DynamicGridSolver.MAX_ROUNDS

    asyncio.run(run())


def test_dynamic_grid_settles_when_no_matches() -> None:
    async def run() -> None:
        frame = FakeFrame(
            counts={".rc-imageselect-tile, .task-image": 9},
            texts={".prompt-text": "select cars"},
        )
        vision = FakeVision(indices=[])  # nothing to click -> settle immediately
        poll, _state = make_token_poll([None, "DONE"])
        solver = DynamicGridSolver(vision=vision, token_poll=poll)

        token = await solver.run(frame, ChallengeContext())
        assert token == "DONE"
        assert len(vision.calls) == 1
        # Submitted to finish the settled grid.
        assert any(c[0] == ".button-submit, .submit" for c in frame.clicks)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Dispatcher: single detect() + shape observer (no double classify)
# ---------------------------------------------------------------------------


def test_dispatcher_detects_once_and_reports_shape() -> None:
    """solve() classifies exactly once and hands the shape to on_detected."""
    async def run() -> None:
        class CountingClassifier(ChallengeClassifier):
            def __init__(self) -> None:
                super().__init__()
                self.calls = 0

            async def detect(self, frame: Any, ctx: ChallengeContext):
                self.calls += 1
                return ChallengeShape.GRID_SELECT

        class Stub:
            async def run(self, frame: Any, ctx: ChallengeContext) -> Optional[str]:
                return "TOK"

        clf = CountingClassifier()
        dispatcher = ChallengeDispatcher(clf, {})
        dispatcher.register(ChallengeShape.GRID_SELECT, Stub())

        seen: List[ChallengeShape] = []
        token = await dispatcher.solve(
            FakeFrame(), ChallengeContext(), on_detected=seen.append
        )
        assert token == "TOK"
        # Exactly one classify pass (the old detect()+solve() ran it twice).
        assert clf.calls == 1
        assert seen == [ChallengeShape.GRID_SELECT]

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Humanised pointer motion for slide / drag / area-bbox (P1-5 parity with grid)
# ---------------------------------------------------------------------------


class RecordingMouse:
    def __init__(self) -> None:
        self.moves: List[Any] = []
        self.downs = 0
        self.ups = 0

    async def move(self, x: float, y: float) -> None:
        self.moves.append((x, y))

    async def down(self) -> None:
        self.downs += 1

    async def up(self) -> None:
        self.ups += 1


class RecordingPage:
    def __init__(self) -> None:
        self.mouse = RecordingMouse()


def test_slide_uses_humanised_drag_when_page_present() -> None:
    """The slider drag presses, traces a multi-step path, and releases once."""
    async def run() -> None:
        frame = FakeFrame(counts={}, texts={})
        poll, _state = make_token_poll([None, "SLID"])
        solver = CanvasSlideSolver(vision=None, token_poll=poll)
        page = RecordingPage()
        ctx = ChallengeContext(
            extra={
                "page": page,
                "distance": 120.0,
                "handle_origin": (20.0, 200.0),
                "humanize": True,
                "humanize_jitter_ms": 0,
            }
        )
        token = await solver.run(frame, ctx)
        assert token == "SLID"
        # A real press-drag-release with travel (not one linear teleport).
        assert page.mouse.downs == 1 and page.mouse.ups == 1
        assert len(page.mouse.moves) > 5
        # The drag ends at the computed target (origin.x + distance).
        assert page.mouse.moves[-1] == (140.0, 200.0)

    asyncio.run(run())


def test_drag_drop_uses_humanised_drag_when_page_present() -> None:
    async def run() -> None:
        frame = FakeFrame(counts={}, texts={})
        poll, _state = make_token_poll([None, "DRAGGED"])
        solver = DragDropSolver(vision=None, token_poll=poll)
        page = RecordingPage()
        ctx = ChallengeContext(
            extra={
                "page": page,
                "source": (10.0, 10.0),
                "target": (100.0, 80.0),
                "humanize": True,
                "humanize_jitter_ms": 0,
            }
        )
        token = await solver.run(frame, ctx)
        assert token == "DRAGGED"
        assert page.mouse.downs >= 1 and page.mouse.ups >= 1
        assert len(page.mouse.moves) > 5

    asyncio.run(run())


def test_area_bbox_uses_human_pointer_click_when_page_present() -> None:
    async def run() -> None:
        frame = FakeFrame(counts={}, texts={})
        poll, _state = make_token_poll([None, "CLICKED"])
        solver = AreaBBoxSolver(vision=None, token_poll=poll)
        page = RecordingPage()
        ctx = ChallengeContext(
            extra={
                "page": page,
                "point": (55.0, 66.0),
                "humanize": True,
                "humanize_jitter_ms": 0,
            }
        )
        token = await solver.run(frame, ctx)
        assert token == "CLICKED"
        # Human path pressed the mouse (no teleport positional locator click).
        assert page.mouse.downs >= 1 and page.mouse.ups >= 1
        assert not any(
            c[0] == AreaBBoxSolver.IMAGE_SELECTOR for c in frame.clicks
        )

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Screenshot-pixel → CSS-pixel coordinate mapping (high-DPR correctness)
# ---------------------------------------------------------------------------


def test_png_pixel_size_reads_ihdr() -> None:
    assert BaseShapeSolver.png_pixel_size(_png_bytes(900, 600)) == (900, 600)


def test_png_pixel_size_none_for_non_png() -> None:
    assert BaseShapeSolver.png_pixel_size(b"not-a-png") is None
    assert BaseShapeSolver.png_pixel_size(b"") is None
    assert BaseShapeSolver.png_pixel_size("shot".encode()) is None


def test_screenshot_css_scale_ratio_and_identity_fallback() -> None:
    # CSS 300 wide, screenshot 900 wide => DPR 3 => scale 1/3.
    box = {"x": 0.0, "y": 0.0, "width": 300.0, "height": 300.0}
    sx, sy = BaseShapeSolver.screenshot_css_scale(box, _png_bytes(900, 900))
    assert round(sx, 4) == round(1 / 3, 4)
    assert round(sy, 4) == round(1 / 3, 4)
    # No box or non-PNG screenshot => identity (unchanged DPR-1 behaviour).
    assert BaseShapeSolver.screenshot_css_scale(None, _png_bytes(900, 900)) == (1.0, 1.0)
    assert BaseShapeSolver.screenshot_css_scale(box, b"not-png") == (1.0, 1.0)


def test_area_bbox_scales_high_dpr_point_and_adds_offset() -> None:
    """A 3× screenshot point maps to CSS space and gains the element offset.

    On a DPR-3 (mobile) context the screenshot is 900px for a 300px CSS image;
    the model's point (450,450) is the image centre in screenshot pixels, which
    must click the CSS centre at (150,150) relative to the image, i.e. absolute
    (50+150, 60+150) once the element's page offset is added.
    """
    async def run() -> None:
        sel = AreaBBoxSolver.IMAGE_SELECTOR
        frame = FakeFrame(
            counts={},
            texts={},
            boxes={sel: {"x": 50.0, "y": 60.0, "width": 300.0, "height": 300.0}},
            shots={sel: _png_bytes(900, 900)},
        )

        class PointVision:
            async def classify(self, req: Any):
                return SimpleNamespace(point=(450.0, 450.0), indices=[], confidence=0.9)

        poll, _state = make_token_poll([None, "CLICKED"])
        solver = AreaBBoxSolver(vision=PointVision(), token_poll=poll)
        page = RecordingPage()
        ctx = ChallengeContext(
            extra={"page": page, "humanize": True, "humanize_jitter_ms": 0}
        )
        token = await solver.run(frame, ctx)
        assert token == "CLICKED"
        # The area click lands at the scaled + offset absolute point. (A submit
        # click follows and moves the cursor again, so assert membership, not
        # the last move — ease_path forces the exact target so it's present.)
        assert (200.0, 210.0) in page.mouse.moves

    asyncio.run(run())


def test_slide_scales_distance_by_dpr() -> None:
    """The slide distance is divided by DPR so the handle travels CSS pixels."""
    async def run() -> None:
        bg_sel = CanvasSlideSolver.BG_SELECTOR
        frame = FakeFrame(
            counts={},
            texts={},
            boxes={bg_sel: {"x": 0.0, "y": 0.0, "width": 100.0, "height": 100.0}},
            shots={bg_sel: _png_bytes(300, 300)},  # DPR 3
        )

        class DistVision:
            async def classify(self, req: Any):
                # 180 screenshot px => 60 CSS px at DPR 3.
                return SimpleNamespace(distance=180.0, indices=[], confidence=0.9)

        poll, _state = make_token_poll([None, "SLID"])
        solver = CanvasSlideSolver(vision=DistVision(), token_poll=poll)
        page = RecordingPage()
        ctx = ChallengeContext(
            extra={
                "page": page,
                "handle_origin": (10.0, 50.0),
                "humanize": True,
                "humanize_jitter_ms": 0,
            }
        )
        token = await solver.run(frame, ctx)
        assert token == "SLID"
        # Handle ends at origin.x + scaled distance (10 + 60), same y.
        assert page.mouse.moves[-1] == (70.0, 50.0)

    asyncio.run(run())


def test_slide_falls_back_to_raw_move_without_page() -> None:
    """With no page (fake frame carries the mouse), the raw stepped path runs."""
    async def run() -> None:
        frame = FakeFrame(counts={}, texts={})
        frame.mouse = RecordingMouse()  # type: ignore[attr-defined]
        poll, _state = make_token_poll([None, "SLID"])
        solver = CanvasSlideSolver(vision=None, token_poll=poll)
        ctx = ChallengeContext(
            extra={
                "distance": 90.0,
                "handle_origin": (5.0, 100.0),
                "humanize": False,
            }
        )
        token = await solver.run(frame, ctx)
        assert token == "SLID"
        # Raw stepped path still presses + moves via the frame's mouse.
        assert frame.mouse.downs == 1 and frame.mouse.ups == 1
        assert len(frame.mouse.moves) > 5

    asyncio.run(run())
