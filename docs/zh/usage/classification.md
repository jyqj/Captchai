# 图像分类使用指南

图像分类任务将一张或多张验证码图片发送给 OpenAI-compatible 视觉模型，返回匹配格子的索引或布尔值答案。无需浏览器自动化，这些均为纯视觉模型 API 调用。

## 支持的任务类型

| 任务类型 | 说明 |
|---------|------|
| `HCaptchaClassification` | hCaptcha 3×3 网格——返回匹配格子索引 |
| `ReCaptchaV2Classification` | reCAPTCHA v2 3×3/4×4 网格——返回匹配格子索引 |
| `FunCaptchaClassification` | FunCaptcha 2×3 网格——返回正确格子索引 |
| `AwsClassification` | AWS 验证码图像选择 |

## 返回字段

| 任务类型 | 返回字段 | 示例 |
|---------|---------|------|
| `HCaptchaClassification` | `objects` 或 `answer` | `[0, 2, 5]` 或 `true` |
| `ReCaptchaV2Classification` | `objects` | `[0, 3, 6]` |
| `FunCaptchaClassification` | `objects` | `[4]` |
| `AwsClassification` | `objects` | `[1]` |

## HCaptchaClassification 示例

```json
{
  "clientKey": "your-client-key",
  "task": {
    "type": "HCaptchaClassification",
    "queries": ["<base64-image-1>", "<base64-image-2>", "<base64-image-3>"],
    "question": "Please click each image containing a bicycle"
  }
}
```

`queries` 字段接受 base64 编码的图片列表（每个格子一张）。

## ReCaptchaV2Classification 示例

```json
{
  "clientKey": "your-client-key",
  "task": {
    "type": "ReCaptchaV2Classification",
    "image": "<base64-encoded-grid-image>",
    "question": "Select all images with traffic lights"
  }
}
```

`image` 字段为完整的 reCAPTCHA 网格图片（3×3 = 9 格，或 4×4 = 16 格）。格子编号从 0 开始，从左到右、从上到下。

## FunCaptchaClassification 示例

```json
{
  "clientKey": "your-client-key",
  "task": {
    "type": "FunCaptchaClassification",
    "image": "<base64-encoded-grid-image>",
    "question": "Pick the image that shows a boat facing left"
  }
}
```

## 注意事项

- 分类通过任务管理器异步执行：`createTask` 立即返回，模型调用在后台进行；请轮询 `getTaskResult`。
- 分类属于 tier-1（本地后端）调用。本地视觉模型由 `LOCAL_BASE_URL`、`LOCAL_API_KEY`、`LOCAL_MODEL`（默认 `Qwen/Qwen3.5-2B`）配置，准确性取决于该模型。
- 不会自动用云端聊天模型替代分类模型。仅当 `MODEL_CONNECTION_FALLBACK=true`（默认）且本地调用出现连接错误，同时 `VISION_CLOUD_ENABLED=true` 且预算允许付费调用时，才会使用云端模型；该路径需要配置 `CLOUD_BASE_URL` / `CLOUD_API_KEY` / `CLOUD_MODEL`。
- 图片按原样传入：仅当缺少 `data:image/...;base64,` 前缀时补上（用 Pillow 嗅探 mime 类型），求解器**不会**缩放或做像素级归一化。若模型需要，请自行预先缩放。
- 每次尝试（`CAPTCHA_RETRIES`）在瞬时失败（包括模型回复无法解析）时会重试，且每次尝试都会记入成本账本。
