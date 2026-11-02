# 沙箱 DeepSeek 域名解析故障

检查日期：2026-10-23（Asia/Shanghai）。这是此前使用 DeepSeek 时的故障记录：当时聊天在建立模型连接前失败。当前默认配置已切换为可从沙箱访问的集团 IdeaLab，默认 `qwen3.8-flash`；免费只读管理 API 已验证 Token 及三个可选模型授权，尚未验收真实聊天生成。以下 DeepSeek DNS 故障仍未解决。

## 可交给沙箱平台管理员的排障信息

> 沙箱：`1dea2a5d89c2427eaf8b4f151950687b`。
> 应用需要通过 HTTPS 访问 `api.deepseek.com:443`。
> 容器中 `socket.getaddrinfo("api.deepseek.com", 443)` 返回 `gaierror(-2)`；通过已配置的 DNS 地址 `100.100.2.136`、`100.100.2.138` 查询 A 记录均得到 RCODE 3（NXDOMAIN），答案数为 0。同一路径查询 `api.openai.com` 可得到答案。
> 平台动态策略显示 `default_action=allow`，已通过官方 CLI 成功合并 `allow=api.deepseek.com`，之后解析仍失败。
> 请检查 egress DNS 代理的命中/拒绝日志、静态 deny 规则、动态策略同步及上游 DNS，恢复该域名的正常解析与 HTTPS 出站。现有结果还无法区分代理拦截和上游解析故障。

## 证据边界

- [OpenSandbox 官方文档](https://open-sandbox.ai/architecture/network/egress)说明，DNS 请求会被透明转发至 egress 代理，域名拒绝可表现为 NXDOMAIN；静态 `deny.always` 优先于动态 API 规则。分别指定两个 resolver 仍可能经过同一个代理，不能据此认定两个实际上游 DNS 均故障。
- 应用容器看不到 `/var/egress/rules/deny.always` 或 `allow.always`，这不代表旁路容器中没有静态规则。
- 未修改系统 DNS、hosts、TLS 校验或网络拦截规则，未切换到未授权代理，也未读取平台身份凭据。
- 检查使用 DNS 和未认证 HTTPS 连接；未调用付费模型。Web 页面可用与模型连通性是两个独立检查项。

## 应用侧已完成

- 发布 `b8130dc`：共享聊天用固定中文说明区分 DNS、TLS、连接、超时、认证、额度和上游错误；不返回内部异常原文。
- 本机全量 228 项测试通过；沙箱 11 项共享错误测试通过，全部使用假模型或模拟传输。
- 当时的账号版本在沙箱内部登录页面返回 200，未登录首页重定向 303，历史接口拒绝未登录请求 401；后续版本已改为免登录匿名会话。浏览器端到端和真实模型生成尚未验收。

如需切回 DeepSeek，平台修复后应先确认沙箱中的系统解析及未认证 HTTPS 连接恢复，再打开网页发送一条消息验证真实聊天。当前 Web 已取消账号注册和登录；原 DeepSeek 私有凭据及聊天数据保留。
