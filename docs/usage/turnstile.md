# Cloudflare Turnstile Usage

Cloudflare Turnstile is an invisible or widget-based CAPTCHA alternative. The solver visits the target page with Chromium, interacts with the Turnstile widget, and extracts the resulting token from a hidden `cf-turnstile-response` input field.

## Supported task types

| Task type | Description |
|-----------|-------------|
| `TurnstileTaskProxyless` | Standard Turnstile solving |
| `TurnstileTaskProxylessM1` | Same path, alternate tier naming |

## Required fields

| Field | Type | Description |
|-------|------|-------------|
| `websiteURL` | string | Full URL of the page containing the Turnstile widget |
| `websiteKey` | string | The Turnstile `data-sitekey` value |

## Optional fields

| Field | Type | Description |
|-------|------|-------------|
| `action` | string | The widget's configured `action`. Required if the site sets one — a token generated without the matching action is rejected. |
| `cData` | string | The widget's `cData` customer payload. Must match if configured. |
| `chlPageData` | string | Advanced `chlPageData` value for challenge-page widgets. |
| `userAgent` | string | Force a specific User-Agent so the token you submit downstream matches. If omitted, the solver picks one and returns it in `solution.userAgent`. |
| `proxyType` / `proxyAddress` / `proxyPort` / `proxyLogin` / `proxyPassword` | | Solve through a proxy. **Strongly recommended:** Cloudflare binds the token to the egress IP, so it must be generated from the same IP you submit from. |
| `proxy` | string | Alternative single-string proxy form, e.g. `http://user:pass@host:port`. |

## How solving works (sitekey injection)

The solver does **not** load the raw target page (which is often behind a Cloudflare interstitial that blocks headless Chromium before the widget renders). Instead it intercepts the top-level document request for `websiteURL` and serves a minimal page that renders the Turnstile widget for your `sitekey` — fulfilled *as* the target origin, so the token is bound to the correct hostname. `action` / `cData` / `chlPageData` are forwarded to `turnstile.render`.

> **Out of scope:** the full-page "Checking your browser…" managed challenge (the `cf_clearance` cookie flow) is a different mechanism from the Turnstile widget and is not solved here.

## Solution field

Unlike reCAPTCHA tasks, the result is returned in `solution.token` (not `solution.gRecaptchaResponse`). The `userAgent` used during solving is echoed back — submit the token with this exact UA and (if used) the same proxy IP:

```json
{
  "errorId": 0,
  "status": "ready",
  "solution": {
    "token": "0.ufq5RgSV...",
    "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ... Chrome/131.0.0.0 Safari/537.36"
  }
}
```

## Test targets

Cloudflare provides official dummy site keys for testing:

| Site key | Behavior | URL |
|----------|----------|-----|
| `1x00000000000000000000AA` | Always passes | Any domain |
| `2x00000000000000000000AB` | Always fails | Any domain |
| `3x00000000000000000000FF` | Forces interactive challenge | Any domain |

The React Turnstile demo is a good live test target:

- **URL:** `https://react-turnstile.vercel.app/basic`
- **Site key:** `1x00000000000000000000AA` (test key, always passes)

## Create a task

```bash
curl -X POST http://localhost:8000/createTask \
  -H "Content-Type: application/json" \
  -d '{
    "clientKey": "your-client-key",
    "task": {
      "type": "TurnstileTaskProxyless",
      "websiteURL": "https://react-turnstile.vercel.app/basic",
      "websiteKey": "1x00000000000000000000AA"
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

When ready:

```json
{
  "errorId": 0,
  "status": "ready",
  "solution": {
    "token": "XXXX.DUMMY.TOKEN.XXXX"
  }
}
```

!!! info "Dummy token"
    Cloudflare test keys (`1x00000000000000000000AA`) return the dummy token `XXXX.DUMMY.TOKEN.XXXX`. This is the expected and correct behavior for test sitekeys — the token is accepted by Cloudflare's test infrastructure.

## Acceptance status

| Target | Site key | Status |
|--------|----------|--------|
| `https://react-turnstile.vercel.app/basic` | `1x00000000000000000000AA` | ✅ Dummy token returned |

## Operational notes

- Turnstile auto-solves most of the time without user interaction; the solver polls for the token after page load.
- Real production sitekeys will return a real token (not the dummy token).
- The `TurnstileTaskProxylessM1` type uses the same implementation path.
