# Getting Started

## Requirements

- Python 3.10+
- Chromium installed through Playwright (or an optional hardened runtime)
- Network access to:
  - target sites you want to solve against
  - your configured OpenAI-compatible model endpoint(s)

## Installation

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install --with-deps chromium
```

Optional:

```bash
# Shared state for multi-worker / restart survival
pip install "redis>=4.2"

# Patched Chromium runtime
pip install rebrowser-playwright

# Patched Firefox runtime (enterprise hCaptcha)
pip install camoufox==0.4.11 && python -m camoufox fetch
```

## Environment variables

CaptchAI has two independent OpenAI-compatible model backends: `LOCAL_*` (self-hosted vision model, cheap/high-throughput) and `CLOUD_*` (remote model used for audio transcription and hard-grid escalation).

| Variable | Description | Default |
| --- | --- | --- |
| `CLIENT_KEY` | Client auth key used as `clientKey` | unset |
| `ALLOW_ANONYMOUS` | Permit startup without `CLIENT_KEY` on a non-loopback bind | `false` |
| `ADMIN_KEY` | Bearer key for `/admin/*` | unset |
| `RATE_LIMIT_RPM` | Per-client-IP requests/minute (validated `>= 1`) | `60` |
| `SSRF_PROTECT` | Pre-navigation public-URL check for hCaptcha/Turnstile | `true` |
| `LOCAL_BASE_URL` | Local OpenAI-compatible endpoint | `http://localhost:30000/v1` |
| `LOCAL_API_KEY` | Local endpoint key | `EMPTY` |
| `LOCAL_MODEL` | Local vision model | `Qwen/Qwen3.5-2B` |
| `CLOUD_BASE_URL` | Cloud OpenAI-compatible endpoint | placeholder URL |
| `CLOUD_API_KEY` | Cloud endpoint key | unset |
| `CLOUD_MODEL` | Cloud chat model | `gpt-5.4` |
| `CLOUD_AUDIO_MODEL` | Speech-to-text model for reCAPTCHA v2 audio | `whisper-1` |
| `CLOUD_MAX_CONCURRENCY` | Max concurrent cloud model calls | `4` |
| `LOCAL_MAX_CONCURRENCY` | Max concurrent local model calls (0 = unlimited) | `0` |
| `MODEL_CONNECTION_FALLBACK` | Retry connection failures on the other backend | `true` |
| `CAPTCHA_RETRIES` | Retry attempts | `3` |
| `CAPTCHA_TIMEOUT` | Model call timeout (s) | `30` |
| `CAPTCHA_SOLVE_TIMEOUT` | Per-task wall-clock budget (s) | `180` |
| `BROWSER_HEADLESS` | Run the browser headless | `true` |
| `BROWSER_TIMEOUT` | Browser timeout (s) | `30` |
| `BROWSER_RUNTIME` | `chromium` / `rebrowser` / `camoufox` | `chromium` |
| `BROWSER_RUNTIME_STRICT` | Fail instead of falling back when the runtime is unavailable | `false` |
| `SERVER_HOST` | Bind host | `0.0.0.0` |
| `SERVER_PORT` | Bind port | `8000` |
| `REDIS_URL` | Shared task store / ledger / accounting / proxy pool | unset |

Legacy names (`CAPTCHA_BASE_URL`, `CAPTCHA_API_KEY`, `CAPTCHA_MODEL`, `CAPTCHA_MULTIMODAL_MODEL`) are still accepted as fallbacks, but they no longer mean "one endpoint for everything":

- Both `CLOUD_BASE_URL` and `LOCAL_BASE_URL` fall back to `CAPTCHA_BASE_URL` when unset (so a legacy single endpoint configures both backends).
- `LOCAL_MODEL` falls back to `CAPTCHA_MULTIMODAL_MODEL`; `CLOUD_MODEL` falls back to `CAPTCHA_MODEL`.
- `LOCAL_API_KEY` falls back to `CAPTCHA_API_KEY`; `CLOUD_API_KEY` falls back to `CAPTCHA_API_KEY`.

Prefer the split `LOCAL_*` / `CLOUD_*` names in new deployments. See [`src/core/config.py`](../src/core/config.py) for the complete list.

## Start the service

```bash
export CLIENT_KEY="your-client-key"
export LOCAL_BASE_URL="http://localhost:30000/v1"
export LOCAL_MODEL="Qwen/Qwen3.5-2B"
export CLOUD_BASE_URL="https://your-openai-compatible-endpoint/v1"
export CLOUD_API_KEY="your-api-key"
export CLOUD_MODEL="gpt-5.4"
python main.py
```

Auth is fail-closed: if `CLIENT_KEY` is unset and `SERVER_HOST` is not loopback (`127.0.0.1`, `::1`, `localhost`), startup fails unless `ALLOW_ANONYMOUS=true` is set explicitly. `ALLOW_ANONYMOUS` only relaxes that startup check; when `CLIENT_KEY` is configured it is still required on every API call.

`ADMIN_KEY` is optional but recommended: when set, `/admin/*` requires `Authorization: Bearer <ADMIN_KEY>`.

## Verify startup

### Root endpoint

```bash
curl http://localhost:8000/
```

### Liveness / details

```bash
curl http://localhost:8000/api/v1/health
```

The response includes the registered task types, the actual vs requested browser runtime (`browser_runtime`, `browser_runtime_requested`, `browser_runtime_effective`, `browser_runtime_degraded`, `browser_driver`), `degraded` / `degraded_subsystems`, per-backend model circuit health, and the configured model names.

### Readiness

```bash
curl -i http://localhost:8000/ready
```

Returns HTTP 200 with `"status": "ready"` when required subsystems are up, or HTTP 503 with `"status": "not_ready"` and per-check details. The browser check applies only when browser task types are registered; Redis is pinged only when `REDIS_URL` is configured. Note there is no bare `/health` route: `/api/v1/health` is the liveness/details endpoint.

## Multi-worker topology with Redis

Without `REDIS_URL`, every subsystem is process-local: a restart loses tasks and spend history, and two workers cannot see each other's tasks.

With `REDIS_URL` set, these are **shared across workers** and survive restarts:

- task records (`captcha:task:*`) and `getTaskResult` visibility
- idempotency claims (`captcha:idem:*`, `SET NX`)
- the cost ledger behind `getBalance` and `/admin/metrics`
- per-sitekey success accounting
- proxy-pool inventory, health, cooldown, and per-sitekey stats

These remain **per process**:

- the in-flight task queue and concurrency semaphores (`QUEUE_MAX_SIZE`, `BROWSER_*_CONCURRENCY`, `VISION_CONCURRENCY`)
- the rate-limit token bucket (`RATE_LIMIT_RPM`)
- warm browser sessions (`SESSION_*`)
- model-pool concurrency limits (`CLOUD_MAX_CONCURRENCY`, `LOCAL_MAX_CONCURRENCY`)
- budget-guard in-flight reservations (caps are checked against shared ledger spend, but concurrent reservations on different workers can each see the same headroom; a global cap can be overshot by roughly `workers × per-request estimate`). Run a single worker for strict cap enforcement.

Requires the optional `redis>=4.2` package (not in `requirements.txt`).

```bash
pip install "redis>=4.2"
export REDIS_URL="redis://localhost:6379/0"
```

## Local and self-hosted model support

Both model paths use **OpenAI-compatible APIs**. Point `LOCAL_BASE_URL` at a self-hosted SGLang/vLLM server (image input + chat completions) and `CLOUD_BASE_URL` at a hosted provider (chat completions plus `audio.transcriptions` for the reCAPTCHA v2 audio path). The project documents generic compatibility rather than claiming validation for every provider stack.

See [Local Model Deployment](deployment/local-model.md) for a full SGLang walkthrough.
