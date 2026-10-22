# API Reference

## Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/createTask` | Submit a captcha task |
| POST | `/getTaskResult` | Poll for a result |
| POST | `/getBalance` | Starting credit minus ledger spend |
| POST | `/reportCorrect` | Report that a returned token was accepted downstream |
| POST | `/reportIncorrect` | Report that a returned token was rejected downstream |
| GET | `/admin/metrics` | Cost summary from the ledger |
| GET | `/admin/proxies` | Proxy-pool and warm-session snapshot |
| GET | `/api/v1/health` | Liveness + runtime details (always 200) |
| GET | `/ready` | Readiness probe (200 ready / 503 not ready) |
| GET | `/` | Service description and registered task types |

All task endpoints are JSON-based and follow the YesCaptcha-style async task pattern. Errors are normally reported in the JSON body with `errorId: 1`; the transport status stays **200** unless noted. `HTTPException` (404/405) keeps FastAPI's own handling.

A per-client-IP rate limiter (`RATE_LIMIT_RPM`, default 60) may answer any non-health path with:

```json
{"errorId": 1, "errorCode": "ERROR_RATE_LIMITED", "errorDescription": "Rate limit exceeded; retry after 1.4s"}
```

with a `Retry-After` header. `/api/v1/health` is exempt; `/ready` is not.

---

## Authentication

- Task and report endpoints check `clientKey` against `CLIENT_KEY` when it is configured, using a constant-time comparison. A mismatch returns `ERROR_KEY_DOES_NOT_EXIST`.
- With no `CLIENT_KEY`, any `clientKey` string (including empty) is accepted. Task ownership is still enforced: `getTaskResult` compares a SHA-256 hash of the presented key with the `clientKeyHash` persisted on the task record (falling back to the in-memory `_clientKey` for pre-upgrade tasks), and the report endpoints compare it with the `client_key` in the ledger record. A mismatch returns `ERROR_NO_SUCH_CAPCHA_ID` (so task existence is not leaked).
- `/admin/*` auth is described in [Admin endpoints](#admin-endpoints).
- The service refuses to start keyless on a non-loopback `SERVER_HOST` unless `ALLOW_ANONYMOUS=true`.

---

## `POST /createTask`

### Request shape

```json
{
  "clientKey": "your-client-key",
  "idempotencyKey": "optional-retry-key",
  "task": {
    "type": "RecaptchaV3TaskProxyless",
    "websiteURL": "https://antcpt.com/score_detector/",
    "websiteKey": "6LcR_okUAAAAAPYrPe-HK_0RULO1aZM15ENyM-Mf",
    "pageAction": "homepage"
  }
}
```

`clientKey` is required (max 128 chars). `idempotencyKey` is optional (max 128 chars). The `task` object rejects unknown properties (`extra="forbid"`), so a typo is an `ERROR_BAD_REQUEST` rather than a silently ignored field.

### Supported task types (19 total)

#### reCAPTCHA v3 (browser-based)

- `RecaptchaV3TaskProxyless`
- `RecaptchaV3TaskProxylessM1`
- `RecaptchaV3TaskProxylessM1S7`
- `RecaptchaV3TaskProxylessM1S9`
- `RecaptchaV3EnterpriseTask`
- `RecaptchaV3EnterpriseTaskM1`

Required: `websiteURL`, `websiteKey`. Optional: `pageAction`, `minScore` (accepted, not enforced), `isInvisible`, `userAgent`, proxy fields, `egress`.

#### reCAPTCHA v2 (browser-based)

- `NoCaptchaTaskProxyless`
- `RecaptchaV2TaskProxyless`
- `RecaptchaV2EnterpriseTaskProxyless`

Required: `websiteURL`, `websiteKey`. Optional: `isInvisible`, `userAgent`, proxy fields, `egress`.

#### hCaptcha (browser-based)

- `HCaptchaTaskProxyless`

Required: `websiteURL`, `websiteKey`. Optional: `isInvisible`, `rqdata`, `enterprisePayload`, `realPage`, `userAgent`, proxy fields, `egress`. Enterprise mode is detected from `rqdata` / `enterprisePayload`; see [hCaptcha usage](usage/hcaptcha.md).

#### Cloudflare Turnstile (browser-based)

- `TurnstileTaskProxyless`
- `TurnstileTaskProxylessM1`

Required: `websiteURL`, `websiteKey`. Optional: `action`, `cData`, `chlPageData`, `userAgent`, proxy fields, `egress`.

#### Image recognition

- `ImageToTextTask`
- `ImageToTextTaskMuggle`
- `ImageToTextTaskM1`

Required: `body` (base64-encoded image).

#### Image classification

- `HCaptchaClassification`
- `ReCaptchaV2Classification`
- `FunCaptchaClassification`
- `AwsClassification`

Required: at least one of `image`, `images`, `body`, or `queries` (base64-encoded). Optional: `question`.

### Task object fields

| Field | Type | Notes |
|-------|------|-------|
| `type` | string | Task type (see above) |
| `websiteURL` | string | Target page (browser tasks) |
| `websiteKey` | string | Widget sitekey (browser tasks) |
| `pageAction` | string | reCAPTCHA v3 action |
| `minScore` | number | Compatibility only, not enforced |
| `isInvisible` | bool | Invisible widget |
| `action`, `cData`, `chlPageData` | string | Turnstile render options; must match the site's configuration |
| `rqdata` | string | hCaptcha Enterprise nonce |
| `enterprisePayload` | object | hCaptcha Enterprise render options (spread flat onto `hcaptcha.render`) |
| `realPage` | bool | hCaptcha page strategy override (`HCAPTCHA_REAL_PAGE` default) |
| `userAgent` | string | Force UA; echoed in `solution.userAgent` |
| `proxyType`, `proxyAddress`, `proxyPort`, `proxyLogin`, `proxyPassword` | | Split proxy fields |
| `proxy` | string | Single-string proxy, optionally annotated `\|kind=residential\|country=DE` |
| `egress` | string | `auto` (default), `proxyless`, `pool`, or `task` |
| `body`, `image`, `images` | string / list | Base64 image input. For the `images` / `queries` list forms, each entry is capped at 10M chars and the list at 20 entries; single `body` / `image` strings are not size-bounded. |
| `question`, `queries` | string / list | Classification prompt and per-cell images |

### Idempotency semantics

The idempotency key is scoped per caller as `"{clientKey or 'anon'}:{idempotencyKey}"` and bound to a SHA-256 fingerprint of the task payload plus caller key.

- Replaying the same key with the same payload returns the original `taskId`; no second solve is started.
- Replaying the same key with a different payload returns `ERROR_DUPLICATE_TASK_ID`; use the original payload or a new key.
- With `REDIS_URL`, the claim is a shared `SET NX` with TTL `max(60, CAPTCHA_SOLVE_TIMEOUT × 4)` seconds (720 s by default), so a retry may land on any worker.
- Without Redis, coalescing is process-local and lives as long as the task record (10 minutes).

### Success response

```json
{
  "errorId": 0,
  "taskId": "uuid-string"
}
```

### Error responses

```json
{"errorId": 1, "errorCode": "ERROR_KEY_DOES_NOT_EXIST", "errorDescription": "Invalid clientKey"}
```

```json
{"errorId": 1, "errorCode": "ERROR_TASK_NOT_SUPPORTED", "errorDescription": "Task type 'X' is not supported. Supported: [...]"}
```

```json
{"errorId": 1, "errorCode": "ERROR_TASK_PROPERTY_EMPTY", "errorDescription": "websiteURL and websiteKey are required"}
```

```json
{"errorId": 1, "errorCode": "ERROR_DUPLICATE_TASK_ID", "errorDescription": "idempotencyKey was already used with a different payload; replay the original request or use a new idempotencyKey"}
```

```json
{"errorId": 1, "errorCode": "ERROR_NO_SLOT_AVAILABLE", "errorDescription": "in-flight tasks at capacity (128)"}
```

A malformed body (missing required field, unknown task property, wrong type) is answered with HTTP 200:

```json
{"errorId": 1, "errorCode": "ERROR_BAD_REQUEST", "errorDescription": "body.task.websiteURL: ..."}
```

---

## `POST /getTaskResult`

### Request

```json
{
  "clientKey": "your-client-key",
  "taskId": "uuid-from-createTask"
}
```

### Processing response

```json
{
  "errorId": 0,
  "status": "processing"
}
```

### Ready response

```json
{
  "errorId": 0,
  "status": "ready",
  "solution": {
    "gRecaptchaResponse": "token..."
  }
}
```

### Solution fields

`SolutionObject` accepts and returns these fields (absent fields are omitted):

| Field | Type | Meaning |
|-------|------|---------|
| `gRecaptchaResponse` | string | reCAPTCHA v2/v3 and hCaptcha token (YesCaptcha field name) |
| `token` | string | Turnstile token |
| `text` | string | Image-to-text structured JSON, serialized as a string |
| `objects` | list[int] | Classification cell indices |
| `answer` | bool or list[int] | hCaptcha classification boolean/list answer |
| `userAgent` | string | UA used for the solve; reuse it on your downstream submit |
| `timezoneId` | string | Fingerprint timezone used for the solve |
| `acceptLanguage` | string | Fingerprint `Accept-Language` used for the solve |
| `proxyKind` | string | `proxyless`, `pool_proxy`, or `task_proxy` |
| `egressServer` | string | Credential-free `scheme://host:port` of the solve egress, or null |
| `warnings` | list[string] | Non-fatal, caller-facing caveats (e.g. enterprise token cannot be server-verified) |

If a solver produces a solution that cannot be validated against this shape, the task fails with `ERROR_CAPTCHA_UNSOLVABLE`.

### Failure responses

```json
{"errorId": 1, "errorCode": "ERROR_KEY_DOES_NOT_EXIST", "errorDescription": "Invalid clientKey"}
```

```json
{"errorId": 1, "errorCode": "ERROR_NO_SUCH_CAPCHA_ID", "errorDescription": "Task not found"}
```

The misspelling `CAPCHA` is intentional (YesCaptcha compatibility). The same code is returned when the task exists but the caller key does not own it.

```json
{"errorId": 1, "errorCode": "ERROR_CAPTCHA_TIMEOUT", "errorDescription": "Solve exceeded 180s budget"}
```

```json
{"errorId": 1, "errorCode": "ERROR_CAPTCHA_UNSOLVABLE", "errorDescription": "..."}
```

```json
{"errorId": 1, "errorCode": "ERROR_TASK_NOT_SUPPORTED", "errorDescription": "Task type 'X' is not supported"}
```

---

## `POST /getBalance`

### Request

```json
{
  "clientKey": "your-client-key"
}
```

### Response

```json
{
  "errorId": 0,
  "balance": 98500.25
}
```

`balance = max(0, ACCOUNT_BALANCE_USD - ledger spend for this clientKey)`. `ACCOUNT_BALANCE_USD` defaults to `99999.0`. With Redis, the ledger spend is shared across workers; it is process-local otherwise. If services failed to initialise, the configured starting balance is returned unchanged.

An invalid `clientKey` (when `CLIENT_KEY` is configured) returns HTTP 200 with `errorId: 1` and `balance: 0.0` — this response shape has no `errorCode`/`errorDescription` fields:

```json
{
  "errorId": 1,
  "balance": 0.0
}
```

---

## `POST /reportCorrect` and `POST /reportIncorrect`

Feed the real downstream outcome of a token back into proxy health, the per-sitekey real-outcome bucket, success accounting, and session reputation.

### Request

```json
{
  "clientKey": "your-client-key",
  "taskId": "uuid-from-createTask"
}
```

### Response

```json
{"errorId": 0}
```

Rules:

- The caller must own the task (or the ledger record must have no owner). A mismatch or missing record returns `ERROR_NO_SUCH_CAPCHA_ID`.
- Reports are claimed atomically per task. A repeat report (concurrent or sequential) returns `errorId: 0` without re-running the side effects.
- No ledger record / services not initialised returns `ERROR_NO_SUCH_CAPCHA_ID`.
- Invalid `clientKey` returns `ERROR_KEY_DOES_NOT_EXIST`.

---

## Admin endpoints

### Authorization

`GET /admin/metrics` and `GET /admin/proxies` share one auth policy:

1. If `ADMIN_KEY` is configured, only `Authorization: Bearer <ADMIN_KEY>` is accepted. A missing/bad header returns **HTTP 403** with `{"errorId":1,"errorCode":"ERROR_KEY_DOES_NOT_EXIST","errorDescription":"Invalid or missing admin key"}`. The `clientKey` query param is ignored.
2. Otherwise, if `CLIENT_KEY` is configured, the deprecated `?clientKey=<CLIENT_KEY>` query param is accepted; a mismatch returns HTTP 200 with the same error envelope.
3. Otherwise (fully keyless mode), the request is served only from a loopback client (`127.0.0.1` / `::1`); others get HTTP 403.

### `GET /admin/metrics`

```json
{
  "errorId": 0,
  "summary": {
    "count": 120,
    "cost_usd": 0.42,
    "by_outcome": {"ready": 110, "failed": 10},
    "by_model": {"gpt-5.4": {"count": 20, "cost_usd": 0.3}},
    "by_proxy_kind": {"proxyless": 60, "pool_proxy": 60},
    "cost_per_success": 0.0038
  }
}
```

When services failed to initialise:

```json
{"errorId": 0, "summary": {}, "note": "services not initialised"}
```

### `GET /admin/proxies`

```json
{
  "errorId": 0,
  "proxies": [
    {
      "id": "proxy-id",
      "server": "http://1.2.3.4:8080",
      "kind": "residential",
      "state": "healthy",
      "success_count": 10,
      "fail_count": 1,
      "consecutive_fails": 0,
      "success_rate": 0.9091,
      "cooldown_remaining": 0.0,
      "cost_per_gb": 3.5,
      "bytes_used": 123456,
      "country": "DE",
      "timezone": "Europe/Berlin",
      "locale": "de-DE",
      "geo_probed": true,
      "sitekeys": {"sitekey": {"success": 8, "fail": 0}},
      "real_sitekeys": {"sitekey": {"success": 5, "fail": 1}}
    }
  ],
  "sessions": [
    {
      "id": "session-id",
      "in_use": false,
      "warm": true,
      "solves": 3,
      "reputation": 1.0,
      "user_agent": "Mozilla/5.0 ...",
      "proxy_id": "proxy-id",
      "created_at": 1729500000.0
    }
  ]
}
```

`username`, `password`, `proxyLogin`, `proxyPassword`, and `sticky_session_id` are stripped from proxy rows, and userinfo is removed from `server`. If services are unavailable the response is `{"errorId":0,"proxies":[],"sessions":[]}`.

---

## `GET /api/v1/health`

Liveness and runtime details. Always HTTP 200, never gated on subsystem health.

```json
{
  "status": "ok",
  "supported_task_types": ["RecaptchaV3TaskProxyless", "..."],
  "browser_headless": true,
  "browser_runtime": "camoufox",
  "browser_runtime_requested": "camoufox",
  "browser_runtime_effective": "camoufox",
  "browser_runtime_degraded": false,
  "browser_driver": "playwright",
  "browser_ready": true,
  "degraded": false,
  "degraded_subsystems": [],
  "model_backends": {
    "local": {
      "name": "local",
      "model": "Qwen/Qwen3.5-2B",
      "state": "closed",
      "healthy": true,
      "success_count": 42,
      "failure_count": 0,
      "consecutive_failures": 0,
      "success_rate": 1.0,
      "cooldown_remaining": 0.0,
      "last_error": null,
      "client_created": true
    },
    "cloud": {"...": "..."}
  },
  "cloud_model": "gpt-5.4",
  "local_model": "Qwen/Qwen3.5-2B"
}
```

Notes:

- `browser_runtime` is the runtime that actually launched. `browser_runtime_effective` additionally distinguishes a fallback such as `chromium(rebrowser-unavailable)`. `browser_runtime_degraded` is true when actual ≠ requested.
- `supported_task_types` is the set actually registered. If browser startup failed, browser types are absent and only vision types are listed.
- `degraded_subsystems` comes from the lifespan (for example `["browser", "proxy_pool"]`); an optional degraded plane does not by itself fail readiness.
- There is no bare `/health` route in this build. `/health` and `/api/v1/health` are both exempt from the rate limiter (useful if a reverse proxy maps probes to `/health`), but only `/api/v1/health` is registered.

---

## `GET /ready`

Readiness probe, intended for orchestrators (the bundled `render.yaml` uses it).

Ready — HTTP 200:

```json
{
  "status": "ready",
  "ready": true,
  "checks": {"browser": "ok", "redis": "not_configured"}
}
```

Not ready — HTTP 503:

```json
{
  "status": "not_ready",
  "ready": false,
  "checks": {"browser": "unavailable", "redis": "unavailable"}
}
```

Checks:

- `checks.browser`: `ok` / `unavailable` when at least one browser task type is registered; `disabled` for a vision-only deployment.
- `checks.redis`: `ok` / `unavailable` when `REDIS_URL` is configured (one PING with a 1.5 s timeout); `not_configured` otherwise.
- `checks.degraded`: present only when the lifespan recorded degraded optional subsystems. It does not change the status code.

The probe is cheap (no admin/pool scans) and never raises. It is subject to `RATE_LIMIT_RPM` (unlike `/api/v1/health`).

---

## `GET /`

Returns a compact service description and the registered task types at runtime:

```json
{
  "service": "captchai",
  "version": "3.0.0",
  "endpoints": {
    "createTask": "/createTask",
    "getTaskResult": "/getTaskResult",
    "getBalance": "/getBalance",
    "health": "/api/v1/health"
  },
  "supported_task_types": ["..."]
}
```

---

## Error code summary

| Code | Where | Meaning |
|------|-------|---------|
| `ERROR_KEY_DOES_NOT_EXIST` | createTask, getTaskResult, reports, admin | Invalid/missing `clientKey` (admin: invalid/missing Bearer or non-loopback keyless) |
| `ERROR_TASK_NOT_SUPPORTED` | createTask, getTaskResult | Task type is not registered (e.g. a degraded solver plane) |
| `ERROR_TASK_PROPERTY_EMPTY` | createTask | Required `websiteURL`/`websiteKey`, image, or classification input missing |
| `ERROR_DUPLICATE_TASK_ID` | createTask | Same idempotency key used with a different payload |
| `ERROR_NO_SLOT_AVAILABLE` | createTask | In-flight task cap reached (`QUEUE_MAX_SIZE`) |
| `ERROR_BAD_REQUEST` | any JSON endpoint | Framework request validation failed (missing/wrong fields, unknown task properties) |
| `ERROR_NO_SUCH_CAPCHA_ID` | getTaskResult, reports | Task missing, expired, or not owned by the caller |
| `ERROR_CAPTCHA_TIMEOUT` | getTaskResult | Solve exceeded `CAPTCHA_SOLVE_TIMEOUT` or was cancelled |
| `ERROR_CAPTCHA_UNSOLVABLE` | getTaskResult | Solver failed, target rejected/blocked, SSRF-guard rejection, or invalid solution |
| `ERROR_RATE_LIMITED` | any non-health path | Per-client-IP request budget exhausted |
| `ERROR_INTERNAL` | any endpoint | Unhandled server error (HTTP 200 envelope) |
