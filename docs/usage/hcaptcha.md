# hCaptcha Usage

hCaptcha presents a CAPTCHA challenge via an iframe widget. The solver renders the widget for your sitekey (sitekey injection, served as the target origin), clicks the checkbox and, **when hCaptcha escalates to an image grid challenge, solves it with the configured vision model** — screenshotting each task tile, asking the model which tiles match the prompt, clicking them, and submitting. This is the path Stripe Radar / checkout hCaptcha typically requires, since a headless browser is almost always given a visual challenge.

## Supported task type

| Task type | Description |
|-----------|-------------|
| `HCaptchaTaskProxyless` | Browser-based hCaptcha solving with vision-model challenge solving |

## Required fields

| Field | Type | Description |
|-------|------|-------------|
| `websiteURL` | string | Full URL of the page containing the captcha |
| `websiteKey` | string | The `data-sitekey` value from the page's HTML |

## Optional fields

| Field | Type | Description |
|-------|------|-------------|
| `isInvisible` | bool | Render the widget as invisible and call `execute()` (passive flow). |
| `rqdata` | string | hCaptcha Enterprise `rqdata` nonce. Forwarded to `hcaptcha.render`; required by enterprise widgets that set it. |
| `userAgent` | string | Force a UA so the token matches your downstream submission. Echoed back in `solution.userAgent`. |
| `proxyType` / `proxyAddress` / `proxyPort` / `proxyLogin` / `proxyPassword` | | Solve through a proxy. Recommended for reputation-bound / IP-bound flows. |
| `proxy` | string | Single-string proxy form, e.g. `http://user:pass@host:port`. |

## Stripe note

Stripe's payment/checkout flows present hCaptcha (often enterprise). To solve reliably:

1. Pass the exact `websiteURL` that embeds the widget and the correct `websiteKey`.
2. Provide `rqdata` if Stripe's page sets it on the widget.
3. Bind a proxy + `userAgent` and reuse the returned `solution.userAgent` (and same IP) when you submit the token.
4. Quality of the grid-challenge solve depends on your configured vision model (`LOCAL_MODEL`).

## Test targets

hCaptcha provides official test keys that produce predictable results:

| URL | Site key | Behavior |
|-----|----------|----------|
| `https://accounts.hcaptcha.com/demo` | `10000000-ffff-ffff-ffff-000000000001` | Always passes (test key) |
| `https://accounts.hcaptcha.com/demo` | `20000000-ffff-ffff-ffff-000000000002` | Enterprise safe-user test |
| `https://demo.hcaptcha.com/` | `10000000-ffff-ffff-ffff-000000000001` | Always passes (test key) |

## Create a task

```bash
curl -X POST http://localhost:8000/createTask \
  -H "Content-Type: application/json" \
  -d '{
    "clientKey": "your-client-key",
    "task": {
      "type": "HCaptchaTaskProxyless",
      "websiteURL": "https://accounts.hcaptcha.com/demo",
      "websiteKey": "10000000-ffff-ffff-ffff-000000000001"
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
    "gRecaptchaResponse": "P1_eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9..."
  }
}
```

!!! note "Response field name"
    The token is returned in `solution.gRecaptchaResponse` for YesCaptcha API compatibility, even though hCaptcha natively uses the `h-captcha-response` field name.

## Acceptance status

| Target | Site key | Status | Notes |
|--------|----------|--------|-------|
| `https://accounts.hcaptcha.com/demo` | `10000000-ffff-ffff-ffff-000000000001` | ⚠️ Challenge-dependent | Headless browsers may still receive image challenges |

### Headless browser note

Even with the test site key (`10000000-ffff-ffff-ffff-000000000001`), hCaptcha may present an image challenge when the widget detects a headless browser. The solver clicks the checkbox and polls for a token for up to 30 seconds.

For headless environments, the recommended approach is to use the `HCaptchaClassification` task type to solve the image grid challenge, then inject the token. See [Image Classification](classification.md) for details.

## Image classification (HCaptchaClassification)

For programmatic grid classification without browser automation, see [Image Classification](classification.md).

## Operational notes

- hCaptcha challenges may require more time than reCAPTCHA v2 — the solver waits up to 5 seconds after clicking.
- Real-world sites with aggressive bot detection may require additional fingerprinting improvements.
- Test keys (`10000000-ffff-ffff-ffff-000000000001`) always pass and are useful for flow validation.
