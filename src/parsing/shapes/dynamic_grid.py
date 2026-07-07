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

    # Dynamic reCAPTCHA tiles use a different selector than hCaptcha grids.
    TILE_SELECTOR = ".rc-imageselect-tile, .task-image"

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
            indices = self.valid_indices(
                getattr(result, "indices", []) if result is not None else [], count
            )
            log.info(
                "dynamic_grid round %d: selected=%s (of %d)",
                round_idx + 1,
                indices,
                count,
            )

            # No more matches -> the grid has settled; submit and finish.
            if not indices:
                await self.click_submit(frame)
                break

            for i in indices:
                await self.click_tile(frame, i)

            # Dynamic grids reveal replacements without a submit between clicks;
            # loop back to re-screenshot the reappearing tiles.

        # Final submit + token check after the loop settles.
        await self.click_submit(frame)
        return await self.poll_token()
