"""Adapter bridging the dispatcher's ClassifyRequest to the VisionRouter.

The shape solvers (``src/parsing/shapes``) speak a small, decoupled request type
(:class:`~src.parsing.shapes.base.ClassifyRequest`) and only require an object
exposing ``async classify(req) -> result`` with ``result.indices`` and
``result.confidence``. The :class:`~src.parsing.vision.VisionRouter` speaks a
richer :class:`~src.parsing.vision.VisionRequest`. This adapter translates
between the two and pins the task tier / sitekey / client_key for a given solve.
"""

from __future__ import annotations

from typing import Any, Optional

from .vision import VisionRequest, VisionResult, VisionRouter


class VisionAdapter:
    """Wraps a VisionRouter so shape solvers can call ``classify(ClassifyRequest)``."""

    def __init__(
        self,
        router: VisionRouter,
        *,
        task_tier: int = 2,
        sitekey: Optional[str] = None,
        client_key: Optional[str] = None,
    ) -> None:
        self._router = router
        self._task_tier = task_tier
        self._sitekey = sitekey
        self._client_key = client_key
        # Running totals so the caller can attribute consumption to the ledger.
        self.total_vision_calls = 0
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.last_model: Optional[str] = None

    async def classify(self, req: Any) -> VisionResult:
        extra = getattr(req, "extra", {}) or {}
        vision_req = VisionRequest(
            prompt=getattr(req, "prompt", "") or "",
            images=list(getattr(req, "images", []) or []),
            task_tier=int(extra.get("task_tier", self._task_tier)),
            grid_size=extra.get("grid_size"),
            task_id=extra.get("task_id"),
            sitekey=self._sitekey,
        )
        result = await self._router.classify(vision_req, client_key=self._client_key)

        self.total_vision_calls += max(1, result.votes)
        self.total_input_tokens += result.usage.input_tokens
        self.total_output_tokens += result.usage.output_tokens
        self.last_model = result.model
        return result
