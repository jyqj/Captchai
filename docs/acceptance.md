# Acceptance

This page documents acceptance targets for each supported captcha type, including the test URLs, site keys, and observed outcomes during local validation runs.

## Summary

| Captcha type | Target | Status |
|-------------|--------|--------|
| reCAPTCHA v3 | `https://antcpt.com/score_detector/` | ✅ Token returned |
| Cloudflare Turnstile | `https://react-turnstile.vercel.app/basic` | ✅ Dummy token returned |
| reCAPTCHA v2 | `https://www.google.com/recaptcha/api2/demo` | ⚠️ Requires audio challenge (see notes) |
| hCaptcha | `https://accounts.hcaptcha.com/demo` | ⚠️ Challenge-dependent |
| Image-to-Text | Local base64 image | ✅ Text returned via vision model |
| Classification | Local base64 grid | ✅ Object indices returned via vision model |

---

## reCAPTCHA v3 — Primary acceptance target

**URL:** `https://antcpt.com/score_detector/`  
**Site key:** `6LcR_okUAAAAAPYrPe-HK_0RULO1aZM15ENyM-Mf`

### Acceptance checklist

1. Install dependencies and Playwright Chromium.
2. Start the service: `python main.py`
3. Confirm `GET /api/v1/health` returns all 19 supported types.
4. Create a `RecaptchaV3TaskProxyless` task.
5. Poll `POST /getTaskResult` until `status=ready`.
6. Confirm a non-empty `solution.gRecaptchaResponse`.

### Curl example

```bash
TASK=$(curl -s -X POST http://localhost:8000/createTask \
  -H "Content-Type: application/json" \
  -d '{
    "clientKey": "your-key",
    "task": {
      "type": "RecaptchaV3TaskProxyless",
      "websiteURL": "https://antcpt.com/score_detector/",
      "websiteKey": "6LcR_okUAAAAAPYrPe-HK_0RULO1aZM15ENyM-Mf",
      "pageAction": "homepage"
    }
  }' | python -c "import sys,json; print(json.load(sys.stdin)['taskId'])")

curl -s -X POST http://localhost:8000/getTaskResult \
  -H "Content-Type: application/json" \
  -d '{"clientKey":"your-key","taskId":"'"$TASK"'"}'
```

### Verified outcome

- Service startup: ✅
- Health endpoint: ✅ (19 types registered)
- Task creation: ✅
- Token returned: ✅ (non-empty `gRecaptchaResponse`, length ~1060 chars)

---

## Cloudflare Turnstile

**URL:** `https://react-turnstile.vercel.app/basic`  
**Site key:** `1x00000000000000000000AA` (Cloudflare official test key — always passes)

### Curl example

```bash
curl -s -X POST http://localhost:8000/createTask \
  -H "Content-Type: application/json" \
  -d '{
    "clientKey": "your-key",
    "task": {
      "type": "TurnstileTaskProxyless",
      "websiteURL": "https://react-turnstile.vercel.app/basic",
      "websiteKey": "1x00000000000000000000AA"
    }
  }'
```

### Verified outcome

- Token returned: ✅ `XXXX.DUMMY.TOKEN.XXXX` (expected for Cloudflare test sitekeys)

---

## reCAPTCHA v2

**URL:** `https://www.google.com/recaptcha/api2/demo`  
**Site key:** `6Le-wvkSAAAAAPBMRTvw0Q4Muexq9bi0DJwx_mJ-`

### Behavior with headless Chromium

Headless browsers are detected by Google's risk analysis engine. The checkbox click succeeds, but a visual image challenge is presented rather than issuing a token immediately.

**Implemented mitigation:** The solver falls back to the **audio challenge path** — clicking the audio button in the challenge dialog, downloading the MP3 through the solve's egress, transcribing via the configured speech-to-text model, and submitting the transcript.

!!! note "Audio challenge transcription"
    The audio path uses `CLOUD_AUDIO_MODEL` (default `whisper-1`) on the `CLOUD_BASE_URL` endpoint via `audio.transcriptions`. `CLOUD_MODEL` is a chat model and is not used for audio. Accuracy depends on the configured endpoint. Audio URLs are restricted to `https` on Google-owned host suffixes, redirects are refused, and the download is capped at 5 MB.

### Curl example

```bash
curl -s -X POST http://localhost:8000/createTask \
  -H "Content-Type: application/json" \
  -d '{
    "clientKey": "your-key",
    "task": {
      "type": "NoCaptchaTaskProxyless",
      "websiteURL": "https://www.google.com/recaptcha/api2/demo",
      "websiteKey": "6Le-wvkSAAAAAPBMRTvw0Q4Muexq9bi0DJwx_mJ-"
    }
  }'
```

### Status

⚠️ Functionally implemented with audio challenge fallback. Success rate depends on model audio capability and Google's current challenge difficulty.

---

## hCaptcha

**URL:** `https://accounts.hcaptcha.com/demo`  
**Site key:** `10000000-ffff-ffff-ffff-000000000001` (hCaptcha official test key)

### Behavior

The hCaptcha test key (`10000000-ffff-ffff-ffff-000000000001`) is designed to always pass — but headless browsers detected as bots still receive an image challenge. The solver clicks the checkbox iframe and polls for a token for up to 30 seconds.

### Status

⚠️ Checkbox click succeeds. Token issuance depends on hCaptcha's bot detection score. For test environments, using the [HCaptchaClassification](usage/classification.md) task type (direct image classification) is the recommended integration path.

---

## Image-to-Text

Any base64-encoded image can be sent to `ImageToTextTask`. The vision model returns a structured description suitable for click/slide/drag_match captcha automation.

### Status

✅ Works with any OpenAI-compatible vision model endpoint. Accuracy depends on model capability.

---

## What these results mean

- ✅ **reCAPTCHA v3** and **Turnstile** are fully functional and pass in every local test run.
- ⚠️ **reCAPTCHA v2** and **hCaptcha** browser-based solving is limited by headless browser detection. These captcha types are primarily intended to be integrated with `HCaptchaClassification` / `ReCaptchaV2Classification` task types for image grid solving, or via audio challenge transcription.
- The service is designed as a **backend solver for flow2api** — in practice, real-world integrations extract the image challenge frames and send them to the classification endpoint, rather than relying on full browser automation to pass the widget.
