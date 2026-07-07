"""Vision routing for grid / image captcha classification.

``VisionRouter`` picks a model per (tier, budget), runs the call, and — when a
hard tier-2 grid comes back with low confidence — escalates to self-consistency
voting: it samples the model several times and majority-votes each tile in or
out, recomputing confidence as the agreement ratio.

This module is intentionally decoupled from the consumption layer.  ``ledger``
and ``budget`` are accepted as opaque, optional, duck-typed objects; every use
is guarded with ``is not None``.  Nothing here imports the ledger/budget modules.
"""

from __future__ import annotations

import base64
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Optional

from src.assets.model_pool import ModelPool, ModelUsage

SYSTEM_PROMPT = (
    "You are a strict CAPTCHA grid classifier. You are shown a challenge prompt "
    "and one or more images (tiles of a grid, indexed left-to-right, "
    "top-to-bottom starting at 0, or a single grid image). Decide which tile "
    "indices satisfy the prompt.\n"
    "Respond with STRICT JSON ONLY, no markdown, no prose:\n"
    '{"indices": [<int>, ...], "confidence": <float 0.0-1.0>}\n'
    "indices is the list of matching tile indices (may be empty). confidence is "
    "your certainty from 0.0 to 1.0."
)

# Temperature used for self-consistency voting samples (must be > 0 so the
# model produces varied samples to vote over).
_VOTE_TEMPERATURE = 0.8

_DEFAULT_CONFIDENCE = 0.5


@dataclass
class VisionRequest:
    prompt: str
    images: list  # list[bytes]: raw PNG bytes per tile OR a single grid image
    task_tier: int = 1  # 1 = bulk/cheap (local), 2 = hard grid (cloud)
    grid_size: "int | None" = None  # e.g. 9 for 3x3; None for single-image
    task_id: "str | None" = None
    sitekey: "str | None" = None
    detail: "str | None" = None  # override; else tier default


@dataclass
class VisionResult:
    indices: list
    confidence: float
    model: str
    usage: ModelUsage
    votes: int = 1
    raw: str = ""


class VisionRouter:
    """Chooses a model per (tier, budget), runs voting + a confidence gate."""

    def __init__(
        self,
        model_pool: ModelPool,
        config,
        *,
        ledger=None,
        budget=None,
    ) -> None:
        self._pool = model_pool
        self._config = config
        self._ledger = ledger
        self._budget = budget

    # -- routing ----------------------------------------------------------

    def _route(self, req: VisionRequest) -> str:
        if req.task_tier >= 2:
            if getattr(self._config, "vision_cloud_enabled", False):
                return "cloud"
            return "local"
        return "local"

    # -- public API -------------------------------------------------------

    async def classify(
        self, req: VisionRequest, *, client_key: "str | None" = None
    ) -> VisionResult:
        model_name = self._route(req)

        # Budget gate — only paid (cloud) calls are checked. A denial with a
        # suggested downgrade falls back to the cheaper local model.
        if self._budget is not None and model_name == "cloud":
            est_cost = self._estimate_cost(req)
            decision = await self._budget.check(
                client_key, est_cost, model=model_name
            )
            if decision is not None and not getattr(decision, "allowed", True):
                downgrade = getattr(decision, "downgrade_to", None)
                if downgrade:
                    model_name = downgrade

        client = self._pool.get(model_name)

        content, usage = await self._call(client, req, temperature=0.0)
        indices, confidence, raw = self._parse(content)
        total_usage = usage

        threshold = float(getattr(self._config, "vision_confidence_threshold", 0.0))
        samples = int(getattr(self._config, "vision_vote_samples", 1))
        cloud_enabled = bool(getattr(self._config, "vision_cloud_enabled", False))

        should_vote = (
            confidence < threshold
            and req.task_tier >= 2
            and cloud_enabled
            and samples > 1
        )

        votes = 1
        if should_vote:
            voted_indices, voted_conf, vote_usage, votes = await self._vote(
                client, req, samples
            )
            indices = voted_indices
            confidence = voted_conf
            total_usage = total_usage.add(vote_usage)

        return VisionResult(
            indices=indices,
            confidence=confidence,
            model=client.name,
            usage=total_usage,
            votes=votes,
            raw=raw,
        )

    # -- voting -----------------------------------------------------------

    async def _vote(self, client, req: VisionRequest, samples: int):
        counts: Counter = Counter()
        usage_total = ModelUsage()
        for _ in range(samples):
            content, usage = await self._call(
                client, req, temperature=_VOTE_TEMPERATURE
            )
            usage_total = usage_total.add(usage)
            sample_indices, _conf, _raw = self._parse(content)
            for idx in set(sample_indices):
                counts[idx] += 1

        # A tile is selected if a strict majority of samples chose it.
        selected = sorted(
            idx for idx, c in counts.items() if c * 2 > samples
        )

        if counts:
            # Agreement per candidate tile: how consistently samples agreed on
            # the in/out decision for that tile. Confidence = mean agreement.
            agreements = [
                max(c, samples - c) / float(samples) for c in counts.values()
            ]
            confidence = sum(agreements) / len(agreements)
        else:
            # Every sample agreed the grid is empty.
            confidence = 1.0

        return selected, confidence, usage_total, samples

    # -- model call -------------------------------------------------------

    async def _call(self, client, req: VisionRequest, *, temperature: float):
        messages = self._build_messages(req)
        timeout = float(getattr(self._config, "captcha_timeout", 30))
        return await client.chat(
            messages=messages,
            temperature=temperature,
            max_tokens=512,
            timeout=timeout,
        )

    def _build_messages(self, req: VisionRequest) -> list:
        detail = self._detail_for(req)
        user_content: list = [{"type": "text", "text": req.prompt}]
        for image in req.images:
            b64 = base64.b64encode(image).decode()
            user_content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/png;base64,{}".format(b64),
                        "detail": detail,
                    },
                }
            )
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]

    def _detail_for(self, req: VisionRequest) -> str:
        if req.detail is not None:
            return req.detail
        if req.task_tier >= 2:
            return str(getattr(self._config, "vision_tier2_detail", "high"))
        return "low"

    # -- cost estimation --------------------------------------------------

    @staticmethod
    def _estimate_cost(req: VisionRequest) -> float:
        # Crude positive estimate so a real BudgetGuard (which treats cost <= 0
        # as always allowed) actually evaluates the cap. Kept intentionally
        # simple to avoid coupling to the pricing/consumption layer.
        n_images = max(1, len(req.images))
        return 0.002 * n_images

    # -- parsing ----------------------------------------------------------

    @staticmethod
    def _parse(content: str):
        raw = content or ""
        text = raw.strip()

        fence = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
        if fence:
            text = fence.group(1).strip()

        indices: list = []
        confidence: Optional[float] = None

        try:
            data = json.loads(text)
        except Exception:
            data = None

        if isinstance(data, dict):
            idx = data.get("indices")
            if isinstance(idx, list):
                indices = _coerce_int_list(idx)
            conf = data.get("confidence")
            if isinstance(conf, (int, float)) and not isinstance(conf, bool):
                confidence = float(conf)
        else:
            # Regex fallbacks when the model didn't return clean JSON.
            idx_match = re.search(
                r"indices\"?\s*[:=]\s*\[([^\]]*)\]", text, re.IGNORECASE
            )
            if idx_match:
                indices = [int(n) for n in re.findall(r"-?\d+", idx_match.group(1))]
            else:
                bracket = re.search(r"\[([^\]]*)\]", text)
                if bracket:
                    indices = [int(n) for n in re.findall(r"-?\d+", bracket.group(1))]
            conf_match = re.search(
                r"confidence\"?\s*[:=]?\s*([0-9]*\.?[0-9]+)", text, re.IGNORECASE
            )
            if conf_match:
                confidence = float(conf_match.group(1))

        if confidence is None:
            confidence = _DEFAULT_CONFIDENCE
        # Clamp to [0, 1].
        if confidence < 0.0:
            confidence = 0.0
        elif confidence > 1.0:
            confidence = 1.0

        return indices, confidence, raw


def _coerce_int_list(values: list) -> list:
    out: list = []
    for v in values:
        if isinstance(v, bool):
            continue
        if isinstance(v, int):
            out.append(v)
        elif isinstance(v, float):
            out.append(int(v))
        elif isinstance(v, str):
            s = v.strip()
            if s.lstrip("-").isdigit():
                out.append(int(s))
    return out
