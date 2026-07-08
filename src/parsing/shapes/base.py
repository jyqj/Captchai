"""Shared machinery for shape solvers.

``BaseShapeSolver`` holds the small, guarded, awaitable DOM helpers every shape
solver needs (counting tiles, screenshotting tiles in parallel, reading the
prompt, clicking, polling for a token). All Playwright interaction is duck-typed
against a ``FrameLocator``/``Page`` so tests can inject fakes and run with no
browser.

The vision object and an optional ``token_poll`` callable are injected via the
constructor. Solvers never construct a vision client themselves.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, List, Optional

from ..dispatcher import ChallengeContext, VisionClient
from .human_cursor import human_click

log = logging.getLogger(__name__)

# A token poll returns the solved token or None when not ready yet.
TokenPoll = Callable[[], Awaitable[Optional[str]]]


@dataclass
class ClassifyRequest:
    """The request object handed to ``vision.classify``.

    This is the shape the injected vision client must understand. The integrator
    adapts their VisionRouter to accept this (or a superset) and to return a
    result with ``.indices: list[int]`` and ``.confidence: float``.
    """

    prompt: str = ""
    images: List[bytes] = field(default_factory=list)
    shape: str = ""
    # Free-form hints (e.g. coordinate request, grid dimensions, task_id).
    extra: dict = field(default_factory=dict)


class BaseShapeSolver:
    """Base class with shared, guarded DOM helpers.

    Subclasses implement ``async run(self, frame, ctx) -> Optional[str]``.
    """

    # Default selectors; subclasses may override.
    TILE_SELECTOR = ".task-image"
    SUBMIT_SELECTOR = ".button-submit, .submit"
    PROMPT_SELECTORS = (".prompt-text", ".challenge-prompt", "h2.prompt-text")

    # Bound on multi-round loops so a misbehaving challenge can't spin forever.
    MAX_ROUNDS = 6

    def __init__(
        self,
        vision: Optional[VisionClient] = None,
        token_poll: Optional[TokenPoll] = None,
    ) -> None:
        self._vision = vision
        self._token_getter: Optional[TokenPoll] = token_poll

    # -- token -------------------------------------------------------------

    async def poll_token(self) -> Optional[str]:
        """Invoke the injected token poll, if any. Guarded."""
        if self._token_getter is None:
            return None
        try:
            token = await self._token_getter()
        except Exception as exc:  # noqa: BLE001
            log.debug("token_poll raised: %s", exc)
            return None
        if isinstance(token, str) and token:
            return token
        return None

    # -- reading -----------------------------------------------------------

    async def count_tiles(self, frame: Any, selector: Optional[str] = None) -> int:
        sel = selector or self.TILE_SELECTOR
        try:
            return int(await frame.locator(sel).count())
        except Exception:  # noqa: BLE001
            return 0

    async def read_prompt(self, frame: Any) -> str:
        for sel in self.PROMPT_SELECTORS:
            try:
                text = await frame.locator(sel).first.inner_text()
                if text:
                    return str(text).strip()
            except Exception:  # noqa: BLE001
                continue
        return ""

    async def _screenshot_tile(self, frame: Any, selector: str, index: int) -> bytes:
        try:
            return await frame.locator(selector).nth(index).screenshot()
        except Exception:  # noqa: BLE001
            return b""

    async def screenshot_element(self, frame: Any, selector: str) -> bytes:
        """Screenshot the first element matching *selector*; empty bytes on failure."""
        try:
            return await frame.locator(selector).first.screenshot()
        except Exception:  # noqa: BLE001
            return b""

    async def screenshot_tiles(
        self, frame: Any, count: int, selector: Optional[str] = None
    ) -> List[bytes]:
        """Screenshot every tile in parallel (perf win over a sequential loop)."""
        sel = selector or self.TILE_SELECTOR
        if count <= 0:
            return []
        shots = await asyncio.gather(
            *(self._screenshot_tile(frame, sel, i) for i in range(count))
        )
        return list(shots)

    # -- acting ------------------------------------------------------------

    async def click_tile(
        self, frame: Any, index: int, selector: Optional[str] = None
    ) -> bool:
        sel = selector or self.TILE_SELECTOR
        try:
            await frame.locator(sel).nth(index).click()
            return True
        except Exception as exc:  # noqa: BLE001
            log.debug("click tile %d failed: %s", index, exc)
            return False

    async def click_submit(self, frame: Any, selector: Optional[str] = None) -> bool:
        sel = selector or self.SUBMIT_SELECTOR
        try:
            await frame.locator(sel).first.click()
            return True
        except Exception as exc:  # noqa: BLE001
            log.debug("submit click failed: %s", exc)
            return False

    # -- human-like acting -------------------------------------------------

    async def human_click_tile(
        self, frame: Any, index: int, ctx: ChallengeContext, selector: Optional[str] = None
    ) -> bool:
        """Click a tile with a human-like pointer path when a page is available.

        hCaptcha scores mouse dynamics, so tiles are clicked by moving
        ``page.mouse`` along an eased/jittered path (see :mod:`human_cursor`)
        to a randomised point inside the tile, with a click dwell. When no page
        is threaded on ``ctx.extra`` (unit tests) or humanisation is disabled,
        or the geometry can't be read, this degrades to the plain
        :meth:`click_tile`.
        """
        return await self._human_click_locator(
            frame, selector or self.TILE_SELECTOR, ctx, index=index
        )

    async def human_click_submit(
        self, frame: Any, ctx: ChallengeContext, selector: Optional[str] = None
    ) -> bool:
        """Human-like submit click; degrades to :meth:`click_submit`."""
        return await self._human_click_locator(
            frame, selector or self.SUBMIT_SELECTOR, ctx, index=None
        )

    async def _human_click_locator(
        self, frame: Any, selector: str, ctx: ChallengeContext, *, index: Optional[int]
    ) -> bool:
        page = ctx.extra.get("page")
        humanize = ctx.extra.get("humanize", True)
        if page is not None and humanize:
            try:
                locator = frame.locator(selector)
                locator = locator.nth(index) if index is not None else locator.first
                box = await locator.bounding_box()
                if box:
                    new_cursor = await human_click(
                        page,
                        box,
                        cursor=ctx.extra.get("_cursor"),
                        jitter_ms=float(ctx.extra.get("humanize_jitter_ms", 90.0)),
                    )
                    if new_cursor is not None:
                        ctx.extra["_cursor"] = new_cursor
                        return True
            except Exception as exc:  # noqa: BLE001 - fall back to plain click
                log.debug("human click on %s failed: %s", selector, exc)
        if index is not None:
            return await self.click_tile(frame, index, selector)
        return await self.click_submit(frame, selector)

    async def classify(self, req: ClassifyRequest) -> Optional[Any]:
        """Call the injected vision client. Returns the raw result or None."""
        if self._vision is None:
            return None
        try:
            return await self._vision.classify(req)
        except Exception as exc:  # noqa: BLE001
            log.warning("vision.classify failed: %s", exc)
            return None

    @staticmethod
    def valid_indices(indices: Any, count: int) -> List[int]:
        """Coerce and clamp vision indices to the [0, count) range, de-duped."""
        out: List[int] = []
        seen = set()
        try:
            iterator = list(indices)
        except TypeError:
            return out
        for raw in iterator:
            try:
                i = int(raw)
            except (TypeError, ValueError):
                continue
            if 0 <= i < count and i not in seen:
                seen.add(i)
                out.append(i)
        return out
