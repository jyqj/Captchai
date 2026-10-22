# CaptchAI

可自托管的验证码求解服务，兼容 YesCaptcha 异步 API。

```
pip install -r requirements.txt && playwright install --with-deps chromium
CLIENT_KEY=your-key python main.py
# → http://localhost:8000/api/v1/health
```

**19 种任务类型** — reCAPTCHA v2/v3、hCaptcha（含企业版）、Cloudflare Turnstile、图转文、网格分类。

[English README](README.md)

> **默认拒绝启动。** 未设置 `CLIENT_KEY` 且 `SERVER_HOST` 不是回环地址时，服务会拒绝启动，
> 除非显式设置 `ALLOW_ANONYMOUS=true`。见[安全与认证](#安全与认证)。

---

## 为什么做这个

flow2api 等项目需要一个 `createTask` / `getTaskResult` 后端来返回真实可用的 token。CaptchAI 就是这个后端。

它**不是**套壳无头浏览器。企业级 hCaptcha（Stripe）、Cloudflare、Google 会把 token 绑定到出口 IP、User-Agent 和浏览器指纹——裸 `playwright.chromium.launch()` 一上来就被识别。CaptchAI 解决的正是这件事：

- **硬化浏览器运行时。** 原生 Chromium、`rebrowser`（打补丁的 Chromium，可选包），或 **Camoufox**（打补丁的 Firefox，引擎级反指纹，可选包）。企业级目标应使用 Camoufox。运行时是进程级的；回退后的实际运行时由 `/api/v1/health` 上报。
- **每上下文一致指纹。** UA、platform、WebGL 厂商/渲染器、语言、时区——每次求解取自同一份连贯画像，而不是到处复用同一份隐身脚本。
- **代理与 UA 绑定。** 令牌绑定出口 IP + UA。solution 会回显两者（`solution.userAgent`、`solution.egressServer`），方便下游提交保持一致。
- **拟人交互。** 指针轨迹、滚动、游走——在 hCaptcha 被动评分触发前填充真实 `motionData` 缓冲。
- **成本可控视觉。** 图像挑战优先走廉价本地模型；困难网格才升级到云端模型，配合自一致投票。

---

## 架构

```
                    POST /createTask { clientKey, task }
                                 │
                                 ▼
                    ┌───────────────────────┐
                    │   FastAPI + Router    │  YesCaptcha 协议
                    └──────────┬────────────┘
                               │
                               ▼
                    ┌───────────────────────┐
                    │     TaskManager       │  有界队列、分离并发池、
                    └──────┬──────────┬─────┘  10 分钟任务 TTL、可选 Redis
                           │          │
              ┌────────────┘          └────────────┐
              ▼                                    ▼
  ┌──────────────────────┐            ┌──────────────────────┐
  │    浏览器求解器       │            │     视觉求解器        │
  │                      │            │                      │
  │  RecaptchaV3Solver   │            │  CaptchaRecognizer   │
  │  RecaptchaV2Solver   │            │  ClassificationSolver│
  │  HCaptchaSolver      │            │                      │
  │  TurnstileSolver     │            │  VisionRouter        │
  └──────────┬───────────┘            │  本地 → 云端          │
             │                        └──────────────────────┘
             ▼
  ┌──────────────────────┐
  │   BrowserManager     │  单一共享进程
  │                      │
  │  每次求解独立上下文：  │
  │  - 代理绑定           │
  │  - 一致指纹           │
  │  - 隐身 JS / DOM     │
  │    桥接 (Camoufox)   │
  │  - 资源拦截           │
  └──────────────────────┘

                    POST /getTaskResult { taskId }
                                 │
                                 ▼
                    solution.gRecaptchaResponse / .token / .text
```

### 源码结构

```
src/
├── api/routes.py             # createTask / getTaskResult / getBalance / report / admin / health / ready
├── core/
│   ├── config.py             # 所有环境变量、默认值、校验
│   ├── task_types.py          # 唯一注册表：19 种任务类型 → 求解器 + 校验规则
│   └── services.py           # 服务容器（单例）
├── services/
│   ├── task_manager.py        # 有界准入、分离并发、TTL、幂等
│   ├── browser.py             # BrowserManager：启动、运行时选择、上下文工厂
│   ├── browser_solver.py      # BaseBrowserSolver：上下文获取/释放、重试、账本
│   ├── injected_widget.py     # 共享基类：注入页面、DOM 桥接、_poll_token
│   ├── hcaptcha.py            # HCaptchaSolver：复选框/隐身/企业版/挑战分发
│   ├── turnstile.py           # TurnstileSolver
│   ├── recaptcha_v3.py        # RecaptchaV3Solver
│   ├── recaptcha_v2.py        # RecaptchaV2Solver
│   ├── recognition.py         # 图转文（CaptchaRecognizer）
│   ├── classification.py      # 网格分类（ClassificationSolver）
│   └── vision_solver.py       # 共享模型调用 + 计量适配层
├── parsing/
│   ├── dispatcher.py          # ChallengeClassifier + ChallengeDispatcher（形状检测）
│   ├── vision.py              # VisionRouter：本地 → 云端 + 自一致投票
│   └── shapes/                # 按形状的求解器：grid_select、dynamic_grid、area_bbox、slide、drag_drop
├── assets/
│   ├── fingerprint.py         # 一致 UA/WebGL/screen 画像 + 隐身 JS 生成
│   ├── proxy_pool.py          # 代理轮换、带宽计量、地理探测（可选 Redis 后端）
│   ├── session_pool.py        # 浏览器暖会话复用
│   └── geo_probe.py           # IP → 时区/语言对齐
├── consumption/
│   ├── ledger.py              # 逐次求解成本记录（模型、token 数、结果；可选 Redis）
│   ├── accounting.py          # 按 sitekey 的成功率统计（可选 Redis）
│   ├── budget.py              # 带在途预留的花费上限
│   └── token_verify.py        # 求解后 token siteverify（可选）
└── orchestration/
    └── store.py               # 任务结果持久化（内存或 Redis）
```

---

## 任务类型

| 分类 | 类型 | 返回字段 |
|------|------|---------|
| reCAPTCHA v3 | `RecaptchaV3TaskProxyless`, `…M1`, `…M1S7`, `…M1S9` | `gRecaptchaResponse` |
| reCAPTCHA v3 企业版 | `RecaptchaV3EnterpriseTask`, `…M1` | `gRecaptchaResponse` |
| reCAPTCHA v2 | `NoCaptchaTaskProxyless`, `RecaptchaV2TaskProxyless`, `…EnterpriseTaskProxyless` | `gRecaptchaResponse` |
| hCaptcha | `HCaptchaTaskProxyless` | `gRecaptchaResponse` |
| Cloudflare Turnstile | `TurnstileTaskProxyless`, `…M1` | `token` |
| 图转文 | `ImageToTextTask`, `…Muggle`, `…M1` | `text` |
| 分类 | `HCaptchaClassification`, `ReCaptchaV2Classification`, `FunCaptchaClassification`, `AwsClassification` | `objects`（HCaptcha 也可能返回 `answer`） |

所有类型统一定义在 [`src/core/task_types.py`](src/core/task_types.py)。solution 还可能包含 `userAgent`、`timezoneId`、`acceptLanguage`、`proxyKind`、`egressServer`、`warnings`；详见 [API 参考](docs/zh/api-reference.md)。

---

## API 接口

| 端点 | 方法 | 作用 |
|------|------|------|
| `/createTask` | POST | 提交验证码任务 |
| `/getTaskResult` | POST | 轮询结果 |
| `/getBalance` | POST | 起始额度减去该客户端的账本花费 |
| `/reportCorrect` | POST | 报告令牌下游被接受 |
| `/reportIncorrect` | POST | 报告令牌下游被拒 |
| `/api/v1/health` | GET | 存活状态 + 运行时详情（始终 200） |
| `/ready` | GET | 就绪探针（200 就绪 / 503 未就绪） |
| `/admin/metrics` | GET | 按模型 / 结果的成本汇总 |
| `/admin/proxies` | GET | 代理池与会话快照（凭据已脱敏） |
| `/` | GET | 服务说明与已注册类型 |

设置 `ADMIN_KEY` 后，管理端点接受 `Authorization: Bearer $ADMIN_KEY`。未设置时，若配置了 `CLIENT_KEY` 则回退到旧式 `?clientKey=` 查询参数；两者都未配置时管理端点**仅限回环地址**。建议设置 `ADMIN_KEY`。

<details>
<summary>创建 + 轮询示例</summary>

```bash
# 创建
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

# 轮询
curl -s -X POST http://localhost:8000/getTaskResult \
  -H "Content-Type: application/json" \
  -d '{"clientKey": "your-key", "taskId": "<上一步返回的 taskId>"}'
```
</details>

---

## 配置项

所有配置通过环境变量设置，完整定义在 [`src/core/config.py`](src/core/config.py)；数字/布尔值格式错误会在启动时报出具体变量名并失败。

### 安全与服务器

| 变量 | 默认值 | 作用 |
|------|--------|------|
| `CLIENT_KEY` | — | 所有 API 调用必须作为 `clientKey` 提供的认证密钥 |
| `ALLOW_ANONYMOUS` | `false` | 允许在非回环地址上启动无密钥服务（仅启动门槛） |
| `ADMIN_KEY` | — | `/admin/*` 的 Bearer 密钥；设置后只接受 `Authorization: Bearer` |
| `RATE_LIMIT_RPM` | `60` | 每客户端 IP 每分钟请求数（进程内令牌桶；必须 `>= 1`） |
| `SSRF_PROTECT` | `true` | hCaptcha/Turnstile 页面路径的导航前公网 URL 校验（reCAPTCHA v2/v3 始终校验） |
| `SERVER_HOST` | `0.0.0.0` | 监听地址；回环地址跳过无密钥拒绝启动检查 |
| `SERVER_PORT` | `8000` | 监听端口（`main.py` 也兼容 `PORT`） |

### 模型后端

| 变量 | 默认值 | 作用 |
|------|--------|------|
| `LOCAL_BASE_URL` | `http://localhost:30000/v1` | 本地视觉模型端点（SGLang/vLLM） |
| `LOCAL_API_KEY` | `EMPTY` | 本地端点密钥 |
| `LOCAL_MODEL` | `Qwen/Qwen3.5-2B` | 本地模型名称 |
| `LOCAL_MAX_CONCURRENCY` | `0` | 本地模型并发调用数（0 = 不限） |
| `CLOUD_BASE_URL` | — | 云端模型端点（OpenAI 兼容） |
| `CLOUD_API_KEY` | — | 云端模型密钥 |
| `CLOUD_MODEL` | `gpt-5.4` | 云端模型名称 |
| `CLOUD_AUDIO_MODEL` | `whisper-1` | reCAPTCHA v2 音频路径的语音转写模型（`audio.transcriptions`） |
| `CLOUD_MAX_CONCURRENCY` | `4` | 云端模型并发调用数 |
| `MODEL_CONNECTION_FALLBACK` | `true` | 连接失败时在另一后端重试（本地↔云端） |

### 浏览器与隐身

| 变量 | 默认值 | 作用 |
|------|--------|------|
| `BROWSER_RUNTIME` | `chromium` | `chromium`（原生）/ `rebrowser`（可选补丁驱动）/ `camoufox`（可选补丁 Firefox） |
| `BROWSER_RUNTIME_STRICT` | `false` | 硬化运行时不可用/启动失败时直接失败，而不是回退 |
| `BROWSER_HEADLESS` | `true` | 无头模式 |
| `BROWSER_TIMEOUT` | `30` | 页面加载超时（秒） |
| `BROWSER_DISABLE_SANDBOX` | auto | `true`/`false` 强制开启/关闭 `--no-sandbox`；未设置时仅 POSIX root 启用 |
| `BROWSER_DISABLE_SITE_ISOLATION` | `false` | 显式启用 Chromium 关闭站点隔离的参数 |
| `CAMOUFOX_HUMANIZE` | `true` | Camoufox 拟人光标 |
| `CAMOUFOX_BLOCK_WEBRTC` | `true` | 屏蔽 WebRTC IP 泄漏 |
| `CAMOUFOX_OS` | — | 锁定伪造 OS（`windows`/`macos`/`linux`） |

运行时语义：

- `chromium` 启动 Playwright 自带的原生 Chromium。
- `rebrowser` 需要可选的 `rebrowser-playwright` 包，并通过其补丁驱动启动。包缺失或无法导入时，宽松模式会大声告警、回退到原生 Playwright，并记录 `effective_runtime=chromium(rebrowser-unavailable)`、`driver=playwright`。`BROWSER_RUNTIME_STRICT=true` 会把该情况变成启动错误。
- `camoufox` 需要可选的 `camoufox` 包及其浏览器（`python -m camoufox fetch`）。缺失/无法启动 → 大声回退到原生 Chromium（严格模式下直接失败）。
- 硬化运行时启动失败时，宽松模式同样回退到原生 Chromium。运行时是进程级的，不是按任务切换。
- `GET /api/v1/health` 暴露 `browser_runtime`（实际）、`browser_runtime_requested`、`browser_runtime_effective`、`browser_runtime_degraded`、`browser_driver`、`browser_ready`，因此静默降级可被观测。

### 企业级 hCaptcha

| 变量 | 默认值 | 作用 |
|------|--------|------|
| `ENTERPRISE_REQUIRE_RESIDENTIAL` | `true` | 企业级求解要求住宅/移动池代理 |
| `ENTERPRISE_FRESH_CONTEXT` | `true` | 每次企业级求解使用全新浏览器上下文 |
| `ENTERPRISE_REQUIRE_HARDENED_RUNTIME` | `false` | 在原生 Chromium 上拒绝企业级求解 |
| `ENTERPRISE_REQUIRE_RESIDENTIAL_ON_TASK` | `false` | 对调用方任务代理强制住宅注解 |
| `POOL_EGRESS_EXPOSE_CREDENTIALS` | `false` | 池代理求解返回带凭据的 `egressServer` |
| `HCAPTCHA_REAL_PAGE` | `false` | hCaptcha 默认页面策略 |
| `HCAPTCHA_DEVICE_PERSISTENCE` | `false` | 按出口重新注入 `hmt` 设备信任 cookie |
| `HCAPTCHA_INVISIBLE_MOTION_SECONDS` | `3.0` | 隐身 `execute()` 前运动秒数 |
| `HCAPTCHA_INVISIBLE_PASSIVE_BUDGET` | `4.0` | 被动令牌等待（秒） |
| `HCAPTCHA_RQDATA_TTL` | `30` | rqdata 新鲜度告警预算（秒） |

无任务代理的企业级求解会被强制为 `egress=pool`；该代理池必须由 `PROXY_POOL` 填充，并带 `|kind=residential` 或 `|kind=mobile` 注解。见 [hCaptcha 使用指南](docs/zh/usage/hcaptcha.md#企业版出口策略proxy_pool--住宅代理)。

### 求解、并发与状态

| 变量 | 默认值 | 作用 |
|------|--------|------|
| `CAPTCHA_RETRIES` | `3` | 求解/模型重试次数 |
| `CAPTCHA_TIMEOUT` | `30` | 模型调用超时（秒） |
| `CAPTCHA_SOLVE_TIMEOUT` | `180` | 单任务墙钟预算（秒）；别名 `SOLVE_TIMEOUT` |
| `QUEUE_MAX_SIZE` | `128` | 最大在途任务数；超出返回 `ERROR_NO_SLOT_AVAILABLE` |
| `BROWSER_CONCURRENCY` | `4` | 浏览器并发池基数 |
| `BROWSER_PROXYLESS_CONCURRENCY` | `4` | 服务端直连浏览器求解池 |
| `BROWSER_PROXIED_CONCURRENCY` | `4` | 调用方代理浏览器求解池 |
| `BROWSER_POOL_PROXY_CONCURRENCY` | `4` | 服务端代理池求解池 |
| `VISION_CONCURRENCY` | `8` | 纯视觉任务并发池 |
| `SESSION_POOL_SIZE` | `4` | 暖浏览器会话池大小（每进程） |
| `SESSION_MAX_SOLVES` | `8` | 单个暖会话最多复用次数 |
| `SESSION_PREWARM` | `false` | 启动时预创建直连暖会话 |
| `SESSION_IDLE_TTL_SECONDS` | `300` | 空闲超过该时长的暖会话退役（0 关闭） |
| `SESSION_MAX_AGE_SECONDS` | `1800` | 存活超过该时长的暖会话退役（0 关闭） |
| `REDIS_URL` | — | 共享任务存储 / 账本 / 统计 / 代理池（可选） |
| `PROXY_POOL` | — | 服务端代理清单，每行一个或逗号分隔 |
| `PROXY_COOLDOWN` | `120` | 代理失败后的冷却时间（秒） |
| `PROXY_MAX_CONSECUTIVE_FAILS` | `3` | 连续失败多少次后冷却 |
| `PROXY_MAX_GB` | `0` | 单代理带宽上限 GB（0 = 不限） |
| `PROXY_GEO_PROBE` | `true` | 首次签出时探测代理出口国家 |

### 视觉路由

| 变量 | 默认值 | 作用 |
|------|--------|------|
| `VISION_CLOUD_ENABLED` | `true` | 允许困难网格升级到云端模型 |
| `VISION_VOTE_SAMPLES` | `3` | 每个网格的自一致采样数（1 关闭投票） |
| `VISION_CONFIDENCE_THRESHOLD` | `0.6` | 触发升级的一致度/置信度阈值 |
| `VISION_VOTE_CONCURRENT` | `true` | 并发执行投票采样 |
| `VISION_INLINE_ESCALATE` | `true` | 低置信度本地调用就地重试云端 |
| `VISION_STITCH_GRID` | `true` | 发送一张拼接图而非 N 张瓦片 |
| `VISION_TRUST_SELF_CONFIDENCE` | `false` | 信任模型自报置信度 |

旧版 `CAPTCHA_BASE_URL` / `CAPTCHA_API_KEY` / `CAPTCHA_MODEL` / `CAPTCHA_MULTIMODAL_MODEL` 仍作为云端/本地取值的回退，但拆分后的 `CLOUD_*` / `LOCAL_*` 优先，文档统一使用新名称。

### 幂等

`createTask` 接受可选的 `idempotencyKey`（最长 128 字符）。键按调用方作用域为 `"{clientKey or 'anon'}:{idempotencyKey}"`，并绑定任务负载（含调用方密钥）的 SHA-256 指纹：

- 同键 + 同负载 → 返回原 `taskId`，不会二次求解或计费；
- 同键 + 不同负载 → `ERROR_DUPLICATE_TASK_ID`；
- 配置 `REDIS_URL` 时，claim 是跨 worker 的 `SET NX`，有效期为 `max(60, CAPTCHA_SOLVE_TIMEOUT × 4)` 秒（默认 720 秒）；
- 未配置 Redis 时，存储与 claim 均为进程内，任务记录保留 10 分钟。

### 健康与就绪

- `GET /api/v1/health` 是存活接口，始终返回 200，包含 `status`、`supported_task_types`、浏览器运行时字段、`degraded`、`degraded_subsystems`、`model_backends`、`cloud_model`、`local_model`。它不参与流量门控。
- `GET /ready` 是就绪接口：所需子系统正常时返回 200，否则 503。注册了浏览器任务类型时 `checks.browser` 为 `ok`/`unavailable`（纯视觉部署为 `disabled`）；设置 `REDIS_URL` 时 `checks.redis` 为 `ok`/`unavailable`（否则 `not_configured`）。可选的降级平面会写入 `checks.degraded`，但不会使探针失败。本版本没有裸 `/health` 路由。

---

## 安全与认证

- **默认拒绝启动。** 若 `CLIENT_KEY` 未设置、`ALLOW_ANONYMOUS` 为 false，且 `SERVER_HOST` 不是回环地址（`127.0.0.1`、`::1` 或 `localhost`），lifespan 会抛错，进程拒绝启动。请设置 `CLIENT_KEY`，或显式设置 `ALLOW_ANONYMOUS=true`。
- **`ALLOW_ANONYMOUS` 只是启动门槛，不是逐请求放行。** 配置了 `CLIENT_KEY` 时，所有任务端点仍要求它。未配置 `CLIENT_KEY` 时，任意 `clientKey` 字符串（包括空字符串）都被接受；`getTaskResult` 与 report 端点会以常量时间比较传入密钥与任务记录的密钥，因此调用方无法读取他人任务或对他人的任务提交 report。
- **常量时间比较**（`hmac.compare_digest`）用于 `CLIENT_KEY` 与 `ADMIN_KEY`。
- **管理认证。** 设置 `ADMIN_KEY` 后，`/admin/*` 要求 `Authorization: Bearer <key>`；查询参数被忽略，缺失/错误返回 HTTP 403 + `ERROR_KEY_DOES_NOT_EXIST`。未设置 `ADMIN_KEY` 时，若配置了 `CLIENT_KEY` 则旧的 `?clientKey=` 形式可用；完全无密钥时管理端仅限回环客户端（否则 HTTP 403）。
- **限流。** 进程内按客户端 IP 的令牌桶（`RATE_LIMIT_RPM`，默认 60；配置校验要求 `>= 1`）。超限请求返回 HTTP 200 + `{"errorId":1,"errorCode":"ERROR_RATE_LIMITED"}`，并带 `Retry-After` 头。`/api/v1/health`（以及反向代理映射的 `/health`）绕过限流；`/ready` 不绕过。每个 worker 各自执行自己的桶。
- **SSRF 防护。** 调用方提供的 `websiteURL` 在签出代理/上下文前校验：必须为 http(s)；主机必须可解析；所有解析地址必须是公网地址（回环、RFC1918/ULA、链路本地含 `169.254.169.254`、保留、多播、未指定地址均拒绝）。reCAPTCHA v2/v3 始终执行；hCaptcha/Turnstile 页面路径在 `SSRF_PROTECT=true`（默认）时执行，仅在 DNS 解析失败时放行。reCAPTCHA 音频 URL 另行限制为 `https` 且主机后缀为 `google.com` / `recaptcha.net` / `gstatic.com`，拒绝重定向，响应体上限 5 MB，配置了任务代理时通过该代理下载。
- **输入限制。** Task 对象拒绝未知字段（`extra="forbid"`）；列表形式 `images` / `queries` 的每个条目上限 1000 万字符、列表最多 20 项（单个 `body` / `image` 字符串没有大小限制）；框架校验错误会改写为 YesCaptcha 信封。

---

## 部署

### 本地

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install --with-deps chromium
export CLIENT_KEY="your-key"
python main.py
```

可选硬化运行时：

```bash
# rebrowser（打补丁的 Chromium）
pip install rebrowser-playwright
export BROWSER_RUNTIME=rebrowser

# Camoufox（打补丁的 Firefox）
pip install camoufox==0.4.11 && python -m camoufox fetch
export BROWSER_RUNTIME=camoufox
export BROWSER_RUNTIME_STRICT=true
```

### Docker (Render)

```bash
docker build -f Dockerfile.render -t captchai .
docker run -p 8000:8000 -e CLIENT_KEY=your-key captchai
```

一键部署详见 [`render.yaml`](render.yaml)（健康检查指向 `/ready`）。

### 使用 Redis 共享状态

```bash
pip install "redis>=4.2"
export REDIS_URL="redis://localhost:6379/0"
```

设置 `REDIS_URL` 后，任务存储、成本账本、成功率统计、代理池与幂等 claim 跨 worker 共享并在重启后保留。以下仍是**每进程**状态：任务准入队列与并发信号量、限流桶、暖浏览器会话、模型池并发上限、预算守卫的在途预留。预算上限基于共享账本的已记录花费检查，但不同 worker 上的并发预留可能同时看到相同余量，因此全局上限在准入瞬间可能被超出约 `worker 数 × 单请求估算`。需要严格上限时请使用单 worker，或预留余量。

Redis 记录 TTL 为 `CAPTCHA_SOLVE_TIMEOUT × 4`（默认 720 秒）。未配置 Redis 时一切均在进程内，重启会丢失任务与花费历史。

### 健康探针

- 就绪：`GET /ready`（200/503），`render.yaml` 即使用该路径。
- 存活/详情：`GET /api/v1/health`（始终 200）。

---

## 开发

```bash
# 测试
python -m pytest tests/ -q

# 类型检查
npx pyright

# 文档
python -m mkdocs build --strict
```

---

## 限制

- 任务状态默认保存在内存（10 分钟任务 TTL）；设置 `REDIS_URL` 可获得跨 worker 可见性与重启保留。
- `minScore` 仅为兼容性字段，当前不做分数控制。
- 令牌有效性取决于运行时、IP 信誉和目标站行为；企业级令牌在没有 siteverify 密钥时无法由服务端验证。
- 限流与预算在途预留是每进程的；需要严格全局限制时请使用单 worker。
- 并非所有商业打码平台功能均已复现。

---

## 免责声明

本项目仅供合法安全研究、渗透测试和技术学习使用。你对合规性自行负责。详见 [DISCLAIMER.zh-CN.md](DISCLAIMER.zh-CN.md)。

## License

[MIT](LICENSE)
