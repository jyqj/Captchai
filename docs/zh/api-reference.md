# API 参考

## 接口列表

| 方法 | 路径 | 作用 |
|------|------|------|
| POST | `/createTask` | 提交验证码任务 |
| POST | `/getTaskResult` | 轮询结果 |
| POST | `/getBalance` | 起始额度减去账本花费 |
| POST | `/reportCorrect` | 报告返回的令牌下游被接受 |
| POST | `/reportIncorrect` | 报告返回的令牌下游被拒 |
| GET | `/admin/metrics` | 账本成本汇总 |
| GET | `/admin/proxies` | 代理池与暖会话快照 |
| GET | `/api/v1/health` | 存活状态 + 运行时详情（始终 200） |
| GET | `/ready` | 就绪探针（200 就绪 / 503 未就绪） |
| GET | `/` | 服务说明与已注册任务类型 |

所有任务接口均基于 JSON，遵循 YesCaptcha 风格的异步任务模式。错误通常通过响应体中的 `errorId: 1` 表示，传输状态码保持 **200**（除非特别说明）。`HTTPException`（404/405）保留 FastAPI 自身行为。

按客户端 IP 的限流器（`RATE_LIMIT_RPM`，默认 60）可能对任何非健康路径返回：

```json
{"errorId": 1, "errorCode": "ERROR_RATE_LIMITED", "errorDescription": "Rate limit exceeded; retry after 1.4s"}
```

并带 `Retry-After` 响应头。`/api/v1/health` 豁免；`/ready` 不豁免。

---

## 认证

- 配置了 `CLIENT_KEY` 时，任务与 report 端点会用常量时间比较校验 `clientKey`。不匹配返回 `ERROR_KEY_DOES_NOT_EXIST`。
- 未配置 `CLIENT_KEY` 时，任意 `clientKey` 字符串（包括空字符串）都被接受，但任务所有权仍被强制校验：`getTaskResult` 会把传入密钥的 SHA-256 哈希与任务记录中持久化的 `clientKeyHash` 比较（升级前的任务回退到内存中的 `_clientKey`），report 端点则与账本记录中的 `client_key` 比较；不匹配返回 `ERROR_NO_SUCH_CAPCHA_ID`（不泄露任务是否存在）。
- `/admin/*` 的认证见[管理端点](#管理端点)。
- 无 `CLIENT_KEY` 且 `SERVER_HOST` 非回环时，服务拒绝启动，除非 `ALLOW_ANONYMOUS=true`。

---

## `POST /createTask`

### 请求结构

```json
{
  "clientKey": "your-client-key",
  "idempotencyKey": "optional-retry-key",
  "task": {
    "type": "RecaptchaV3TaskProxyless",
    "websiteURL": "https://antcpt.com/score_detector/",
    "websiteKey": "6LcR_okUAAAAAPYrPe-HK_0RULO1aZM15ENyM-Mf",
    "pageAction": "homepage"
  }
}
```

`clientKey` 必填（最长 128 字符）。`idempotencyKey` 可选（最长 128 字符）。`task` 对象拒绝未知属性（`extra="forbid"`），拼写错误的字段会返回 `ERROR_BAD_REQUEST`，而不是被静默忽略。

### 支持的任务类型（共 19 种）

#### reCAPTCHA v3（浏览器）

- `RecaptchaV3TaskProxyless`
- `RecaptchaV3TaskProxylessM1`
- `RecaptchaV3TaskProxylessM1S7`
- `RecaptchaV3TaskProxylessM1S9`
- `RecaptchaV3EnterpriseTask`
- `RecaptchaV3EnterpriseTaskM1`

必填：`websiteURL`、`websiteKey`。可选：`pageAction`、`minScore`（接受但不生效）、`isInvisible`、`userAgent`、代理字段、`egress`。

#### reCAPTCHA v2（浏览器）

- `NoCaptchaTaskProxyless`
- `RecaptchaV2TaskProxyless`
- `RecaptchaV2EnterpriseTaskProxyless`

必填：`websiteURL`、`websiteKey`。可选：`isInvisible`、`userAgent`、代理字段、`egress`。

#### hCaptcha（浏览器）

- `HCaptchaTaskProxyless`

必填：`websiteURL`、`websiteKey`。可选：`isInvisible`、`rqdata`、`enterprisePayload`、`realPage`、`userAgent`、代理字段、`egress`。企业版由 `rqdata` / `enterprisePayload` 自动识别；见 [hCaptcha 使用指南](usage/hcaptcha.md)。

#### Cloudflare Turnstile（浏览器）

- `TurnstileTaskProxyless`
- `TurnstileTaskProxylessM1`

必填：`websiteURL`、`websiteKey`。可选：`action`、`cData`、`chlPageData`、`userAgent`、代理字段、`egress`。

#### 图片识别

- `ImageToTextTask`
- `ImageToTextTaskMuggle`
- `ImageToTextTaskM1`

必填：`body`（base64 编码图片）。

#### 图像分类

- `HCaptchaClassification`
- `ReCaptchaV2Classification`
- `FunCaptchaClassification`
- `AwsClassification`

必填：`image`、`images`、`body`、`queries` 中至少一个（base64 编码）。可选：`question`。

### Task 对象字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | string | 任务类型（见上） |
| `websiteURL` | string | 目标页面（浏览器任务） |
| `websiteKey` | string | 组件 sitekey（浏览器任务） |
| `pageAction` | string | reCAPTCHA v3 action |
| `minScore` | number | 仅兼容，不生效 |
| `isInvisible` | bool | 不可见组件 |
| `action`、`cData`、`chlPageData` | string | Turnstile 渲染参数，必须与站点配置一致 |
| `rqdata` | string | hCaptcha 企业版 nonce |
| `enterprisePayload` | object | hCaptcha 企业版渲染参数（平铺传给 `hcaptcha.render`） |
| `realPage` | bool | hCaptcha 页面策略覆盖（默认取 `HCAPTCHA_REAL_PAGE`） |
| `userAgent` | string | 强制 UA；会回显到 `solution.userAgent` |
| `proxyType`、`proxyAddress`、`proxyPort`、`proxyLogin`、`proxyPassword` | | 拆分式代理字段 |
| `proxy` | string | 单字符串代理，可带 `\|kind=residential\|country=DE` 注解 |
| `egress` | string | `auto`（默认）、`proxyless`、`pool` 或 `task` |
| `body`、`image`、`images` | string / list | base64 图片输入。`images` / `queries` 列表形式每个条目上限 1000 万字符、列表最多 20 项；单个 `body` / `image` 字符串没有大小限制。 |
| `question`、`queries` | string / list | 分类提示词与逐格图片 |

### 幂等语义

幂等键按调用方作用域为 `"{clientKey or 'anon'}:{idempotencyKey}"`，并绑定任务负载（含调用方密钥）的 SHA-256 指纹。

- 同键 + 同负载返回原 `taskId`，不会启动第二次求解。
- 同键 + 不同负载返回 `ERROR_DUPLICATE_TASK_ID`；请复用原负载或使用新键。
- 配置 `REDIS_URL` 时，claim 是共享的 `SET NX`，TTL 为 `max(60, CAPTCHA_SOLVE_TIMEOUT × 4)` 秒（默认 720 秒），重试可落到任意 worker。
- 未配置 Redis 时，合并是进程内的，生命周期与任务记录一致（10 分钟）。

### 成功响应

```json
{
  "errorId": 0,
  "taskId": "uuid-string"
}
```

### 错误响应

```json
{"errorId": 1, "errorCode": "ERROR_KEY_DOES_NOT_EXIST", "errorDescription": "Invalid clientKey"}
```

```json
{"errorId": 1, "errorCode": "ERROR_TASK_NOT_SUPPORTED", "errorDescription": "Task type 'X' is not supported. Supported: [...]"}
```

```json
{"errorId": 1, "errorCode": "ERROR_TASK_PROPERTY_EMPTY", "errorDescription": "websiteURL and websiteKey are required"}
```

```json
{"errorId": 1, "errorCode": "ERROR_DUPLICATE_TASK_ID", "errorDescription": "idempotencyKey was already used with a different payload; replay the original request or use a new idempotencyKey"}
```

```json
{"errorId": 1, "errorCode": "ERROR_NO_SLOT_AVAILABLE", "errorDescription": "in-flight tasks at capacity (128)"}
```

请求体格式错误（缺少必填字段、未知 task 属性、类型错误）返回 HTTP 200：

```json
{"errorId": 1, "errorCode": "ERROR_BAD_REQUEST", "errorDescription": "body.task.websiteURL: ..."}
```

---

## `POST /getTaskResult`

### 请求

```json
{
  "clientKey": "your-client-key",
  "taskId": "uuid-from-createTask"
}
```

### 处理中响应

```json
{
  "errorId": 0,
  "status": "processing"
}
```

### 完成响应

```json
{
  "errorId": 0,
  "status": "ready",
  "solution": {
    "gRecaptchaResponse": "token..."
  }
}
```

### solution 字段

`SolutionObject` 接受并返回以下字段（不存在的字段会省略）：

| 字段 | 类型 | 含义 |
|------|------|------|
| `gRecaptchaResponse` | string | reCAPTCHA v2/v3 与 hCaptcha 令牌（YesCaptcha 字段名） |
| `token` | string | Turnstile 令牌 |
| `text` | string | 图转文结构化 JSON 序列化字符串 |
| `objects` | list[int] | 分类格子索引 |
| `answer` | bool 或 list[int] | hCaptcha 分类的布尔/列表答案 |
| `userAgent` | string | 求解所用 UA；下游提交请复用 |
| `timezoneId` | string | 求解所用指纹时区 |
| `acceptLanguage` | string | 求解所用指纹 `Accept-Language` |
| `proxyKind` | string | `proxyless`、`pool_proxy` 或 `task_proxy` |
| `egressServer` | string | 求解出口的免凭据 `scheme://host:port`，或 null |
| `warnings` | list[string] | 非致命、面向调用方的提示（如企业令牌无法服务端验证） |

若 solver 产出的 solution 无法通过该结构校验，任务以 `ERROR_CAPTCHA_UNSOLVABLE` 失败。

### 失败响应

```json
{"errorId": 1, "errorCode": "ERROR_KEY_DOES_NOT_EXIST", "errorDescription": "Invalid clientKey"}
```

```json
{"errorId": 1, "errorCode": "ERROR_NO_SUCH_CAPCHA_ID", "errorDescription": "Task not found"}
```

`CAPCHA` 的拼写是刻意的（兼容 YesCaptcha）。任务存在但调用方密钥不拥有它时也返回该错误码。

```json
{"errorId": 1, "errorCode": "ERROR_CAPTCHA_TIMEOUT", "errorDescription": "Solve exceeded 180s budget"}
```

```json
{"errorId": 1, "errorCode": "ERROR_CAPTCHA_UNSOLVABLE", "errorDescription": "..."}
```

```json
{"errorId": 1, "errorCode": "ERROR_TASK_NOT_SUPPORTED", "errorDescription": "Task type 'X' is not supported"}
```

---

## `POST /getBalance`

### 请求

```json
{
  "clientKey": "your-client-key"
}
```

### 响应

```json
{
  "errorId": 0,
  "balance": 98500.25
}
```

`balance = max(0, ACCOUNT_BALANCE_USD - 该 clientKey 的账本花费)`。`ACCOUNT_BALANCE_USD` 默认 `99999.0`。配置 Redis 时账本花费跨 worker 共享，否则为进程内。若服务未初始化，返回配置的起始额度。

配置了 `CLIENT_KEY` 时，无效 `clientKey` 返回 HTTP 200、`errorId: 1`、`balance: 0.0`——该响应结构没有 `errorCode` / `errorDescription` 字段：

```json
{
  "errorId": 1,
  "balance": 0.0
}
```

---

## `POST /reportCorrect` 与 `POST /reportIncorrect`

把令牌在下游的真实结果反馈给代理健康、按 sitekey 的真实结果桶、成功率统计和会话信誉。

### 请求

```json
{
  "clientKey": "your-client-key",
  "taskId": "uuid-from-createTask"
}
```

### 响应

```json
{"errorId": 0}
```

规则：

- 调用方必须拥有该任务（或账本记录无 owner）。不匹配或记录不存在返回 `ERROR_NO_SUCH_CAPCHA_ID`。
- 每个任务的 report 会被原子 claim。重复 report（并发或顺序）返回 `errorId: 0`，不会再次执行副作用。
- 无账本记录 / 服务未初始化返回 `ERROR_NO_SUCH_CAPCHA_ID`。
- `clientKey` 无效返回 `ERROR_KEY_DOES_NOT_EXIST`。

---

## 管理端点

### 认证

`GET /admin/metrics` 与 `GET /admin/proxies` 共用一套认证策略：

1. 配置了 `ADMIN_KEY` 时，只接受 `Authorization: Bearer <ADMIN_KEY>`。缺失/错误返回 **HTTP 403** 与 `{"errorId":1,"errorCode":"ERROR_KEY_DOES_NOT_EXIST","errorDescription":"Invalid or missing admin key"}`。`clientKey` 查询参数被忽略。
2. 否则，若配置了 `CLIENT_KEY`，接受已弃用的 `?clientKey=<CLIENT_KEY>` 查询参数；不匹配返回 HTTP 200 与同样的错误信封。
3. 否则（完全无密钥模式），仅回环客户端（`127.0.0.1` / `::1`）可访问；其他返回 HTTP 403。

### `GET /admin/metrics`

```json
{
  "errorId": 0,
  "summary": {
    "count": 120,
    "cost_usd": 0.42,
    "by_outcome": {"ready": 110, "failed": 10},
    "by_model": {"gpt-5.4": {"count": 20, "cost_usd": 0.3}},
    "by_proxy_kind": {"proxyless": 60, "pool_proxy": 60},
    "cost_per_success": 0.0038
  }
}
```

服务未初始化时：

```json
{"errorId": 0, "summary": {}, "note": "services not initialised"}
```

### `GET /admin/proxies`

```json
{
  "errorId": 0,
  "proxies": [
    {
      "id": "proxy-id",
      "server": "http://1.2.3.4:8080",
      "kind": "residential",
      "state": "healthy",
      "success_count": 10,
      "fail_count": 1,
      "consecutive_fails": 0,
      "success_rate": 0.9091,
      "cooldown_remaining": 0.0,
      "cost_per_gb": 3.5,
      "bytes_used": 123456,
      "country": "DE",
      "timezone": "Europe/Berlin",
      "locale": "de-DE",
      "geo_probed": true,
      "sitekeys": {"sitekey": {"success": 8, "fail": 0}},
      "real_sitekeys": {"sitekey": {"success": 5, "fail": 1}}
    }
  ],
  "sessions": [
    {
      "id": "session-id",
      "in_use": false,
      "warm": true,
      "solves": 3,
      "reputation": 1.0,
      "user_agent": "Mozilla/5.0 ...",
      "proxy_id": "proxy-id",
      "created_at": 1729500000.0
    }
  ]
}
```

代理行会移除 `username`、`password`、`proxyLogin`、`proxyPassword` 与 `sticky_session_id`，并从 `server` 中去掉 userinfo。服务不可用时返回 `{"errorId":0,"proxies":[],"sessions":[]}`。

---

## `GET /api/v1/health`

存活与运行时详情。始终 HTTP 200，不以子系统健康为门控。

```json
{
  "status": "ok",
  "supported_task_types": ["RecaptchaV3TaskProxyless", "..."],
  "browser_headless": true,
  "browser_runtime": "camoufox",
  "browser_runtime_requested": "camoufox",
  "browser_runtime_effective": "camoufox",
  "browser_runtime_degraded": false,
  "browser_driver": "playwright",
  "browser_ready": true,
  "degraded": false,
  "degraded_subsystems": [],
  "model_backends": {
    "local": {
      "name": "local",
      "model": "Qwen/Qwen3.5-2B",
      "state": "closed",
      "healthy": true,
      "success_count": 42,
      "failure_count": 0,
      "consecutive_failures": 0,
      "success_rate": 1.0,
      "cooldown_remaining": 0.0,
      "last_error": null,
      "client_created": true
    },
    "cloud": {"...": "..."}
  },
  "cloud_model": "gpt-5.4",
  "local_model": "Qwen/Qwen3.5-2B"
}
```

说明：

- `browser_runtime` 是实际启动的运行时。`browser_runtime_effective` 还会区分 `chromium(rebrowser-unavailable)` 这类回退。实际 ≠ 请求时 `browser_runtime_degraded` 为 true。
- `supported_task_types` 是实际注册的集合。浏览器启动失败时浏览器类型会缺失，只列出视觉类型。
- `degraded_subsystems` 来自 lifespan（例如 `["browser", "proxy_pool"]`）；可选的降级平面本身不会使就绪探针失败。
- 本版本没有裸 `/health` 路由。`/health` 与 `/api/v1/health` 都豁免限流（便于反向代理把探针映射到 `/health`），但只有 `/api/v1/health` 已注册。

---

## `GET /ready`

就绪探针，供编排器使用（仓库自带的 `render.yaml` 即使用它）。

就绪 — HTTP 200：

```json
{
  "status": "ready",
  "ready": true,
  "checks": {"browser": "ok", "redis": "not_configured"}
}
```

未就绪 — HTTP 503：

```json
{
  "status": "not_ready",
  "ready": false,
  "checks": {"browser": "unavailable", "redis": "unavailable"}
}
```

检查项：

- `checks.browser`：注册了至少一种浏览器任务类型时为 `ok` / `unavailable`；纯视觉部署为 `disabled`。
- `checks.redis`：配置了 `REDIS_URL` 时为 `ok` / `unavailable`（一次 PING，1.5 秒超时）；否则 `not_configured`。
- `checks.degraded`：仅当 lifespan 记录了降级的可选子系统时出现，不影响状态码。

探针开销很低（不扫描 admin/池），且从不抛错。它受 `RATE_LIMIT_RPM` 限制（与 `/api/v1/health` 不同）。

---

## `GET /`

返回精简的服务说明与运行时已注册的任务类型：

```json
{
  "service": "captchai",
  "version": "3.0.0",
  "endpoints": {
    "createTask": "/createTask",
    "getTaskResult": "/getTaskResult",
    "getBalance": "/getBalance",
    "health": "/api/v1/health"
  },
  "supported_task_types": ["..."]
}
```

---

## 错误码汇总

| 错误码 | 出现位置 | 含义 |
|--------|----------|------|
| `ERROR_KEY_DOES_NOT_EXIST` | createTask、getTaskResult、report、admin | `clientKey` 无效/缺失（admin：Bearer 无效/缺失，或非回环的无密钥访问） |
| `ERROR_TASK_NOT_SUPPORTED` | createTask、getTaskResult | 任务类型未注册（如求解器平面降级） |
| `ERROR_TASK_PROPERTY_EMPTY` | createTask | 缺少 `websiteURL`/`websiteKey`、图片或分类输入 |
| `ERROR_DUPLICATE_TASK_ID` | createTask | 同一幂等键配不同负载 |
| `ERROR_NO_SLOT_AVAILABLE` | createTask | 在途任务达到上限（`QUEUE_MAX_SIZE`） |
| `ERROR_BAD_REQUEST` | 所有 JSON 端点 | 框架请求校验失败（缺字段/类型错误/未知 task 属性） |
| `ERROR_NO_SUCH_CAPCHA_ID` | getTaskResult、report | 任务不存在、已过期或不属于调用方 |
| `ERROR_CAPTCHA_TIMEOUT` | getTaskResult | 求解超过 `CAPTCHA_SOLVE_TIMEOUT` 或被取消 |
| `ERROR_CAPTCHA_UNSOLVABLE` | getTaskResult | 求解失败、目标拒绝/拦截、SSRF 校验拒绝或 solution 非法 |
| `ERROR_RATE_LIMITED` | 所有非健康路径 | 客户端 IP 请求预算耗尽 |
| `ERROR_INTERNAL` | 任意端点 | 未处理的服务端错误（HTTP 200 信封） |
