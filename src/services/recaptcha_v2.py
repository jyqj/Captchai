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
from typing import Any

import httpx

from .browser_solver import (
    BaseBrowserSolver,
    egress_from_params,
    fingerprint_geo_from_params,
)

log = logging.getLogger(__name__)

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

        last_error: Exception | None = None
        for attempt in range(self._config.captcha_retries):
            started = time.monotonic()
            try:
                token, user_agent = await self._solve_once(
                    website_url, website_key, is_invisible, params
                )
                await self._record(
                    params,
                    website_key,
                    client_key,
                    "ready",
                    started,
                    task_type=params.get("type", "RecaptchaV2TaskProxyless"),
                    challenge_shape="audio",
                )
                tz, accept = fingerprint_geo_from_params(params)
                return {
                    "gRecaptchaResponse": token,
                    "userAgent": user_agent,
                    "timezoneId": tz,
                    "acceptLanguage": accept,
                    **egress_from_params(params),
                }
            except Exception as exc:
                last_error = exc
                await self._record(
                    params,
                    website_key,
                    client_key,
                    "failed",
                    started,
                    task_type=params.get("type", "RecaptchaV2TaskProxyless"),
                    challenge_shape="audio",
                )
                log.warning(
                    "reCAPTCHA v2 attempt %d/%d failed: %s",
                    attempt + 1,
                    self._config.captcha_retries,
                    exc,
                )
                if attempt < self._config.captcha_retries - 1:
                    await asyncio.sleep(2)

        raise RuntimeError(
            f"reCAPTCHA v2 failed after {self._config.captcha_retries} attempts: {last_error}"
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
                token = await self._solve_checkbox(page)

            if not isinstance(token, str) or len(token) < 20:
                raise RuntimeError(f"Invalid reCAPTCHA v2 token: {token!r}")

            log.info("Got reCAPTCHA v2 token (len=%d)", len(token))
            solved = True
            return token, user_agent
        finally:
            await self._release_context(solve_context, solved, params)

    async def _solve_checkbox(self, page: Any) -> str | None:
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
            token = await self._solve_audio_challenge(page)
        except Exception as exc:
            log.warning("Audio challenge path failed: %s", exc)
            token = None

        return token

    async def _solve_audio_challenge(self, page: Any) -> str | None:
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

        # Transcribe via the vision/language model (base64 audio → text)
        transcript = await self._transcribe_audio(audio_bytes)
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

    async def _transcribe_audio(self, audio_bytes: bytes) -> str | None:
        """Send audio bytes to the OpenAI-compatible audio transcription endpoint."""
        import base64

        audio_b64 = base64.b64encode(audio_bytes).decode()
        payload = {
            "model": self._config.captcha_model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "This is a reCAPTCHA audio challenge. "
                                "The audio contains spoken digits or words. "
                                "Transcribe exactly what is spoken, digits only, "
                                "separated by spaces. Reply with only the transcription."
                            ),
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:audio/mp3;base64,{audio_b64}"},
                        },
                    ],
                }
            ],
            "max_tokens": 50,
            "temperature": 0,
        }

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self._config.captcha_base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._config.captcha_api_key}"},
                json=payload,
            )
            if resp.status_code != 200:
                raise RuntimeError(f"Transcription API error {resp.status_code}: {resp.text[:200]}")
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()
