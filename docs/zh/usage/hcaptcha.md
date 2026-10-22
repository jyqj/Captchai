# hCaptcha 使用指南

hCaptcha 通过 iframe 组件展示验证码挑战。求解器为你的 sitekey 渲染组件（注入页面，按目标源站提供），点击复选框；**当 hCaptcha 升级为图像网格挑战时，使用配置的视觉模型求解**——逐格截图、询问模型哪些格子匹配提示、点击并提交。Stripe Radar / 结账 hCaptcha 通常走这条路径，因为无头浏览器几乎总会收到视觉挑战。

## 支持的任务类型

| 任务类型 | 说明 |
|---------|------|
| `HCaptchaTaskProxyless` | 基于浏览器的 hCaptcha 求解，含视觉模型挑战求解 |

## 必填字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `websiteURL` | string | 包含验证码的页面完整 URL |
| `websiteKey` | string | 页面 HTML 中的 `data-sitekey` 值 |

## 可选字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `isInvisible` | bool | 以不可见方式渲染组件。求解器等待组件加载，注入**连续**的人类运动时间线（多段游走/滚动，持续 `HCAPTCHA_INVISIBLE_MOTION_SECONDS`，让被动评分看到真实的 `motionData` 缓冲而不是一次短促抖动），然后触发 `execute()`——它**不会**点击复选框（不可见组件没有复选框）。移动端指纹会注入触摸滚动而非鼠标轨迹。被动判定获得更长预算（`HCAPTCHA_INVISIBLE_PASSIVE_BUDGET`）；若仍升级为视觉挑战，则继续分发求解。 |
| `rqdata` | string | hCaptcha 企业版 `rqdata` nonce。传给 `hcaptcha.render`；设置该值的组件需要它。 |
| `enterprisePayload` | object | 企业版渲染配置（`rqdata`、`sentry`、`endpoint`、`reportapi`、`assethost`、`imghost` 等）。其键会**平铺**传给 `hcaptcha.render`，与 JS API 一致。若在此传入 `rqdata`（YesCaptcha 约定），也会到达组件——无需额外顶层 `rqdata`。 |
| `realPage` | bool | 覆盖本任务的页面策略。`false` 在目标主机名上提供合成页面（token-relay）；`true` 导航真实目标并 hook `hcaptcha.render`（运行站点自身的反机器人 JS）。默认取 `HCAPTCHA_REAL_PAGE`（false），企业版任务也一样。 |
| `userAgent` | string | 强制 UA，使令牌匹配下游提交；回显到 `solution.userAgent`。 |
| `egress` | string | `auto`（默认）/ `task` / `pool` / `proxyless`。企业版/Stripe 请用 `task` + 自己的住宅代理（见 Stripe 说明）。 |
| `proxyType` / `proxyAddress` / `proxyPort` / `proxyLogin` / `proxyPassword` | | 通过代理求解。对绑定信誉/IP 的流程强烈推荐。 |
| `proxy` | string | 单字符串代理形式，如 `http://user:pass@host:port`。可带 `\|kind=residential` / `\|country=DE` 后缀注解。 |

## Stripe 说明

Stripe 的支付/结账流程使用 hCaptcha Enterprise（Radar）。企业版由 `rqdata` / `enterprisePayload` 自动识别。要稳定求解：

1. 传入精确的 `websiteURL`（正确**主机名**——组件把令牌绑定到 `document.location.hostname`）和正确的 `websiteKey`。
2. 每次挑战从 Stripe 自己的页面抓取**新鲜** `rqdata`（一次性且与会话绑定）并传入（顶层或 `enterprisePayload` 内）。过期/缺失的 `rqdata` 即使网格答对，Radar 也会拒绝令牌。
3. **匹配出口 IP。** 企业版会评估 IP 一致性，令牌必须通过你提交银行卡绑定请求的**同一 IP** 铸造。使用 `egress=task` + 你**自己的住宅/移动代理**，并在 Stripe 调用中复用该代理；同时复用 `solution.userAgent` 与（`solution.timezoneId` / `acceptLanguage`）。
    - 若依赖服务端池（`egress=pool`），池必须由 `PROXY_POOL` 填充（见[企业版出口策略](#企业版出口策略proxy_pool--住宅代理)）。铸造 IP 是你无法直接使用的服务端代理——设置 `POOL_EGRESS_EXPOSE_CREDENTIALS=true` 可获得可复用的带凭据 `egressServer`，否则令牌 IP 与提交 IP 不匹配，Radar 很可能拒绝。
4. 企业版默认使用**注入页面**（token-relay），而不是导航真实 Stripe URL——后者通常无法从干净上下文复现。只有当特定站点自身 JS 必须运行时，才按任务设置 `realPage: true`。
5. **使用硬化运行时。** 企业版检测器会标记原生 Chromium 的自动化 + 软件 WebGL 信号（伪造的 GPU 字符串与真实 SwiftShader 渲染器矛盾）。设置 `BROWSER_RUNTIME=camoufox`；设置 `ENTERPRISE_REQUIRE_HARDENED_RUNTIME=true` 可**拒绝**（而非仅警告）在原生 Chromium 上执行企业版求解。
6. **可选：持久化设备。** 设置 `HCAPTCHA_DEVICE_PERSISTENCE=true`，把一次求解捕获的 hCaptcha 设备信任 cookie（`hmt`）重新注入同一出口的下一个全新上下文，呈现*回头设备*而非零历史设备——与 `ENTERPRISE_FRESH_CONTEXT` 搭配（全新上下文隔离 **且** 每 IP 稳定设备）。仅内存；重启后从冷启动开始。
7. **注意 rqdata 时钟。** 一次性 `rqdata` 生命周期很短；求解慢于 `HCAPTCHA_RQDATA_TTL` 秒（默认 30）时会在 `solution.warnings` 追加 `rqdata nonce may have expired` 提示。看到该提示时请抓取更新的 `rqdata` 和/或使用更快的出口。
8. 网格/区域挑战的求解质量取决于所配置的视觉模型（`LOCAL_MODEL` / `CLOUD_MODEL`）。

!!! warning "企业版令牌无法由服务端验证"
    在 token-relay 模式下，服务不持有目标 sitekey 的 siteverify 密钥，因此**无法确认**企业版令牌会被接受。企业版求解会在 `solution.warnings` 中提示令牌绑定 IP 且不可验证——每次求解抓取**新鲜** `rqdata` 并从同一出口提交。可通过 `/reportCorrect`（或你自己的下游检查）确认实际接受情况。

## 企业版出口策略（PROXY_POOL + 住宅代理）

企业版由 `rqdata` / `enterprisePayload` 识别。求解前会强制执行出口策略：

| 请求 | 结果 |
|------|------|
| `egress` 未设置 / `auto` + 任务代理 | 视为 `egress=task` |
| `egress` 未设置 / `auto` + 无任务代理 | 强制 `egress=pool` |
| `egress=proxyless` | 拒绝（`ERROR_CAPTCHA_UNSOLVABLE`）——企业版不能用服务端 IP |
| `egress=task` + 无代理 | 拒绝 |
| `egress=pool` | 允许；签出住宅/移动池代理 |

**默认企业版路径要求 `PROXY_POOL`。** 服务端代理池只从 `PROXY_POOL` 环境变量填充——每行一个或逗号分隔，使用与任务代理相同的单字符串语法：

```bash
export PROXY_POOL="http://user:pass@1.2.3.4:8080|kind=residential|country=DE
http://user:pass@5.6.7.8:9090|kind=mobile|country=US"
```

- `kind` 默认为 `datacenter`。请注解 `|kind=residential` 或 `|kind=mobile`；`ENTERPRISE_REQUIRE_RESIDENTIAL=true`（默认）时，池签出只接受这两类，若没有可用代理会以明确的 "requires a residential ... pool proxy" 错误失败。移动代理被视为住宅等价。
- 没有任何 `PROXY_POOL` 条目（或默认策略下只有 datacenter 条目）时，无任务代理的企业版求解会失败——**不会**静默改用直连。
- 配置 `REDIS_URL` 时，池清单、健康、冷却和按 sitekey 统计跨 worker 共享；否则池为每进程。仅在测试/开发时可设 `ENTERPRISE_REQUIRE_RESIDENTIAL=false` 以接受任意池代理。
- `egress=task` 时，调用方代理通常由调用方负责：允许使用并记录警告，不检查其 kind。设置 `ENTERPRISE_REQUIRE_RESIDENTIAL_ON_TASK=true` 可拒绝未注解 `|kind=residential` / `|kind=mobile` 的任务代理（未注解视为 datacenter）。
- `egress=pool` 通过调用方通常无法复用的服务端出口 IP 铸造令牌。仅当客户端可信且下游提交需要可复用的带凭据 `egressServer` 时，才设置 `POOL_EGRESS_EXPOSE_CREDENTIALS=true`。

### URL 安全

`SSRF_PROTECT=true`（默认）时，hCaptcha 页面路径会在导航前校验 `websiteURL`：仅 http(s)，主机必须可解析，且所有解析地址必须为公网。DNS 解析失败为放行（记录日志后继续导航）；解析到回环/私有/链路本地/保留/多播/未指定地址则作为不可重试的求解错误拒绝。仅在受控测试环境中才设置 `SSRF_PROTECT=false`。

## 测试目标

hCaptcha 提供官方测试密钥，可产生可预测结果：

| URL | Site key | 行为 |
|-----|----------|------|
| `https://accounts.hcaptcha.com/demo` | `10000000-ffff-ffff-ffff-000000000001` | 始终通过（测试密钥） |
| `https://accounts.hcaptcha.com/demo` | `20000000-ffff-ffff-ffff-000000000002` | 企业版安全用户测试 |
| `https://demo.hcaptcha.com/` | `10000000-ffff-ffff-ffff-000000000001` | 始终通过（测试密钥） |

## 创建任务

```bash
curl -X POST http://localhost:8000/createTask \
  -H "Content-Type: application/json" \
  -d '{
    "clientKey": "your-client-key",
    "task": {
      "type": "HCaptchaTaskProxyless",
      "websiteURL": "https://accounts.hcaptcha.com/demo",
      "websiteKey": "10000000-ffff-ffff-ffff-000000000001"
    }
  }'
```

响应：

```json
{
  "errorId": 0,
  "taskId": "uuid-string"
}
```

## 轮询结果

```bash
curl -X POST http://localhost:8000/getTaskResult \
  -H "Content-Type: application/json" \
  -d '{
    "clientKey": "your-client-key",
    "taskId": "uuid-from-createTask"
  }'
```

就绪时：

```json
{
  "errorId": 0,
  "status": "ready",
  "solution": {
    "gRecaptchaResponse": "P1_eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9..."
  }
}
```

!!! note "响应字段名"
    令牌以 `solution.gRecaptchaResponse` 返回（兼容 YesCaptcha API），尽管 hCaptcha 原生使用 `h-captcha-response` 字段名。

## 验收状态

| 目标 | Site key | 状态 | 说明 |
|------|----------|------|------|
| `https://accounts.hcaptcha.com/demo` | `10000000-ffff-ffff-ffff-000000000001` | ⚠️ 依赖挑战类型 | 无头浏览器可能仍会收到图像挑战 |

### 无头浏览器说明

即使使用测试密钥（`10000000-ffff-ffff-ffff-000000000001`），hCaptcha 在检测到无头浏览器时仍可能弹出图像挑战。求解器点击复选框并轮询令牌。

对于无头环境，推荐的集成方式是使用 `HCaptchaClassification` 任务类型求解图像网格挑战。详见[图像分类](classification.md)。

## 图像分类（HCaptchaClassification）

无需浏览器自动化的程序化网格分类，详见[图像分类](classification.md)。

## 注意事项

- hCaptcha 挑战可能比 reCAPTCHA v2 更耗时——求解器点击后最长等待 5 秒，并受轮询预算约束。
- 具有激进机器人检测的真实站点可能需要额外的指纹改进。
- 测试密钥（`10000000-ffff-ffff-ffff-000000000001`）始终通过，适合验证流程。
