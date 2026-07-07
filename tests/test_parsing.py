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
)
from src.parsing.shapes.dynamic_grid import DynamicGridSolver
from src.parsing.shapes.grid_select import GridSelectSolver


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
        return f"shot:{self._selector}:{self._index}".encode()

    async def click(self, **kwargs: Any) -> None:
        self._frame.clicks.append((self._selector, self._index, kwargs))

    async def bounding_box(self) -> dict:
        return self._frame.boxes.get(
            self._selector, {"x": 10.0, "y": 20.0, "width": 40.0, "height": 40.0}
        )


class FakeFrame:
    """Duck-typed FrameLocator/Page that returns canned data and records calls."""

    def __init__(self, counts=None, texts=None, boxes=None):
        self.counts = counts or {}
        self.texts = texts or {}
        self.boxes = boxes or {}
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
