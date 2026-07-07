"""Drag-and-drop solver (drag a puzzle piece onto its target slot).

Uses ``page.mouse`` primitives (move -> down -> stepped moves -> up) to trace a
slightly curved, multi-step path so the motion isn't a single teleport. Source
and target coordinates come from vision (``result.source``/``result.target`` or
paired ``result.indices``) or from ``ctx.extra``.
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional, Tuple

from ..dispatcher import ChallengeContext, ChallengeShape
from .base import BaseShapeSolver, ClassifyRequest

log = logging.getLogger(__name__)


class DragDropSolver(BaseShapeSolver):
    SHAPE = ChallengeShape.DRAG_DROP

    STEPS = 25

    async def run(self, frame: Any, ctx: ChallengeContext) -> Optional[str]:
        token = await self.poll_token()
        if token:
            return token

        prompt = await self.read_prompt(frame) or ctx.prompt
        result = await self.classify(
            ClassifyRequest(
                prompt=prompt,
                images=[],
                shape=self.SHAPE.value,
                extra={"task_id": ctx.task_id, "want": "drag_path"},
            )
        )

        endpoints = self._extract_endpoints(result, ctx)
        if endpoints is None:
            log.info("drag_drop: no source/target coordinates available")
            return await self.poll_token()

        (sx, sy), (tx, ty) = endpoints
        await self._drag(frame, sx, sy, tx, ty)
        await self.click_submit(frame)
        return await self.poll_token()

    def _extract_endpoints(
        self, result: Any, ctx: ChallengeContext
    ) -> Optional[Tuple[Tuple[float, float], Tuple[float, float]]]:
        src = getattr(result, "source", None) if result is not None else None
        dst = getattr(result, "target", None) if result is not None else None
        if self._is_point(src) and self._is_point(dst):
            return (float(src[0]), float(src[1])), (float(dst[0]), float(dst[1]))

        idx = getattr(result, "indices", None) if result is not None else None
        if isinstance(idx, (list, tuple)) and len(idx) >= 4:
            return (float(idx[0]), float(idx[1])), (float(idx[2]), float(idx[3]))

        src = ctx.extra.get("source")
        dst = ctx.extra.get("target")
        if self._is_point(src) and self._is_point(dst):
            return (float(src[0]), float(src[1])), (float(dst[0]), float(dst[1]))

        return None

    @staticmethod
    def _is_point(value: Any) -> bool:
        return isinstance(value, (list, tuple)) and len(value) >= 2

    def _path(
        self, sx: float, sy: float, tx: float, ty: float
    ) -> List[Tuple[float, float]]:
        """A gently arced, eased path from source to target."""
        points: List[Tuple[float, float]] = []
        for step in range(1, self.STEPS + 1):
            t = step / self.STEPS
            eased = t * t * (3 - 2 * t)  # smoothstep
            x = sx + (tx - sx) * eased
            y = sy + (ty - sy) * eased
            # small vertical arc that peaks mid-drag
            arc = -12.0 * (eased - eased * eased)
            points.append((x, y + arc))
        return points

    async def _drag(
        self, frame: Any, sx: float, sy: float, tx: float, ty: float
    ) -> bool:
        mouse = getattr(frame, "mouse", None)
        if mouse is None:
            log.debug("drag_drop: frame exposes no mouse")
            return False
        try:
            await mouse.move(sx, sy)
            await mouse.down()
            for x, y in self._path(sx, sy, tx, ty):
                await mouse.move(x, y)
            await mouse.up()
            return True
        except Exception as exc:  # noqa: BLE001
            log.debug("drag_drop drag failed: %s", exc)
            return False
