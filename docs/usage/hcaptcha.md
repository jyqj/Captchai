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
| `enterprisePayload` | object | Enterprise render config (`rqdata`, `sentry`, `endpoint`, `reportapi`, `assethost`, `imghost`, …). Its keys are **spread flat** onto `hcaptcha.render`, matching the JS API. If you send `rqdata` here (the YesCaptcha convention) it reaches the widget — no separate top-level `rqdata` needed. |
| `realPage` | bool | Override the page strategy for this task. `false` (enterprise default) serves a synthetic page at the target hostname (token-relay). `true` navigates the real target and hooks `hcaptcha.render` (runs the site's own anti-bot JS). Defaults to `HCAPTCHA_REAL_PAGE`. |
| `userAgent` | string | Force a UA so the token matches your downstream submission. Echoed back in `solution.userAgent`. |
| `egress` | string | `auto` (default) / `task` / `pool` / `proxyless`. For enterprise/Stripe use `task` with your own residential proxy (see Stripe note). |
| `proxyType` / `proxyAddress` / `proxyPort` / `proxyLogin` / `proxyPassword` | | Solve through a proxy. Recommended for reputation-bound / IP-bound flows. |
| `proxy` | string | Single-string proxy form, e.g. `http://user:pass@host:port`. Optional `\|kind=residential` / `\|country=DE` suffixes annotate the proxy. |

## Stripe note

Stripe's payment/checkout flows present hCaptcha Enterprise (Radar). Enterprise is detected automatically from `rqdata` / `enterprisePayload`. To solve reliably:

1. Pass the exact `websiteURL` (correct **hostname** — the widget binds the token to `document.location.hostname`) and the correct `websiteKey`.
2. Capture a **fresh** `rqdata` per challenge from Stripe's own page (it's single-use and session-bound) and pass it (top-level or inside `enterprisePayload`). A stale/missing `rqdata` mints a token Radar rejects even if the grid is answered correctly.
3. **Match the egress IP.** Enterprise scores IP consistency, so the token must be minted through the **same IP** you submit the card-binding request from. Use `egress=task` with your **own residential/mobile proxy** and reuse that proxy for the Stripe call. Reuse `solution.userAgent` and (`solution.timezoneId` / `acceptLanguage`) too.
    - If you rely on the server-side pool (`egress=pool`), the minted IP is a server proxy you can't reach — set `POOL_EGRESS_EXPOSE_CREDENTIALS=true` to receive a reusable credentialed `egressServer`, otherwise the token's IP won't match your submit and Radar is likely to reject it.
4. Enterprise defaults to the **injected page** (token-relay) rather than navigating the real Stripe URL, which usually can't be reproduced from a clean context. Set `realPage: true` per task only if a specific site's own JS must run.
5. Quality of any grid/area challenge solve depends on your configured vision model (`LOCAL_MODEL` / `CLOUD_MODEL`).

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
