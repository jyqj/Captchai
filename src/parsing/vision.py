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

import asyncio
import base64
import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from typing import Any, Optional

from src.assets.model_pool import ModelPool, ModelUsage
from src.parsing.image_grid import compose_grid

log = logging.getLogger(__name__)

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

COORDINATE_PROMPT = (
    "You are a strict CAPTCHA image analyzer. You are shown a challenge prompt "
    "and a single image. Identify the pixel coordinates (x, y) of the point "
    "that satisfies the prompt.\n"
    "Respond with STRICT JSON ONLY, no markdown, no prose:\n"
    '{"x": <int>, "y": <int>, "confidence": <float 0.0-1.0>}\n'
    "x and y are pixel coordinates relative to the image's top-left corner."
)

SLIDE_DISTANCE_PROMPT = (
    "You are a strict CAPTCHA slide-puzzle analyzer. You are shown a slider "
    "puzzle image. Determine the horizontal pixel distance the slider piece "
    "must travel to fill the gap.\n"
    "Respond with STRICT JSON ONLY, no markdown, no prose:\n"
    '{"distance": <int>, "confidence": <float 0.0-1.0>}\n'
    "distance is in pixels from the handle's current position."
)

DRAG_PATH_PROMPT = (
    "You are a strict CAPTCHA drag-and-drop analyzer. You are shown a puzzle "
    "image where a piece must be dragged to a target slot. Identify the pixel "
    "coordinates of the source (piece center) and target (slot center).\n"
    "Respond with STRICT JSON ONLY, no markdown, no prose:\n"
    '{"source": [<int>, <int>], "target": [<int>, <int>], "confidence": <float 0.0-1.0>}\n'
    "Coordinates are pixels relative to the image's top-left corner."
)

_SHAPE_PROMPTS = {
    "grid_select": SYSTEM_PROMPT,
    "recaptcha_dynamic": SYSTEM_PROMPT,
    "area_bbox": COORDINATE_PROMPT,
    "canvas_slide": SLIDE_DISTANCE_PROMPT,
    "drag_drop": DRAG_PATH_PROMPT,
}

# Shapes whose images are independent tiles of one grid — the only ones where a
# single-image montage preserves the classifier's index contract. Coordinate
# shapes (area_bbox / slide / drag) already send one image, so stitching is a
# no-op for them and must never apply (it would corrupt pixel coordinates).
_STITCHABLE_SHAPES = {"grid_select", "recaptcha_dynamic"}

# Temperature used for self-consistency voting samples (must be > 0 so the
# model produces varied samples to vote over).
_VOTE_TEMPERATURE = 0.8

_DEFAULT_CONFIDENCE = 0.5

# Exception class names that indicate a *connection / availability* failure
# (backend down, refused, timed out) rather than a bad request. Matched by
# class name across the MRO so we stay dependency-light (no hard import of
# openai / httpx) — covers openai.APIConnectionError / APITimeoutError, httpx
# ConnectError / ConnectTimeout / ReadTimeout / PoolTimeout, and the builtin
# ConnectionError / TimeoutError.
_CONNECTION_ERROR_NAMES = frozenset(
    {
        "APIConnectionError",
        "APITimeoutError",
        "InternalServerError",
        "ConnectionError",
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "ReadError",
        "PoolTimeout",
        "TimeoutError",
        "ConnectionRefusedError",
        "ConnectionResetError",
    }
)


def _is_connection_error(exc: BaseException) -> bool:
    """True when ``exc`` (or any base) is a connection/availability failure."""
    for klass in type(exc).__mro__:
        if klass.__name__ in _CONNECTION_ERROR_NAMES:
            return True
    return False


@dataclass
class VisionRequest:
    prompt: str
    images: list  # list[bytes]: raw PNG bytes per tile OR a single grid image
    task_tier: int = 1  # 1 = bulk/cheap (local), 2 = hard grid (cloud)
    grid_size: "int | None" = None  # e.g. 9 for 3x3; None for single-image
    task_id: "str | None" = None
    sitekey: "str | None" = None
    detail: "str | None" = None  # override; else tier default
    shape: str = "grid_select"


@dataclass
class VisionResult:
    indices: list
    confidence: float
    model: str
    usage: ModelUsage
    votes: int = 1
    raw: str = ""
    point: "tuple[float, float] | None" = None
    distance: "float | None" = None
    source: "tuple[float, float] | None" = None
    target: "tuple[float, float] | None" = None


class VisionRouter:
    """Chooses a model per (tier, budget), runs voting + a confidence gate."""

    def __init__(
        self,
        model_pool: ModelPool,
        config,
        *,
        ledger=None,
        budget=None,
        accounting=None,
    ) -> None:
        self._pool = model_pool
        self._config = config
        self._ledger = ledger
        self._budget = budget
        self._accounting = accounting

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
        # Collapse a multi-tile grid into a single montage before routing so the
        # cost estimate, the single call, and every vote sample all operate on
        # one image instead of N. Transparent to the index contract (tiles keep
        # their left-to-right / top-to-bottom order) and reversible via config.
        req = await self._maybe_stitch(req)
        model_name = self._route(req)

        # Encode each image's base64 ONCE and reuse it across the initial call,
        # every vote sample, and any inline cloud escalation — instead of
        # re-encoding the same (potentially large) PNG on every _build_messages
        # call. The data-url ``detail`` (which does vary by tier) is applied
        # cheaply per call around this cached payload.
        image_b64 = [base64.b64encode(img).decode() for img in req.images]

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

        client, content, usage = await self._call_with_backend_fallback(
            model_name, req, client_key, image_b64
        )
        indices, confidence, raw = self._parse(content)
        total_usage = usage

        threshold = float(getattr(self._config, "vision_confidence_threshold", 0.0))
        samples = int(getattr(self._config, "vision_vote_samples", 1))
        cloud_enabled = bool(getattr(self._config, "vision_cloud_enabled", False))
        # getattr default False mirrors ``vision_cloud_enabled``: the Config /
        # env default is True, but pre-existing test configs that don't set
        # this field keep the old tier-1-no-escalation behavior.
        inline_escalate = bool(
            getattr(self._config, "vision_inline_escalate", False)
        )

        votes = 1

        # WP4: inline tier-1 → tier-2 escalation. A low-confidence local result
        # gets a single cloud retry (optionally followed by cloud voting) before
        # the solver falls back to a full browser redo. The cloud call goes
        # through the same budget gate as the tier-2 path; a denial downgrades
        # back to the local result (no escalation). Transparent to shape
        # solvers — they still read result.indices / result.confidence.
        if (
            req.task_tier < 2
            and confidence < threshold
            and cloud_enabled
            and inline_escalate
            and await self._budget_allows_cloud(req, client_key)
        ):
            cloud_client = self._pool.get("cloud")
            # Re-route detail to tier-2 ("high") without mutating the caller's
            # request object. task_tier=2 only affects _detail_for; _vote and
            # _build_messages ignore it.
            cloud_req = replace(req, task_tier=2)
            cloud_content, cloud_usage = await self._call(
                cloud_client, cloud_req, temperature=0.0, image_b64=image_b64
            )
            total_usage = total_usage.add(cloud_usage)
            cloud_indices, cloud_conf, cloud_raw = self._parse(cloud_content)

            # The cloud result is authoritative now (it's the latest read).
            indices = cloud_indices
            confidence = cloud_conf
            content = cloud_content
            raw = cloud_raw
            client = cloud_client

            # If cloud confidence is still below threshold, engage the existing
            # voting path on the cloud client (only when samples > 1).
            if cloud_conf < threshold and samples > 1:
                voted_indices, voted_conf, vote_usage, votes = await self._vote(
                    cloud_client, cloud_req, samples, image_b64
                )
                indices = voted_indices
                confidence = voted_conf
                total_usage = total_usage.add(vote_usage)

        # Existing tier-2 voting path (only when no inline escalation happened
        # — the elif is mutually exclusive with the if above).
        elif (
            confidence < threshold
            and req.task_tier >= 2
            and cloud_enabled
            and samples > 1
        ):
            voted_indices, voted_conf, vote_usage, votes = await self._vote(
                client, req, samples, image_b64
            )
            indices = voted_indices
            confidence = voted_conf
            total_usage = total_usage.add(vote_usage)

        shape_fields = self._parse_shape_fields(content, req.shape)
        return VisionResult(
            indices=indices,
            confidence=confidence,
            model=client.name,
            usage=total_usage,
            votes=votes,
            raw=raw,
            **shape_fields,
        )

    async def _maybe_stitch(self, req: VisionRequest) -> VisionRequest:
        """Return ``req`` with its tiles composed into one grid image, or as-is.

        Gated by ``VISION_STITCH_GRID`` (default on). Applies only to grid-shape
        requests with 2+ tiles; any failure (Pillow missing, undecodable tile)
        returns the original request so the per-tile path runs unchanged. The
        montage's row/column count is appended to the prompt so the model can
        reason about the exact layout, and ``grid_size`` is recorded.

        ``compose_grid`` (Pillow decode + paste + PNG encode) is CPU-bound and
        would block the event loop under concurrency, so it runs in a worker
        thread via ``asyncio.to_thread`` — freeing the loop to service other
        solves' I/O while the montage is built.
        """
        if not getattr(self._config, "vision_stitch_grid", True):
            return req
        if req.shape not in _STITCHABLE_SHAPES or len(req.images) < 2:
            return req
        composed = await asyncio.to_thread(compose_grid, list(req.images))
        if composed is None:
            return req
        image_bytes, rows, cols = composed
        n = len(req.images)
        prompt = (
            f"{req.prompt}\n\nThe image is a single {rows}x{cols} grid montage of "
            f"{n} tiles laid out left-to-right, top-to-bottom (index 0 is top-left, "
            f"index {n - 1} is the last tile). Return the matching tile indices."
        )
        return replace(req, images=[image_bytes], prompt=prompt, grid_size=n)

    async def _budget_allows_cloud(
        self, req: VisionRequest, client_key: "str | None"
    ) -> bool:
        """Budget gate for an inline escalation cloud call.

        Returns True when there is no budget, or when the budget allows the
        cloud call. Returns False on denial — the caller keeps the local
        result instead of escalating. Mirrors the existing tier-2 budget
        check; a denial here does NOT downgrade (the local result is already
        in hand and is the appropriate fallback).
        """
        if self._budget is None:
            return True
        est_cost = self._estimate_cost(req)
        decision = await self._budget.check(client_key, est_cost, model="cloud")
        if decision is None:
            return True
        return bool(getattr(decision, "allowed", True))

    # -- backend fallback -------------------------------------------------

    async def _call_with_backend_fallback(
        self, model_name: str, req: VisionRequest, client_key, image_b64: list
    ):
        """Run the initial call, retrying the other backend on a connection error.

        The routed backend (local for tier-1, cloud for tier-2) is tried first.
        If it fails with a *connection* error (service down / refused / timeout)
        — not a parse or bad-request error — the call retries once on the other
        backend: ``local → cloud`` (gated by ``vision_cloud_enabled`` + budget,
        since cloud is paid) or ``cloud → local`` (always, local is free). This
        turns a dead local model service from an instant solve failure into a
        transparent cloud solve. Returns ``(client, content, usage)``.
        """
        client = self._pool.get(model_name)
        try:
            content, usage = await self._call(
                client, req, temperature=0.0, image_b64=image_b64
            )
            return client, content, usage
        except Exception as exc:  # noqa: BLE001
            fallback_enabled = bool(
                getattr(self._config, "model_connection_fallback", True)
            )
            if not fallback_enabled or not _is_connection_error(exc):
                raise
            alt = await self._fallback_backend(model_name, req, client_key)
            if alt is None:
                raise
            log.warning(
                "vision backend %r failed (%s: %s); falling back to %r",
                model_name,
                type(exc).__name__,
                exc,
                alt,
            )
            alt_client = self._pool.get(alt)
            content, usage = await self._call(
                alt_client, req, temperature=0.0, image_b64=image_b64
            )
            return alt_client, content, usage

    async def _fallback_backend(
        self, model_name: str, req: VisionRequest, client_key
    ) -> "str | None":
        """Pick the alternate backend for a connection-error retry, or None."""
        if model_name == "cloud":
            return "local"  # local is free + self-hosted; always a valid fallback
        # local → cloud only when cloud is enabled and the budget allows it.
        if not getattr(self._config, "vision_cloud_enabled", False):
            return None
        if await self._budget_allows_cloud(req, client_key):
            return "cloud"
        return None

    # -- voting -----------------------------------------------------------

    async def _vote(self, client, req: VisionRequest, samples: int, image_b64: list):
        counts: Counter = Counter()
        usage_total = ModelUsage()

        # WP4: run vote samples concurrently via asyncio.gather (default) — the
        # samples are independent and the dominant latency is model round-trip,
        # so gathering cuts vote wall-time from samples×RTT to ~1×RTT. The
        # serial loop is kept behind VISION_VOTE_CONCURRENT=false as an escape
        # hatch for backends that rate-limit concurrent requests.
        concurrent = bool(getattr(self._config, "vision_vote_concurrent", True))
        if concurrent:
            results = await asyncio.gather(
                *(
                    self._call(
                        client, req, temperature=_VOTE_TEMPERATURE, image_b64=image_b64
                    )
                    for _ in range(samples)
                )
            )
        else:
            results = []
            for _ in range(samples):
                content, usage = await self._call(
                    client, req, temperature=_VOTE_TEMPERATURE, image_b64=image_b64
                )
                results.append((content, usage))

        for content, usage in results:
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

    async def _call(
        self, client, req: VisionRequest, *, temperature: float, image_b64: list
    ):
        messages = self._build_messages(req, image_b64)
        timeout = float(getattr(self._config, "captcha_timeout", 30))
        return await client.chat(
            messages=messages,
            temperature=temperature,
            max_tokens=512,
            timeout=timeout,
        )

    def _build_messages(self, req: VisionRequest, image_b64: list) -> list:
        """Build the chat messages, reusing pre-encoded base64 image payloads.

        ``image_b64`` is the list of base64 strings encoded once in ``classify``
        (aligned to ``req.images``); only the per-call ``detail`` is applied
        here, so the same PNG isn't re-encoded on every vote sample.
        """
        detail = self._detail_for(req)
        system_prompt = _SHAPE_PROMPTS.get(req.shape, SYSTEM_PROMPT)
        user_content: list = [{"type": "text", "text": req.prompt}]
        for b64 in image_b64:
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
            {"role": "system", "content": system_prompt},
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

    @staticmethod
    def _parse_shape_fields(content: str, shape: str) -> dict:
        """Extract shape-specific fields from model output."""
        raw = content or ""
        text = raw.strip()
        fence = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
        if fence:
            text = fence.group(1).strip()
        try:
            data = json.loads(text)
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}

        fields: dict = {}
        if shape == "area_bbox":
            x = data.get("x")
            y = data.get("y")
            if x is not None and y is not None:
                fields["point"] = (float(x), float(y))
        elif shape == "canvas_slide":
            d = data.get("distance")
            if d is not None:
                fields["distance"] = float(d)
        elif shape == "drag_drop":
            src = data.get("source")
            tgt = data.get("target")
            if isinstance(src, (list, tuple)) and len(src) >= 2:
                fields["source"] = (float(src[0]), float(src[1]))
            if isinstance(tgt, (list, tuple)) and len(tgt) >= 2:
                fields["target"] = (float(tgt[0]), float(tgt[1]))
        return fields


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
