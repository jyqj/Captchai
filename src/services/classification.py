"""Image classification solvers for various captcha types.

Supports HCaptchaClassification, ReCaptchaV2Classification,
FunCaptchaClassification, and AwsClassification task types.

All classification tasks send images + question text to an OpenAI-compatible
vision model for analysis and return structured coordinate/index results.
"""

from __future__ import annotations

import asyncio
import base64
import io
from typing import Any

from PIL import Image

from ..parsing.model_call import ModelCallRequest
from .vision_solver import VisionSolveBase, parse_json_object

HCAPTCHA_SYSTEM_PROMPT = """\
You are an image classification assistant for HCaptcha challenges.
Given a question and one or more base64-encoded images, determine which images match the question.

Return STRICT JSON only. No markdown, no extra text.

For single-image questions (is this image X?):
{"answer": true}  or  {"answer": false}

For multi-image grid questions (select all images containing X):
{"answer": [0, 2, 5]}
where numbers are 0-indexed positions of matching images.

Rules:
- Return ONLY the JSON object, nothing else.
- Be precise with your classification.
"""

RECAPTCHA_V2_SYSTEM_PROMPT = """\
You are an image classification assistant for reCAPTCHA v2 challenges.
Given a question and a grid image (3x3 or 4x4), identify which cells match the question.

The image cells are numbered 0-8 (3x3) or 0-15 (4x4), left-to-right, top-to-bottom.

Return STRICT JSON only:
{"objects": [0, 3, 6]}
where numbers are 0-indexed positions of matching cells.

Rules:
- Return ONLY the JSON object, nothing else.
- If no cells match, return {"objects": []}.
"""

FUNCAPTCHA_SYSTEM_PROMPT = """\
You are an image classification assistant for FunCaptcha challenges.
Given a question and a grid image (typically 2x3 = 6 cells), identify which cell
is the correct answer.

Cells are numbered 0-5, left-to-right, top-to-bottom.

Return STRICT JSON only:
{"objects": [3]}
where the number is the 0-indexed position of the correct cell.

Rules:
- Return ONLY the JSON object, nothing else.
- Usually only one cell is correct.
"""

AWS_SYSTEM_PROMPT = """\
You are an image classification assistant for AWS CAPTCHA challenges.
Given a question and one or more images, identify the correct answer.

Return STRICT JSON only:
{"objects": [1]}
where the number is the 0-indexed position of the matching image.

Rules:
- Return ONLY the JSON object, nothing else.
"""


class ClassificationSolver(VisionSolveBase):
    """Solves image classification captchas — a thin adapter over the seam.

    Supplies only the per-task prompt corpus, image extraction and the
    ``{answer|objects}`` output parsing; the model client, budget gate,
    connection-error backend fallback and cost metering are inherited from
    :class:`~src.services.vision_solver.VisionSolveBase`.
    """

    async def solve(self, params: dict[str, Any]) -> dict[str, Any]:
        task_type = params.get("type", "")
        system_prompt = self._get_system_prompt(task_type)
        question = params.get("question", "") or params.get("queries", "")

        # Handle different image field names across task types
        images = self._extract_images(params)
        if not images:
            raise ValueError("No image data provided")

        # PIL decode/format sniffing is CPU-bound; keep it off the event loop.
        image_urls = await asyncio.gather(
            *(asyncio.to_thread(self._prepare_image, img_b64) for img_b64 in images)
        )
        req = ModelCallRequest(
            system_prompt=system_prompt,
            user_text=question if question else "Classify this captcha image.",
            image_urls=list(image_urls),
            tier=1,
            detail="high",
            temperature=0.05,
            max_tokens=512,
            est_cost=0.002 * max(1, len(image_urls)),
        )
        # Parsing runs INSIDE the retried + metered unit: a malformed reply is
        # retried (and metered on every attempt) rather than escaping after the
        # model call already succeeded.
        return await self._invoke(
            req,
            params,
            challenge_shape="classification",
            parse=lambda result: self._normalize_answer(
                task_type, parse_json_object(result.content)
            ),
        )

    @classmethod
    def _normalize_answer(
        cls, task_type: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        """Validate a parsed model reply into the documented solution shape.

        The API contract is ``{"answer": bool | list[int]}`` for
        ``HCaptchaClassification`` and ``{"objects": list[int]}`` for the grid
        adapters (``ReCaptchaV2Classification`` / ``FunCaptchaClassification`` /
        ``AwsClassification``). Returning the raw model JSON let arbitrary keys
        and wrong types reach ``SolutionObject`` (a validation 500) — or leak
        model chatter into the caller's solution. Anything that cannot be
        normalised raises ``ValueError``, which surfaces as
        ``ERROR_CAPTCHA_UNSOLVABLE`` after the retry budget.
        """
        if not isinstance(data, dict):
            raise ValueError(
                f"model reply must be a JSON object, got {type(data).__name__}"
            )
        # ``HCaptchaClassification`` documents BOTH fields ("objects or
        # answer", including the single-image boolean form); the grid adapters
        # document ``objects`` only, so an ``answer`` list is mapped onto it.
        is_hcaptcha = task_type == "HCaptchaClassification"
        if "objects" in data or (not is_hcaptcha and "answer" in data):
            objects = data.get("objects", data.get("answer"))
            if isinstance(objects, int) and not isinstance(objects, bool):
                objects = [objects]
            if isinstance(objects, list):
                return {"objects": cls._normalize_indices(objects)}
            raise ValueError(
                f"'objects' must be a list of indices, got {objects!r}"
            )
        if "answer" in data:
            answer = data["answer"]
            if isinstance(answer, bool):
                return {"answer": answer}
            if isinstance(answer, int) and not isinstance(answer, bool):
                answer = [answer]
            if isinstance(answer, list):
                return {"answer": cls._normalize_indices(answer)}
            raise ValueError(
                f"'answer' must be a boolean or a list of indices, got {answer!r}"
            )
        raise ValueError(
            f"model reply has neither 'answer' nor 'objects': {sorted(data)[:5]}"
        )

    @staticmethod
    def _normalize_indices(values: list[Any]) -> list[int]:
        """Validate cell indices: non-negative ints only, de-duplicated in order."""
        indices: list[int] = []
        for value in values:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"cell index must be an integer, got {value!r}")
            if value < 0 or value > 4095:
                raise ValueError(f"cell index out of range: {value!r}")
            if value not in indices:
                indices.append(value)
        return indices

    @staticmethod
    def _get_system_prompt(task_type: str) -> str:
        prompts = {
            "HCaptchaClassification": HCAPTCHA_SYSTEM_PROMPT,
            "ReCaptchaV2Classification": RECAPTCHA_V2_SYSTEM_PROMPT,
            "FunCaptchaClassification": FUNCAPTCHA_SYSTEM_PROMPT,
            "AwsClassification": AWS_SYSTEM_PROMPT,
        }
        return prompts.get(task_type, RECAPTCHA_V2_SYSTEM_PROMPT)

    @staticmethod
    def _extract_images(params: dict[str, Any]) -> list[str]:
        """Extract base64 image(s) from various param formats."""
        images: list[str] = []

        if "image" in params:
            images.append(params["image"])

        if "images" in params:
            imgs = params["images"]
            if isinstance(imgs, list):
                images.extend(imgs)
            elif isinstance(imgs, str):
                images.append(imgs)

        if "body" in params and not images:
            images.append(params["body"])

        # HCaptcha queries format: list of base64 strings
        if "queries" in params and isinstance(params["queries"], list):
            images.extend(params["queries"])

        return images

    @staticmethod
    def _prepare_image(b64_data: str) -> str:
        """Ensure image is properly formatted as a data URL."""
        if b64_data.startswith("data:image"):
            return b64_data
        try:
            img_bytes = base64.b64decode(b64_data)
            img = Image.open(io.BytesIO(img_bytes))
            fmt = img.format or "PNG"
            mime = f"image/{fmt.lower()}"
            return f"data:{mime};base64,{b64_data}"
        except Exception:
            return f"data:image/png;base64,{b64_data}"
