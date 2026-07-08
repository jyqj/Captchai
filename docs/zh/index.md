# CaptchAI

<div class="hero hero--light" markdown>

<div class="hero__visual">
  <img src="../assets/captchai-hero.png" alt="CaptchAI — 自托管验证码求解服务">
</div>

<div class="hero__copy" markdown>

## ⚡ 面向自托管场景的 YesCaptcha 风格验证码服务

CaptchAI 将 **FastAPI**、**Playwright** 与 **OpenAI-compatible 多模态模型** 组合为一个全面的验证码求解服务，支持 **19 种任务类型**，适用于 **flow2api** 与类似集成场景。

<div class="hero__actions" markdown>

[快速开始](getting-started.md){ .md-button .md-button--primary }
[API 参考](api-reference.md){ .md-button }
[GitHub](https://github.com/captchai-org/captchai){ .md-button }

</div>

</div>

</div>

## ✨ 项目亮点

<div class="grid cards feature-cards" markdown>

-   :material-api: **YesCaptcha 风格 API**

    ---

    覆盖 reCAPTCHA v2/v3、hCaptcha、Turnstile 和图像分类的异步 `createTask` / `getTaskResult` 语义。

-   :material-google-chrome: **浏览器自动化求解**

    ---

    Playwright + Chromium 为 reCAPTCHA v2/v3、hCaptcha 和 Cloudflare Turnstile 生成令牌。

-   :material-image-search: **多模态图片识别**

    ---

    通过 OpenAI-compatible 视觉模型进行 HCaptcha、reCAPTCHA、FunCaptcha、AWS 图像分类。

-   :material-cloud-outline: **自托管部署**

    ---

    支持本地运行，配合 Render 和 Hugging Face Spaces 指南完成生产部署。

</div>

## 🧠 支持的任务类型

### 浏览器自动化求解（12 种）

| 分类 | 任务类型 |
|------|---------|
| **reCAPTCHA v3** | `RecaptchaV3TaskProxyless`, `RecaptchaV3TaskProxylessM1`, `RecaptchaV3TaskProxylessM1S7`, `RecaptchaV3TaskProxylessM1S9` |
| **reCAPTCHA v3 企业版** | `RecaptchaV3EnterpriseTask`, `RecaptchaV3EnterpriseTaskM1` |
| **reCAPTCHA v2** | `NoCaptchaTaskProxyless`, `RecaptchaV2TaskProxyless`, `RecaptchaV2EnterpriseTaskProxyless` |
| **hCaptcha** | `HCaptchaTaskProxyless` |
| **Cloudflare Turnstile** | `TurnstileTaskProxyless`, `TurnstileTaskProxylessM1` |

### 图片识别（3 种）

| 任务类型 | 说明 |
|---------|------|
| `ImageToTextTask` | 受 Argus 启发的多模态识别 |
| `ImageToTextTaskMuggle` | 文本/字母数字识别 |
| `ImageToTextTaskM1` | 异步图片文本识别 |

### 图像分类（4 种）

| 任务类型 | 说明 |
|---------|------|
| `HCaptchaClassification` | hCaptcha 网格图像分类 |
| `ReCaptchaV2Classification` | reCAPTCHA v2 网格选择 |
| `FunCaptchaClassification` | FunCaptcha 图像选择 |
| `AwsClassification` | AWS 验证码图像分类 |

## 🚀 快速入口

<div class="grid cards feature-cards" markdown>

-   :material-rocket-launch-outline: **快速开始**

    ---

    安装依赖、配置环境变量，并在本地启动服务。

    [打开快速开始](getting-started.md)

-   :material-file-document-outline: **API 参考**

    ---

    查看全部 19 种任务类型、接口和请求格式。

    [打开 API 参考](api-reference.md)

-   :material-play-box-outline: **验收说明**

    ---

    验证 detector 目标流程，并确认 token 返回行为。

    [打开验收指南](acceptance.md)

-   :material-server-outline: **部署指南**

    ---

    按 Render 或 Hugging Face Spaces 路径部署你的服务实例。

    [打开部署指南](deployment/render.md)

</div>

## 📌 范围说明

CaptchAI 实现了**覆盖 19 种任务类型的 YesCaptcha 风格 API**，涵盖 reCAPTCHA v2/v3、hCaptcha、Cloudflare Turnstile 和图像分类。浏览器自动化任务依赖 Playwright，可能需要针对特定目标站点调优。图像分类利用多模态视觉模型，准确性取决于模型质量。
