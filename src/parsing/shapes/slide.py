"""Canvas slide solver (slider-puzzle / "drag the piece into the gap").

Computes a horizontal drag distance (from vision or ``ctx.extra['distance']``)
and performs a human-like eased drag of the slider handle using ``page.mouse``.
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional, Tuple

from ..dispatcher import ChallengeContext, ChallengeShape
from .base import BaseShapeSolver, ClassifyRequest
from .human_cursor import human_drag

log = logging.getLogger(__name__)


class CanvasSlideSolver(BaseShapeSolver):
    SHAPE = ChallengeShape.CANVAS_SLIDE

    HANDLE_SELECTOR = ".slide-handle, .slider, .geetest_slider_button"
    BG_SELECTOR = "canvas.slideBg, .geetest_canvas_bg, .slider-bg, .puzzle-image, .captcha-image"
    STEPS = 30

    async def run(self, frame: Any, ctx: ChallengeContext) -> Optional[str]:
        token = await self.poll_token()
        if token:
            return token

        prompt = await self.read_prompt(frame) or ctx.prompt
        shot = await self.screenshot_element(frame, self.BG_SELECTOR)
        images = [shot] if shot else []
        result = await self.classify(
            ClassifyRequest(
                prompt=prompt,
                images=images,
                shape=self.SHAPE.value,
                extra={"task_id": ctx.task_id, "want": "slide_distance"},
            )
        )

        start = await self._handle_origin(frame, ctx)
        distance = self._extract_distance(result, ctx)
        if distance is None or start is None:
            log.info("canvas_slide: missing distance or handle origin")
            return await self.poll_token()

        sx, sy = start
        await self._slide(frame, sx, sy, distance, ctx)
        return await self.poll_token()

    async def _handle_origin(
        self, frame: Any, ctx: ChallengeContext
    ) -> Optional[Tuple[float, float]]:
        origin = ctx.extra.get("handle_origin")
        if isinstance(origin, (list, tuple)) and len(origin) >= 2:
            return float(origin[0]), float(origin[1])
        try:
            box = await frame.locator(self.HANDLE_SELECTOR).first.bounding_box()
        except Exception as exc:  # noqa: BLE001
            log.debug("canvas_slide: bounding_box failed: %s", exc)
            box = None
        if isinstance(box, dict) and "x" in box and "y" in box:
            cx = float(box["x"]) + float(box.get("width", 0)) / 2
            cy = float(box["y"]) + float(box.get("height", 0)) / 2
            return cx, cy
        return None

    def _extract_distance(self, result: Any, ctx: ChallengeContext) -> Optional[float]:
        dist = getattr(result, "distance", None) if result is not None else None
        if dist is not None:
            return float(dist)
        idx = getattr(result, "indices", None) if result is not None else None
        if isinstance(idx, (list, tuple)) and len(idx) >= 1:
            return float(idx[0])
        cdist = ctx.extra.get("distance")
        if cdist is not None:
            return float(cdist)
        return None

    def _steps(self, distance: float) -> List[float]:
        """Eased per-step x offsets that sum to ``distance``."""
        offsets: List[float] = []
        prev = 0.0
        for step in range(1, self.STEPS + 1):
            t = step / self.STEPS
            eased = t * t * (3 - 2 * t)  # smoothstep
            cur = distance * eased
            offsets.append(cur - prev)
            prev = cur
        return offsets

    async def _slide(
        self, frame: Any, sx: float, sy: float, distance: float, ctx: ChallengeContext
    ) -> bool:
        page = ctx.extra.get("page")
        humanize = ctx.extra.get("humanize", True)
        jitter_ms = float(ctx.extra.get("humanize_jitter_ms", 90.0))
        end = (sx + distance, sy)
        # Prefer the humanised drag (grab dwell + eased/jittered path + settle
        # before release) so the slider's scored pointer dynamics look human,
        # exactly like the tile clicks already do. Falls back to the raw stepped
        # move when humanisation is disabled or the page has no press-capable
        # mouse (tests / odd frames).
        owner = page if getattr(page, "mouse", None) is not None else frame
        if humanize and await human_drag(owner, (sx, sy), end, jitter_ms=jitter_ms):
            return True
        mouse = getattr(owner, "mouse", None)
        if mouse is None:
            log.debug("canvas_slide: no mouse available on page or frame")
            return False
        try:
            await mouse.move(sx, sy)
            await mouse.down()
            x = sx
            for dx in self._steps(distance):
                x += dx
                await mouse.move(x, sy)
            await mouse.up()
            return True
        except Exception as exc:  # noqa: BLE001
            log.debug("canvas_slide slide failed: %s", exc)
            return False
