# 本地模型部署

CaptchAI 支持使用 [SGLang](https://github.com/sgl-project/sglang)、[vLLM](https://github.com/vllm-project/vllm) 或任何 OpenAI 兼容推理服务在本地部署图像识别和分类模型。

本指南以 [Qwen3.5-2B](https://modelscope.cn/models/Qwen/Qwen3.5-2B) + SGLang 为例。

## 架构：本地模型 vs 云端模型

CaptchAI 使用两种模型后端：

| 后端 | 角色 | 环境变量 | 默认值 |
|------|------|---------|-------|
| **本地模型** | 图像识别与分类（高吞吐，自托管） | `LOCAL_BASE_URL`, `LOCAL_API_KEY`, `LOCAL_MODEL`, `LOCAL_MAX_CONCURRENCY` | `http://localhost:30000/v1`, `EMPTY`, `Qwen/Qwen3.5-2B`, `0`（不限） |
| **云端模型** | 音频转录与困难网格升级（强大远程 API） | `CLOUD_BASE_URL`, `CLOUD_API_KEY`, `CLOUD_MODEL`, `CLOUD_AUDIO_MODEL`, `CLOUD_MAX_CONCURRENCY` | 外部端点, 你的密钥, `gpt-5.4`, `whisper-1`, `4` |

`MODEL_CONNECTION_FALLBACK=true`（默认）会在连接失败时在另一后端重试一次：本地 → 云端仅在 `VISION_CLOUD_ENABLED=true` 且预算允许时；云端 → 本地始终允许。

## 前置要求

- Python 3.10+
- NVIDIA GPU + CUDA（推荐 8GB+ 显存用于 Qwen3.5-2B）

## 第一步：安装 SGLang

```bash
pip install "sglang[all]>=0.4.6.post1"
```

## 第二步：启动模型服务

### 从 ModelScope 下载（国内推荐）

```bash
export SGLANG_USE_MODELSCOPE=true
python -m sglang.launch_server \
  --model-path Qwen/Qwen3.5-2B \
  --host 0.0.0.0 \
  --port 30000
```

### 从 Hugging Face 下载

```bash
python -m sglang.launch_server \
  --model-path Qwen/Qwen3.5-2B \
  --host 0.0.0.0 \
  --port 30000
```

### 多 GPU 部署

```bash
python -m sglang.launch_server \
  --model-path Qwen/Qwen3.5-2B \
  --host 0.0.0.0 \
  --port 30000 \
  --tensor-parallel-size 2
```

启动后，服务在 `http://localhost:30000/v1` 提供 OpenAI 兼容 API。

## 第三步：验证模型服务

```bash
curl http://localhost:30000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Qwen/Qwen3.5-2B",
    "messages": [{"role": "user", "content": "Hello"}],
    "max_tokens": 32
  }'
```

## 第四步：配置 CaptchAI

```bash
# 本地模型（SGLang 自托管）
export LOCAL_BASE_URL="http://localhost:30000/v1"
export LOCAL_API_KEY="EMPTY"
export LOCAL_MODEL="Qwen/Qwen3.5-2B"

# 云端模型（远程 API，用于音频转录 / 困难网格升级）
export CLOUD_BASE_URL="https://your-api-endpoint/v1"
export CLOUD_API_KEY="sk-your-key"
export CLOUD_MODEL="gpt-5.4"
export CLOUD_AUDIO_MODEL="whisper-1"

# 其他配置
export CLIENT_KEY="your-client-key"
export BROWSER_HEADLESS=true
```

## 第五步：启动 CaptchAI

```bash
python main.py
```

健康/详情端点为 `GET /api/v1/health`，返回两个模型后端、浏览器实际运行时（`browser_runtime`、`browser_runtime_requested`、`browser_runtime_effective`、`browser_runtime_degraded`、`browser_driver`）以及 `degraded_subsystems`。编排器就绪探针使用 `GET /ready`（HTTP 200/503；注册了浏览器任务类型时检查浏览器，配置 `REDIS_URL` 时检查 Redis）。

## 向后兼容

旧版环境变量（`CAPTCHA_BASE_URL`、`CAPTCHA_API_KEY`、`CAPTCHA_MODEL`、`CAPTCHA_MULTIMODAL_MODEL`）仍作为拆分后后端的回退。`CLOUD_BASE_URL` 回退到 `CAPTCHA_BASE_URL`；`LOCAL_BASE_URL` 仅在未设置时使用 `CAPTCHA_BASE_URL`。`CLOUD_MODEL`/`LOCAL_MODEL` 分别回退到 `CAPTCHA_MODEL`/`CAPTCHA_MULTIMODAL_MODEL`，两个 API key 都回退到 `CAPTCHA_API_KEY`。设置后 `LOCAL_*` 与 `CLOUD_*` 优先生效。`CLOUD_AUDIO_MODEL` 没有旧版别名。

## 推荐模型

| 模型 | 大小 | 用途 | 显存 |
|------|------|------|------|
| `Qwen/Qwen3.5-2B` | 2B | 图像识别与分类 | ~5 GB |
| `Qwen/Qwen3.5-7B` | 7B | 更高精度分类 | ~15 GB |
| `Qwen/Qwen3.5-2B-FP8` | 2B（量化） | 低显存需求 | ~3 GB |
