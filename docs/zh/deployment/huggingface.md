# Hugging Face Spaces 部署

本指南说明如何使用 **Hugging Face Spaces** 的 Docker Space 来部署 CaptchAI。

## 什么时候适合用 Hugging Face Spaces

当你有以下需求时，Hugging Face Spaces 会比较合适：

- 希望快速部署一个公开或私有演示环境
- 希望使用图形界面的托管流程
- 希望在 Space 设置里直接管理密钥
- 不想自己维护 VPS，但又需要 Docker 部署环境

## 1. 准备仓库

请确认仓库中已经包含：

- `Dockerfile.render`
- `main.py`
- `requirements.txt`
- `src/` 应用目录

对于 Hugging Face Spaces，当前 Dockerfile 可以直接作为起点，因为它已经包含 Python 依赖安装和 Playwright Chromium 安装步骤。

## 2. 创建 Docker Space

在 Hugging Face 中：

1. 创建新的 **Space**。
2. SDK 选择 **Docker**。
3. 根据需要选择公开或私有。
4. 将 Space 连接到本仓库，或上传项目文件。

## 3. 配置密钥和变量

在 Space 设置中添加以下 secrets：

- `CLIENT_KEY`
- `CAPTCHA_API_KEY`

按需添加或覆盖变量：

- `CAPTCHA_BASE_URL`
- `CAPTCHA_MODEL`
- `CAPTCHA_MULTIMODAL_MODEL`
- `BROWSER_HEADLESS=true`
- `BROWSER_TIMEOUT=30`
- `SERVER_PORT=7860`

Hugging Face Spaces 通常对外暴露 `7860` 端口，因此建议设置 `SERVER_PORT=7860`。

## 4. 确认启动命令

容器应通过以下命令启动应用：

```bash
python main.py
```

当前入口已经支持通过环境变量读取端口。

## 5. 等待构建完成

当 Space 开始构建后：

- 观察构建日志
- 确认依赖安装成功
- 确认 Playwright Chromium 安装成功
- 等待应用进入运行状态

## 6. 验证部署结果

当 Space 可访问后，先验证：

### 根接口

```bash
curl https://<your-space-subdomain>.hf.space/
```

### 健康检查

```bash
curl https://<your-space-subdomain>.hf.space/api/v1/health
```

### 创建 detector 任务

```bash
curl -X POST https://<your-space-subdomain>.hf.space/createTask \
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

- Hugging Face Spaces 部署方便，但冷启动和资源限制会影响 Playwright 这类浏览器自动化负载。
- 相比纯 API 服务，浏览器自动化对共享托管环境更敏感。
- 如果你需要更强的运行时控制，建议使用 Render 或自有基础设施。

## 推荐用途

Hugging Face Spaces 更适合：

- 验证
- 演示
- 低流量内部使用
- 作为文档中可直接访问的公开部署示例
