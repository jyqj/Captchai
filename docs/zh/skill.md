# Agent Skill

CaptchAI 在 `skills/` 目录下附带了可复用的 skills。

## 可用 skills

- `skills/captchai/` — 用于部署、验证、集成和运维服务
- `skills/captchai-image/` — 用于生成 README、文档和发布素材所需的公开安全图片

## For humans

如果你的工具支持直接读取本地 skill 目录，可以把下面这些目录复制到你的本地 skills 目录中：

```text
skills/captchai/
skills/captchai-image/
```

如果你的工具会缓存 skill 元信息，请复制后重启。

## Let an LLM do it

你也可以把下面这段话直接贴给支持工具调用的 LLM agent：

```text
Install the CaptchAI skills from this repository and make them available in my local skills directory. Then show me how to use the operational skill for deployment and the image skill for generating README or docs visuals.
```

## 运维 skill 的作用

`captchai` skill 主要覆盖：

- 本地启动
- 环境变量配置
- YesCaptcha 风格 API 使用
- flow2api 集成
- Render 部署
- Hugging Face Spaces 部署
- 任务验收与排障

## 图片 skill 的作用

`captchai-image` skill 主要覆盖：

- README Hero 图 prompt
- 文档插图
- 面向公开仓库的安全技术视觉素材
- 架构风格图片
- 面向 agent 工作流的可复用图像生成 prompt

## 设计保证

这些 skill 的设计目标包括：

- 只使用占位符密钥
- 与当前已实现任务类型保持一致
- 明确说明当前限制
- 避免嵌入 secrets、私有接口地址或客户数据
