# CaptchAI

Self-hosted captcha solver with a YesCaptcha-compatible async API.

```
pip install -r requirements.txt && playwright install --with-deps chromium
CLIENT_KEY=your-key python main.py
# → http://localhost:8000/api/v1/health
```

**19 task types** — reCAPTCHA v2/v3, hCaptcha (incl. enterprise), Cloudflare Turnstile, image-to-text, grid classification.

[中文说明](README.zh-CN.md)

---

## Why this exists

flow2api and similar projects need a `createTask` / `getTaskResult` backend that returns real, usable tokens. CaptchAI is that backend.

It is *not* a thin headless-Chromium wrapper. Enterprise hCaptcha (Stripe), Cloudflare, and Google bind tokens to egress IP, User-Agent, and browser fingerprint — a stock `playwright.chromium.launch()` is flagged on sight. CaptchAI solves that:

- **Hardened browser runtimes.** Stock Chromium, `rebrowser` (patched Chromium), or **Camoufox** (patched Firefox with engine-level anti-fingerprint). Enterprise targets should use Camoufox.
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
                    │     TaskManager       │  async queue, 10-min TTL
                    └──────┬──────────┬─────┘
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
├── api/routes.py             # createTask / getTaskResult / getBalance / report / health
├── core/
│   ├── config.py             # all env vars, defaults, validation
│   ├── task_types.py          # single registry: 19 task types → provider + validation
│   └── services.py           # service container (singletons)
├── services/
│   ├── task_manager.py        # async in-memory queue + TTL + concurrency pool
│   ├── browser.py             # BrowserManager: launch, context factory, resource blocking
│   ├── browser_solver.py      # BaseBrowserSolver: acquire/release context, retry, ledger
│   ├── injected_widget.py     # shared base: injected page, DOM bridge, _poll_token
│   ├── hcaptcha.py            # HCaptchaSolver: checkbox/invisible/enterprise/challenge dispatch
│   ├── turnstile.py           # TurnstileSolver
│   ├── recaptcha_v3.py        # RecaptchaV3Solver
│   ├── recaptcha_v2.py        # RecaptchaV2Solver
│   ├── recognition.py         # image-to-text (CaptchaRecognizer)
│   ├── classification.py      # grid classification (ClassificationSolver)
│   └── vision_solver.py       # VisionRouter: local-first, cloud escalation
├── parsing/
│   ├── dispatcher.py          # ChallengeClassifier + ChallengeDispatcher (shape detection)
│   ├── vision.py              # VisionRouter: local → cloud with self-consistency voting
│   └── shapes/                # per-shape solvers: grid_select, dynamic_grid, area_bbox, slide, drag_drop
├── assets/
│   ├── fingerprint.py         # coherent UA/WebGL/screen profiles + stealth JS generation
│   ├── proxy_pool.py          # proxy rotation, bandwidth metering, geo probe
│   ├── session_pool.py        # warm browser session reuse
│   └── geo_probe.py           # IP → timezone/locale alignment
├── consumption/
│   ├── ledger.py              # per-solve cost recording (model, tokens, outcome)
│   ├── accounting.py          # per-sitekey success stats
│   ├── budget.py              # per-client balance tracking
│   └── token_verify.py        # post-solve token siteverify (optional)
└── orchestration/
    └── store.py               # task result persistence
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
| Classification | `HCaptchaClassification`, `ReCaptchaV2Classification`, `FunCaptchaClassification`, `AwsClassification` | `objects` |

All types are defined once in [`src/core/task_types.py`](src/core/task_types.py).

---

## API

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/createTask` | POST | Submit a captcha task |
| `/getTaskResult` | POST | Poll for result |
| `/getBalance` | POST | Account balance |
| `/reportCorrect` | POST | Report token accepted downstream |
| `/reportIncorrect` | POST | Report token rejected downstream |
| `/api/v1/health` | GET | Runtime status |
| `/admin/metrics` | GET | Per-sitekey / per-model cost summary |
| `/admin/proxies` | GET | Proxy pool health |

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

All configuration is via environment variables. Full definitions live in [`src/core/config.py`](src/core/config.py).

### Essentials

| Variable | Default | What it does |
|----------|---------|------|
| `CLIENT_KEY` | — | Auth key for all API calls |
| `LOCAL_BASE_URL` | `http://localhost:30000/v1` | Local vision model endpoint (SGLang/vLLM) |
| `LOCAL_MODEL` | `Qwen/Qwen3.5-2B` | Local model name |
| `CLOUD_BASE_URL` | — | Cloud model endpoint (OpenAI-compatible) |
| `CLOUD_API_KEY` | — | Cloud model key |
| `CLOUD_MODEL` | `gpt-5.4` | Cloud model name |

### Browser & stealth

| Variable | Default | What it does |
|----------|---------|------|
| `BROWSER_RUNTIME` | `chromium` | `chromium` / `rebrowser` / `camoufox` |
| `BROWSER_RUNTIME_STRICT` | `false` | Fail startup if hardened runtime unavailable |
| `BROWSER_HEADLESS` | `true` | Headless mode |
| `BROWSER_TIMEOUT` | `30` | Page load timeout (s) |
| `CAMOUFOX_HUMANIZE` | `true` | Camoufox human-like cursor |
| `CAMOUFOX_BLOCK_WEBRTC` | `true` | Block WebRTC IP leak |
| `CAMOUFOX_OS` | — | Pin spoofed OS (`windows`, `macos`, `linux`) |

### Solving

| Variable | Default | What it does |
|----------|---------|------|
| `CAPTCHA_RETRIES` | `3` | Retries per task |
| `CAPTCHA_TIMEOUT` | `30` | Model call timeout (s) |
| `CAPTCHA_MAX_CONCURRENCY` | `4` | Max concurrent browser solves |
| `CAPTCHA_SOLVE_TIMEOUT` | `180` | Per-task wall-clock budget (s) |
| `HUMAN_PASSIVE_MOTION_SECONDS` | `1.4` | Pre-checkbox motion (s) |
| `VISION_STITCH_GRID` | `true` | Stitch grid tiles into one image |
| `PROXY_MAX_GB` | `0` | Per-proxy bandwidth cap (0 = unlimited) |

### Enterprise hCaptcha

| Variable | Default | What it does |
|----------|---------|------|
| `ENTERPRISE_REQUIRE_HARDENED_RUNTIME` | `false` | Refuse enterprise solve on stock Chromium |
| `ENTERPRISE_FRESH_CONTEXT` | `true` | Fresh context per enterprise solve |
| `HCAPTCHA_DEVICE_PERSISTENCE` | `false` | Re-seed device-trust cookies across solves |
| `HCAPTCHA_INVISIBLE_MOTION_SECONDS` | `3.0` | Motion before invisible execute() |
| `HCAPTCHA_INVISIBLE_PASSIVE_BUDGET` | `4.0` | Passive token wait before challenge (s) |
| `HCAPTCHA_RQDATA_TTL` | `30` | rqdata freshness budget (s) |

### Proxy & egress

Tokens are IP- and UA-bound. Pass a proxy for real sitekeys; the solution echoes the egress so your downstream submit matches:

```jsonc
{
  "type": "HCaptchaTaskProxyless",
  "websiteURL": "https://example.com",
  "websiteKey": "...",
  "proxyType": "http",
  "proxyAddress": "1.2.3.4",
  "proxyPort": 8080,
  "proxyLogin": "user",
  "proxyPassword": "pass",
  "rqdata": "...",                    // enterprise
  "enterprisePayload": { "sentry": true }
}
```

Response includes `solution.userAgent`, `solution.proxyKind`, `solution.egressServer`.

---

## Deployment

### Local

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install --with-deps chromium
# optional: pip install camoufox==0.4.11 && python -m camoufox fetch
export CLIENT_KEY="your-key"
python main.py
```

### Docker (Render)

```bash
docker build -f Dockerfile.render -t captchai .
docker run -p 8000:8000 -e CLIENT_KEY=your-key captchai
```

One-click deploy via [`render.yaml`](render.yaml).

### Camoufox (enterprise hCaptcha)

```bash
pip install camoufox==0.4.11
python -m camoufox fetch
export BROWSER_RUNTIME=camoufox
export BROWSER_RUNTIME_STRICT=true
python main.py
```

---

## Development

```bash
# tests (12k+ lines, ~100 tests)
python -m pytest tests/ -q

# type check
npx pyright

# docs
python -m mkdocs build --strict
```

---

## Limitations

- In-memory task store, 10-minute TTL — no persistence across restarts
- `minScore` accepted for compatibility but not enforced
- Token validity depends on runtime, IP reputation, and target-site behavior
- Not all commercial solver features are replicated

---

## Disclaimer

This project is for legitimate research, security testing, and educational use only. You are solely responsible for compliance with applicable laws and terms of service. See [DISCLAIMER.md](DISCLAIMER.md).

## License

[MIT](LICENSE)
