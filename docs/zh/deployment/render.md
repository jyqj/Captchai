# Render 部署

本指南说明如何使用仓库中已经提供的 Docker 文件，把 CaptchAI 部署到 **Render**。

## 什么时候适合用 Render

当你有以下需求时，Render 是一个很合适的选择：

- 希望获得稳定的公网地址
- 希望方便地管理密钥和环境变量
- 希望使用简单的 Docker 部署流程
- 希望比演示型托管平台拥有更稳定的运行环境

## 1. 准备仓库

当前仓库已经包含 Render 所需文件：

- `Dockerfile.render`
- `render.yaml`
- `main.py`
- `requirements.txt`
- `src/`

应用默认监听 `8000` 端口，同时也兼容 Render 注入的 `PORT` 环境变量。

## 2. 创建 Render 服务

在 Render 中：

1. 创建新的 **Web Service**。
2. 连接你的 GitHub 仓库。
3. Runtime 选择 **Docker**。
4. 指向以下配置：
   - Dockerfile：`Dockerfile.render`
   - Context：仓库根目录

你也可以直接导入仓库中的 `render.yaml` blueprint。

## 3. 配置环境变量

### 必需密钥

请在 Render 控制台中配置以下受保护变量：

- `CLIENT_KEY` — 公网 Render 地址**必需**。未设置时服务会拒绝启动（默认拒绝），除非 `ALLOW_ANONYMOUS=true`。
- `CLOUD_API_KEY` — 使用云端模型（音频转写 / 困难网格升级）时需要。

### 建议变量

- `CLOUD_BASE_URL=https://your-openai-compatible-endpoint/v1`
- `CLOUD_MODEL=gpt-5.4`
- `CLOUD_AUDIO_MODEL=whisper-1`
- `LOCAL_BASE_URL=http://<your-model-host>:30000/v1`（仅当有可从 Render 访问的本地视觉模型）
- `LOCAL_MODEL=Qwen/Qwen3.5-2B`
- `ADMIN_KEY=<强随机值>`（保护 `/admin/*`）
- `RATE_LIMIT_RPM=60`
- `SSRF_PROTECT=true`
- `CAPTCHA_RETRIES=3`
- `CAPTCHA_TIMEOUT=30`
- `BROWSER_HEADLESS=true`
- `BROWSER_TIMEOUT=30`
- `BROWSER_DISABLE_SANDBOX=true` — 仓库 Dockerfile 以非 root 用户运行，因此默认启用沙箱。仅当 Render 容器无法提供 Chromium 沙箱所需的 user-namespace 支持时才设置该项。
- `REDIS_URL` — 多实例部署与重启保留推荐使用（Render Redis 附加服务）。共享与每进程状态的说明见 `docs/zh/getting-started.md`。

仓库自带的 `render.yaml` 仍列出旧版 `CAPTCHA_*` 变量；它们仍作为 `CLOUD_*` / `LOCAL_*` 的回退被接受。建议用上面的拆分名称覆盖。

Render 健康检查使用 `GET /ready`（HTTP 200 就绪 / 503 未就绪），而不是详细状态端点 `/api/v1/health`。

## 4. 触发首次部署

保存配置后：

- 等待镜像构建完成
- 确认 Python 依赖安装成功
- 确认 Playwright Chromium 安装成功
- 等待服务进入 healthy 状态

## 5. 验证部署结果

当 Render 提供 URL 后，先检查：

### 根接口

```bash
curl https://<your-render-service>.onrender.com/
```

### 健康检查

```bash
curl https://<your-render-service>.onrender.com/api/v1/health
```

### 创建 detector 任务

```bash
curl -X POST https://<your-render-service>.onrender.com/createTask \
  -H "Content-Type: application/json" \
  -d '{
    "clientKey": "your-client-key",
    "task": {
      "type": "RecaptchaV3TaskProxyless",
      "websiteURL": "https://antcpt.com/score_detector/",
      "websiteKey": "6LcR_okUAAAAAPYrPe-HK_0RULO1aZM15ENyM-Mf",
      "pageAction": "homepage"
    }
  }'
```

## 运行说明

- 相比轻量演示型托管平台，Render 更适合浏览器自动化类服务。
- 但浏览器求解依然会受到冷启动、IP 质量和容器资源限制的影响。
- 如果你需要更强的运行时控制，建议迁移到自有基础设施。

## 推荐用途

Render 很适合作为以下场景的默认部署方案：

- 持续在线的公网服务
- flow2api 联调
- 低到中等流量的生产环境
- 不想自己维护服务器时的快速上线
