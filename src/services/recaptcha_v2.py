"""reCAPTCHA v2 solver using Playwright browser automation.

Supports NoCaptchaTaskProxyless, RecaptchaV2TaskProxyless,
and RecaptchaV2EnterpriseTaskProxyless task types.

Strategy:
  1. Visit the target page with a realistic browser context.
  2. Click the reCAPTCHA checkbox.
  3. If the challenge dialog appears (bot detected), switch to the audio
     challenge, download the audio file, transcribe it via the configured
     speech-to-text model, and submit the text.
  4. Extract the gRecaptchaResponse token.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .browser_solver import BaseBrowserSolver

log = logging.getLogger(__name__)


@dataclass
class _AudioMeter:
    """Vision-shaped usage accumulator so ``BaseBrowserSolver._record`` can meter
    the audio-transcription call (model / tokens / calls / latency) the same way
    it meters a vision solve — stashed on ``params["_vision"]``."""

    last_model: str | None = None
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_vision_calls: int = 0
    total_vision_ms: int = 0

_EXTRACT_TOKEN_JS = """
() => {
    const textarea = document.querySelector('#g-recaptcha-response')
        || document.querySelector('[name="g-recaptcha-response"]');
    if (textarea && textarea.value && textarea.value.length > 20) {
        return textarea.value;
    }
    const gr = window.grecaptcha?.enterprise || window.grecaptcha;
    if (gr && typeof gr.getResponse === 'function') {
        const resp = gr.getResponse();
        if (resp && resp.length > 20) return resp;
    }
    return null;
}
"""

# Selector for the reCAPTCHA challenge bframe (the iframe that contains the
# visual/audio challenge once Google decides the bot needs to prove itself).
_CHALLENGE_IFRAME = 'iframe[title*="recaptcha challenge"]'
# The audio download link inside the bframe — used as the event-driven signal
# that the audio player has finished rendering (replaces a fixed sleep).
_AUDIO_DOWNLOAD_LINK = ".rc-audiochallenge-tdownload-link"


class RecaptchaV2Solver(BaseBrowserSolver):
    """Solves reCAPTCHA v2 tasks via headless Chromium with checkbox clicking.

    Falls back to the audio challenge path when Google presents a visual
    challenge to the headless browser. Context acquisition / release and
    proxy categorisation are handled by :class:`BaseBrowserSolver`.
    """

    async def solve(self, params: dict[str, Any]) -> dict[str, Any]:
        website_url = params["websiteURL"]
        website_key = params["websiteKey"]
        is_invisible = params.get("isInvisible", False)
        client_key = params.get("_clientKey")

        # reCAPTCHA v2 uses the standard auto egress selection (task proxy →
        # pool → proxyless); only set when the caller left it unspecified.
        params.setdefault("egress", "auto")

        return await self._solve_with_retries(
            params,
            sitekey=website_key,
            client_key=client_key,
            attempt_fn=lambda: self._solve_once(
                website_url, website_key, is_invisible, params
            ),
            build_solution=lambda token, ua: {
                "gRecaptchaResponse": token,
                "userAgent": ua,
            },
            provider="reCAPTCHA v2",
            default_task_type=params.get("type", "RecaptchaV2TaskProxyless"),
            default_challenge_shape="audio",
            verify_provider="recaptcha",
        )

    async def _solve_once(
        self, website_url: str, website_key: str, is_invisible: bool, params: dict[str, Any]
    ) -> tuple[str, str]:
        solve_context = await self._acquire_context(params)
        self._stash_fingerprint_geo(solve_context, params)
        context = solve_context.context
        user_agent = solve_context.user_agent
        page = await context.new_page()

        solved = False
        try:
            timeout_ms = self._config.browser_timeout * 1000
            await page.goto(
                website_url, wait_until="domcontentloaded", timeout=timeout_ms
            )
            await page.mouse.move(400, 300)
            await asyncio.sleep(0.5)

            if is_invisible:
                token = await page.evaluate(
                    """
                    ([key]) => new Promise((resolve, reject) => {
                        const gr = window.grecaptcha?.enterprise || window.grecaptcha;
                        if (!gr) { reject(new Error('grecaptcha not found')); return; }
                        gr.ready(() => {
                            gr.execute(key).then(resolve).catch(reject);
                        });
                    })
                    """,
                    [website_key],
                )
            else:
                token = await self._solve_checkbox(page, params)

            if not isinstance(token, str) or len(token) < 20:
                raise RuntimeError(f"Invalid reCAPTCHA v2 token: {token!r}")

            log.info("Got reCAPTCHA v2 token (len=%d)", len(token))
            solved = True
            return token, user_agent
        finally:
            await self._release_context(solve_context, solved, params)

    async def _solve_checkbox(self, page: Any, params: dict[str, Any]) -> str | None:
        """Click the reCAPTCHA checkbox. If a visual challenge appears, try audio path."""
        # The checkbox iframe always has title="reCAPTCHA"
        checkbox_frame = page.frame_locator('iframe[title="reCAPTCHA"]').first
        checkbox = checkbox_frame.locator("#recaptcha-anchor")
        await checkbox.click(timeout=10_000)

        # Wait for the challenge bframe to appear (or a token to be issued
        # immediately for low-risk sessions). Bounded; replaces a fixed 2s sleep.
        try:
            await page.wait_for_selector(_CHALLENGE_IFRAME, timeout=4_000)
        except Exception:
            pass

        # Check if token was issued immediately (low-risk sessions)
        token = await page.evaluate(_EXTRACT_TOKEN_JS)
        if isinstance(token, str) and len(token) > 20:
            return token

        # Challenge dialog appeared — try audio challenge path
        log.info("reCAPTCHA challenge detected, attempting audio path")
        try:
            token = await self._solve_audio_challenge(page, params)
        except Exception as exc:
            log.warning("Audio challenge path failed: %s", exc)
            token = None

        return token

    async def _solve_audio_challenge(self, page: Any, params: dict[str, Any]) -> str | None:
        """Click the audio button in the bframe and transcribe the audio."""
        # Click the audio challenge button
        bframe = page.frame_locator(_CHALLENGE_IFRAME)
        audio_btn = bframe.locator("#recaptcha-audio-button")
        await audio_btn.click(timeout=8_000)

        # Wait for the audio download link to render inside the bframe
        # (bounded; replaces a fixed 3s sleep). The locator's wait_for returns
        # the instant the link exists in the iframe's DOM.
        bframe = page.frame_locator(_CHALLENGE_IFRAME)
        try:
            await bframe.locator(_AUDIO_DOWNLOAD_LINK).wait_for(timeout=6_000)
        except Exception:
            # Fall back to a short grace period if the wait_for times out —
            # the bframe might still be loading the audio player.
            await asyncio.sleep(1)

        # Get the audio source URL — try multiple selectors
        audio_src = None
        for selector in [
            ".rc-audiochallenge-tdownload-link",
            "a[href*='.mp3']",
            "audio source",
        ]:
            try:
                element = bframe.locator(selector).first
                audio_src = await element.get_attribute("href", timeout=5_000) or await element.get_attribute("src", timeout=1_000)
                if audio_src:
                    break
            except Exception:
                continue

        if not audio_src:
            raise RuntimeError("Could not find audio challenge download link")

        # Download the audio file
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(audio_src)
            resp.raise_for_status()
            audio_bytes = resp.content

        # Transcribe via the shared model pool (metered + budget-gated).
        transcript = await self._transcribe_audio(audio_bytes, params)
        log.info("Audio transcribed: %r", transcript[:40] if transcript else None)

        if not transcript:
            raise RuntimeError("Audio transcription returned empty result")

        # Submit the transcript
        audio_input = bframe.locator("#audio-response")
        await audio_input.fill(transcript.strip().lower())
        verify_btn = bframe.locator("#recaptcha-verify-button")
        await verify_btn.click(timeout=8_000)
        await asyncio.sleep(2)

        return await page.evaluate(_EXTRACT_TOKEN_JS)

    async def _transcribe_audio(
        self, audio_bytes: bytes, params: dict[str, Any]
    ) -> str | None:
        """Transcribe the challenge audio through the shared ModelPool.

        Routes through ``services.model_pool`` so the call is (a) made against
        the proper speech-to-text endpoint (``audio.transcriptions``, not a
        chat call with the mp3 wedged into an ``image_url`` — which a text model
        can't decode), (b) bounded by the model concurrency semaphore, (c)
        budget-gated, and (d) metered: a usage accumulator is stashed on
        ``params["_vision"]`` so the base ``_record`` attributes the cost to the
        cloud model in the ledger. Without wired services it falls back to a
        direct transcription call (still the correct endpoint).
        """
        model = getattr(self._config, "cloud_audio_model", "whisper-1")
        services = self._services

        if services is not None and getattr(services, "model_pool", None) is not None:
            # Budget gate for the paid cloud transcription (rough per-clip est).
            budget = getattr(services, "budget", None)
            client_key = params.get("_clientKey")
            if budget is not None:
                decision = await budget.check(client_key, 0.006, model="cloud")
                if decision is not None and not getattr(decision, "allowed", True):
                    raise RuntimeError(
                        "audio transcription denied by budget cap"
                    )
            started = time.monotonic()
            text, usage = await services.model_pool.cloud.transcribe_audio(
                audio_bytes,
                model=model,
                filename="challenge.mp3",
                timeout=float(getattr(self._config, "captcha_timeout", 30)),
            )
            # Meter it: stash a vision-shaped accumulator the base _record reads.
            params["_vision"] = _AudioMeter(
                last_model="cloud",
                total_input_tokens=usage.input_tokens,
                total_output_tokens=usage.output_tokens,
                total_vision_calls=1,
                total_vision_ms=int((time.monotonic() - started) * 1000),
            )
            return text or None

        # No wired services (e.g. standalone use): call the transcription
        # endpoint directly. Still the correct endpoint — not a chat/image hack.
        try:
            from openai import AsyncOpenAI  # noqa: WPS433 - optional at runtime
        except Exception as exc:  # pragma: no cover - openai is in requirements
            raise RuntimeError("openai package required for audio transcription") from exc
        client = AsyncOpenAI(
            base_url=self._config.cloud_base_url, api_key=self._config.cloud_api_key
        )
        resp = await client.audio.transcriptions.create(
            model=model, file=("challenge.mp3", audio_bytes)
        )
        text = getattr(resp, "text", None)
        return (text or "").strip() or None
