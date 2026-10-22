# reCAPTCHA v2 Usage

reCAPTCHA v2 presents users with an "I'm not a robot" checkbox. The solver visits the target page with a real browser, clicks the checkbox, and extracts the resulting `gRecaptchaResponse` token. When Google escalates to a challenge, the solver uses the audio challenge and transcribes it with a speech-to-text model.

## Supported task types

| Task type | Description |
|-----------|-------------|
| `NoCaptchaTaskProxyless` | Standard reCAPTCHA v2 checkbox |
| `RecaptchaV2TaskProxyless` | Same as above, alternate naming |
| `RecaptchaV2EnterpriseTaskProxyless` | reCAPTCHA v2 Enterprise variant |

## Required fields

| Field | Type | Description |
|-------|------|-------------|
| `websiteURL` | string | Full URL of the page containing the captcha |
| `websiteKey` | string | The `data-sitekey` value from the page's HTML |

## Optional fields

| Field | Type | Description |
|-------|------|-------------|
| `isInvisible` | bool | Set `true` for invisible reCAPTCHA; the solver calls `grecaptcha.execute()` instead of clicking a checkbox |
| `userAgent` | string | Force a UA so the token matches your downstream submission; echoed in `solution.userAgent` |
| `proxyType` / `proxyAddress` / `proxyPort` / `proxyLogin` / `proxyPassword` | | Solve through a proxy (IP-bound tokens) |
| `proxy` | string | Single-string form, e.g. `http://user:pass@host:port` |
| `egress` | string | `auto` (default), `proxyless`, `pool`, or `task` |

## SSRF and URL validation

Before a proxy or browser context is checked out, `websiteURL` must pass a public-URL pre-flight: scheme `http`/`https`, hostname resolvable, and every resolved address public. Loopback, RFC1918/ULA private, link-local (including `169.254.169.254`), reserved, multicast, and unspecified addresses are rejected. reCAPTCHA v2 always runs this check (it is not gated by `SSRF_PROTECT`). A rejected URL surfaces as `ERROR_CAPTCHA_UNSOLVABLE`.

## Test target

The official Google demo page is suitable for acceptance validation:

- **URL:** `https://www.google.com/recaptcha/api2/demo`
- **Site key:** `6Le-wvkSAAAAAPBMRTvw0Q4Muexq9bi0DJwx_mJ-`

## Create a task

```bash
curl -X POST http://localhost:8000/createTask \
  -H "Content-Type: application/json" \
  -d '{
    "clientKey": "your-client-key",
    "task": {
      "type": "NoCaptchaTaskProxyless",
      "websiteURL": "https://www.google.com/recaptcha/api2/demo",
      "websiteKey": "6Le-wvkSAAAAAPBMRTvw0Q4Muexq9bi0DJwx_mJ-"
    }
  }'
```

Response:

```json
{
  "errorId": 0,
  "taskId": "uuid-string"
}
```

## Poll for result

```bash
curl -X POST http://localhost:8000/getTaskResult \
  -H "Content-Type: application/json" \
  -d '{
    "clientKey": "your-client-key",
    "taskId": "uuid-from-createTask"
  }'
```

When ready, you receive:

```json
{
  "errorId": 0,
  "status": "ready",
  "solution": {
    "gRecaptchaResponse": "03AGdBq24..."
  }
}
```

## Invisible reCAPTCHA

For pages using invisible reCAPTCHA (no visible checkbox), add `"isInvisible": true`. The solver calls `grecaptcha.execute()` directly instead of clicking the checkbox.

## Audio challenge path

Google's risk analysis often detects headless browsers and presents a visual image challenge instead of issuing a token. The reCAPTCHA v2 solver then falls back to the audio challenge:

1. Click the checkbox; Google presents a challenge dialog.
2. Click the audio button in the challenge dialog.
3. Download the MP3 audio file.
4. Transcribe it with the **cloud speech-to-text model** configured by `CLOUD_AUDIO_MODEL` (default `whisper-1`) through the OpenAI-compatible `audio.transcriptions` endpoint.
5. Submit the transcript to receive the token.

!!! warning "Audio transcription requirement"
    The audio path uses `CLOUD_AUDIO_MODEL` — a Whisper-family speech-to-text model on `CLOUD_BASE_URL` with `CLOUD_API_KEY`. `CLOUD_MODEL` is a chat model and is **not** used for transcription. The call is budget-gated and metered like other paid model calls; accuracy and availability depend on the configured endpoint.

### Audio download safety

The audio URL is read from Google's challenge iframe, which is third-party DOM from this process's perspective, so it is treated as untrusted:

- URL must be `https` and the host must be `google.com`, `recaptcha.net`, or `gstatic.com` (or a subdomain of one of those);
- redirects are refused;
- the body is streamed with a hard 5 MB cap;
- when the solve has a task proxy, the download goes through it (`http`/`https` proxy servers only; SOCKS proxy URLs are skipped rather than failing the solve), so the audio fetch shares the solve egress.

## Recommended integration path

For reliable reCAPTCHA v2 solving in production, consider using the **classification task** approach:

1. Extract the challenge image grid from the page using Playwright.
2. Send the grid image to `RecaptchaV2Classification` with the challenge question.
3. Use the returned cell indices to programmatically click the matching tiles.

See [Image Classification](classification.md) for details.

## Operational notes

- Token validity is approximately 120 seconds; submit promptly.
- The `RecaptchaV2EnterpriseTaskProxyless` type uses the same browser path.
- On less aggressive sites (not Google's own demo), the checkbox click may succeed without triggering a challenge.
- `isInvisible`, `userAgent`, proxy fields, and `egress` are documented in the [API reference](../api-reference.md).
