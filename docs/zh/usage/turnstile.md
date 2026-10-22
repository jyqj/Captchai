# Cloudflare Turnstile 使用指南

Cloudflare Turnstile 是一种无感或组件式验证码替代方案。求解器使用 Chromium 访问目标页面，与 Turnstile 组件交互，并从隐藏的 `cf-turnstile-response` 输入字段中提取令牌。

导航前，当 `SSRF_PROTECT=true`（默认）时会校验 `websiteURL`：仅 http(s)，主机必须可解析，且所有解析地址必须为公网。DNS 解析失败为放行（记录日志）；解析到回环/私有/链路本地/保留/多播/未指定地址则拒绝。

## 支持的任务类型

| 任务类型 | 说明 |
|---------|------|
| `TurnstileTaskProxyless` | 标准 Turnstile 求解 |
| `TurnstileTaskProxylessM1` | 同上，备用命名 |

## 必填字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `websiteURL` | string | 包含 Turnstile 组件的页面完整 URL |
| `websiteKey` | string | Turnstile `data-sitekey` 值 |

## 返回字段

结果在 `solution.token`（而非 `solution.gRecaptchaResponse`）中返回：

```json
{
  "errorId": 0,
  "status": "ready",
  "solution": {
    "token": "XXXX.DUMMY.TOKEN.XXXX"
  }
}
```

## 测试目标

Cloudflare 提供官方测试密钥：

| Site key | 行为 |
|----------|------|
| `1x00000000000000000000AA` | 始终通过（可见组件） |
| `2x00000000000000000000AB` | 始终失败 |
| `3x00000000000000000000FF` | 强制交互式挑战 |

推荐测试页面：

- **URL：** `https://react-turnstile.vercel.app/basic`
- **Site key：** `1x00000000000000000000AA`（测试密钥，始终通过）

## 创建任务

```bash
curl -X POST http://localhost:8000/createTask \
  -H "Content-Type: application/json" \
  -d '{
    "clientKey": "your-client-key",
    "task": {
      "type": "TurnstileTaskProxyless",
      "websiteURL": "https://react-turnstile.vercel.app/basic",
      "websiteKey": "1x00000000000000000000AA"
    }
  }'
```

## 验收状态

| 目标 | Site key | 状态 |
|------|----------|------|
| `https://react-turnstile.vercel.app/basic` | `1x00000000000000000000AA` | ✅ 已返回令牌 |

!!! info "Dummy token 说明"
    Cloudflare 测试密钥返回 `XXXX.DUMMY.TOKEN.XXXX`，这是预期行为，表明组件已正常识别。

## 注意事项

- Turnstile 大多数情况下自动完成，无需用户交互。
- 生产环境的真实密钥将返回真实令牌（非 dummy）。
