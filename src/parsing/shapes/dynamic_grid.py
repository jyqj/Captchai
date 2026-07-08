"""Dynamic-grid solver (reCAPTCHA "keep clicking as new images fade in").

Like ``GridSelectSolver`` but after clicking, matching tiles are replaced with
new images, so we re-screenshot and re-classify in a loop. Terminates when a
token appears, when vision returns no matches, or after ``MAX_ROUNDS``.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from ..dispatcher import ChallengeContext, ChallengeShape
from .base import BaseShapeSolver, ClassifyRequest

log = logging.getLogger(__name__)


class DynamicGridSolver(BaseShapeSolver):
    SHAPE = ChallengeShape.RECAPTCHA_DYNAMIC

    TILE_SELECTOR = ".rc-imageselect-tile, .task-image"
    REFRESH_SELECTOR = ".rc-button-reload, .reload-button-holder button, [id='recaptcha-reload-button']"
    CONFIDENCE_FLOOR = 0.4

    async def run(self, frame: Any, ctx: ChallengeContext) -> Optional[str]:
        prompt = await self.read_prompt(frame) or ctx.prompt

        for round_idx in range(self.MAX_ROUNDS):
            token = await self.poll_token()
            if token:
                return token

            count = await self.count_tiles(frame)
            if count == 0:
                log.info("dynamic_grid: no tiles on round %d", round_idx + 1)
                break

            shots = await self.screenshot_tiles(frame, count)
            result = await self.classify(
                ClassifyRequest(
                    prompt=prompt,
                    images=shots,
                    shape=self.SHAPE.value,
                    extra={"task_id": ctx.task_id, "round": round_idx, "dynamic": True},
                )
            )

            confidence = getattr(result, "confidence", 1.0) if result is not None else 0.0
            indices = self.valid_indices(
                getattr(result, "indices", []) if result is not None else [], count
            )
            log.info(
                "dynamic_grid round %d: selected=%s (of %d) confidence=%.2f",
                round_idx + 1,
                indices,
                count,
                confidence,
            )

            if confidence < self.CONFIDENCE_FLOOR and round_idx < self.MAX_ROUNDS - 1:
                log.info(
                    "dynamic_grid: confidence %.2f below floor %.2f, attempting reload",
                    confidence, self.CONFIDENCE_FLOOR,
                )
                reloaded = await self._try_reload(frame)
                if reloaded:
                    continue

            if not indices:
                await self.click_submit(frame)
                break

            for i in indices:
                await self.click_tile(frame, i)

        await self.click_submit(frame)
        return await self.poll_token()

    async def _try_reload(self, frame: Any) -> bool:
        """Click the challenge reload button if available. Returns True on success."""
        try:
            locator = frame.locator(self.REFRESH_SELECTOR).first
            if await locator.count() > 0:
                await locator.click(timeout=3_000)
                return True
        except Exception as exc:  # noqa: BLE001
            log.debug("dynamic_grid: reload click failed: %s", exc)
        return False
