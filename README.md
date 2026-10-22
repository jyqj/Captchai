# CaptchAI

Self-hosted captcha solver with a YesCaptcha-compatible async API.

```
pip install -r requirements.txt && playwright install --with-deps chromium
CLIENT_KEY=your-key python main.py
# → http://localhost:8000/api/v1/health
```

**19 task types** — reCAPTCHA v2/v3, hCaptcha (incl. enterprise), Cloudflare Turnstile, image-to-text, grid classification.

[中文说明](README.zh-CN.md)

> **Fail-closed auth.** With no `CLIENT_KEY`, the service refuses to start when
> `SERVER_HOST` is not loopback unless you explicitly set
> `ALLOW_ANONYMOUS=true`. See [Security](#security--auth).

---

## Why this exists

flow2api and similar projects need a `createTask` / `getTaskResult` backend that returns real, usable tokens. CaptchAI is that backend.

It is *not* a thin headless-Chromium wrapper. Enterprise hCaptcha (Stripe), Cloudflare, and Google bind tokens to egress IP, User-Agent, and browser fingerprint — a stock `playwright.chromium.launch()` is flagged on sight. CaptchAI solves that:

- **Hardened browser runtimes.** Stock Chromium, `rebrowser` (patched Chromium, optional package), or **Camoufox** (patched Firefox with engine-level anti-fingerprint, optional package). Enterprise targets should use Camoufox. The runtime is process-wide; the actual runtime after any fallback is reported by `/api/v1/health`.
- **Coherent per-context fingerprint.** UA, platform, WebGL vendor/renderer, languages, timezone — drawn from one consistent profile per solve, not one shared stealth script.
- **Proxy & UA binding.** The solved token is bound to the egress IP + UA. The solution echoes both (`solution.userAgent`, `solution.egressServer`) so your downstream submit matches.
- **Human-like interaction.** Pointer paths, scroll, wander — seed a real `motionData` buffer before hCaptcha's passive scoring fires.
- **Cost-aware vision.** Image challenges route to a cheap local model first; hard grids escalate to a cloud model with self-consistency voting.

---

## Architecture

```
                    POST /createTask { clientKey, task }
                                 │
                                 ▼
                    ┌───────────────────────┐
                    │   FastAPI + Router    │  YesCaptcha protocol
                    └──────────┬────────────┘
                               │
                               ▼
                    ┌───────────────────────┐
                    │     TaskManager       │  bounded queue, split pools,
                    └──────┬──────────┬─────┘  10-min task TTL, optional Redis
                           │          │
              ┌────────────┘          └────────────┐
              ▼                                    ▼
  ┌──────────────────────┐            ┌──────────────────────┐
  │   Browser Solvers    │            │    Vision Solvers     │
  │                      │            │                      │
  │  RecaptchaV3Solver   │            │  CaptchaRecognizer   │
  │  RecaptchaV2Solver   │            │  ClassificationSolver│
  │  HCaptchaSolver      │            │                      │
  │  TurnstileSolver     │            │  VisionRouter        │
  └──────────┬───────────┘            │  local → cloud       │
             │                        └──────────────────────┘
             ▼
  ┌──────────────────────┐
  │   BrowserManager     │  one shared process
  │                      │
  │  per-solve context:  │
  │  - proxy binding     │
  │  - fingerprint       │
  │  - stealth JS / DOM  │
  │    bridge (Camoufox) │
  │  - resource blocking │
  └──────────────────────┘

                    POST /getTaskResult { taskId }
                                 │
                                 ▼
                    solution.gRecaptchaResponse / .token / .text
```

### Source layout

```
src/
├── api/routes.py             # createTask / getTaskResult / getBalance / report / admin / health / ready
├── core/
│   ├── config.py             # all env vars, defaults, validation
│   ├── task_types.py          # single registry: 19 task types → provider + validation
│   └── services.py           # service container (singletons)
├── services/
│   ├── task_manager.py        # bounded admission, split concurrency, TTL, idempotency
│   ├── browser.py             # BrowserManager: launch, runtime selection, context factory
│   ├── browser_solver.py      # BaseBrowserSolver: context acquisition, retry, ledger
│   ├── injected_widget.py     # shared base: injected page, DOM bridge, _poll_token
│   ├── hcaptcha.py            # HCaptchaSolver: checkbox/invisible/enterprise/challenge dispatch
│   ├── turnstile.py           # TurnstileSolver
│   ├── recaptcha_v3.py        # RecaptchaV3Solver
│   ├── recaptcha_v2.py        # RecaptchaV2Solver
│   ├── recognition.py         # image-to-text (CaptchaRecognizer)
│   ├── classification.py      # grid classification (ClassificationSolver)
│   └── vision_solver.py       # shared model-call + metering adapter
├── parsing/
│   ├── dispatcher.py          # ChallengeClassifier + ChallengeDispatcher (shape detection)
│   ├── vision.py              # VisionRouter: local → cloud with self-consistency voting
│   └── shapes/                # per-shape solvers: grid_select, dynamic_grid, area_bbox, slide, drag_drop
├── assets/
│   ├── fingerprint.py         # coherent UA/WebGL/screen profiles + stealth JS generation
│   ├── proxy_pool.py          # proxy rotation, bandwidth metering, geo probe (optional Redis backend)
│   ├── session_pool.py        # warm browser session reuse
│   └── geo_probe.py           # IP → timezone/locale alignment
├── consumption/
│   ├── ledger.py              # per-solve cost recording (model, tokens, outcome; optional Redis)
│   ├── accounting.py          # per-sitekey success stats (optional Redis)
│   ├── budget.py              # spend caps with in-flight reservations
│   └── token_verify.py        # post-solve token siteverify (optional)
└── orchestration/
    └── store.py               # task result persistence (in-memory or Redis)
```

---

## Task types

| Category | Types | Solution field |
|----------|-------|----------------|
| reCAPTCHA v3 | `RecaptchaV3TaskProxyless`, `…M1`, `…M1S7`, `…M1S9` | `gRecaptchaResponse` |
| reCAPTCHA v3 Enterprise | `RecaptchaV3EnterpriseTask`, `…M1` | `gRecaptchaResponse` |
| reCAPTCHA v2 | `NoCaptchaTaskProxyless`, `RecaptchaV2TaskProxyless`, `…EnterpriseTaskProxyless` | `gRecaptchaResponse` |
| hCaptcha | `HCaptchaTaskProxyless` | `gRecaptchaResponse` |
| Cloudflare Turnstile | `TurnstileTaskProxyless`, `…M1` | `token` |
| Image-to-text | `ImageToTextTask`, `…Muggle`, `…M1` | `text` |
| Classification | `HCaptchaClassification`, `ReCaptchaV2Classification`, `FunCaptchaClassification`, `AwsClassification` | `objects` (HCaptcha may also return `answer`) |

All types are defined once in [`src/core/task_types.py`](src/core/task_types.py). Solution objects can also include `userAgent`, `timezoneId`, `acceptLanguage`, `proxyKind`, `egressServer`, and `warnings`; see the [API reference](docs/api-reference.md).

---

## API

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/createTask` | POST | Submit a captcha task |
| `/getTaskResult` | POST | Poll for result |
| `/getBalance` | POST | Starting credit minus this client's ledger spend |
| `/reportCorrect` | POST | Report token accepted downstream |
| `/reportIncorrect` | POST | Report token rejected downstream |
| `/api/v1/health` | GET | Liveness + runtime details (always 200) |
| `/ready` | GET | Readiness probe (200 ready / 503 not ready) |
| `/admin/metrics` | GET | Per-model / per-outcome cost summary |
| `/admin/proxies` | GET | Proxy-pool and session snapshot (credentials redacted) |
| `/` | GET | Service description and registered types |

Admin endpoints accept `Authorization: Bearer $ADMIN_KEY` when `ADMIN_KEY` is set. Without it, they fall back to the legacy `?clientKey=` query param when `CLIENT_KEY` is set, and are **loopback-only** when neither key is configured. Setting `ADMIN_KEY` is recommended.

<details>
<summary>Create + poll example</summary>

```bash
# create
curl -s -X POST http://localhost:8000/createTask \
  -H "Content-Type: application/json" \
  -d '{
    "clientKey": "your-key",
    "task": {
      "type": "HCaptchaTaskProxyless",
      "websiteURL": "https://example.com",
      "websiteKey": "site-key"
    }
  }'

# poll
curl -s -X POST http://localhost:8000/getTaskResult \
  -H "Content-Type: application/json" \
  -d '{"clientKey": "your-key", "taskId": "<taskId from above>"}'
```
</details>

---

## Configuration

All configuration is via environment variables. Full definitions live in [`src/core/config.py`](src/core/config.py); malformed numbers/booleans fail startup with the variable named.

### Security & server

| Variable | Default | What it does |
|----------|---------|--------------|
| `CLIENT_KEY` | — | Auth key every API call must present as `clientKey` |
| `ALLOW_ANONYMOUS` | `false` | Allow starting a keyless service on a non-loopback bind (startup gate only) |
| `ADMIN_KEY` | — | Bearer key for `/admin/*`; when set, only `Authorization: Bearer` is accepted |
| `RATE_LIMIT_RPM` | `60` | Per-client-IP requests/minute (in-process token bucket; must be `>= 1`) |
| `SSRF_PROTECT` | `true` | Pre-navigation public-URL check for the hCaptcha/Turnstile page path (reCAPTCHA v2/v3 always validate) |
| `SERVER_HOST` | `0.0.0.0` | Bind address; loopback values skip the keyless fail-closed check |
| `SERVER_PORT` | `8000` | Bind port (also honours `PORT` in `main.py`) |

### Model backends

| Variable | Default | What it does |
|----------|---------|--------------|
| `LOCAL_BASE_URL` | `http://localhost:30000/v1` | Local vision model endpoint (SGLang/vLLM) |
| `LOCAL_API_KEY` | `EMPTY` | Local endpoint key |
| `LOCAL_MODEL` | `Qwen/Qwen3.5-2B` | Local model name |
| `LOCAL_MAX_CONCURRENCY` | `0` | Local model calls in flight (0 = unlimited) |
| `CLOUD_BASE_URL` | — | Cloud model endpoint (OpenAI-compatible) |
| `CLOUD_API_KEY` | — | Cloud model key |
| `CLOUD_MODEL` | `gpt-5.4` | Cloud model name |
| `CLOUD_AUDIO_MODEL` | `whisper-1` | Speech-to-text model for the reCAPTCHA v2 audio path (`audio.transcriptions`) |
| `CLOUD_MAX_CONCURRENCY` | `4` | Cloud model calls in flight |
| `MODEL_CONNECTION_FALLBACK` | `true` | Retry a connection-failed call on the other backend (local↔cloud) |

### Browser & stealth

| Variable | Default | What it does |
|----------|---------|--------------|
| `BROWSER_RUNTIME` | `chromium` | `chromium` (stock) / `rebrowser` (optional patched driver) / `camoufox` (optional patched Firefox) |
| `BROWSER_RUNTIME_STRICT` | `false` | Make an unavailable/failed hardened runtime fatal instead of falling back |
| `BROWSER_HEADLESS` | `true` | Headless mode |
| `BROWSER_TIMEOUT` | `30` | Page load timeout (s) |
| `BROWSER_DISABLE_SANDBOX` | auto | `true`/`false` force `--no-sandbox` on/off; unset = only when root on POSIX |
| `BROWSER_DISABLE_SITE_ISOLATION` | `false` | Opt in to Chromium site-isolation-disable flags |
| `CAMOUFOX_HUMANIZE` | `true` | Camoufox human-like cursor |
| `CAMOUFOX_BLOCK_WEBRTC` | `true` | Block WebRTC IP leak |
| `CAMOUFOX_OS` | — | Pin spoofed OS (`windows`, `macos`, `linux`) |

Runtime semantics:

- `chromium` launches the stock Playwright Chromium bundled with your Playwright install.
- `rebrowser` requires the optional `rebrowser-playwright` package. It launches through that patched driver. If the package is missing or cannot be imported, lenient mode logs a loud warning, falls back to stock Playwright, and records `effective_runtime=chromium(rebrowser-unavailable)`, `driver=playwright`. `BROWSER_RUNTIME_STRICT=true` makes this a startup error.
- `camoufox` requires the optional `camoufox` package plus its downloaded browser (`python -m camoufox fetch`). Missing/unlaunchable → loud fallback to stock Chromium (or fatal in strict mode).
- If a hardened runtime fails to launch, lenient mode also falls back to stock Chromium. The runtime is process-wide, not per task.
- `GET /api/v1/health` exposes `browser_runtime` (actual), `browser_runtime_requested`, `browser_runtime_effective`, `browser_runtime_degraded`, `browser_driver`, and `browser_ready`, so a silent degrade is observable.

### Enterprise hCaptcha

| Variable | Default | What it does |
|----------|---------|--------------|
| `ENTERPRISE_REQUIRE_RESIDENTIAL` | `true` | Require a residential/mobile pool proxy for enterprise solves |
| `ENTERPRISE_FRESH_CONTEXT` | `true` | Fresh browser context per enterprise solve |
| `ENTERPRISE_REQUIRE_HARDENED_RUNTIME` | `false` | Refuse enterprise solves on stock Chromium |
| `ENTERPRISE_REQUIRE_RESIDENTIAL_ON_TASK` | `false` | Enforce residential annotation on caller-supplied task proxies |
| `POOL_EGRESS_EXPOSE_CREDENTIALS` | `false` | Return a credentialed `egressServer` for pool-proxy solves |
| `HCAPTCHA_REAL_PAGE` | `false` | Default page strategy for hCaptcha |
| `HCAPTCHA_DEVICE_PERSISTENCE` | `false` | Re-seed the `hmt` device-trust cookie per egress |
| `HCAPTCHA_INVISIBLE_MOTION_SECONDS` | `3.0` | Motion before invisible `execute()` |
| `HCAPTCHA_INVISIBLE_PASSIVE_BUDGET` | `4.0` | Passive token wait before challenge (s) |
| `HCAPTCHA_RQDATA_TTL` | `30` | rqdata freshness warning budget (s) |

Enterprise egress with no task proxy is forced to `egress=pool`; that pool must be seeded from `PROXY_POOL` with `|kind=residential` or `|kind=mobile` entries. See [hCaptcha usage](docs/usage/hcaptcha.md#enterprise-egress-policy-proxy_pool--residential).

### Solving, concurrency & state

| Variable | Default | What it does |
|----------|---------|--------------|
| `CAPTCHA_RETRIES` | `3` | Solve/model retry attempts |
| `CAPTCHA_TIMEOUT` | `30` | Model call timeout (s) |
| `CAPTCHA_SOLVE_TIMEOUT` | `180` | Per-task wall-clock budget (s); alias `SOLVE_TIMEOUT` |
| `QUEUE_MAX_SIZE` | `128` | Max in-flight tasks; excess admission returns `ERROR_NO_SLOT_AVAILABLE` |
| `BROWSER_CONCURRENCY` | `4` | Base browser-concurrency pool |
| `BROWSER_PROXYLESS_CONCURRENCY` | `4` | Pool for server-egress browser solves |
| `BROWSER_PROXIED_CONCURRENCY` | `4` | Pool for caller-proxy browser solves |
| `BROWSER_POOL_PROXY_CONCURRENCY` | `4` | Pool for server proxy-pool solves |
| `VISION_CONCURRENCY` | `8` | Concurrency pool for pure-vision tasks |
| `SESSION_POOL_SIZE` | `4` | Warm browser-session pool size (per process) |
| `SESSION_MAX_SOLVES` | `8` | Reuse a warm session for at most this many solves |
| `SESSION_PREWARM` | `false` | Pre-create proxyless warm sessions at startup |
| `SESSION_IDLE_TTL_SECONDS` | `300` | Retire a warm session idle longer than this (0 disables) |
| `SESSION_MAX_AGE_SECONDS` | `1800` | Retire a warm session older than this (0 disables) |
| `REDIS_URL` | — | Shared task store / ledger / accounting / proxy pool (optional) |
| `PROXY_POOL` | — | Server-side proxy inventory, one per line or comma-separated |
| `PROXY_COOLDOWN` | `120` | Cooldown after a proxy failure (s) |
| `PROXY_MAX_CONSECUTIVE_FAILS` | `3` | Failures before a proxy is cooled down |
| `PROXY_MAX_GB` | `0` | Per-proxy bandwidth cap in GB (0 = unlimited) |
| `PROXY_GEO_PROBE` | `true` | Probe proxy exit country on first checkout |

### Vision routing

| Variable | Default | What it does |
|----------|---------|--------------|
| `VISION_CLOUD_ENABLED` | `true` | Allow hard grids to escalate to the cloud model |
| `VISION_VOTE_SAMPLES` | `3` | Self-consistency samples per grid (1 disables voting) |
| `VISION_CONFIDENCE_THRESHOLD` | `0.6` | Agreement/confidence threshold that triggers escalation |
| `VISION_VOTE_CONCURRENT` | `true` | Run vote samples concurrently |
| `VISION_INLINE_ESCALATE` | `true` | Retry a low-confidence local call inline on cloud |
| `VISION_STITCH_GRID` | `true` | Send one stitched montage instead of N tiles |
| `VISION_TRUST_SELF_CONFIDENCE` | `false` | Trust the model's self-reported confidence |

Legacy `CAPTCHA_BASE_URL` / `CAPTCHA_API_KEY` / `CAPTCHA_MODEL` / `CAPTCHA_MULTIMODAL_MODEL` are still accepted as fallbacks for the cloud/local values, but the split `CLOUD_*` / `LOCAL_*` names take precedence and are what the docs use.

### Idempotency

`createTask` accepts an optional `idempotencyKey` (max 128 chars). Keys are scoped per caller as `"{clientKey or 'anon'}:{idempotencyKey}"` and bound to a SHA-256 fingerprint of the task payload, including the caller key:

- same key + same payload → the original `taskId` is returned, no second solve or spend;
- same key + different payload → `ERROR_DUPLICATE_TASK_ID`;
- with `REDIS_URL`, the claim is a shared `SET NX` valid for `max(60, CAPTCHA_SOLVE_TIMEOUT × 4)` seconds (720 s by default), so retries can land on any worker;
- without Redis the store and claims are process-local and the task record lives for 10 minutes.

### Health and readiness

- `GET /api/v1/health` is liveness. It always returns 200 with `status`, `supported_task_types`, browser runtime fields, `degraded`, `degraded_subsystems`, `model_backends`, `cloud_model`, and `local_model`. It does not gate traffic.
- `GET /ready` is readiness. It returns 200 when required subsystems are up and 503 otherwise. `checks.browser` is `ok`/`unavailable` when browser task types are registered (`disabled` for vision-only deployments); `checks.redis` is `ok`/`unavailable` when `REDIS_URL` is set (`not_configured` otherwise). Degraded optional planes are reported under `checks.degraded` but do not fail readiness. There is no bare `/health` route in this build.

---

## Security & auth

- **Fail-closed startup.** If `CLIENT_KEY` is unset, `ALLOW_ANONYMOUS` is false, and `SERVER_HOST` is not loopback (`127.0.0.1`, `::1`, or `localhost`), the lifespan raises and the process refuses to start. Set `CLIENT_KEY`, or explicitly opt in with `ALLOW_ANONYMOUS=true`.
- **`ALLOW_ANONYMOUS` is a startup gate, not a per-request bypass.** When `CLIENT_KEY` *is* set, every task endpoint still requires it. When `CLIENT_KEY` is unset, any `clientKey` string (including empty) is accepted; `getTaskResult` and the report endpoints check that the presented key matches the key recorded for the task (constant-time), so one caller cannot read or report on another caller's task.
- **Constant-time key comparison** (`hmac.compare_digest`) is used for `CLIENT_KEY` and `ADMIN_KEY`.
- **Admin auth.** With `ADMIN_KEY` set, `/admin/*` requires `Authorization: Bearer <key>`; the query param is ignored and a bad/missing header returns HTTP 403 with `ERROR_KEY_DOES_NOT_EXIST`. Without `ADMIN_KEY`, the deprecated `?clientKey=` form works when `CLIENT_KEY` is set; in fully keyless mode admin access is restricted to loopback clients (HTTP 403 otherwise).
- **Rate limiting.** Per-client-IP token bucket in-process (`RATE_LIMIT_RPM`, default 60; config validation requires `>= 1`). Over-budget requests get HTTP 200 with `{"errorId":1,"errorCode":"ERROR_RATE_LIMITED"}` and a `Retry-After` header. `/api/v1/health` (and `/health`, should a proxy map it) bypass the limiter; `/ready` does not. Each worker enforces its own bucket.
- **SSRF protection.** Caller-supplied `websiteURL` is validated before a proxy/context is checked out: scheme must be http(s); the host must resolve; every resolved address must be public (loopback, RFC1918/ULA, link-local incl. `169.254.169.254`, reserved, multicast, unspecified are rejected). reCAPTCHA v2/v3 always run this check; the hCaptcha/Turnstile page path runs it when `SSRF_PROTECT=true` (the default) and fails open only on a DNS resolution error. reCAPTCHA audio URLs are separately pinned to `https` on `google.com` / `recaptcha.net` / `gstatic.com` host suffixes, redirects are refused, the body is capped at 5 MB, and the download goes through the solve's task proxy when one is configured.
- **Input limits.** Task objects reject unknown fields (`extra="forbid"`); each entry of the list-form `images` / `queries` fields is capped at 10M chars and the list at 20 entries (single `body` / `image` strings are not size-bounded); framework validation errors are rewritten to the YesCaptcha envelope.

---

## Deployment

### Local

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install --with-deps chromium
export CLIENT_KEY="your-key"
python main.py
```

Optional hardened runtimes:

```bash
# rebrowser (patched Chromium)
pip install rebrowser-playwright
export BROWSER_RUNTIME=rebrowser

# Camoufox (patched Firefox)
pip install camoufox==0.4.11 && python -m camoufox fetch
export BROWSER_RUNTIME=camoufox
export BROWSER_RUNTIME_STRICT=true
```

### Docker (Render)

```bash
docker build -f Dockerfile.render -t captchai .
docker run -p 8000:8000 -e CLIENT_KEY=your-key captchai
```

One-click deploy via [`render.yaml`](render.yaml) (its health check points at `/ready`).

### Shared state with Redis

```bash
pip install "redis>=4.2"
export REDIS_URL="redis://localhost:6379/0"
```

With `REDIS_URL` set, the task store, cost ledger, success accounting, proxy pool, and idempotency claims are shared across workers and survive restarts. The following remain **per process**: the task admission queue and concurrency semaphores, the rate-limit bucket, warm browser sessions, model-pool concurrency limits, and the budget guard's in-flight reservations. Budget caps are checked against the shared ledger's recorded spend, but concurrent reservations on different workers can each see the same headroom, so a global cap can be overshot by roughly `workers × per-request estimate` at the moment of admission. Run one worker for strict cap enforcement, or leave headroom.

Redis records use `CAPTCHA_SOLVE_TIMEOUT × 4` as their TTL (720 s default). Without Redis everything is in-process and a restart loses tasks and spend history.

### Health probes

- Readiness: `GET /ready` (200/503), as used by `render.yaml`.
- Liveness/details: `GET /api/v1/health` (always 200).

---

## Development

```bash
# tests
python -m pytest tests/ -q

# type check
npx pyright

# docs
python -m mkdocs build --strict
```

---

## Limitations

- Task state is in-memory by default (10-minute task TTL); set `REDIS_URL` for cross-worker visibility and restart survival.
- `minScore` is accepted for compatibility but not enforced.
- Token validity depends on runtime, IP reputation, and target-site behavior; enterprise tokens cannot be server-verified without a siteverify secret.
- Rate limiting and budget in-flight reservations are per process; use a single worker for strict global limits.
- Not all commercial solver features are replicated.

---

## Disclaimer

This project is for legitimate research, security testing, and educational use only. You are solely responsible for compliance with applicable laws and terms of service. See [DISCLAIMER.md](DISCLAIMER.md).

## License

[MIT](LICENSE)
