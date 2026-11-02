# 本机 Codex MCP 清单

2026-10-24 按本机 Codex 的 `mcp_servers` 配置整理，共 16 个条目。这里只表示已经配置，不表示每个服务当前可用、已登录或被 Codex 成功加载。鉴权值、OAuth 缓存、桌面桥接状态和私人目录均未复制。

| 配置名称 | 服务或启动方式 | 迁移说明 |
| --- | --- | --- |
| chrome-devtools | `chrome-devtools-mcp` | 本机 Chrome 调试，需单独安装 |
| ai-auto-collection | 集团包 `@alife/mcp-auto-collection` | 使用集团 npm registry，填写安装后的入口 |
| alibaba_code | `https://mcp.alibaba-inc.com/code/mcp` | `ALIBABA_MCP_TOKEN` Bearer |
| aone-km | `https://mcp.alibaba-inc.com/aone-km/mcp` | 原配置未显式指定请求头；需单独验证登录方式 |
| odps-mcp | `https://mcp.alibaba-inc.com/odps-mcp/mcp` | `ALIBABA_MCP_TOKEN` Bearer |
| stt-qa-ai-tools | `https://pre-mcp.alibaba-inc.com/stt-qa-ai-tools/mcp` | 预发端点，`ALIBABA_MCP_TOKEN` Bearer |
| yuque | `https://mcp.alibaba-inc.com/yuque/mcp` | `ALIBABA_MCP_TOKEN` Bearer |
| libra-abtest-mcp | `https://mcp.alibaba-inc.com/libra-abtest-mcp/mcp` | 原配置通过 `mcp-remote` |
| group-env | `https://mcp.alibaba-inc.com/group-env/mcp` | 原配置通过 `mcp-remote` |
| openaiDeveloperDocs | `https://developers.openai.com/mcp` | 官方文档 MCP |
| loghouse-mcp | `https://mcp.alibaba-inc.com/loghouse-mcp/mcp` | 原配置通过 `mcp-remote` |
| sunfire-mcp | `https://mcp.alibaba-inc.com/sunfire-mcp/mcp` | 原配置通过 `mcp-remote` |
| tpp-ci-mcp | `https://mcp.alibaba-inc.com/tpp-ci-mcp/mcp` | 原配置未显式指定请求头；需单独验证登录方式 |
| code | 与 `alibaba_code` 相同的 Code 端点 | 原配置无显式 Bearer；按鉴权方式择一启用 |
| computer-use | Codex 桌面 Computer Use 应用 | 原配置已禁用，不能迁移为 Linux 服务 |
| node_repl | 桌面应用自带 Node REPL | 依赖桌面桥接与可信服务，模板仅保留占位说明 |

## 在个人 joyagent 中接入

[examples/mcp.codex.toml](../examples/mcp.codex.toml) 使用 joyagent 的 `[mcp.servers.NAME]` 格式。所有条目默认 `enabled=false`，不会在普通启动时安装 npm 包、启动浏览器授权或调用内部服务。

1. 手动安装经过审核的固定版本依赖，例如 `npm install --prefix /owner/tools mcp-remote@<版本>`；把模板中的 `command` 改为真实的绝对可执行路径，例如 `/owner/tools/node_modules/.bin/mcp-remote`。
2. 复制所需服务段到自己的 `agent.toml`，改为 `enabled=true`。需要 Bearer 的服务从启动 joyagent 的环境读取 `ALIBABA_MCP_TOKEN`，不要将 Token 写入仓库。其他服务的登录需独立完成，Codex 的登录缓存不会自动迁移。
3. 先运行 `python3 -m joyagent doctor` 检查配置和命令存在性。它不连接 MCP，也不证明鉴权成功。
4. 服务工具导出为 `mcp_<server>_<tool>`，不合规或过长的名称会规范化并附加哈希。按实际工具名称配置 `policy.toml` 的 `tools.allow`；不要默认授予整个内部系统的写权限。
5. 重启后在聊天输入框用 `/tools` 查看已授权工具，使用 `/tool 工具名 {"参数":"值"}` 调用。此显式入口无需聊天模型参与。

joyagent 当前使用 stdio MCP；HTTP 服务由可选桥接程序处理。模板采用 [mcp-remote 官方自定义请求头方式](https://github.com/punkpeye/mcp-remote#custom-headers)：环境变量中的 Bearer 值由桥接程序展开，配置文件和命令参数只保留占位符。桥接程序是单独安装的第三方依赖，OAuth 状态应由用户自己管理。

## 共享 Web 的范围

这份个人 MCP 配置不会自动启用到共享服务。内部工具可能持有部署、查询数据、发消息等用户权限，不能把个人 Codex 登录状态直接授予所有匿名访客。

共享服务支持 `shared=true`、`sandbox=true` 的单文件 MCP 程序，由管理员提供 `script` 路径，每个匿名访客启动自己的进程，只能访问自己的工作区、只读运行库和该脚本，不继承环境凭据，也没有网络。项目自带的 `examples/mcp_server.py` 可以验证发现工具、调用和结果回显的完整流程，配置见[共享 Web 部署](deploy-shared-web.md)。当前不支持在此隔离模式中直接运行需要联网鉴权的集团 HTTP MCP；清单整理和模板导出不等同于这些服务已经在共享网页接通。
