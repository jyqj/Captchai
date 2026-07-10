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
from typing import Any, Awaitable, Callable, List, Optional, Tuple

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

    # -- coordinate mapping (screenshot pixels -> element CSS pixels) --------
    #
    # A Playwright element ``screenshot()`` is rasterised at the context's
    # ``device_scale_factor``. On a high-DPR context (the mobile / carrier
    # fingerprints used for enterprise hCaptcha set DPR 2.6–3.0) the PNG is
    # 2–3× larger than the element's CSS box. Coordinate shapes (area-select,
    # slide, drag) ask the vision model for a point / distance *in that
    # screenshot*, so the answer is in screenshot-pixel space and must be
    # scaled back to CSS pixels before it is applied to ``page.mouse`` /
    # ``locator.click(position=)`` (both of which consume CSS pixels). Without
    # this the click lands DPR× too far and misses — silently tanking exactly
    # the area-select challenges enterprise Radar leans on.

    @staticmethod
    def png_pixel_size(data: Any) -> Optional[Tuple[int, int]]:
        """``(width, height)`` from PNG bytes, or ``None`` if not a parseable PNG.

        Reads the IHDR dimensions straight from the header (no Pillow / decode)
        so it's cheap on the hot path. Playwright screenshots are PNG by
        default; anything else (or a fake test payload) returns ``None`` and the
        caller falls back to an identity (1.0) scale.
        """
        if not isinstance(data, (bytes, bytearray)):
            return None
        if len(data) < 24 or bytes(data[:8]) != b"\x89PNG\r\n\x1a\n":
            return None
        try:
            import struct

            width, height = struct.unpack(">II", bytes(data[16:24]))
        except Exception:  # noqa: BLE001
            return None
        if width > 0 and height > 0:
            return int(width), int(height)
        return None

    async def element_box(
        self, frame: Any, selector: str, index: Optional[int] = None
    ) -> Optional[dict]:
        """``bounding_box()`` (main-frame CSS coords) for an element, or ``None``.

        Guarded so a fake frame / detached node degrades to ``None`` (the caller
        then skips coordinate scaling and offset).
        """
        try:
            locator = frame.locator(selector)
            locator = locator.nth(index) if index is not None else locator.first
            box = await locator.bounding_box()
        except Exception:  # noqa: BLE001
            return None
        if isinstance(box, dict) and "width" in box and "height" in box:
            return box
        return None

    @staticmethod
    def screenshot_css_scale(box: Optional[dict], shot: Any) -> Tuple[float, float]:
        """Scale factors mapping screenshot-pixel coords → element CSS-pixel coords.

        Returns ``(css_w / png_w, css_h / png_h)`` so ``model_coord * scale`` is
        in the element's CSS space. Falls back to ``(1.0, 1.0)`` whenever the
        box or the screenshot dimensions can't be read (identity == the
        pre-fix behaviour, so DPR-1 desktop solves are unchanged).
        """
        if not box:
            return (1.0, 1.0)
        size = BaseShapeSolver.png_pixel_size(shot)
        if not size:
            return (1.0, 1.0)
        px_w, px_h = size
        css_w = float(box.get("width") or 0.0)
        css_h = float(box.get("height") or 0.0)
        sx = (css_w / px_w) if px_w > 0 and css_w > 0 else 1.0
        sy = (css_h / px_h) if px_h > 0 and css_h > 0 else 1.0
        return (sx, sy)

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
