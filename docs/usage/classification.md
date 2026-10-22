# Image Classification Usage

Image classification tasks send one or more captcha images to an OpenAI-compatible vision model and return the indices of matching cells or a boolean answer. No browser automation is involved — these are pure vision model API calls.

## Supported task types

| Task type | Description |
|-----------|-------------|
| `HCaptchaClassification` | hCaptcha 3x3 grid — returns matching cell indices |
| `ReCaptchaV2Classification` | reCAPTCHA v2 3x3 / 4x4 grid — returns matching cell indices |
| `FunCaptchaClassification` | FunCaptcha 2x3 grid — returns the correct cell index |
| `AwsClassification` | AWS CAPTCHA image selection |

## Solution fields

| Task type | Solution field | Example |
|-----------|---------------|---------|
| `HCaptchaClassification` | `objects` or `answer` | `[0, 2, 5]` or `true` |
| `ReCaptchaV2Classification` | `objects` | `[0, 3, 6]` |
| `FunCaptchaClassification` | `objects` | `[4]` |
| `AwsClassification` | `objects` | `[1]` |

## HCaptchaClassification

### Request shape

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

The `queries` field accepts a list of base64-encoded images (one per grid cell). The `question` field is the challenge prompt displayed to the user.

### Response

```json
{
  "errorId": 0,
  "status": "ready",
  "solution": {
    "objects": [1, 4]
  }
}
```

## ReCaptchaV2Classification

### Request shape

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

The `image` field is a single base64-encoded image of the full reCAPTCHA grid (3×3 = 9 cells or 4×4 = 16 cells). Cells are numbered 0–8 (or 0–15), left-to-right, top-to-bottom.

### Response

```json
{
  "errorId": 0,
  "status": "ready",
  "solution": {
    "objects": [0, 3, 6]
  }
}
```

## FunCaptchaClassification

### Request shape

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

The grid is typically 2×3 (6 cells). Usually one answer is expected.

### Response

```json
{
  "errorId": 0,
  "status": "ready",
  "solution": {
    "objects": [3]
  }
}
```

## AwsClassification

### Request shape

```json
{
  "clientKey": "your-client-key",
  "task": {
    "type": "AwsClassification",
    "image": "<base64-encoded-image>",
    "question": "Select the image that matches"
  }
}
```

### Response

```json
{
  "errorId": 0,
  "status": "ready",
  "solution": {
    "objects": [1]
  }
}
```

## Create and poll (generic example)

```bash
# Step 1: create task
TASK_ID=$(curl -s -X POST http://localhost:8000/createTask \
  -H "Content-Type: application/json" \
  -d '{
    "clientKey": "your-client-key",
    "task": {
      "type": "ReCaptchaV2Classification",
      "image": "'$(base64 -w0 captcha.png)'",
      "question": "Select all images with traffic lights"
    }
  }' | python -c "import sys,json; print(json.load(sys.stdin)['taskId'])")

# Step 2: poll result
curl -s -X POST http://localhost:8000/getTaskResult \
  -H "Content-Type: application/json" \
  -d "{\"clientKey\":\"your-client-key\",\"taskId\":\"$TASK_ID\"}"
```

## Operational notes

- Classification runs asynchronously through the task manager: `createTask` returns immediately and the model call happens in the background; poll `getTaskResult`.
- Classification is a tier-1 (local-backend) call. The local vision model is configured by `LOCAL_BASE_URL`, `LOCAL_API_KEY`, and `LOCAL_MODEL` (default `Qwen/Qwen3.5-2B`). Accuracy depends on that model.
- There is no automatic substitution of the cloud chat model for classification. The cloud model is used only when `MODEL_CONNECTION_FALLBACK=true` (default) and the local call fails with a connection error, and only if `VISION_CLOUD_ENABLED=true` and the budget allows a paid call. Set `CLOUD_BASE_URL` / `CLOUD_API_KEY` / `CLOUD_MODEL` for that path.
- Images are passed through as supplied: a missing `data:image/...;base64,` prefix is added (the mime type is sniffed with Pillow), but the solver does **not** resize or otherwise normalize the pixels. Pre-scale your images if the model benefits from it.
- Each attempt is retried (`CAPTCHA_RETRIES`) on transient failures, including an unparseable model reply, and each attempt is recorded in the cost ledger.
