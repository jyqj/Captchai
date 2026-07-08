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

- `CLIENT_KEY`
- `CAPTCHA_API_KEY`

### 建议变量

- `CAPTCHA_BASE_URL=https://your-openai-compatible-endpoint/v1`
- `CAPTCHA_MODEL=gpt-5.4`
- `CAPTCHA_MULTIMODAL_MODEL=qwen3.5-2b`
- `CAPTCHA_RETRIES=3`
- `CAPTCHA_TIMEOUT=30`
- `BROWSER_HEADLESS=true`
- `BROWSER_TIMEOUT=30`

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
