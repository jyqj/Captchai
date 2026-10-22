# reCAPTCHA v2 使用指南

reCAPTCHA v2 向用户展示"我不是机器人"复选框。求解器使用真实浏览器访问目标页面，点击复选框，并提取生成的 `gRecaptchaResponse` 令牌。当 Google 升级为挑战时，求解器使用音频挑战并通过语音转写模型识别。

## 支持的任务类型

| 任务类型 | 说明 |
|---------|------|
| `NoCaptchaTaskProxyless` | 标准 reCAPTCHA v2 复选框 |
| `RecaptchaV2TaskProxyless` | 同上，备用命名 |
| `RecaptchaV2EnterpriseTaskProxyless` | reCAPTCHA v2 企业版 |

## 必填字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `websiteURL` | string | 包含验证码的页面完整 URL |
| `websiteKey` | string | 页面 HTML 中的 `data-sitekey` 值 |

## 可选字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `isInvisible` | bool | 不可见 reCAPTCHA 设为 `true`；求解器调用 `grecaptcha.execute()` 而非点击复选框 |
| `userAgent` | string | 强制 UA，使令牌匹配下游提交；回显到 `solution.userAgent` |
| `proxyType` / `proxyAddress` / `proxyPort` / `proxyLogin` / `proxyPassword` | | 通过代理求解（令牌绑定 IP） |
| `proxy` | string | 单字符串形式，如 `http://user:pass@host:port` |
| `egress` | string | `auto`（默认）、`proxyless`、`pool` 或 `task` |

## SSRF 与 URL 校验

在签出代理或浏览器上下文之前，`websiteURL` 必须通过公网 URL 预检：协议为 `http`/`https`、主机可解析、所有解析地址均为公网地址。回环、RFC1918/ULA 私有、链路本地（含 `169.254.169.254`）、保留、多播和未指定地址都会被拒绝。reCAPTCHA v2 始终执行该检查（不受 `SSRF_PROTECT` 门控）。被拒绝的 URL 表现为 `ERROR_CAPTCHA_UNSOLVABLE`。

## 测试目标

Google 官方 Demo 页面适合验收测试：

- **URL：** `https://www.google.com/recaptcha/api2/demo`
- **Site key：** `6Le-wvkSAAAAAPBMRTvw0Q4Muexq9bi0DJwx_mJ-`

## 创建任务

```bash
curl -X POST http://localhost:8000/createTask \
  -H "Content-Type: application/json" \
  -d '{
    "clientKey": "your-client-key",
    "task": {
      "type": "NoCaptchaTaskProxyless",
      "websiteURL": "https://www.google.com/recaptcha/api2/demo",
      "websiteKey": "6Le-wvkSAAAAAPBMRTvw0Q4Muexq9bi0DJwx_mJ-"
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

就绪时返回：

```json
{
  "errorId": 0,
  "status": "ready",
  "solution": {
    "gRecaptchaResponse": "03AGdBq24..."
  }
}
```

## 不可见 reCAPTCHA

对于使用不可见 reCAPTCHA 的页面（无可见复选框），添加 `"isInvisible": true`。求解器直接调用 `grecaptcha.execute()`，而不是点击复选框。

## 音频挑战路径

Google 的风险分析经常检测到无头浏览器，并弹出图像挑战而非直接签发令牌。此时 reCAPTCHA v2 求解器回退到音频挑战：

1. 点击复选框；Google 弹出挑战对话框。
2. 在挑战对话框中点击音频按钮。
3. 下载 MP3 音频文件。
4. 通过 `CLOUD_AUDIO_MODEL`（默认 `whisper-1`）配置的**云端语音转写模型**，调用 OpenAI 兼容的 `audio.transcriptions` 端点进行转写。
5. 提交转录文本以获得令牌。

!!! warning "音频转写要求"
    音频路径使用 `CLOUD_AUDIO_MODEL`——即 `CLOUD_BASE_URL` + `CLOUD_API_KEY` 上的 Whisper 系列语音转写模型。`CLOUD_MODEL` 是聊天模型，**不会**用于转写。该调用与其他付费模型调用一样受预算门控并计费；准确性和可用性取决于所配置的端点。

### 音频下载安全

音频 URL 读取自 Google 的挑战 iframe，从本进程视角看属于第三方 DOM，因此按不可信输入处理：

- URL 必须为 `https`，主机必须是 `google.com`、`recaptcha.net` 或 `gstatic.com`（或其子域名）；
- 拒绝重定向；
- 响应体流式读取并硬限制 5 MB；
- 求解配置了任务代理时，下载通过该代理进行（仅支持 `http`/`https` 代理服务器；SOCKS 代理 URL 会被跳过而不是让求解失败），因此音频下载与求解共享出口。

## 推荐的集成方案

对于生产环境中可靠的 reCAPTCHA v2 求解，建议使用**分类任务**方案：

1. 使用 Playwright 从页面提取挑战图像网格。
2. 将网格图片发送给 `ReCaptchaV2Classification`，附上挑战问题。
3. 使用返回的格子索引以编程方式点击匹配的格子。

详见[图像分类](classification.md)。

## 注意事项

- 令牌有效期约 120 秒，请尽快提交。
- `RecaptchaV2EnterpriseTaskProxyless` 类型使用相同的浏览器路径。
- 在侵入性较低的网站（非 Google 自己的 Demo），复选框点击可能直接成功而无需挑战。
- `isInvisible`、`userAgent`、代理字段与 `egress` 详见 [API 参考](../api-reference.md)。
