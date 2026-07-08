# CaptchAI

<div class="hero hero--light" markdown>

<div class="hero__visual">
  <img src="assets/captchai-hero.png" alt="CaptchAI — self-hostable captcha solving service">
</div>

<div class="hero__copy" markdown>

## ⚡ Self-hostable captcha solving with a clean YesCaptcha-style API

CaptchAI combines **FastAPI**, **Playwright**, and **OpenAI-compatible multimodal models** into a focused service for **flow2api** and similar integrations. Supports **19 task types** across reCAPTCHA, hCaptcha, Cloudflare Turnstile, and image classification.

<div class="hero__actions" markdown>

[Get started](getting-started.md){ .md-button .md-button--primary }
[API reference](api-reference.md){ .md-button }
[GitHub](https://github.com/captchai-org/captchai){ .md-button }

</div>

</div>

</div>

## ✨ Highlights

<div class="grid cards feature-cards" markdown>

-   :material-api: **YesCaptcha-style API**

    ---

    Familiar async `createTask` / `getTaskResult` semantics covering reCAPTCHA v2/v3, hCaptcha, Turnstile, and image classification.

-   :material-google-chrome: **Browser-based solving**

    ---

    Playwright + Chromium automate token generation for reCAPTCHA v2/v3, hCaptcha, and Cloudflare Turnstile.

-   :material-image-search: **Multimodal image recognition**

    ---

    Route image captcha analysis through OpenAI-compatible vision models for HCaptcha, reCAPTCHA, FunCaptcha, and AWS classification.

-   :material-cloud-outline: **Self-hosted deployment**

    ---

    Run locally or follow the included Render and Hugging Face Spaces deployment guides.

</div>

## 🧠 Supported task types

### Browser-based solving (12 types)

| Category | Task Types |
|----------|-----------|
| **reCAPTCHA v3** | `RecaptchaV3TaskProxyless`, `RecaptchaV3TaskProxylessM1`, `RecaptchaV3TaskProxylessM1S7`, `RecaptchaV3TaskProxylessM1S9` |
| **reCAPTCHA v3 Enterprise** | `RecaptchaV3EnterpriseTask`, `RecaptchaV3EnterpriseTaskM1` |
| **reCAPTCHA v2** | `NoCaptchaTaskProxyless`, `RecaptchaV2TaskProxyless`, `RecaptchaV2EnterpriseTaskProxyless` |
| **hCaptcha** | `HCaptchaTaskProxyless` |
| **Cloudflare Turnstile** | `TurnstileTaskProxyless`, `TurnstileTaskProxylessM1` |

### Image recognition (3 types)

| Task Type | Description |
|-----------|-------------|
| `ImageToTextTask` | Argus-inspired multimodal recognition for click, slide, and drag captchas |
| `ImageToTextTaskMuggle` | Text/alphanumeric image recognition |
| `ImageToTextTaskM1` | Async image text recognition |

### Image classification (4 types)

| Task Type | Description |
|-----------|-------------|
| `HCaptchaClassification` | hCaptcha grid image classification |
| `ReCaptchaV2Classification` | reCAPTCHA v2 grid cell selection |
| `FunCaptchaClassification` | FunCaptcha image selection |
| `AwsClassification` | AWS CAPTCHA image classification |

## 🚀 Quick paths

<div class="grid cards feature-cards" markdown>

-   :material-rocket-launch-outline: **Quick start**

    ---

    Install dependencies, configure environment variables, and launch the service locally.

    [Open quick start](getting-started.md)

-   :material-file-document-outline: **API reference**

    ---

    Review all 19 supported task types, endpoints, and request formats.

    [Open API reference](api-reference.md)

-   :material-play-box-outline: **Acceptance**

    ---

    Validate detector-target behavior and confirm token generation flow.

    [Open acceptance guide](acceptance.md)

-   :material-server-outline: **Deployment**

    ---

    Follow the Render or Hugging Face Spaces guides for a production-facing instance.

    [Open deployment guide](deployment/render.md)

</div>

## 📌 Scope note

CaptchAI implements a **YesCaptcha-style API surface covering 19 task types** across reCAPTCHA v2/v3, hCaptcha, Cloudflare Turnstile, and image classification. Browser-based tasks rely on Playwright automation and may require tuning for specific target sites. Image classification leverages multimodal vision models and accuracy depends on model quality.
