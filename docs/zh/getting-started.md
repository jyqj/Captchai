# 快速开始

## 环境要求

- Python 3.10+
- 通过 Playwright 安装 Chromium（或可选硬化运行时）
- 具备访问以下资源的网络能力：
  - 目标网站
  - 你配置的 OpenAI-compatible 模型端点

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install --with-deps chromium
```

可选：

```bash
# 多 worker / 重启保留的共享状态
pip install "redis>=4.2"

# 打补丁的 Chromium 运行时
pip install rebrowser-playwright

# 打补丁的 Firefox 运行时（企业级 hCaptcha）
pip install camoufox==0.4.11 && python -m camoufox fetch
```

## 环境变量

CaptchAI 使用两个独立的 OpenAI-compatible 模型后端：`LOCAL_*`（自托管视觉模型，廉价/高吞吐）与 `CLOUD_*`（远程模型，用于音频转写与困难网格升级）。

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `CLIENT_KEY` | 客户端鉴权密钥，作为 `clientKey` 使用 | 未设置 |
| `ALLOW_ANONYMOUS` | 允许在非回环地址上无 `CLIENT_KEY` 启动 | `false` |
| `ADMIN_KEY` | `/admin/*` 的 Bearer 密钥 | 未设置 |
| `RATE_LIMIT_RPM` | 每客户端 IP 每分钟请求数（校验 `>= 1`） | `60` |
| `SSRF_PROTECT` | hCaptcha/Turnstile 的导航前公网 URL 校验 | `true` |
| `LOCAL_BASE_URL` | 本地 OpenAI-compatible 端点 | `http://localhost:30000/v1` |
| `LOCAL_API_KEY` | 本地端点密钥 | `EMPTY` |
| `LOCAL_MODEL` | 本地视觉模型 | `Qwen/Qwen3.5-2B` |
| `CLOUD_BASE_URL` | 云端 OpenAI-compatible 端点 | 占位 URL |
| `CLOUD_API_KEY` | 云端端点密钥 | 未设置 |
| `CLOUD_MODEL` | 云端聊天模型 | `gpt-5.4` |
| `CLOUD_AUDIO_MODEL` | reCAPTCHA v2 音频的语音转写模型 | `whisper-1` |
| `CLOUD_MAX_CONCURRENCY` | 云端模型最大并发调用数 | `4` |
| `LOCAL_MAX_CONCURRENCY` | 本地模型最大并发调用数（0 = 不限） | `0` |
| `MODEL_CONNECTION_FALLBACK` | 连接失败时在另一后端重试 | `true` |
| `CAPTCHA_RETRIES` | 重试次数 | `3` |
| `CAPTCHA_TIMEOUT` | 模型调用超时（秒） | `30` |
| `CAPTCHA_SOLVE_TIMEOUT` | 单任务墙钟预算（秒） | `180` |
| `BROWSER_HEADLESS` | 是否无头运行浏览器 | `true` |
| `BROWSER_TIMEOUT` | 浏览器超时（秒） | `30` |
| `BROWSER_RUNTIME` | `chromium` / `rebrowser` / `camoufox` | `chromium` |
| `BROWSER_RUNTIME_STRICT` | 运行时不可用时直接失败而不是回退 | `false` |
| `SERVER_HOST` | 监听地址 | `0.0.0.0` |
| `SERVER_PORT` | 监听端口 | `8000` |
| `REDIS_URL` | 共享任务存储 / 账本 / 统计 / 代理池 | 未设置 |

旧版名称（`CAPTCHA_BASE_URL`、`CAPTCHA_API_KEY`、`CAPTCHA_MODEL`、`CAPTCHA_MULTIMODAL_MODEL`）仍作为回退接受，但已不再表示"一个端点包打天下"：

- `CLOUD_BASE_URL` 与 `LOCAL_BASE_URL` 未设置时都回退到 `CAPTCHA_BASE_URL`（因此旧版单端点配置会同时作用于两个后端）。
- `LOCAL_MODEL` 回退到 `CAPTCHA_MULTIMODAL_MODEL`；`CLOUD_MODEL` 回退到 `CAPTCHA_MODEL`。
- `LOCAL_API_KEY` 回退到 `CAPTCHA_API_KEY`；`CLOUD_API_KEY` 回退到 `CAPTCHA_API_KEY`。

新部署请优先使用拆分后的 `LOCAL_*` / `CLOUD_*` 名称。完整列表见 [`src/core/config.py`](../../src/core/config.py)。

## 启动服务

```bash
export CLIENT_KEY="your-client-key"
export LOCAL_BASE_URL="http://localhost:30000/v1"
export LOCAL_MODEL="Qwen/Qwen3.5-2B"
export CLOUD_BASE_URL="https://your-openai-compatible-endpoint/v1"
export CLOUD_API_KEY="your-api-key"
export CLOUD_MODEL="gpt-5.4"
python main.py
```

认证默认拒绝启动：若 `CLIENT_KEY` 未设置且 `SERVER_HOST` 不是回环地址（`127.0.0.1`、`::1`、`localhost`），启动会失败，除非显式设置 `ALLOW_ANONYMOUS=true`。`ALLOW_ANONYMOUS` 只放宽该启动检查；配置了 `CLIENT_KEY` 时，每个 API 调用仍必须提供它。

`ADMIN_KEY` 可选但推荐：设置后 `/admin/*` 要求 `Authorization: Bearer <ADMIN_KEY>`。

## 验证启动

### 根接口

```bash
curl http://localhost:8000/
```

### 存活 / 详情

```bash
curl http://localhost:8000/api/v1/health
```

响应包含已注册任务类型、实际与请求的浏览器运行时（`browser_runtime`、`browser_runtime_requested`、`browser_runtime_effective`、`browser_runtime_degraded`、`browser_driver`）、`degraded` / `degraded_subsystems`、各模型后端熔断健康状态以及配置的模型名称。

### 就绪

```bash
curl -i http://localhost:8000/ready
```

所需子系统正常时返回 HTTP 200 与 `"status": "ready"`；否则返回 HTTP 503 与 `"status": "not_ready"` 及逐项检查详情。仅在注册了浏览器任务类型时检查浏览器；仅在配置 `REDIS_URL` 时 PING Redis。注意本版本没有裸 `/health` 路由：`/api/v1/health` 是存活/详情端点。

## Redis 多 worker 拓扑

未配置 `REDIS_URL` 时，所有子系统均为进程内：重启会丢失任务与花费历史，两个 worker 也看不到彼此的任务。

设置 `REDIS_URL` 后，以下内容**跨 worker 共享**并在重启后保留：

- 任务记录（`captcha:task:*`）与 `getTaskResult` 可见性
- 幂等 claim（`captcha:idem:*`，`SET NX`）
- `getBalance` 与 `/admin/metrics` 背后的成本账本
- 按 sitekey 的成功率统计
- 代理池清单、健康、冷却与按 sitekey 统计

以下仍是**每进程**状态：

- 在途任务队列与并发信号量（`QUEUE_MAX_SIZE`、`BROWSER_*_CONCURRENCY`、`VISION_CONCURRENCY`）
- 限流令牌桶（`RATE_LIMIT_RPM`）
- 暖浏览器会话（`SESSION_*`）
- 模型池并发上限（`CLOUD_MAX_CONCURRENCY`、`LOCAL_MAX_CONCURRENCY`）
- 预算守卫的在途预留（上限基于共享账本已记录花费检查，但不同 worker 的并发预留可能同时看到相同余量；全局上限可能被超出约 `worker 数 × 单请求估算`）。需要严格上限时请使用单 worker。

需要可选的 `redis>=4.2` 包（不在 `requirements.txt` 中）。

```bash
pip install "redis>=4.2"
export REDIS_URL="redis://localhost:6379/0"
```

## 本地 / 自托管模型支持

两个模型路径都使用 **OpenAI-compatible API**。将 `LOCAL_BASE_URL` 指向自托管 SGLang/vLLM 服务（支持图像输入 + chat completions），将 `CLOUD_BASE_URL` 指向托管服务商（chat completions，以及 reCAPTCHA v2 音频路径所需的 `audio.transcriptions`）。文档采用通用兼容性表述，而不是对每一种模型服务栈做完整验证承诺。

完整 SGLang 部署步骤见[本地模型部署](deployment/local-model.md)。
