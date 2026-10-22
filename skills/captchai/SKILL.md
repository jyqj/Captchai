---
name: captchai
description: Deploy, configure, validate, and integrate the CaptchAI captcha-solving service. Use when working with YesCaptcha-style APIs, flow2api integration, reCAPTCHA/hCaptcha/Turnstile task creation, image classification, SGLang local model deployment, Render/Hugging Face cloud deployment, or OpenAI-compatible multimodal model setup. Also use when the user asks how to self-host a captcha-solving service or wants request/response examples for CaptchAI.
---

# CaptchAI Skill

Operational guidance for deploying and integrating the CaptchAI service.

## Model architecture

CaptchAI uses two OpenAI-compatible backends:

- **Local model** — self-hosted via SGLang/vLLM (e.g. `Qwen/Qwen3.5-2B`). Handles image recognition and classification tasks. Configured via `LOCAL_BASE_URL`, `LOCAL_API_KEY`, `LOCAL_MODEL`, `LOCAL_MAX_CONCURRENCY`.
- **Cloud model** — remote OpenAI-compatible API (e.g. `gpt-5.4`). Handles hard-grid escalation and audio transcription. Configured via `CLOUD_BASE_URL`, `CLOUD_API_KEY`, `CLOUD_MODEL`, `CLOUD_AUDIO_MODEL` (speech-to-text, default `whisper-1`), `CLOUD_MAX_CONCURRENCY`.

`MODEL_CONNECTION_FALLBACK=true` retries a connection-failed call on the other backend (local→cloud gated by `VISION_CLOUD_ENABLED` + budget; cloud→local always).

## Supported task types (19 total)

### Browser-based (12)
`RecaptchaV3TaskProxyless`, `RecaptchaV3TaskProxylessM1`, `RecaptchaV3TaskProxylessM1S7`, `RecaptchaV3TaskProxylessM1S9`, `RecaptchaV3EnterpriseTask`, `RecaptchaV3EnterpriseTaskM1`, `NoCaptchaTaskProxyless`, `RecaptchaV2TaskProxyless`, `RecaptchaV2EnterpriseTaskProxyless`, `HCaptchaTaskProxyless`, `TurnstileTaskProxyless`, `TurnstileTaskProxylessM1`

### Image recognition (3)
`ImageToTextTask`, `ImageToTextTaskMuggle`, `ImageToTextTaskM1`

### Image classification (4)
`HCaptchaClassification`, `ReCaptchaV2Classification`, `FunCaptchaClassification`, `AwsClassification`

## Production prerequisites

1. **Auth must be explicit.** The lifespan fails closed: no `CLIENT_KEY` + non-loopback `SERVER_HOST` (default `0.0.0.0`) + `ALLOW_ANONYMOUS=false` (default) means the process refuses to start. Set `CLIENT_KEY`, or set `ALLOW_ANONYMOUS=true` deliberately for an open deployment.
2. **Admin access.** Set `ADMIN_KEY` and send `Authorization: Bearer <ADMIN_KEY>` to `/admin/*`. Without it, admin falls back to `?clientKey=` (deprecated) or loopback-only when fully keyless.
3. **Rate limit.** `RATE_LIMIT_RPM` (default 60, validated `>= 1`) is an in-process per-client-IP token bucket. Over-budget requests receive HTTP 200 with `ERROR_RATE_LIMITED` and `Retry-After`. Multiple workers each enforce their own bucket.
4. **SSRF.** `SSRF_PROTECT=true` (default) enables the pre-navigation public-URL check on the hCaptcha/Turnstile page path; reCAPTCHA v2/v3 always validate `websiteURL`. Leave it on unless you are in a controlled test environment.
5. **Probes.** Use `GET /ready` (200/503; browser check when browser types are registered, Redis ping when `REDIS_URL` is set) for orchestration, and `GET /api/v1/health` (always 200) for details. There is no bare `/health` route.
6. **Shared state.** `REDIS_URL` (requires `pip install "redis>=4.2"`) shares the task store, ledger, accounting, proxy pool, and idempotency claims across workers. Rate limiting, task admission, warm sessions, model concurrency, and budget in-flight reservations stay per process — use one worker for strict budget caps.
7. **Hardened runtimes are optional installs.** `BROWSER_RUNTIME=rebrowser` needs `rebrowser-playwright`; `camoufox` needs the `camoufox` package plus `python -m camoufox fetch`. Without `BROWSER_RUNTIME_STRICT=true`, a missing runtime falls back to stock Chromium with a loud warning and `effective_runtime=chromium(rebrowser-unavailable)`; `/api/v1/health` reports requested vs actual.

## Local model setup (SGLang)

```bash
pip install "sglang[all]>=0.4.6.post1"
# From ModelScope (China):
export SGLANG_USE_MODELSCOPE=true
python -m sglang.launch_server --model-path Qwen/Qwen3.5-2B --port 30000
```

Then configure CaptchAI:
```bash
export LOCAL_BASE_URL="http://localhost:30000/v1"
export LOCAL_MODEL="Qwen/Qwen3.5-2B"
```

## Startup checklist

1. Install dependencies: `pip install -r requirements.txt && playwright install --with-deps chromium`
2. Start the local model server (SGLang on port 30000) if you use `LOCAL_*`
3. Set env vars: `LOCAL_BASE_URL`, `CLOUD_BASE_URL`, `CLOUD_API_KEY`, `CLIENT_KEY`, `ADMIN_KEY`, `RATE_LIMIT_RPM`, `SSRF_PROTECT`
4. Start the service: `python main.py`
5. Verify liveness: `curl http://localhost:8000/api/v1/health`; readiness: `curl -i http://localhost:8000/ready`
6. Test: create a reCAPTCHA v3 task against `https://antcpt.com/score_detector/` with key `6LcR_okUAAAAAPYrPe-HK_0RULO1aZM15ENyM-Mf`

## Integration notes

- `createTask` supports `idempotencyKey`; keys are scoped per `clientKey` and bound to the payload. Same payload returns the original `taskId`; a different payload returns `ERROR_DUPLICATE_TASK_ID`.
- In keyless mode, `getTaskResult` and the report endpoints require the same `clientKey` used at admission.
- `getBalance` = `ACCOUNT_BALANCE_USD` minus shared/process ledger spend, floored at 0. An invalid key returns `{errorId:1, balance:0.0}` (no error code field).
- Enterprise hCaptcha forces an egress policy: no task proxy → `egress=pool`; `egress=proxyless` is refused. Seed `PROXY_POOL` with `|kind=residential` or `|kind=mobile` entries, or pass your own residential task proxy. `ENTERPRISE_REQUIRE_RESIDENTIAL` defaults to true.

## Response rules

1. Prefer the repository's documented behavior over assumptions.
2. Use placeholder credentials only. Never expose real secrets.
3. Be explicit about limitations:
   - `minScore` is compatibility-only
   - Task storage is in-memory by default with a 10-minute task TTL; `REDIS_URL` adds cross-worker persistence
   - Rate limiting and budget in-flight reservations are per process
   - reCAPTCHA v2 and hCaptcha may require image classification fallback in headless environments
   - reCAPTCHA v2 audio transcription uses `CLOUD_AUDIO_MODEL`, not `CLOUD_MODEL`
4. For deployment help, reference `docs/deployment/local-model.md`, `docs/deployment/render.md`, `docs/deployment/huggingface.md`.
5. For API usage, reference `docs/api-reference.md` and the usage guides under `docs/usage/`.
