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
  <strong>面向 <a href="https://github.com/TheSmallHanCat/flow2api">flow2api</a> 及类似集成场景的可自托管、YesCaptcha 兼容验证码服务</strong>
  <br/>
  <em>19 种任务类型 · reCAPTCHA v2/v3 · hCaptcha（含企业版）· Cloudflare Turnstile · 图像分类</em>
</p>

<p align="center">
  <a href="#-快速开始">快速开始</a> •
  <a href="#-架构">架构</a> •
  <a href="#-任务类型">任务类型</a> •
  <a href="#-反检测与隐身">反检测</a> •
  <a href="#-配置项">配置项</a> •
  <a href="#-部署">部署</a>
</p>

<p align="center">
  <a href="README.md">English README</a> •
  <a href="https://github.com/jyqj/Captchai/tree/main/docs/zh">在线文档</a> •
  <a href="https://github.com/jyqj/Captchai/blob/main/docs/zh/deployment/render.md">Render 部署指南</a> •
  <a href="https://github.com/jyqj/Captchai/blob/main/docs/zh/deployment/huggingface.md">Hugging Face Spaces 指南</a>
</p>

<p align="center">
  <img src="docs/assets/captchai-hero.png" alt="CaptchAI" width="680">
</p>

---

## ✨ 这是什么？

**CaptchAI** 是一个可直接自托管的验证码求解服务，讲 **YesCaptcha 异步 API**（`createTask` / `getTaskResult` / `getBalance`），覆盖 **19 种任务类型**。可作为 **flow2api** 或任何遵循该协议的系统的第三方打码后端直接接入。

它与"套壳无头浏览器"的两点关键区别：

- **面向真实、绑定 IP 与指纹的令牌**——硬化浏览器运行时、每上下文一致指纹、代理/User-Agent 绑定、拟人交互，而不是一上来就被反爬系统识别的裸 Chromium。
- **成本可控**——图像挑战优先走廉价的本地视觉模型，只有困难网格才升级到云端模型，并附带逐次求解的成本账本。

| 能力 | 详情 |
|------|------|
| **浏览器自动化** | Playwright 求解 reCAPTCHA v2/v3、hCaptcha、Cloudflare Turnstile |
| **硬化运行时** | 支持原生 Chromium、`rebrowser`，以及 **Camoufox**（Firefox，引擎级反指纹）应对企业级目标 |
| **图片识别** | 本地多模态模型（通过 SGLang 部署 Qwen3.5-2B）处理图转文验证码 |
| **图像分类** | 本地视觉模型进行 hCaptcha / reCAPTCHA v2 / FunCaptcha / AWS 网格分类 |
| **API 兼容** | 完整的 YesCaptcha `createTask` / `getTaskResult` / `getBalance` 协议 |
| **部署方式** | 本地、Render、Hugging Face Spaces —— 开箱即用的 Docker 支持 |

---

## 📦 快速开始

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install --with-deps chromium

# 本地模型（通过 SGLang 自托管）—— 图像任务
export LOCAL_BASE_URL="http://localhost:30000/v1"
export LOCAL_MODEL="Qwen/Qwen3.5-2B"

# 云端模型（OpenAI 兼容）—— 困难网格升级
export CLOUD_BASE_URL="https://your-openai-compatible-endpoint/v1"
export CLOUD_API_KEY="your-api-key"
export CLOUD_MODEL="gpt-5.4"

export CLIENT_KEY="your-client-key"
python main.py
```

验证服务：

```bash
curl http://localhost:8000/api/v1/health
```

---

## 🏗 架构

<p align="center">
  <img src="docs/assets/captchai-diagram.png" alt="CaptchAI 架构图" width="560">
</p>

FastAPI 前端接收 YesCaptcha 任务并交给对应求解器；求解器驱动一个共享浏览器（单进程、每次求解独立上下文），并把图像工作委托给视觉层。

| 组件 | 职责 |
|------|------|
| **FastAPI** | 实现 YesCaptcha 协议的 HTTP API |
| **TaskManager** | 异步内存任务队列，10 分钟 TTL |
| **BrowserManager** | 单一共享浏览器；每次求解绑定代理 + 一致指纹的独立上下文 |
| **RecaptchaV3 / V2 求解器** | Playwright 生成 reCAPTCHA v3/企业版令牌、求解 v2 复选框 |
| **HCaptchaSolver** | hCaptcha 复选框 → 被动 → 视觉挑战分发（含企业版 `rqdata`） |
| **TurnstileSolver** | Cloudflare Turnstile 组件求解 |
| **VisionRouter** | 本地优先的图像分析；困难网格升级到云端并做自一致投票 |
| **ClassificationSolver** | 基于视觉模型的图像分类 |

---

## 🧠 任务类型

### 浏览器自动化求解（12 种）

| 分类 | 任务类型 | 返回字段 |
|------|---------|---------|
| reCAPTCHA v3 | `RecaptchaV3TaskProxyless`, `…M1`, `…M1S7`, `…M1S9` | `gRecaptchaResponse` |
| reCAPTCHA v3 企业版 | `RecaptchaV3EnterpriseTask`, `…M1` | `gRecaptchaResponse` |
| reCAPTCHA v2 | `NoCaptchaTaskProxyless`, `RecaptchaV2TaskProxyless`, `RecaptchaV2EnterpriseTaskProxyless` | `gRecaptchaResponse` |
| hCaptcha | `HCaptchaTaskProxyless` | `gRecaptchaResponse` |
| Cloudflare Turnstile | `TurnstileTaskProxyless`, `TurnstileTaskProxylessM1` | `token` |

### 图片识别（3 种）

| 任务类型 | 返回字段 |
|---------|---------|
| `ImageToTextTask` | `text`（结构化 JSON） |
| `ImageToTextTaskMuggle` | `text` |
| `ImageToTextTaskM1` | `text` |

### 图像分类（4 种）

| 任务类型 | 返回字段 |
|---------|---------|
| `HCaptchaClassification` | `objects` / `answer` |
| `ReCaptchaV2Classification` | `objects` |
| `FunCaptchaClassification` | `objects` |
| `AwsClassification` | `objects` |

---

## 🔌 API 接口

| 接口 | 作用 |
|------|------|
| `POST /createTask` | 创建异步验证码任务 |
| `POST /getTaskResult` | 轮询任务执行结果 |
| `POST /getBalance` | 返回兼容性余额 |
| `GET /api/v1/health` | 健康状态检查 |

<details>
<summary><strong>示例：reCAPTCHA v3</strong></summary>

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
<summary><strong>示例：hCaptcha</strong></summary>

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
<summary><strong>示例：Cloudflare Turnstile</strong></summary>

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
<summary><strong>示例：图像分类</strong></summary>

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
<summary><strong>轮询结果</strong></summary>

```bash
curl -X POST http://localhost:8000/getTaskResult \
  -H "Content-Type: application/json" \
  -d '{"clientKey": "your-client-key", "taskId": "uuid-from-createTask"}'
```
</details>

---

## 🕵️ 反检测与隐身

真实 sitekey ——尤其是企业级 hCaptcha（如 Stripe Radar）和 Cloudflare ——评分的是**浏览器本身**，而不只是答案。CaptchAI 的求解链路正是为了扛住这种评分而设计：

- **硬化浏览器运行时。** `BROWSER_RUNTIME=camoufox` 使用打过补丁的 Firefox，在**引擎级别**伪造 navigator/WebGL/canvas/screen（没有可被检测的注入脚本）；`rebrowser` 则是打补丁的 Chromium 方案。原生 Chromium 的自动化与软件 WebGL 信号极易被识别，因此企业级求解应使用硬化运行时。
- **Camoufox 隔离世界 DOM 桥接。** Camoufox 在隔离世界执行页面脚本，因此令牌交接改走隐藏的 **DOM 元素**而非 `window.*` 全局变量——隔离世界的 `evaluate` 读不到那些全局变量。这正是令牌捕获、隐身 `execute()` 触发和错误上报能在 Firefox 硬化运行时上真正工作的原因。
- **每上下文一致指纹。** User-Agent、`navigator.platform`、WebGL 厂商/渲染器、语言、时区都取自同一份连贯画像，而不是把一份写死的隐身脚本到处复用（后者本身就是信号）。
- **代理 + User-Agent 绑定。** 令牌绑定 IP 与 UA；求解走任务/代理池代理，并回显所用的 `userAgent` 与出口身份，方便你在下游提交时保持一致。
- **WebRTC 泄漏防护。** WebRTC 被强制走代理（Camoufox 下则直接屏蔽），使宿主机代理前的真实 IP 不会绕过出口泄漏。
- **拟人交互。** 指针轨迹与 `execute()` 前的运动会填充真实的 `motionData` 缓冲，避免被动 hCaptcha 评分看到"空运动"的机器人浏览器。
- **真实页面模式。** 可选择导航到真实目标页并 hook 其自身的 `render`（关闭资源拦截），适配那些必须运行自有反爬 JS 的站点。

各项开关详见 [配置项](#-配置项) 表格。

---

## ⚙️ 配置项

### 模型后端

CaptchAI 用**本地模型**处理图像任务、**云端模型**处理困难网格升级：

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `LOCAL_BASE_URL` | 本地推理服务地址（SGLang/vLLM） | `http://localhost:30000/v1` |
| `LOCAL_API_KEY` | 本地服务密钥 | `EMPTY` |
| `LOCAL_MODEL` | 本地模型名称 | `Qwen/Qwen3.5-2B` |
| `CLOUD_BASE_URL` | 云端 API 基地址 | 外部端点 |
| `CLOUD_API_KEY` | 云端 API 密钥 | 未设置 |
| `CLOUD_MODEL` | 云端模型名称 | `gpt-5.4` |

### 通用

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `CLIENT_KEY` | 客户端认证密钥 | 未设置 |
| `CAPTCHA_RETRIES` | 重试次数 | `3` |
| `CAPTCHA_TIMEOUT` | 模型请求超时（秒） | `30` |
| `CAPTCHA_MAX_CONCURRENCY` | 最大并发浏览器求解数 | `4` |
| `CAPTCHA_SOLVE_TIMEOUT` | 单任务墙钟预算（秒） | `180` |
| `SERVER_HOST` / `SERVER_PORT` | 监听地址 / 端口 | `0.0.0.0` / `8000` |

### 浏览器与隐身

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `BROWSER_HEADLESS` | 无头浏览器 | `true` |
| `BROWSER_TIMEOUT` | 页面加载超时（秒） | `30` |
| `BROWSER_RUNTIME` | `chromium`（原生）\| `rebrowser` \| `camoufox` | `chromium` |
| `BROWSER_RUNTIME_STRICT` | 硬化运行时不可用时启动直接失败，而非静默降级到原生 Chromium（企业级 hCaptcha 推荐开启） | `false` |
| `CAMOUFOX_HUMANIZE` | Camoufox 内建拟人光标运动 | `true` |
| `CAMOUFOX_BLOCK_WEBRTC` | Camoufox 下屏蔽 WebRTC，防止 IP 绕过代理泄漏 | `true` |
| `CAMOUFOX_OS` | 可选，为 Camoufox 伪造指纹锁定 OS（`windows`/`macos`/`linux`；留空则随机） | 未设置 |
| `HUMAN_PASSIVE_MOTION_SECONDS` | 点击复选框前注入 `motionData` 的游走/滚动秒数 | `1.4` |
| `VISION_STITCH_GRID` | 把网格瓦片拼成一张图再喂给模型（更省）；设为 `false` 保留每瓦片最高分辨率 | `true` |
| `PROXY_MAX_GB` | 单代理带宽配额（GB），超出则烧毁（移出轮换）；`0` 表示不限 | `0` |

### 企业级 hCaptcha

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `ENTERPRISE_REQUIRE_HARDENED_RUNTIME` | 在原生 Chromium 上拒绝（而非仅警告）企业级求解 | `false` |
| `ENTERPRISE_FRESH_CONTEXT` | 每次企业级求解使用全新上下文（避免在同一 sitekey 上复用粘性会话） | `true` |
| `HCAPTCHA_DEVICE_PERSISTENCE` | 按出口身份把 hCaptcha 设备信任 cookie（`hmt`）重新注入全新上下文，使求解呈现为**回访**设备（仅内存） | `false` |
| `HCAPTCHA_INVISIBLE_MOTION_SECONDS` | 隐身组件 `execute()` 前注入的运动秒数 | `3.0` |
| `HCAPTCHA_INVISIBLE_PASSIVE_BUDGET` | 隐身求解在跌落到视觉挑战前的被动令牌等待（秒） | `4.0` |
| `HCAPTCHA_RQDATA_TTL` | 企业版 `rqdata` 新鲜度预算（秒）；较慢的求解会附带"可能已过期"警告，`0` 表示禁用 | `30` |

> 旧版变量（`CAPTCHA_BASE_URL`、`CAPTCHA_API_KEY`、`CAPTCHA_MODEL`、`CAPTCHA_MULTIMODAL_MODEL`）仍作为回退被支持。

### 代理与 User-Agent 绑定

Cloudflare、Google 以及 hCaptcha **企业版**会把令牌绑定到求解时的**出口 IP** 与 **User-Agent**。对真实 sitekey，请传入代理并在下游提交令牌时复用返回的 `solution.userAgent`（以及同一 IP）。solution 会回显出口身份，方便你对齐受 IP 绑定的提交：

- `solution.proxyKind` —— `proxyless` \| `pool_proxy` \| `task_proxy`
- `solution.egressServer` —— 铸造该令牌的免凭据代理网关（`scheme://host:port`），proxyless 求解时为 `null`。

> **企业级 hCaptcha（如 Stripe）：** 自备代理（`egress=task`）让求解与提交共用一个 IP，或让下游提交走返回的 `egressServer`——铸造 IP 与提交 IP 不一致的令牌会被拒。请开启 `BROWSER_RUNTIME=camoufox` + `BROWSER_RUNTIME_STRICT=true`。

```jsonc
"task": {
  "type": "TurnstileTaskProxyless",
  "websiteURL": "https://example.com",
  "websiteKey": "0x4AAA...",
  "action": "login",          // 若组件设置了则填
  "cData": "…",               // 若组件设置了则填
  "userAgent": "Mozilla/5.0 … Chrome/149.0.0.0 Safari/537.36",
  "proxyType": "http", "proxyAddress": "1.2.3.4", "proxyPort": 8080,
  "proxyLogin": "user", "proxyPassword": "pass"
}
```

---

## 🚀 部署

- [本地模型 (SGLang)](https://github.com/jyqj/Captchai/blob/main/docs/zh/deployment/local-model.md) —— 本地部署 Qwen3.5-2B
- [Render 部署](https://github.com/jyqj/Captchai/blob/main/docs/zh/deployment/render.md)
- [Hugging Face Spaces 部署](https://github.com/jyqj/Captchai/blob/main/docs/zh/deployment/huggingface.md)
- [完整文档](https://github.com/jyqj/Captchai/tree/main/docs/zh)

---

## ✅ 测试目标

本服务针对以下公开 reCAPTCHA v3 检测目标完成验证：

- URL：`https://antcpt.com/score_detector/`
- Site key：`6LcR_okUAAAAAPYrPe-HK_0RULO1aZM15ENyM-Mf`

---

## ⚠️ 限制说明

- 任务状态保存在**内存中**，TTL 为 10 分钟
- `minScore` 为兼容性字段，当前不做分数控制
- 浏览器自动化的稳定性取决于运行环境、IP 信誉和目标站行为
- 图像分类质量取决于所使用的视觉模型
- 并非所有商业打码平台功能均已复现

---

## 🔧 开发

```bash
pytest tests/
npx pyright
python -m mkdocs build --strict
```

---

## 📢 免责声明

> **本项目仅供合法的安全研究、渗透测试和技术学习使用。**

- CaptchAI 是一个可自托管的工具。你对自己的部署方式和使用行为负完全责任。
- CAPTCHA 系统的存在是为了保护服务免受滥用。**未经目标网站或服务所有者明确授权，请勿使用本工具绕过 CAPTCHA。**
- 未经授权地对第三方服务进行自动化访问，可能违反其服务条款，并可能在相关法律管辖区（如《计算机欺诈与滥用法》、GDPR 或当地等效法规）下构成违法行为。
- 本项目的作者和贡献者**不承担任何因使用本软件而导致的滥用行为、法律后果或损失的责任**。

完整使用条款与免责声明请参阅 [DISCLAIMER.zh-CN.md](DISCLAIMER.zh-CN.md)。

---

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=jyqj/Captchai&type=Date)](https://www.star-history.com/#jyqj/Captchai&Date)

---

## 📄 License

[MIT](LICENSE) —— 自由使用，开放修改，谨慎部署。
