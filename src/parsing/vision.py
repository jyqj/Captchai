"""Vision routing for grid / image captcha classification.

``VisionRouter`` picks a model per (tier, budget), runs the call, and — when a
tier-2 index challenge comes back with low confidence — escalates to
self-consistency voting: it samples the model several times and majority-votes
each tile in or out, recomputing confidence as the agreement ratio. Coordinate
shapes (area / slider / drag) are combined by clustering their points instead.
A reply that cannot be parsed into its shape's schema is a failed sample, never
an implicit empty answer; when no sample survives, ``VisionParseError`` is
raised so the solve surfaces as ``ERROR_CAPTCHA_UNSOLVABLE``.

This module is intentionally decoupled from the consumption layer.  ``ledger``
and ``budget`` are accepted as opaque, optional, duck-typed objects; every use
is guarded with ``is not None``.  Nothing here imports the ledger/budget modules.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, replace
from typing import Any, Optional

from src.assets.model_pool import ModelPool, ModelUsage
from src.parsing.image_grid import acompose_grid
from src.parsing.model_call import ModelInvoker
from src.services.captcha_errors import CaptchaError

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

# Shapes whose answers are tile indices — the only ones where per-tile majority
# voting is meaningful. Coordinate shapes (area_bbox / slide / drag) answer with
# points/distances and are combined by ``_coordinate_agreement`` instead.
_INDEX_SHAPES = frozenset({"grid_select", "recaptcha_dynamic"})
_COORDINATE_SHAPES = frozenset({"area_bbox", "canvas_slide", "drag_drop"})

# Shapes whose images are independent tiles of one grid — the only ones where a
# single-image montage preserves the classifier's index contract. Coordinate
# shapes (area_bbox / slide / drag) already send one image, so stitching is a
# no-op for them and must never apply (it would corrupt pixel coordinates).
_STITCHABLE_SHAPES = _INDEX_SHAPES

# Temperature used for self-consistency voting samples (must be > 0 so the
# model produces varied samples to vote over).
_VOTE_TEMPERATURE = 0.8

_DEFAULT_CONFIDENCE = 0.5

# Confidence penalty when self-consistency could not be established: only one
# valid sample was parsed, or a tier-2 request fell back to the tier-1 model
# because the cloud backend is disabled. Never let those paths report 1.0.
_SINGLE_SAMPLE_CONFIDENCE_FACTOR = 0.7
_DEGRADED_TIER2_CONFIDENCE_FACTOR = 0.75

# Radius (in image pixels) within which two coordinate samples count as
# agreeing. Overridable via ``VISION_COORD_CLUSTER_TOLERANCE``.
_COORD_CLUSTER_TOLERANCE_PX = 24.0

# Minimum number of successfully parsed samples needed to accept an index vote
# (one-shot solves have a single sample and never vote).
_MIN_INDEX_VOTE_SAMPLES = 2

# Minimum number of numeric tokens a legacy grid-shaped ``indices`` payload
# must carry to answer a coordinate shape: x,y for a point, one distance for a
# slider, two endpoints (4 values) for a drag. A shorter list is a failed
# sample, not a degenerate coordinate answer.
_LEGACY_INDEX_ARITY = {
    "area_bbox": 2,
    "canvas_slide": 1,
    "drag_drop": 4,
}

# Process-wide latch for the "tier-2 requested but cloud disabled" warning so a
# cloudless fleet logs it once instead of once per solve.
_TIER2_UNAVAILABLE_WARNED = False


def _encode_images_b64(images: list) -> list:
    """Base64-encode raw image bytes (CPU-bound; run via ``asyncio.to_thread``)."""
    return [base64.b64encode(img).decode() for img in images]


class VisionParseError(CaptchaError):
    """No model sample could be parsed into the shape's expected schema.

    Treated like any other classified captcha error by the solve loop. The
    exception carries the usage accumulated before the failure (and the backend
    that served it) so :class:`~src.parsing.vision_adapter.VisionAdapter` can
    still meter a failed solve — the ledger records per solve, not per call.
    """

    retryable = True
    outcome = "vision_parse_error"

    def __init__(
        self,
        message: str,
        *,
        usage: Optional[ModelUsage] = None,
        model: str = "",
        votes: int = 1,
        raw: str = "",
    ) -> None:
        super().__init__(message)
        self.usage = usage if usage is not None else ModelUsage()
        self.model = model
        self.votes = votes
        self.raw = raw


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
    # Explicit success flag: a client that cannot raise (e.g. an alternative
    # router injected through the adapter) can signal a parse failure with
    # ``ok=False`` instead. ``VisionRouter`` itself raises ``VisionParseError``.
    ok: bool = True
    error: str = ""


@dataclass
class _ParsedSample:
    """One model reply, validated against the shape's expected schema."""

    indices: list
    confidence: float
    raw: str
    ok: bool
    fields: dict


@dataclass
class _VoteOutcome:
    """Result of one self-consistency vote over ``votes`` samples."""

    ok: bool
    indices: list
    confidence: float
    usage: ModelUsage
    votes: int
    fields: dict
    raw: str
    parsed: int


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
        # The one place a model call is routed, budgeted and made. VisionRouter
        # owns the *grid* semantics (stitch, vote, confidence gate, shape parse)
        # and delegates the invocation core (route → budget → call → backend
        # fallback) to this shared seam — the same seam the free-form vision
        # solvers (classification / recognition) call.
        self._invoker = ModelInvoker(model_pool, config, budget=budget)

    # -- public API -------------------------------------------------------

    async def classify(
        self, req: VisionRequest, *, client_key: "str | None" = None
    ) -> VisionResult:
        # Collapse a multi-tile grid into a single montage before routing so the
        # cost estimate, the single call, and every vote sample all operate on
        # one image instead of N. Transparent to the index contract (tiles keep
        # their left-to-right / top-to-bottom order) and reversible via config.
        req = await self._maybe_stitch(req)
        est_cost = self._estimate_cost(req)
        model_name = self._invoker.route(req.task_tier)

        # Encode each image's base64 ONCE and reuse it across the initial call,
        # every vote sample, and any inline cloud escalation — instead of
        # re-encoding the same (potentially large) PNG on every _build_messages
        # call. The data-url ``detail`` (which does vary by tier) is applied
        # cheaply per call around this cached payload. Base64 of several
        # full-size tiles is CPU-bound, so it runs in a worker thread rather
        # than blocking the event loop (and every concurrent solve with it).
        image_b64 = await asyncio.to_thread(_encode_images_b64, list(req.images))

        # Budget gate — only paid (cloud) calls are checked. A denial with a
        # suggested downgrade falls back to the cheaper local model.
        model_name = await self._invoker.guard_budget(
            model_name, est_cost, client_key
        )

        # Initial call through the shared seam; the returned model name is the
        # backend that actually served (after any connection-error fallback).
        model_name, content, usage = await self._invoker.call_with_fallback(
            model_name,
            self._build_messages(req, image_b64),
            client_key=client_key,
            est_cost=est_cost,
            temperature=0.0,
        )
        initial = self._parse_sample(content, req.shape)
        total_usage = usage
        # Every model call made for this classify, for metering even when the
        # result is discarded (``VisionParseError`` carries it to the adapter).
        call_count = 1

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

        # Agreement over self-report. A single model call reports its OWN
        # confidence, which a model is frequently *confidently wrong* about, so
        # trusting that number as the finality gate lets wrong grids through
        # untested. Index challenges (whose per-tile answers can be
        # majority-voted) instead derive confidence from cross-sample agreement
        # (self-consistency). ``VISION_TRUST_SELF_CONFIDENCE`` restores the
        # legacy self-report gating. The tier split is deliberate:
        #
        #   * tier 2 (the hard path) votes — never takes a single cloud call's
        #     self-reported number at face value.
        #   * tier 1 (the cheap first pass) keeps the self-report gate to decide
        #     inline escalation: a low self-report is a safe "I'm unsure →
        #     escalate" trigger, and a tier-1 overconfident error is caught by
        #     the solver's tier-2 retry (stage-aware escalation), so paying for
        #     a full local vote on every cheap pass isn't warranted.
        trust_self = bool(
            getattr(self._config, "vision_trust_self_confidence", False)
        )
        votable = req.shape in _INDEX_SHAPES

        # A tier-2 request asks for the hard path, whose self-consistency pass
        # runs on the cloud model. With cloud disabled the vote used to be
        # skipped silently; warn once per process and return the tier-1 result
        # at reduced confidence instead of pretending it carries tier-2
        # assurance.
        tier2_unavailable = req.task_tier >= 2 and not cloud_enabled
        if tier2_unavailable:
            self._warn_tier2_unavailable_once()

        # A sample that failed to parse is NEVER allowed to masquerade as "the
        # model answered an empty grid": the booleans below track whether a
        # real, schema-valid sample exists before anything is returned.
        indices = list(initial.indices)
        confidence = initial.confidence
        fields = dict(initial.fields)
        raw = initial.raw
        parsed_ok = initial.ok
        if not parsed_ok:
            log.warning(
                "vision: model output did not match the %r schema: %r",
                req.shape,
                (content or "")[:200],
            )

        # WP4: inline tier-1 → tier-2 escalation. A low-confidence local result
        # gets a single cloud retry (optionally followed by cloud voting) before
        # the solver falls back to a full browser redo. The cloud call goes
        # through the same budget gate as the tier-2 path; a denial downgrades
        # back to the local result (no escalation). Transparent to shape
        # solvers — they still read result.indices / result.confidence.
        if (
            req.task_tier < 2
            and cloud_enabled
            and inline_escalate
            and (not parsed_ok or confidence < threshold)
            and await self._invoker.budget_allows_cloud(est_cost, client_key)
        ):
            # Re-route detail to tier-2 ("high") without mutating the caller's
            # request object. task_tier=2 only affects _detail_for; _vote and
            # _build_messages ignore it.
            cloud_req = replace(req, task_tier=2)
            call_count += 1
            try:
                cloud_content, cloud_usage = await self._call(
                    "cloud", cloud_req, temperature=0.0, image_b64=image_b64
                )
            except Exception as exc:  # noqa: BLE001
                # A failed escalation must not discard a valid local result:
                # previously the cloud error propagated and failed the task even
                # though the local challenge had already been answered.
                log.warning(
                    "vision: inline cloud escalation failed (%s); keeping the "
                    "local result",
                    exc,
                )
            else:
                total_usage = total_usage.add(cloud_usage)
                cloud_sample = self._parse_sample(cloud_content, req.shape)
                if cloud_sample.ok:
                    # The cloud sample is authoritative now (it's the latest
                    # schema-valid read).
                    indices = list(cloud_sample.indices)
                    confidence = cloud_sample.confidence
                    fields = dict(cloud_sample.fields)
                    raw = cloud_sample.raw
                    parsed_ok = True
                    model_name = "cloud"

                    # Vote the escalated cloud result too when it's an index
                    # shape (don't trust its self-report either) or when its
                    # self-report is low.
                    if samples > 1 and (
                        (votable and not trust_self) or confidence < threshold
                    ):
                        call_count += samples
                        try:
                            vote = await self._vote(
                                "cloud", cloud_req, samples, image_b64
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:  # noqa: BLE001
                            # Voting is best-effort once a valid escalated
                            # sample exists; never fail the task over it.
                            log.warning(
                                "vision: cloud vote failed (%s); keeping the "
                                "escalated sample at reduced confidence",
                                exc,
                            )
                            confidence *= _SINGLE_SAMPLE_CONFIDENCE_FACTOR
                        else:
                            total_usage = total_usage.add(vote.usage)
                            if vote.ok:
                                indices = vote.indices
                                confidence = vote.confidence
                                fields = vote.fields or fields
                                raw = vote.raw
                                votes = vote.votes
                            else:
                                log.warning(
                                    "vision: cloud vote rejected (%d/%d samples "
                                    "parsed); keeping the escalated sample at "
                                    "reduced confidence",
                                    vote.parsed,
                                    vote.votes,
                                )
                                confidence *= _SINGLE_SAMPLE_CONFIDENCE_FACTOR
                else:
                    log.warning(
                        "vision: cloud escalation was unparseable; keeping the "
                        "local result"
                    )

        # Hard tier: vote to establish self-consistency. Index shapes vote
        # per tile; coordinate shapes cluster their points/distances inside
        # ``_vote``. A parse-failed first sample is still worth voting on — a
        # later sample may parse. The elif is mutually exclusive with the
        # tier-1 escalation above.
        elif (
            req.task_tier >= 2
            and cloud_enabled
            and samples > 1
            and (
                not parsed_ok
                or (votable and not trust_self)
                or confidence < threshold
            )
        ):
            call_count += samples
            try:
                vote = await self._vote(model_name, req, samples, image_b64)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "vision: self-consistency vote failed (%s); %s",
                    exc,
                    "keeping the initial sample at reduced confidence"
                    if parsed_ok
                    else "no parseable sample remains",
                )
                if parsed_ok:
                    confidence *= _SINGLE_SAMPLE_CONFIDENCE_FACTOR
            else:
                total_usage = total_usage.add(vote.usage)
                if vote.ok:
                    indices = vote.indices
                    confidence = vote.confidence
                    fields = vote.fields or fields
                    raw = vote.raw
                    votes = vote.votes
                    parsed_ok = True
                else:
                    log.warning(
                        "vision: self-consistency vote rejected (%d/%d samples "
                        "parsed); %s",
                        vote.parsed,
                        vote.votes,
                        "keeping the initial sample at reduced confidence"
                        if parsed_ok
                        else "no parseable sample remains",
                    )
                    if parsed_ok:
                        confidence *= _SINGLE_SAMPLE_CONFIDENCE_FACTOR

        if tier2_unavailable and parsed_ok:
            # Tier-1 fallback for a tier-2 request: the answer is still usable,
            # but it never received the cloud self-consistency pass.
            confidence *= _DEGRADED_TIER2_CONFIDENCE_FACTOR

        if not parsed_ok:
            raise VisionParseError(
                "no parseable %s model sample (%d call(s) attempted)"
                % (req.shape, call_count),
                usage=total_usage,
                model=model_name,
                votes=call_count,
                raw=raw,
            )

        return VisionResult(
            indices=indices,
            confidence=confidence,
            model=model_name,
            usage=total_usage,
            votes=votes,
            raw=raw,
            **fields,
        )

    async def _maybe_stitch(self, req: VisionRequest) -> VisionRequest:
        """Return ``req`` with its tiles composed into one grid image, or as-is.

        Gated by ``VISION_STITCH_GRID`` (default on). Applies only to grid-shape
        requests with 2+ tiles; any failure (Pillow missing, undecodable tile)
        returns the original request so the per-tile path runs unchanged. The
        montage's row/column count is appended to the prompt so the model can
        reason about the exact layout, and ``grid_size`` is recorded.

        ``acompose_grid`` (Pillow decode + paste + PNG encode) is CPU-bound and
        runs in a worker thread — freeing the loop to service other solves'
        I/O while the montage is built. Its header-level size guards reject
        implausible tiles before any full decode.
        """
        if not getattr(self._config, "vision_stitch_grid", True):
            return req
        if req.shape not in _STITCHABLE_SHAPES or len(req.images) < 2:
            return req
        composed = await acompose_grid(list(req.images))
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

    # -- voting -----------------------------------------------------------

    async def _vote(
        self, model_name: str, req: VisionRequest, samples: int, image_b64: list
    ) -> _VoteOutcome:
        usage_total = ModelUsage()

        # WP4: run vote samples concurrently via asyncio.gather (default) — the
        # samples are independent and the dominant latency is model round-trip,
        # so gathering cuts vote wall-time from samples×RTT to ~1×RTT. The
        # serial loop is kept behind VISION_VOTE_CONCURRENT=false as an escape
        # hatch for backends that rate-limit concurrent requests.
        #
        # ``return_exceptions=True`` isolates samples: one backend error must
        # not discard the other, already-paid-for samples.
        #
        # Every sample is a *reservation* against the backend's fan-out
        # semaphore (``ModelInvoker.backend_semaphore``): ``samples`` calls per
        # solve, times several concurrent solves, would otherwise multiply into
        # an unbounded burst against a rate-limited backend.
        concurrent = bool(getattr(self._config, "vision_vote_concurrent", True))
        results: list = []
        if concurrent:
            results = list(
                await asyncio.gather(
                    *(
                        self._vote_sample(model_name, req, image_b64)
                        for _ in range(samples)
                    ),
                    return_exceptions=True,
                )
            )
        else:
            for _ in range(samples):
                try:
                    results.append(
                        await self._vote_sample(model_name, req, image_b64)
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - isolate the sample
                    results.append(exc)

        parsed_samples: list = []
        for item in results:
            if isinstance(item, asyncio.CancelledError):
                # Cancellation of the solve must propagate, not be counted as
                # just another failed sample.
                raise item
            if isinstance(item, BaseException):
                log.warning(
                    "vision: vote sample failed (%s: %s)",
                    type(item).__name__,
                    item,
                )
                continue
            content, usage = item
            # Usage is metered even when this sample's output is unparseable —
            # the call was still paid for.
            usage_total = usage_total.add(usage)
            sample = self._parse_sample(content, req.shape)
            if sample.ok:
                parsed_samples.append(sample)
            else:
                log.warning(
                    "vision: vote sample did not match the %r schema: %r",
                    req.shape,
                    (content or "")[:200],
                )

        n = len(parsed_samples)
        if req.shape in _INDEX_SHAPES:
            # Index voting needs a quorum: a lone survivor out of a failed vote
            # is not a "majority". One-shot solves (samples == 1) are accepted.
            min_samples = 1 if samples <= 1 else _MIN_INDEX_VOTE_SAMPLES
            if n < min_samples:
                return _VoteOutcome(
                    ok=False,
                    indices=[],
                    confidence=0.0,
                    usage=usage_total,
                    votes=samples,
                    fields={},
                    raw="",
                    parsed=n,
                )
            indices, confidence = self._index_agreement(parsed_samples, samples)
            fields: dict = {}
        else:
            # Coordinate shapes cannot be index-voted: their answer is a point /
            # distance / endpoint pair, so cluster the coordinates and take the
            # largest cluster's median. A single valid sample is usable only at
            # a reduced confidence.
            if n < 1:
                return _VoteOutcome(
                    ok=False,
                    indices=[],
                    confidence=0.0,
                    usage=usage_total,
                    votes=samples,
                    fields={},
                    raw="",
                    parsed=0,
                )
            indices, confidence, fields = self._coordinate_agreement(
                parsed_samples, req.shape, samples
            )
            if not fields:
                # The samples parsed but carried no usable coordinate (e.g. a
                # one-value legacy ``indices`` payload for a point shape).
                return _VoteOutcome(
                    ok=False,
                    indices=[],
                    confidence=0.0,
                    usage=usage_total,
                    votes=samples,
                    fields={},
                    raw=parsed_samples[0].raw,
                    parsed=n,
                )

        return _VoteOutcome(
            ok=True,
            indices=indices,
            confidence=confidence,
            usage=usage_total,
            votes=samples,
            fields=fields,
            raw=parsed_samples[0].raw,
            parsed=n,
        )

    @staticmethod
    def _index_agreement(parsed_samples: list, samples: int):
        """Majority-vote tile indices over the successfully parsed samples."""
        counts: Counter = Counter()
        for sample in parsed_samples:
            for idx in set(sample.indices):
                counts[idx] += 1

        n = len(parsed_samples)
        # A tile is selected if a strict majority of the parsed samples chose it.
        selected = sorted(idx for idx, c in counts.items() if c * 2 > n)

        if counts:
            # Agreement per candidate tile: how consistently samples agreed on
            # the in/out decision for that tile. Confidence = mean agreement.
            agreements = [max(c, n - c) / float(n) for c in counts.values()]
            confidence = sum(agreements) / len(agreements)
        else:
            # Every PARSED sample agreed the grid is empty. Unparseable samples
            # never reach this branch (they are dropped above), so this is a
            # genuine unanimous answer, not a parse failure in disguise.
            confidence = 1.0

        # Unparsed samples still count against certainty: a vote that only ran
        # on half its samples is not a confident one.
        confidence *= n / float(max(1, samples))
        return selected, min(1.0, confidence)

    def _coordinate_agreement(
        self, parsed_samples: list, shape: str, samples: "int | None" = None
    ) -> "tuple[list, float, dict]":
        """Cluster coordinate answers; return (indices, confidence, fields).

        Pixel coordinates from a model are noisy (a handful of pixels per
        sample), so agreement is defined as membership of the largest cluster
        within ``vision_coord_cluster_tolerance`` pixels rather than exact
        equality. Multiple samples: confidence is the cluster's share of the
        samples, scaled by the mean self-reported confidence. A single usable
        sample is returned at a deliberately reduced confidence — never 1.0.
        When ``samples`` (the number of calls made) is given, the share of
        unparseable samples also damps confidence, so a 1-of-5 result never
        reads as confident as 5-of-5.
        """
        tolerance = float(
            getattr(
                self._config,
                "vision_coord_cluster_tolerance",
                _COORD_CLUSTER_TOLERANCE_PX,
            )
        )
        conf_mean = sum(s.confidence for s in parsed_samples) / len(parsed_samples)
        multi = len(parsed_samples) > 1
        # Unparsed samples count against certainty, mirroring _index_agreement.
        if samples is None:
            parse_ratio = 1.0
        else:
            parse_ratio = len(parsed_samples) / float(max(1, samples))

        if shape == "canvas_slide":
            values = _sample_distances(parsed_samples)
            if not values:
                return [], 0.0, {}
            median, best, total = _largest_scalar_cluster(values, tolerance)
            confidence = (best / total) * conf_mean
            if not multi:
                confidence = conf_mean * _SINGLE_SAMPLE_CONFIDENCE_FACTOR
            return [], min(1.0, confidence * parse_ratio), {"distance": median}

        if shape == "drag_drop":
            sources = _sample_endpoints(parsed_samples, "source")
            targets = _sample_endpoints(parsed_samples, "target")
            if not sources or not targets:
                return [], 0.0, {}
            source, s_best, s_total = _largest_point_cluster(sources, tolerance)
            target, t_best, t_total = _largest_point_cluster(targets, tolerance)
            confidence = min(s_best / s_total, t_best / t_total) * conf_mean
            if not multi:
                confidence = conf_mean * _SINGLE_SAMPLE_CONFIDENCE_FACTOR
            return (
                [],
                min(1.0, confidence * parse_ratio),
                {"source": source, "target": target},
            )

        # area_bbox (and any future point shape).
        points = _sample_points(parsed_samples)
        if not points:
            return [], 0.0, {}
        point, best, total = _largest_point_cluster(points, tolerance)
        confidence = (best / total) * conf_mean
        if not multi:
            confidence = conf_mean * _SINGLE_SAMPLE_CONFIDENCE_FACTOR
        return [], min(1.0, confidence * parse_ratio), {"point": point}

    @staticmethod
    def _warn_tier2_unavailable_once() -> None:
        """Log once per process that tier-2 self-consistency cannot run."""
        global _TIER2_UNAVAILABLE_WARNED
        if _TIER2_UNAVAILABLE_WARNED:
            return
        _TIER2_UNAVAILABLE_WARNED = True
        log.warning(
            "vision: tier-2 self-consistency requested but the cloud backend is "
            "disabled (VISION_CLOUD_ENABLED=false); returning the tier-1 result "
            "at reduced confidence"
        )

    # -- model call -------------------------------------------------------

    async def _vote_sample(
        self, model_name: str, req: VisionRequest, image_b64: list
    ):
        """One self-consistency sample, holding the backend's reservation slot.

        The per-backend semaphore comes from :class:`ModelInvoker` (which sizes
        it from the client's ``max_concurrency`` / the config's
        ``*_max_concurrency`` and falls back to a conservative default), so the
        fan-out of every voting solve in the process shares one bound.
        """
        semaphore = self._invoker.backend_semaphore(model_name)
        if semaphore is None:
            return await self._call(
                model_name,
                req,
                temperature=_VOTE_TEMPERATURE,
                image_b64=image_b64,
            )
        async with semaphore:
            return await self._call(
                model_name,
                req,
                temperature=_VOTE_TEMPERATURE,
                image_b64=image_b64,
            )

    async def _call(
        self, model_name: str, req: VisionRequest, *, temperature: float, image_b64: list
    ):
        """One call on a specific backend through the shared seam."""
        messages = self._build_messages(req, image_b64)
        return await self._invoker.call_backend(
            model_name, messages, temperature=temperature, max_tokens=512
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
        # Coordinate shapes (area / slide / drag) consume pixel positions, and
        # providers down-sample ``detail="low"`` images aggressively — the
        # returned pixel is then quantized far beyond what the click tolerates.
        # We force ``high`` detail for these shapes so the pixel contract the
        # solvers map back through the screenshot→CSS scale stays exact; the
        # alternative (normalized [0,1] coordinates) would need every coordinate
        # prompt, parser and solver to change in lock-step. Index/grid shapes
        # keep the cheap tier default because their answer is a tile index.
        if req.shape in _COORDINATE_SHAPES:
            return "high"
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

    @classmethod
    def _parse_sample(
        cls, content: str, shape: str = "grid_select"
    ) -> _ParsedSample:
        """Validate one model reply against the schema ``shape`` expects.

        ``ok`` is only True when the reply carries the shape's answer in a
        usable form: an ``indices`` list for index shapes, ``x``/``y`` for
        area, ``distance`` for slider, ``source``+``target`` for drag. Anything
        else (prose, truncated JSON, wrong keys, non-numeric values) is a failed
        sample — never an implicit empty grid.
        """
        raw = content or ""
        text = raw.strip()

        fence = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
        if fence:
            text = fence.group(1).strip()

        try:
            data = json.loads(text)
        except Exception:
            data = None

        if isinstance(data, dict):
            return cls._parse_json_sample(data, raw, shape)
        return cls._parse_freeform_sample(text, raw, shape)

    @staticmethod
    def _parse_json_sample(data: dict, raw: str, shape: str) -> _ParsedSample:
        confidence = _clamp_confidence(_coerce_number(data.get("confidence")))
        indices: list = []
        fields: dict = {}
        ok = False

        if shape in _COORDINATE_SHAPES:
            if shape == "area_bbox":
                point = _coerce_point([data.get("x"), data.get("y")])
                if point is not None:
                    fields["point"] = point
                    ok = True
            elif shape == "canvas_slide":
                distance = _coerce_number(data.get("distance"))
                if distance is not None:
                    fields["distance"] = distance
                    ok = True
            elif shape == "drag_drop":
                source = _coerce_point(data.get("source"))
                target = _coerce_point(data.get("target"))
                if source is not None and target is not None:
                    fields["source"] = source
                    fields["target"] = target
                    ok = True
        else:
            # Index shapes (and any unknown shape, which keeps the original grid
            # contract): ``indices`` must be present and a list of usable
            # integers. A present-but-unparseable payload is a failed sample,
            # never an implicit empty grid; the empty list is only valid for a
            # shape that can legitimately answer "select none".
            if isinstance(data.get("indices"), list):
                indices, parsed = _strict_int_list(data["indices"])
                ok = parsed and (
                    bool(indices) or _shape_permits_empty_selection(shape)
                )

        if not ok and shape in _COORDINATE_SHAPES:
            # Legacy fallback the shape solvers already support: a coordinate
            # answer may arrive in the grid ``indices`` form (x, y / distance /
            # source+target packed as numbers). Treat that as parseable only
            # when it is fully numeric AND long enough for the shape's arity —
            # a 1-point drag answer is a failed sample, not a coordinate.
            if isinstance(data.get("indices"), list):
                indices, parsed = _strict_int_list(data["indices"])
                ok = parsed and len(indices) >= _LEGACY_INDEX_ARITY.get(shape, 1)

        return _ParsedSample(
            indices=indices, confidence=confidence, raw=raw, ok=ok, fields=fields
        )

    @staticmethod
    def _parse_freeform_sample(text: str, raw: str, shape: str) -> _ParsedSample:
        # Regex fallbacks when the model didn't return clean JSON. Only index
        # answers have a safe free-form form; a coordinate sample without JSON
        # is schema-mismatched.
        indices: list = []
        confidence: Optional[float] = None
        ok = False

        if shape not in _COORDINATE_SHAPES:
            idx_match = re.search(
                r"indices\"?\s*[:=]\s*\[([^\]]*)\]", text, re.IGNORECASE
            )
            if idx_match:
                indices = _safe_int_list(idx_match.group(1))
                # A grid result must carry at least one numeric token; a
                # bracket of prose/junk is a failed sample, not an empty grid.
                ok = bool(indices)
            else:
                bracket = re.search(r"\[([^\]]*)\]", text)
                if bracket:
                    indices = _safe_int_list(bracket.group(1))
                    ok = bool(indices)

        conf_match = re.search(
            r"confidence\"?\s*[:=]?\s*([0-9]*\.?[0-9]+)", text, re.IGNORECASE
        )
        if conf_match:
            confidence = _coerce_number(conf_match.group(1))

        return _ParsedSample(
            indices=indices,
            confidence=_clamp_confidence(confidence),
            raw=raw,
            ok=ok,
            fields={},
        )

    @staticmethod
    def _parse(content: str):
        """Backward-compatible 3-tuple wrapper around :meth:`_parse_sample`."""
        sample = VisionRouter._parse_sample(content)
        return sample.indices, sample.confidence, sample.raw

    @staticmethod
    def _parse_shape_fields(content: str, shape: str) -> dict:
        """Extract shape-specific fields from model output (garbage-safe)."""
        return dict(VisionRouter._parse_sample(content, shape).fields)


def _coerce_int_list(values: list) -> list:
    out: list = []
    try:
        iterator = list(values)
    except TypeError:
        return out
    for v in iterator:
        if isinstance(v, bool):
            continue
        try:
            if isinstance(v, int):
                out.append(v)
            elif isinstance(v, float):
                # ``int(inf)`` / ``int(nan)`` raise; a non-finite value is not
                # a usable index.
                if math.isfinite(v):
                    out.append(int(v))
            elif isinstance(v, str):
                s = v.strip()
                if s.lstrip("-").isdigit():
                    out.append(int(s))
        except (TypeError, ValueError, OverflowError):
            continue
    return out


def _strict_int_list(values: list) -> "tuple[list, bool]":
    """Coerce a JSON ``indices`` payload, failing on ANY unusable element.

    Unlike :func:`_coerce_int_list`, junk elements are not silently dropped:
    a payload that is present but unparseable must fail the sample rather than
    degrade to a partial — or empty, falsely confident — grid answer.
    """
    out: list = []
    try:
        iterator = list(values)
    except TypeError:
        return out, False
    for v in iterator:
        if isinstance(v, bool):
            return out, False
        try:
            if isinstance(v, int):
                out.append(v)
            elif isinstance(v, float):
                # ``int(inf)`` / ``int(nan)`` raise; non-finite is not usable.
                if not math.isfinite(v):
                    return out, False
                out.append(int(v))
            elif isinstance(v, str):
                s = v.strip()
                if not s.lstrip("-").isdigit():
                    return out, False
                out.append(int(s))
            else:
                return out, False
        except (TypeError, ValueError, OverflowError):
            return out, False
    return out, True


def _shape_permits_empty_selection(shape: str) -> bool:
    """Whether ``[]`` is a legitimate answer for ``shape``.

    Grid/index shapes can legitimately answer "nothing matches" (their solvers
    submit and settle); coordinate shapes always require a real point.
    """
    return shape not in _COORDINATE_SHAPES


def _safe_int_list(text: str) -> list:
    """Extract integers from free-form text; malformed tokens are dropped."""
    out: list = []
    for token in re.findall(r"-?\d+", text):
        try:
            out.append(int(token))
        except (TypeError, ValueError, OverflowError):
            continue
    return out


def _coerce_number(value: Any) -> "float | None":
    """Coerce model output to a finite float, or ``None`` when unusable."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _coerce_point(value: Any) -> "tuple[float, float] | None":
    """Coerce a model coordinate pair to ``(x, y)`` floats, or ``None``."""
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return None
    x = _coerce_number(value[0])
    y = _coerce_number(value[1])
    if x is None or y is None:
        return None
    return x, y


def _clamp_confidence(value: "float | None") -> float:
    """Clamp a confidence to [0, 1]; missing/unusable values use the default."""
    if value is None:
        return _DEFAULT_CONFIDENCE
    return min(1.0, max(0.0, float(value)))


def _median(values: list) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (float(ordered[mid - 1]) + float(ordered[mid])) / 2.0


def _largest_point_cluster(points: list, tolerance: float):
    """Greedy radius clustering; return (median_point, best_size, total)."""
    clusters: list = []
    for point in points:
        for cluster in clusters:
            cx = _median([p[0] for p in cluster])
            cy = _median([p[1] for p in cluster])
            if math.hypot(point[0] - cx, point[1] - cy) <= tolerance:
                cluster.append(point)
                break
        else:
            clusters.append([point])
    best = max(clusters, key=len)
    return (
        (_median([p[0] for p in best]), _median([p[1] for p in best])),
        len(best),
        len(points),
    )


def _largest_scalar_cluster(values: list, tolerance: float):
    """Greedy 1-D radius clustering; return (median, best_size, total)."""
    clusters: list = []
    for value in values:
        for cluster in clusters:
            if abs(value - _median(cluster)) <= tolerance:
                cluster.append(value)
                break
        else:
            clusters.append([value])
    best = max(clusters, key=len)
    return _median(best), len(best), len(values)


def _sample_points(parsed_samples: list) -> list:
    """Point answers from parsed samples (shape fields, else legacy indices)."""
    points: list = []
    for sample in parsed_samples:
        point = sample.fields.get("point")
        if point is None and len(sample.indices) >= 2:
            point = _coerce_point(sample.indices[:2])
        if point is not None:
            points.append(point)
    return points


def _sample_distances(parsed_samples: list) -> list:
    """Distance answers from parsed samples (shape field, else legacy index)."""
    values: list = []
    for sample in parsed_samples:
        distance = sample.fields.get("distance")
        if distance is None and sample.indices:
            distance = _coerce_number(sample.indices[0])
        if distance is not None:
            values.append(distance)
    return values


def _sample_endpoints(parsed_samples: list, name: str) -> list:
    """Drag endpoints from parsed samples (shape fields, else legacy indices)."""
    points: list = []
    for sample in parsed_samples:
        point = sample.fields.get(name)
        if point is None and len(sample.indices) >= 4:
            pair = (0, 1) if name == "source" else (2, 3)
            point = _coerce_point(sample.indices[pair[0]:pair[1] + 1])
        if point is not None:
            points.append(point)
    return points
