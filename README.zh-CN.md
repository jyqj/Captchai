# CaptchAI

可自托管的验证码求解服务，兼容 YesCaptcha 异步 API。

```
pip install -r requirements.txt && playwright install --with-deps chromium
CLIENT_KEY=your-key python main.py
# → http://localhost:8000/api/v1/health
```

**19 种任务类型** — reCAPTCHA v2/v3、hCaptcha（含企业版）、Cloudflare Turnstile、图转文、网格分类。

[English README](README.md)

---

## 为什么做这个

flow2api 等项目需要一个 `createTask` / `getTaskResult` 后端来返回真实可用的 token。CaptchAI 就是这个后端。

它**不是**套壳无头浏览器。企业级 hCaptcha（Stripe）、Cloudflare、Google 会把 token 绑定到出口 IP、User-Agent 和浏览器指纹——裸 `playwright.chromium.launch()` 一上来就被识别。CaptchAI 解决的正是这件事：

- **硬化浏览器运行时。** 原生 Chromium、`rebrowser`（打补丁的 Chromium），或 **Camoufox**（打补丁的 Firefox，引擎级反指纹）。企业级目标应使用 Camoufox。
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
                    │     TaskManager       │  异步队列，10 分钟 TTL
                    └──────┬──────────┬─────┘
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
├── api/routes.py             # createTask / getTaskResult / getBalance / report / health
├── core/
│   ├── config.py             # 所有环境变量、默认值、校验
│   ├── task_types.py          # 唯一注册表：19 种任务类型 → 求解器 + 校验规则
│   └── services.py           # 服务容器（单例）
├── services/
│   ├── task_manager.py        # 异步内存队列 + TTL + 并发池
│   ├── browser.py             # BrowserManager：启动、上下文工厂、资源拦截
│   ├── browser_solver.py      # BaseBrowserSolver：上下文获取/释放、重试、账本
│   ├── injected_widget.py     # 共享基类：注入页面、DOM 桥接、_poll_token
│   ├── hcaptcha.py            # HCaptchaSolver：复选框/隐身/企业版/挑战分发
│   ├── turnstile.py           # TurnstileSolver
│   ├── recaptcha_v3.py        # RecaptchaV3Solver
│   ├── recaptcha_v2.py        # RecaptchaV2Solver
│   ├── recognition.py         # 图转文（CaptchaRecognizer）
│   ├── classification.py      # 网格分类（ClassificationSolver）
│   └── vision_solver.py       # VisionRouter：本地优先、云端升级
├── parsing/
│   ├── dispatcher.py          # ChallengeClassifier + ChallengeDispatcher（形状检测）
│   ├── vision.py              # VisionRouter：本地 → 云端 + 自一致投票
│   └── shapes/                # 按形状的求解器：grid_select、dynamic_grid、area_bbox、slide、drag_drop
├── assets/
│   ├── fingerprint.py         # 一致 UA/WebGL/screen 画像 + 隐身 JS 生成
│   ├── proxy_pool.py          # 代理轮换、带宽计量、地理探测
│   ├── session_pool.py        # 浏览器暖会话复用
│   └── geo_probe.py           # IP → 时区/语言对齐
├── consumption/
│   ├── ledger.py              # 逐次求解成本记录（模型、token 数、结果）
│   ├── accounting.py          # 按 sitekey 的成功率统计
│   ├── budget.py              # 按客户端余额追踪
│   └── token_verify.py        # 求解后 token siteverify（可选）
└── orchestration/
    └── store.py               # 任务结果持久化
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
| 分类 | `HCaptchaClassification`, `ReCaptchaV2Classification`, `FunCaptchaClassification`, `AwsClassification` | `objects` |

所有类型统一定义在 [`src/core/task_types.py`](src/core/task_types.py)。

---

## API 接口

| 端点 | 方法 | 作用 |
|------|------|------|
| `/createTask` | POST | 提交验证码任务 |
| `/getTaskResult` | POST | 轮询结果 |
| `/getBalance` | POST | 查询余额 |
| `/reportCorrect` | POST | 报告令牌下游被接受 |
| `/reportIncorrect` | POST | 报告令牌下游被拒 |
| `/api/v1/health` | GET | 运行时状态 |
| `/admin/metrics` | GET | 按 sitekey / 模型的成本汇总 |
| `/admin/proxies` | GET | 代理池健康快照 |

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

所有配置通过环境变量设置，完整定义在 [`src/core/config.py`](src/core/config.py)。

### 基本

| 变量 | 默认值 | 作用 |
|------|--------|------|
| `CLIENT_KEY` | — | 所有 API 调用的认证密钥 |
| `LOCAL_BASE_URL` | `http://localhost:30000/v1` | 本地视觉模型端点（SGLang/vLLM） |
| `LOCAL_MODEL` | `Qwen/Qwen3.5-2B` | 本地模型名称 |
| `CLOUD_BASE_URL` | — | 云端模型端点（OpenAI 兼容） |
| `CLOUD_API_KEY` | — | 云端模型密钥 |
| `CLOUD_MODEL` | `gpt-5.4` | 云端模型名称 |

### 浏览器与隐身

| 变量 | 默认值 | 作用 |
|------|--------|------|
| `BROWSER_RUNTIME` | `chromium` | `chromium` / `rebrowser` / `camoufox` |
| `BROWSER_RUNTIME_STRICT` | `false` | 硬化运行时不可用则启动失败 |
| `BROWSER_HEADLESS` | `true` | 无头模式 |
| `BROWSER_TIMEOUT` | `30` | 页面加载超时（秒） |
| `CAMOUFOX_HUMANIZE` | `true` | Camoufox 拟人光标 |
| `CAMOUFOX_BLOCK_WEBRTC` | `true` | 屏蔽 WebRTC IP 泄漏 |
| `CAMOUFOX_OS` | — | 锁定伪造 OS（`windows`/`macos`/`linux`） |

### 求解

| 变量 | 默认值 | 作用 |
|------|--------|------|
| `CAPTCHA_RETRIES` | `3` | 每任务重试次数 |
| `CAPTCHA_TIMEOUT` | `30` | 模型调用超时（秒） |
| `CAPTCHA_MAX_CONCURRENCY` | `4` | 最大并发浏览器求解数 |
| `CAPTCHA_SOLVE_TIMEOUT` | `180` | 单任务墙钟预算（秒） |
| `HUMAN_PASSIVE_MOTION_SECONDS` | `1.4` | 复选框前运动秒数 |
| `VISION_STITCH_GRID` | `true` | 拼合网格瓦片为一张图 |
| `PROXY_MAX_GB` | `0` | 单代理带宽上限（0 = 不限） |

### 企业级 hCaptcha

| 变量 | 默认值 | 作用 |
|------|--------|------|
| `ENTERPRISE_REQUIRE_HARDENED_RUNTIME` | `false` | 原生 Chromium 上拒绝企业级求解 |
| `ENTERPRISE_FRESH_CONTEXT` | `true` | 每次企业级求解使用全新上下文 |
| `HCAPTCHA_DEVICE_PERSISTENCE` | `false` | 跨求解重注入设备信任 cookie |
| `HCAPTCHA_INVISIBLE_MOTION_SECONDS` | `3.0` | 隐身 execute() 前运动秒数 |
| `HCAPTCHA_INVISIBLE_PASSIVE_BUDGET` | `4.0` | 被动令牌等待（秒） |
| `HCAPTCHA_RQDATA_TTL` | `30` | rqdata 新鲜度预算（秒） |

### 代理与出口

令牌绑定 IP 与 UA。对真实 sitekey 应传入代理；solution 会回显出口身份，方便下游提交对齐：

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
  "rqdata": "...",                    // 企业版
  "enterprisePayload": { "sentry": true }
}
```

返回包含 `solution.userAgent`、`solution.proxyKind`、`solution.egressServer`。

---

## 部署

### 本地

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install --with-deps chromium
# 可选：pip install camoufox==0.4.11 && python -m camoufox fetch
export CLIENT_KEY="your-key"
python main.py
```

### Docker (Render)

```bash
docker build -f Dockerfile.render -t captchai .
docker run -p 8000:8000 -e CLIENT_KEY=your-key captchai
```

一键部署详见 [`render.yaml`](render.yaml)。

### Camoufox（企业级 hCaptcha）

```bash
pip install camoufox==0.4.11
python -m camoufox fetch
export BROWSER_RUNTIME=camoufox
export BROWSER_RUNTIME_STRICT=true
python main.py
```

---

## 开发

```bash
# 测试（12000+ 行，~100 个用例）
python -m pytest tests/ -q

# 类型检查
npx pyright

# 文档
python -m mkdocs build --strict
```

---

## 限制

- 内存任务存储，10 分钟 TTL——重启后不保留
- `minScore` 兼容性字段，当前不做分数控制
- 令牌有效性取决于运行时、IP 信誉和目标站行为
- 并非所有商业打码平台功能均已复现

---

## 免责声明

本项目仅供合法安全研究、渗透测试和技术学习使用。你对合规性自行负责。详见 [DISCLAIMER.zh-CN.md](DISCLAIMER.zh-CN.md)。

## License

[MIT](LICENSE)
