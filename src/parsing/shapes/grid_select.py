"""Grid-select solver (the classic "click every image with a bus" challenge).

Choreography per round:

  1. read the prompt,
  2. count ``.task-image`` tiles,
  3. screenshot every tile **in parallel** (perf improvement over the old
     sequential loop in ``services/hcaptcha.py``),
  4. ask the injected vision client which indices to click,
  5. click those tiles, then click submit,
  6. poll for a token.

Bounded multi-round loop so multi-image challenges resolve without spinning
forever.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from ..dispatcher import ChallengeContext, ChallengeShape
from .base import BaseShapeSolver, ClassifyRequest

log = logging.getLogger(__name__)


class GridSelectSolver(BaseShapeSolver):
    SHAPE = ChallengeShape.GRID_SELECT
    REFRESH_SELECTOR = ".refresh, .button-refresh, [aria-label='Get a new challenge']"
    CONFIDENCE_FLOOR = 0.4

    async def run(self, frame: Any, ctx: ChallengeContext) -> Optional[str]:
        for round_idx in range(self.MAX_ROUNDS):
            token = await self.poll_token()
            if token:
                return token

            prompt = await self.read_prompt(frame) or ctx.prompt
            count = await self.count_tiles(frame)
            if count == 0:
                log.info("grid_select: no tiles on round %d", round_idx + 1)
                return await self.poll_token()

            shots = await self.screenshot_tiles(frame, count)
            result = await self.classify(
                ClassifyRequest(
                    prompt=prompt,
                    images=shots,
                    shape=self.SHAPE.value,
                    extra={"task_id": ctx.task_id, "round": round_idx},
                )
            )

            confidence = getattr(result, "confidence", 1.0) if result is not None else 0.0
            indices = self.valid_indices(
                getattr(result, "indices", []) if result is not None else [], count
            )
            log.info(
                "grid_select round %d: prompt=%r selected=%s confidence=%.2f",
                round_idx + 1, prompt, indices, confidence,
            )

            if confidence < self.CONFIDENCE_FLOOR and round_idx < self.MAX_ROUNDS - 1:
                log.info(
                    "grid_select: confidence %.2f below floor %.2f, attempting reload",
                    confidence, self.CONFIDENCE_FLOOR,
                )
                reloaded = await self._try_reload(frame)
                if reloaded:
                    continue

            for i in indices:
                await self.click_tile(frame, i)

            await self.click_submit(frame)

            token = await self.poll_token()
            if token:
                return token

            if not indices:
                break

        return await self.poll_token()

    async def _try_reload(self, frame: Any) -> bool:
        """Click the challenge refresh button if available. Returns True on success."""
        try:
            locator = frame.locator(self.REFRESH_SELECTOR).first
            if await locator.count() > 0:
                await locator.click(timeout=3_000)
                return True
        except Exception as exc:  # noqa: BLE001
            log.debug("grid_select: reload click failed: %s", exc)
        return False
