"""Area / bounding-box solver ("click the center of the object").

A single image is presented and the solver must click one point. The injected
vision client returns a click point. We accept it either as explicit coords on
the result (``result.point``/``result.x``/``result.y``), or as the first two
``result.indices`` reused as (x, y) pixel coordinates, or from
``ctx.extra['point']``. The click is performed via ``frame.click`` at a
position, falling back to ``page.mouse``.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Tuple

from ..dispatcher import ChallengeContext, ChallengeShape
from .base import BaseShapeSolver, ClassifyRequest
from .human_cursor import human_point_click, human_tap

log = logging.getLogger(__name__)


class AreaBBoxSolver(BaseShapeSolver):
    SHAPE = ChallengeShape.AREA_BBOX

    IMAGE_SELECTOR = ".single-image, .task-image, canvas"

    async def run(self, frame: Any, ctx: ChallengeContext) -> Optional[str]:
        token = await self.poll_token()
        if token:
            return token

        prompt = await self.read_prompt(frame) or ctx.prompt

        shot = await self._screenshot_tile(frame, self.IMAGE_SELECTOR, 0)
        # Element CSS box (page-viewport coords) + screenshot→CSS scale so the
        # model's screenshot-pixel point can be mapped back to CSS pixels. On a
        # high-DPR (mobile) context the screenshot is 2–3× the element's CSS
        # size, so an unscaled point lands DPR× too far and misses.
        box = await self.element_box(frame, self.IMAGE_SELECTOR, 0)
        result = await self.classify(
            ClassifyRequest(
                prompt=prompt,
                images=[shot] if shot else [],
                shape=self.SHAPE.value,
                extra={"task_id": ctx.task_id, "want": "coordinate"},
            )
        )

        point = self._extract_point(result, ctx)
        if point is None:
            log.info("area_bbox: no click point available")
            return await self.poll_token()

        rel_x, rel_y = self._to_css(point, box, shot)
        await self._click_at(frame, rel_x, rel_y, box, ctx)
        await self.human_click_submit(frame, ctx)
        return await self.poll_token()

    def _to_css(
        self, point: Tuple[float, float], box: Optional[dict], shot: Any
    ) -> Tuple[float, float]:
        """Map a screenshot-pixel model point to element-relative CSS pixels."""
        sx, sy = self.screenshot_css_scale(box, shot)
        return point[0] * sx, point[1] * sy

    def _extract_point(
        self, result: Any, ctx: ChallengeContext
    ) -> Optional[Tuple[float, float]]:
        # 1. explicit point attribute
        pt = getattr(result, "point", None) if result is not None else None
        if isinstance(pt, (list, tuple)) and len(pt) >= 2:
            return float(pt[0]), float(pt[1])

        # 2. explicit x/y attributes
        if result is not None:
            x = getattr(result, "x", None)
            y = getattr(result, "y", None)
            if x is not None and y is not None:
                return float(x), float(y)

        # 3. first two indices reused as coords
        idx = getattr(result, "indices", None) if result is not None else None
        if isinstance(idx, (list, tuple)) and len(idx) >= 2:
            return float(idx[0]), float(idx[1])

        # 4. context-supplied fallback
        cpt = ctx.extra.get("point")
        if isinstance(cpt, (list, tuple)) and len(cpt) >= 2:
            return float(cpt[0]), float(cpt[1])

        return None

    async def _click_at(
        self,
        frame: Any,
        rel_x: float,
        rel_y: float,
        box: Optional[dict],
        ctx: ChallengeContext,
    ) -> bool:
        """Click the point the model chose.

        ``rel_x`` / ``rel_y`` are CSS pixels *relative to the image element's
        top-left* (already scaled out of screenshot-pixel space by the caller).
        ``page.mouse`` consumes absolute viewport coords, so the element's
        page offset (``box.x`` / ``box.y``) is added for the human / raw-mouse
        paths; ``locator.click(position=)`` already takes element-relative
        coords. Without adding the offset the human path clicked the image's
        coordinate as if it were a page coordinate — landing near the page
        origin, not on the object.
        """
        page = ctx.extra.get("page")
        humanize = ctx.extra.get("humanize", True)
        touch = ctx.extra.get("touch", False)
        off_x = float(box["x"]) if box and "x" in box else 0.0
        off_y = float(box["y"]) if box and "y" in box else 0.0
        abs_x, abs_y = off_x + rel_x, off_y + rel_y
        # 1. Human pointer path to the exact point (eased/jittered travel + a
        # click dwell + press duration) so the single-click challenge emits real
        # motionData instead of a teleport. Threads the cursor so a follow-up
        # submit click continues the same path. On a mobile context a trusted
        # touch tap is used instead (mouse events would contradict the phone
        # fingerprint hCaptcha mobile scores).
        if page is not None and humanize:
            try:
                if touch:
                    tapped = await human_tap(page, (abs_x, abs_y))
                    if tapped is not None:
                        ctx.extra["_cursor"] = tapped
                        return True
                new_cursor = await human_point_click(
                    page,
                    (abs_x, abs_y),
                    cursor=ctx.extra.get("_cursor"),
                    jitter_ms=float(ctx.extra.get("humanize_jitter_ms", 90.0)),
                )
                if new_cursor is not None:
                    ctx.extra["_cursor"] = new_cursor
                    return True
            except Exception as exc:  # noqa: BLE001 - fall through to plain click
                log.debug("area_bbox human click failed: %s", exc)

        # 2. Click the image element at a relative position.
        try:
            await frame.locator(self.IMAGE_SELECTOR).first.click(
                position={"x": rel_x, "y": rel_y}
            )
            return True
        except Exception as exc:  # noqa: BLE001
            log.debug("area_bbox positional click failed: %s", exc)

        # 3. Fall back to raw page mouse (absolute viewport coords).
        try:
            target = page or frame
            await target.mouse.click(abs_x, abs_y)
            return True
        except Exception as exc:  # noqa: BLE001
            log.debug("area_bbox mouse click failed: %s", exc)
            return False
