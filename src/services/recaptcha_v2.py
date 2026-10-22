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
import re
import time
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from ..assets.proxy_pool import proxy_from_params
from ..models.task import AttemptContext
from .browser_solver import (
    BaseBrowserSolver,
    SolveStage,
    current_attempt,
)
from .captcha_errors import (
    CaptchaError,
    NonRetryableWidgetError,
    classify_widget_error,
)

log = logging.getLogger(__name__)


@dataclass
class _AudioMeter:
    """Vision-shaped usage accumulator so ``BaseBrowserSolver._record`` can meter
    the audio-transcription call (model / tokens / calls / latency) the same way
    it meters a vision solve — stored on the attempt context's ``vision`` field
    (mirrored to ``params["_vision"]`` at the attempt's release point)."""

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

# Invisible widgets skip the checkbox entirely: execute() is deferred, so the
# solver triggers it explicitly after the page has loaded.
_INVISIBLE_EXECUTE_JS = """
([key]) => new Promise((resolve, reject) => {
    const gr = window.grecaptcha?.enterprise || window.grecaptcha;
    if (!gr) { reject(new Error('grecaptcha not found')); return; }
    gr.ready(() => {
        gr.execute(key).then(resolve).catch(reject);
    });
})
"""

# Selector for the reCAPTCHA challenge bframe (the iframe that contains the
# visual/audio challenge once Google decides the bot needs to prove itself).
_CHALLENGE_IFRAME = 'iframe[title*="recaptcha challenge"]'
# The audio download link inside the bframe — used as the event-driven signal
# that the audio player has finished rendering (replaces a fixed sleep).
_AUDIO_DOWNLOAD_LINK = ".rc-audiochallenge-tdownload-link"

#: The anchor error slot Google uses for site-owner faults ("ERROR for site
#: owner: Invalid site key"). Reading it lets a bad sitekey fail fast instead of
#: burning every retry on a challenge that can never render.
_PROVIDER_ERROR_SELECTOR = ".rc-anchor-error-msg"
_CHECKBOX_IFRAME = 'iframe[title="reCAPTCHA"]'

#: The audio challenge is only ever served by Google's own reCAPTCHA hosts.
#: Anything else in the download link (a tampered challenge DOM, a redirect, a
#: compromised page) is refused rather than fetched from the server's network.
_AUDIO_HOST_SUFFIXES = ("google.com", "recaptcha.net", "gstatic.com")
#: Hard cap on the challenge audio download (Google's clips are tens of KB).
_AUDIO_MAX_BYTES = 5 * 1024 * 1024


def _host_matches_suffix(host: str, suffix: str) -> bool:
    return host == suffix or host.endswith("." + suffix)


def _validate_audio_url(url: str) -> str:
    """Refuse an audio challenge URL outside the reCAPTCHA provider hosts.

    The URL is read out of Google's challenge iframe, but the page could be a
    tampered/compromised one — treating it as trusted network input would let a
    target page pivot the solver into an SSRF fetch. https + an exact provider
    host suffix only.
    """
    parts = urlsplit(url)
    if parts.scheme.lower() != "https":
        raise NonRetryableWidgetError(
            f"reCAPTCHA audio URL must be https (got {parts.scheme or 'no scheme'!r})"
        )
    host = (parts.hostname or "").lower().rstrip(".")
    if not host or not any(
        _host_matches_suffix(host, suffix) for suffix in _AUDIO_HOST_SUFFIXES
    ):
        raise NonRetryableWidgetError(
            f"reCAPTCHA audio URL host {host!r} is not an allowed provider host"
        )
    return url


def _proxy_url_from_playwright(pw: Any) -> Optional[str]:
    """Build a ``scheme://user:pass@host:port`` URL from a Playwright proxy dict."""
    if not isinstance(pw, dict):
        return None
    server = pw.get("server")
    if not server:
        return None
    parts = urlsplit(str(server))
    if parts.scheme not in ("http", "https") or not parts.netloc:
        # socks* would need the optional httpx[socks] extra; skip rather than
        # fail the solve (the request can still be attempted directly).
        return None
    netloc = parts.netloc
    username = pw.get("username")
    if username:
        cred = quote(str(username), safe="")
        if pw.get("password"):
            cred += ":" + quote(str(pw["password"]), safe="")
        netloc = f"{cred}@{netloc}"
    return urlunsplit((parts.scheme, netloc, "", "", ""))


def _httpx_proxy_url(params: dict[str, Any]) -> Optional[str]:
    """The egress the audio download must use: the task proxy, else a pool one.

    The solve context is bound to this egress, and Google answers the audio
    payload consistently there; fetching it from the bare server IP both leaks
    that IP and can make the challenge round trip fail. Warm pool sessions only
    expose a credential-free gateway on ``_egress_server`` (unusable for a
    client fetch), so those keep the direct fetch.
    """
    task_asset = proxy_from_params(params)
    if task_asset is not None:
        url = _proxy_url_from_playwright(task_asset.playwright_proxy())
        if url:
            return url
    attempt = current_attempt()
    override = (
        attempt.proxy_override if attempt is not None else params.get("_proxy_override")
    )
    return _proxy_url_from_playwright(override)


def _classify_recaptcha_error(message: str) -> CaptchaError:
    """Map a reCAPTCHA site-owner error message to a classified error.

    ``invalid site key`` / ``invalid domain`` / ``malformed`` are request-level
    faults: no amount of retrying changes them, so they must surface
    immediately instead of burning the full retry budget (and hammering the
    egress). Unknown messages fall back to the shared classifier.
    """
    text = str(message or "").strip()
    lower = text.lower()
    # Normalise "site key" to "sitekey" so the shared classifier's
    # non-retryable signals match Google's wording.
    normalised = re.sub(r"\bsite\s*key\b", "sitekey", lower)
    if "malformed" in lower or any(
        signal in normalised
        for signal in ("invalid sitekey", "invalid domain", "bad request")
    ):
        return NonRetryableWidgetError(f"reCAPTCHA provider error: {text}")
    return classify_widget_error(normalised, provider="reCAPTCHA v2")


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

    #: Provider label used by the shared real-page template in logs/errors.
    PROVIDER_LABEL = "reCAPTCHA v2"

    async def _solve_once(
        self, website_url: str, website_key: str, is_invisible: bool, params: dict[str, Any]
    ) -> tuple[str, str]:
        """Thin wrapper over the shared real-page template.

        ``is_invisible`` / ``website_key`` are retained for API compatibility;
        the hooks read the same values from ``params["isInvisible"]`` /
        ``params["websiteKey"]`` (the only source ``solve`` used anyway).
        """
        return await self._solve_real_page_once(params, website_url)

    async def _prepare_page(
        self, page: Any, params: dict[str, Any], attempt: AttemptContext
    ) -> None:
        """Human-ish pointer settle before touching the widget."""
        await page.mouse.move(400, 300)
        await asyncio.sleep(0.5)

    async def _extract_token(
        self, page: Any, params: dict[str, Any], attempt: AttemptContext
    ) -> str | None:
        """Invisible widgets execute(); visible ones click the checkbox."""
        if params.get("isInvisible", False):
            return await page.evaluate(
                _INVISIBLE_EXECUTE_JS, [params["websiteKey"]]
            )
        return await self._solve_checkbox(page, params)

    async def _on_provider_error(
        self,
        page: Any,
        params: dict[str, Any],
        attempt: AttemptContext,
        exc: Exception,
    ) -> None:
        """Surface an anchor-level site-owner fault before a generic retry."""
        await self._raise_provider_error(page)

    async def _solve_checkbox(self, page: Any, params: dict[str, Any]) -> str | None:
        """Click the reCAPTCHA checkbox. If a visual challenge appears, try audio path."""
        # The checkbox iframe always has title="reCAPTCHA"
        checkbox_frame = page.frame_locator(_CHECKBOX_IFRAME).first
        checkbox = checkbox_frame.locator("#recaptcha-anchor")
        try:
            await checkbox.click(timeout=10_000)
        except Exception:
            # An anchor-level provider error (invalid sitekey / domain) means
            # the widget never renders; surface it now rather than burning the
            # full retry budget on a challenge that cannot appear.
            await self._raise_provider_error(page)
            raise

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

        # A site-owner fault is non-retryable — fail fast (classified) instead
        # of escalating to the audio path.
        await self._raise_provider_error(page)

        # Challenge dialog appeared — try audio challenge path
        log.info("reCAPTCHA challenge detected, attempting audio path")
        self._stamp_phase(params, SolveStage.CHALLENGE)
        try:
            token = await self._solve_audio_challenge(page, params)
        except CaptchaError:
            # Classified widget errors (rate limit, non-retryable sitekey /
            # malformed audio URL) must reach the retry loop's taxonomy instead
            # of being flattened into a generic, fully-retried miss.
            raise
        except Exception as exc:
            log.warning("Audio challenge path failed: %s", exc)
            token = None

        return token

    @staticmethod
    async def _provider_error_message(page: Any) -> Optional[str]:
        """Read Google's site-owner error slot, if the anchor shows one.

        ``count()`` is checked first so the normal (healthy widget) path has no
        timeout wait for an element that isn't there.
        """
        try:
            frame = page.frame_locator(_CHECKBOX_IFRAME).first
            locator = frame.locator(_PROVIDER_ERROR_SELECTOR)
            if await locator.count() == 0:
                return None
            message = await locator.first.inner_text(timeout=500)
        except Exception:  # noqa: BLE001 - probe must never fail a solve
            return None
        if not isinstance(message, str):
            return None
        message = message.strip()
        return message or None

    async def _raise_provider_error(self, page: Any) -> None:
        """Raise a classified error when the anchor reports a site-owner fault."""
        message = await self._provider_error_message(page)
        if message:
            raise _classify_recaptcha_error(message)

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

        # Download the audio file through the solve's own egress.
        audio_bytes = await self._fetch_audio(audio_src, params)

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

    async def _fetch_audio(
        self, audio_src: str, params: dict[str, Any]
    ) -> bytes:
        """Fetch the challenge audio: provider-host allowlist, no redirects, capped.

        The URL comes from inside Google's challenge iframe, which is
        third-party DOM from the perspective of this process. Fetching it
        blindly was an SSRF primitive (``audio_src`` could point at
        ``http://169.254.169.254/...``); the allowlist pins it to Google's
        reCAPTCHA hosts. Redirects are disabled (a 30x would otherwise bounce
        the allowlisted request to an internal host), the body is streamed with
        a hard 5 MB cap, and the request uses the task proxy when the solve has
        one so the download shares the solve's egress.
        """
        _validate_audio_url(audio_src)
        client_kwargs: dict[str, Any] = {
            "timeout": 30.0,
            "follow_redirects": False,
        }
        proxy_url = _httpx_proxy_url(params)
        if proxy_url:
            client_kwargs["proxy"] = proxy_url
        async with httpx.AsyncClient(**client_kwargs) as client:
            async with client.stream("GET", audio_src) as resp:
                if resp.is_redirect or 300 <= resp.status_code < 400:
                    raise NonRetryableWidgetError(
                        "reCAPTCHA audio download returned a redirect; refusing "
                        "to follow it"
                    )
                resp.raise_for_status()
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > _AUDIO_MAX_BYTES:
                        raise NonRetryableWidgetError(
                            "reCAPTCHA audio download exceeded the 5 MB cap"
                        )
                    chunks.append(chunk)
        if not chunks:
            raise RuntimeError("reCAPTCHA audio download was empty")
        return b"".join(chunks)

    async def _transcribe_audio(
        self, audio_bytes: bytes, params: dict[str, Any]
    ) -> str | None:
        """Transcribe the challenge audio through the shared ModelPool.

        Routes through ``services.model_pool`` so the call is (a) made against
        the proper speech-to-text endpoint (``audio.transcriptions``, not a
        chat call with the mp3 wedged into an ``image_url`` — which a text model
        can't decode), (b) bounded by the model concurrency semaphore, (c)
        budget-gated, and (d) metered: a usage accumulator is stashed on the
        attempt context (``_vision`` mirror) so the base ``_record`` attributes
        the cost to the cloud model in the ledger. Without wired services it
        falls back to a direct transcription call (still the correct endpoint).
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
            started = time.perf_counter()
            text, usage = await services.model_pool.cloud.transcribe_audio(
                audio_bytes,
                model=model,
                filename="challenge.mp3",
                timeout=float(getattr(self._config, "captcha_timeout", 30)),
            )
            # Meter it: a vision-shaped accumulator the shared _record reads.
            # It lives on the attempt context and is mirrored onto params at
            # the attempt's release point; a direct call outside the retry
            # loop (no live attempt) keeps the legacy params write.
            meter = _AudioMeter(
                last_model="cloud",
                total_input_tokens=usage.input_tokens,
                total_output_tokens=usage.output_tokens,
                total_vision_calls=1,
                total_vision_ms=int((time.perf_counter() - started) * 1000),
            )
            attempt = current_attempt()
            if attempt is not None:
                attempt.vision = meter
            else:
                params["_vision"] = meter
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
