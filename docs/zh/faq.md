# 常见问题

## 这能完全替代 YesCaptcha 吗？

不能。它实现的是本仓库所支持任务类型对应的 YesCaptcha 风格 API，而不是对所有商业平台功能的完整覆盖。

## `minScore` 能保证目标 reCAPTCHA 分数吗？

不能。请求模型里保留了 `minScore` 字段用于兼容，但当前 solver 不会根据它做分数控制。

## 可以使用本地或自托管多模态模型吗？

可以，前提是它们提供支持图像输入的 OpenAI-compatible API。

## `ImageToTextTask` 返回的是纯 OCR 文本吗？

不一定。当前实现会把结构化识别结果序列化后放入 `solution.text`。

## 任务状态会持久化吗？

默认不会：任务状态保存在内存中，10 分钟任务 TTL 到期后清理。设置 `REDIS_URL`（需要可选的 `redis>=4.2` 包）可让任务存储、账本、成功率统计、代理池与幂等 claim 跨 worker 共享并在重启后保留。每进程状态（限流、任务准入、暖会话、模型并发、预算在途预留）不共享。

## 为什么没有 CLIENT_KEY 时服务拒绝启动？

这是默认拒绝的认证保护。无密钥且 `SERVER_HOST` 非回环时，除非显式设置 `ALLOW_ANONYMOUS=true`，否则拒绝启动。`ALLOW_ANONYMOUS` 只放宽启动检查；配置了 `CLIENT_KEY` 时，每个 API 调用仍必须提供它。

## 负载均衡器应该探测哪个端点？

就绪探测用 `GET /ready`（HTTP 200/503）。`GET /api/v1/health` 是始终 200 的存活/详情端点；本版本没有裸 `/health` 路由。

## 哪些因素会影响 reCAPTCHA v3 结果？

常见因素包括 IP 质量、浏览器指纹、目标站行为、`pageAction` 是否正确，以及运行环境本身。
