<p align="center">
  <img src="https://img.shields.io/badge/CaptchAI-YesCaptcha--style%20API-2F6BFF?style=for-the-badge" alt="CaptchAI">
  <br/>
  <img src="https://img.shields.io/badge/version-3.0-22C55E?style=flat-square" alt="Version">
  <img src="https://img.shields.io/badge/license-MIT-2563EB?style=flat-square" alt="License">
  <img src="https://img.shields.io/badge/task%20types-19-F59E0B?style=flat-square" alt="Task Types">
  <img src="https://img.shields.io/badge/runtime-FastAPI%20%7C%20Playwright%20%7C%20OpenAI--compatible-7C3AED?style=flat-square" alt="Runtime">
  <img src="https://img.shields.io/badge/stealth-Camoufox%20%7C%20rebrowser-EF4444?style=flat-square" alt="Stealth runtimes">
  <img src="https://img.shields.io/badge/docs-bilingual-2563EB?style=flat-square" alt="Docs">
</p>

<h1 align="center">🧩 CaptchAI</h1>

<p align="center">
  <strong>Self-hostable, YesCaptcha-compatible captcha solver for <a href="https://github.com/TheSmallHanCat/flow2api">flow2api</a> and similar integrations</strong>
  <br/>
  <em>19 task types · reCAPTCHA v2/v3 · hCaptcha (incl. enterprise) · Cloudflare Turnstile · image classification</em>
</p>

<p align="center">
  <a href="#-quick-start">Quick Start</a> •
  <a href="#-architecture">Architecture</a> •
  <a href="#-task-types">Task Types</a> •
  <a href="#-stealth--anti-detection">Stealth</a> •
  <a href="#-configuration">Configuration</a> •
  <a href="#-deployment">Deployment</a>
</p>

<p align="center">
  <a href="README.zh-CN.md">中文说明</a> •
  <a href="https://github.com/jyqj/Captchai/tree/main/docs">Documentation</a> •
  <a href="https://github.com/jyqj/Captchai/blob/main/docs/deployment/render.md">Render Guide</a> •
  <a href="https://github.com/jyqj/Captchai/blob/main/docs/deployment/huggingface.md">Hugging Face Guide</a>
</p>

<p align="center">
  <img src="docs/assets/captchai-hero.png" alt="CaptchAI" width="680">
</p>

---

## ✨ What Is This?

**CaptchAI** is a self-hosted captcha-solving service that speaks the **YesCaptcha async API** (`createTask` / `getTaskResult` / `getBalance`) across **19 task types**. Drop it in as the third-party solver for **flow2api** or anything that expects that protocol.

Two things set it apart from a thin browser wrapper:

- **It's built for real, IP- and fingerprint-bound tokens** — hardened browser runtimes, coherent per-context fingerprints, proxy/User-Agent binding, and human-like interaction — not just a headless Chromium that anti-bot systems flag on sight.
- **It's cost-aware** — image challenges route to a cheap local vision model first and only escalate hard grids to a cloud model, with a per-solve cost ledger.

| Capability | Details |
|-----------|---------|
| **Browser automation** | Playwright for reCAPTCHA v2/v3, hCaptcha, Cloudflare Turnstile |
| **Hardened runtimes** | Stock Chromium, `rebrowser`, or **Camoufox** (Firefox, engine-level anti-fingerprint) for enterprise targets |
| **Image recognition** | Local multimodal model (Qwen3.5-2B via SGLang) for image-to-text captchas |
| **Image classification** | Local vision model for hCaptcha / reCAPTCHA v2 / FunCaptcha / AWS grids |
| **API compatibility** | Full YesCaptcha `createTask` / `getTaskResult` / `getBalance` protocol |
| **Deployment** | Local, Render, Hugging Face Spaces — Docker-ready |

---

## 📦 Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install --with-deps chromium

# Local model (self-hosted via SGLang) — image tasks
export LOCAL_BASE_URL="http://localhost:30000/v1"
export LOCAL_MODEL="Qwen/Qwen3.5-2B"

# Cloud model (OpenAI-compatible) — hard-grid escalation
export CLOUD_BASE_URL="https://your-openai-compatible-endpoint/v1"
export CLOUD_API_KEY="your-api-key"
export CLOUD_MODEL="gpt-5.4"

export CLIENT_KEY="your-client-key"
python main.py
```

Verify:

```bash
curl http://localhost:8000/api/v1/health
```

---

## 🏗 Architecture

<p align="center">
  <img src="docs/assets/captchai-diagram.png" alt="CaptchAI architecture" width="560">
</p>

A FastAPI front end accepts YesCaptcha tasks and hands them to a solver, which drives a shared browser (one process, an isolated context per solve) and delegates image work to the vision layer.

| Component | Responsibility |
|-----------|----------------|
| **FastAPI** | HTTP API implementing the YesCaptcha protocol |
| **TaskManager** | Async in-memory task queue, 10-minute TTL |
| **BrowserManager** | One shared browser; per-solve context with proxy + coherent fingerprint |
| **RecaptchaV3 / V2 Solver** | Playwright reCAPTCHA v3/enterprise token gen and v2 checkbox solving |
| **HCaptchaSolver** | hCaptcha checkbox → passive → visual-challenge dispatch (incl. enterprise `rqdata`) |
| **TurnstileSolver** | Cloudflare Turnstile widget solving |
| **VisionRouter** | Local-first image analysis; escalates hard grids to cloud with self-consistency voting |
| **ClassificationSolver** | Vision-model image classification |

---

## 🧠 Task Types

### Browser-based solving (12)

| Category | Task Types | Solution Field |
|----------|-----------|----------------|
| reCAPTCHA v3 | `RecaptchaV3TaskProxyless`, `…M1`, `…M1S7`, `…M1S9` | `gRecaptchaResponse` |
| reCAPTCHA v3 Enterprise | `RecaptchaV3EnterpriseTask`, `…M1` | `gRecaptchaResponse` |
| reCAPTCHA v2 | `NoCaptchaTaskProxyless`, `RecaptchaV2TaskProxyless`, `RecaptchaV2EnterpriseTaskProxyless` | `gRecaptchaResponse` |
| hCaptcha | `HCaptchaTaskProxyless` | `gRecaptchaResponse` |
| Cloudflare Turnstile | `TurnstileTaskProxyless`, `TurnstileTaskProxylessM1` | `token` |

### Image recognition (3)

| Task Type | Solution Field |
|-----------|----------------|
| `ImageToTextTask` | `text` (structured JSON) |
| `ImageToTextTaskMuggle` | `text` |
| `ImageToTextTaskM1` | `text` |

### Image classification (4)

| Task Type | Solution Field |
|-----------|----------------|
| `HCaptchaClassification` | `objects` / `answer` |
| `ReCaptchaV2Classification` | `objects` |
| `FunCaptchaClassification` | `objects` |
| `AwsClassification` | `objects` |

---

## 🔌 API Surface

| Endpoint | Purpose |
|----------|---------|
| `POST /createTask` | Create an async captcha task |
| `POST /getTaskResult` | Poll task execution result |
| `POST /getBalance` | Return compatibility balance |
| `GET /api/v1/health` | Health and service status |

<details>
<summary><strong>Example: reCAPTCHA v3</strong></summary>

```bash
curl -X POST http://localhost:8000/createTask \
  -H "Content-Type: application/json" \
  -d '{
    "clientKey": "your-client-key",
    "task": {
      "type": "RecaptchaV3TaskProxyless",
      "websiteURL": "https://antcpt.com/score_detector/",
      "websiteKey": "6LcR_okUAAAAAPYrPe-HK_0RULO1aZM15ENyM-Mf",
      "pageAction": "homepage"
    }
  }'
```
</details>

<details>
<summary><strong>Example: hCaptcha</strong></summary>

```bash
curl -X POST http://localhost:8000/createTask \
  -H "Content-Type: application/json" \
  -d '{
    "clientKey": "your-client-key",
    "task": {
      "type": "HCaptchaTaskProxyless",
      "websiteURL": "https://example.com",
      "websiteKey": "hcaptcha-site-key"
    }
  }'
```
</details>

<details>
<summary><strong>Example: Cloudflare Turnstile</strong></summary>

```bash
curl -X POST http://localhost:8000/createTask \
  -H "Content-Type: application/json" \
  -d '{
    "clientKey": "your-client-key",
    "task": {
      "type": "TurnstileTaskProxyless",
      "websiteURL": "https://example.com",
      "websiteKey": "turnstile-site-key"
    }
  }'
```
</details>

<details>
<summary><strong>Example: image classification</strong></summary>

```bash
curl -X POST http://localhost:8000/createTask \
  -H "Content-Type: application/json" \
  -d '{
    "clientKey": "your-client-key",
    "task": {
      "type": "ReCaptchaV2Classification",
      "image": "<base64-encoded-image>",
      "question": "Select all images with traffic lights"
    }
  }'
```
</details>

<details>
<summary><strong>Poll result</strong></summary>

```bash
curl -X POST http://localhost:8000/getTaskResult \
  -H "Content-Type: application/json" \
  -d '{"clientKey": "your-client-key", "taskId": "uuid-from-createTask"}'
```
</details>

---

## 🕵️ Stealth & Anti-Detection

Real sitekeys — especially enterprise hCaptcha (e.g. Stripe Radar) and Cloudflare — score the browser, not just the answer. CaptchAI's solve path is built to survive that scoring:

- **Hardened browser runtimes.** `BROWSER_RUNTIME=camoufox` runs a patched Firefox build that spoofs navigator/WebGL/canvas/screen at the **engine level** (no injected JS to detect); `rebrowser` is a patched-Chromium option. Stock Chromium's automation and software-WebGL signals are trivially flagged, so enterprise solves should use a hardened runtime.
- **Camoufox isolated-world DOM bridge.** Camoufox runs page scripts in an isolated world, so the token handoff goes through hidden **DOM elements** rather than `window.*` globals — which an isolated-world `evaluate` cannot read. This is what makes token capture, the invisible `execute()` trigger, and error surfacing actually work on the Firefox-based hardened runtime.
- **Coherent per-context fingerprint.** User-Agent, `navigator.platform`, WebGL vendor/renderer, languages, and timezone are drawn from one consistent profile per context — not one hard-coded stealth blob reused everywhere (itself a signal).
- **Proxy + User-Agent binding.** Tokens are IP- and UA-bound; the solve runs through the task/pool proxy and echoes back the exact `userAgent` and egress identity so your downstream submit matches.
- **WebRTC leak guard.** WebRTC is forced through the proxy (or blocked under Camoufox) so the host's real pre-proxy IP never leaks past the egress.
- **Human-like interaction.** Pointer paths and pre-`execute()` motion seed a real `motionData` buffer, so passive hCaptcha scoring doesn't see an empty-motion "bot" browser.
- **Real-page mode.** Optionally navigate the real target and hook the widget's own `render` (resource-blocking off) for sites whose own anti-bot JS must run.

See the [Configuration](#-configuration) table for the knobs that drive all of this.

---

## ⚙️ Configuration

### Model backends

CaptchAI uses a **local model** for image tasks and a **cloud model** for hard-grid escalation:

| Variable | Description | Default |
|----------|-------------|---------|
| `LOCAL_BASE_URL` | Local inference server (SGLang/vLLM) | `http://localhost:30000/v1` |
| `LOCAL_API_KEY` | Local server API key | `EMPTY` |
| `LOCAL_MODEL` | Local model name | `Qwen/Qwen3.5-2B` |
| `CLOUD_BASE_URL` | Cloud API base URL | External endpoint |
| `CLOUD_API_KEY` | Cloud API key | unset |
| `CLOUD_MODEL` | Cloud model name | `gpt-5.4` |

### General

| Variable | Description | Default |
|----------|-------------|---------|
| `CLIENT_KEY` | Client authentication key | unset |
| `CAPTCHA_RETRIES` | Retry count | `3` |
| `CAPTCHA_TIMEOUT` | Model timeout (seconds) | `30` |
| `CAPTCHA_MAX_CONCURRENCY` | Max concurrent browser solves | `4` |
| `CAPTCHA_SOLVE_TIMEOUT` | Per-task wall-clock budget (seconds) | `180` |
| `SERVER_HOST` / `SERVER_PORT` | Bind host / port | `0.0.0.0` / `8000` |

### Browser & stealth

| Variable | Description | Default |
|----------|-------------|---------|
| `BROWSER_HEADLESS` | Headless browser | `true` |
| `BROWSER_TIMEOUT` | Page load timeout (seconds) | `30` |
| `BROWSER_RUNTIME` | `chromium` (stock) \| `rebrowser` \| `camoufox` | `chromium` |
| `BROWSER_RUNTIME_STRICT` | Fail startup instead of silently degrading to stock Chromium when a hardened runtime is unavailable (recommended for enterprise hCaptcha) | `false` |
| `CAMOUFOX_HUMANIZE` | Camoufox built-in human-like cursor motion | `true` |
| `CAMOUFOX_BLOCK_WEBRTC` | Block WebRTC under Camoufox to prevent an IP leak past the proxy | `true` |
| `CAMOUFOX_OS` | Optional OS pin for Camoufox's spoofed fingerprint (`windows`/`macos`/`linux`; empty = randomise) | unset |
| `HUMAN_PASSIVE_MOTION_SECONDS` | Seconds of pre-checkbox wander/scroll seeded into `motionData` | `1.4` |
| `VISION_STITCH_GRID` | Compose grid tiles into one montage per model call (cheaper); `false` for max per-tile resolution | `true` |
| `PROXY_MAX_GB` | Per-proxy bandwidth quota (GB) before the proxy is burned; `0` = unlimited | `0` |

### Enterprise hCaptcha

| Variable | Description | Default |
|----------|-------------|---------|
| `ENTERPRISE_REQUIRE_HARDENED_RUNTIME` | Refuse (not just warn about) an enterprise solve on stock Chromium | `false` |
| `ENTERPRISE_FRESH_CONTEXT` | Fresh browser context per enterprise solve (avoids reusing one sticky session on a sitekey) | `true` |
| `HCAPTCHA_DEVICE_PERSISTENCE` | Re-seed the hCaptcha device-trust cookie (`hmt`) per egress into fresh contexts, so a solve presents a **returning** device (in-memory only) | `false` |
| `HCAPTCHA_INVISIBLE_MOTION_SECONDS` | Seconds of motion seeded before an invisible widget's `execute()` | `3.0` |
| `HCAPTCHA_INVISIBLE_PASSIVE_BUDGET` | Passive-token wait (seconds) before falling through to a visual challenge | `4.0` |
| `HCAPTCHA_RQDATA_TTL` | Enterprise `rqdata` freshness budget (seconds); slower solves get an "may have expired" warning. `0` disables | `30` |

> Legacy vars (`CAPTCHA_BASE_URL`, `CAPTCHA_API_KEY`, `CAPTCHA_MODEL`, `CAPTCHA_MULTIMODAL_MODEL`) are still honoured as fallbacks.

### Proxy & User-Agent binding

Cloudflare, Google, and hCaptcha **Enterprise** bind tokens to the **egress IP** and **User-Agent** at solve time. For real sitekeys, pass a proxy and reuse the returned `solution.userAgent` (and the same IP) when you submit the token downstream. The solution echoes the egress so you can align an IP-bound submit:

- `solution.proxyKind` — `proxyless` \| `pool_proxy` \| `task_proxy`
- `solution.egressServer` — the credential-free proxy gateway (`scheme://host:port`) that minted the token, or `null` for proxyless solves.

> **Enterprise hCaptcha (e.g. Stripe):** supply your own proxy (`egress=task`) so the solve and your submit share one IP, or route your submit through the returned `egressServer` — a token minted on a different IP than the submit is rejected. Run `BROWSER_RUNTIME=camoufox` + `BROWSER_RUNTIME_STRICT=true`.

```jsonc
"task": {
  "type": "TurnstileTaskProxyless",
  "websiteURL": "https://example.com",
  "websiteKey": "0x4AAA...",
  "action": "login",          // if the widget sets one
  "cData": "…",               // if the widget sets one
  "userAgent": "Mozilla/5.0 … Chrome/149.0.0.0 Safari/537.36",
  "proxyType": "http", "proxyAddress": "1.2.3.4", "proxyPort": 8080,
  "proxyLogin": "user", "proxyPassword": "pass"
}
```

---

## 🚀 Deployment

- [Local model (SGLang)](https://github.com/jyqj/Captchai/blob/main/docs/deployment/local-model.md) — deploy Qwen3.5-2B locally
- [Render deployment](https://github.com/jyqj/Captchai/blob/main/docs/deployment/render.md)
- [Hugging Face Spaces deployment](https://github.com/jyqj/Captchai/blob/main/docs/deployment/huggingface.md)
- [Full documentation](https://github.com/jyqj/Captchai/tree/main/docs)

---

## ✅ Test Target

Validated against the public reCAPTCHA v3 score detector:

- URL: `https://antcpt.com/score_detector/`
- Site key: `6LcR_okUAAAAAPYrPe-HK_0RULO1aZM15ENyM-Mf`

---

## ⚠️ Limitations

- Tasks are stored **in memory** with a 10-minute TTL
- `minScore` is accepted for compatibility but not enforced
- Browser-based solving depends on environment, IP reputation, and target-site behavior
- Image classification quality depends on the vision model used
- Not all commercial captcha-service features are replicated

---

## 🔧 Development

```bash
pytest tests/
npx pyright
python -m mkdocs build --strict
```

---

## 📢 Disclaimer

> **This project is intended for legitimate research, security testing, and educational purposes only.**

- CaptchAI is a self-hostable tool. You are solely responsible for how you deploy and use it.
- CAPTCHA systems exist to protect services from abuse. **Do not use this tool to bypass CAPTCHAs without explicit permission from the site owner.**
- Unauthorized automated access to third-party services may violate their Terms of Service and applicable laws (e.g. the Computer Fraud and Abuse Act, GDPR, or local equivalents).
- The authors and contributors **accept no liability** for any misuse, legal consequences, or damages arising from use of this software.

See [DISCLAIMER.md](DISCLAIMER.md) for full terms.

---

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=jyqj/Captchai&type=Date)](https://www.star-history.com/#jyqj/Captchai&Date)

---

## 📄 License

[MIT](LICENSE) — use freely, modify openly, deploy carefully.
