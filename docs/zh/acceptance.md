# 验收测试

本页记录各支持验证码类型的验收目标、测试 URL、site key，以及本地验证运行的观察结果。

## 总览

| 验证码类型 | 目标 | 状态 |
|----------|------|------|
| reCAPTCHA v3 | `https://antcpt.com/score_detector/` | ✅ 已返回令牌 |
| Cloudflare Turnstile | `https://react-turnstile.vercel.app/basic` | ✅ 已返回 Dummy 令牌 |
| reCAPTCHA v2 | `https://www.google.com/recaptcha/api2/demo` | ⚠️ 需要音频挑战（见说明） |
| hCaptcha | `https://accounts.hcaptcha.com/demo` | ⚠️ 依赖挑战类型 |
| Image-to-Text | 本地 base64 图片 | ✅ 视觉模型返回文本 |
| 分类任务 | 本地 base64 网格 | ✅ 视觉模型返回对象索引 |

---

## reCAPTCHA v3 — 主要验收目标

**URL：** `https://antcpt.com/score_detector/`  
**Site key：** `6LcR_okUAAAAAPYrPe-HK_0RULO1aZM15ENyM-Mf`

### 验收步骤

1. 安装依赖和 Playwright Chromium。
2. 启动服务：`python main.py`
3. 确认 `GET /api/v1/health` 返回全部 19 种任务类型。
4. 创建 `RecaptchaV3TaskProxyless` 任务。
5. 轮询 `POST /getTaskResult` 直至 `status=ready`。
6. 确认返回非空的 `solution.gRecaptchaResponse`。

### 已验证结果

- 服务启动：✅
- 健康检查端点：✅（19 种类型已注册）
- 任务创建：✅
- 令牌返回：✅（非空 `gRecaptchaResponse`，长度约 1060 字符）

---

## Cloudflare Turnstile

**URL：** `https://react-turnstile.vercel.app/basic`  
**Site key：** `1x00000000000000000000AA`（Cloudflare 官方测试密钥——始终通过）

### 已验证结果

- 令牌返回：✅ `XXXX.DUMMY.TOKEN.XXXX`（Cloudflare 测试密钥的预期行为）

---

## reCAPTCHA v2

**URL：** `https://www.google.com/recaptcha/api2/demo`  
**Site key：** `6Le-wvkSAAAAAPBMRTvw0Q4Muexq9bi0DJwx_mJ-`

### 无头浏览器行为

Google 风险分析引擎会检测无头浏览器。复选框点击成功，但会弹出图像挑战，而不是直接签发令牌。

**已实现的缓解措施：** 求解器回退到**音频挑战路径**——在挑战对话框中点击音频按钮，通过求解出口下载 MP3，经配置的语音转写模型转录，并提交转录文本。

!!! note "音频挑战转写"
    音频路径使用 `CLOUD_AUDIO_MODEL`（默认 `whisper-1`），通过 `CLOUD_BASE_URL` 端点的 `audio.transcriptions` 调用。`CLOUD_MODEL` 是聊天模型，不用于音频。准确性取决于所配置的端点。音频 URL 仅允许 Google 所属主机后缀上的 `https`，拒绝重定向，下载上限 5 MB。

### 状态

⚠️ 已实现音频挑战回退。成功率取决于模型的音频处理能力和 Google 当前的挑战难度。

---

## hCaptcha

**URL：** `https://accounts.hcaptcha.com/demo`  
**Site key：** `10000000-ffff-ffff-ffff-000000000001`（hCaptcha 官方测试密钥）

### 状态

⚠️ 复选框点击成功。令牌签发取决于 hCaptcha 的机器人检测评分。对于测试环境，推荐使用 [HCaptchaClassification](../usage/classification.md) 任务类型（直接图像分类）作为集成方案。

---

## 总结

- ✅ **reCAPTCHA v3** 和 **Turnstile** 完全可用，每次本地测试均通过。
- ⚠️ **reCAPTCHA v2** 和 **hCaptcha** 浏览器自动化求解受无头浏览器检测限制。这些类型主要通过 `HCaptchaClassification` / `ReCaptchaV2Classification` 分类任务进行图像网格求解集成。
- 本服务设计为 **flow2api 的后端打码工具**——实际集成中，通常提取图像挑战帧并发送到分类端点，而非完全依赖浏览器自动化通过组件。
