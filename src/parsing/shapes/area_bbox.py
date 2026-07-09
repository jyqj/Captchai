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
from .human_cursor import human_point_click

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

        x, y = point
        await self._click_at(frame, x, y, ctx)
        await self.human_click_submit(frame, ctx)
        return await self.poll_token()

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
        self, frame: Any, x: float, y: float, ctx: ChallengeContext
    ) -> bool:
        page = ctx.extra.get("page")
        humanize = ctx.extra.get("humanize", True)
        # 1. Human pointer path to the exact point (eased/jittered travel + a
        # click dwell + press duration) so the single-click challenge emits real
        # motionData instead of a teleport. Threads the cursor so a follow-up
        # submit click continues the same path.
        if page is not None and humanize:
            try:
                new_cursor = await human_point_click(
                    page,
                    (x, y),
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
                position={"x": x, "y": y}
            )
            return True
        except Exception as exc:  # noqa: BLE001
            log.debug("area_bbox positional click failed: %s", exc)

        # 3. Fall back to raw page mouse.
        try:
            target = page or frame
            await target.mouse.click(x, y)
            return True
        except Exception as exc:  # noqa: BLE001
            log.debug("area_bbox mouse click failed: %s", exc)
            return False
