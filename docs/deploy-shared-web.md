# 共享 Web 部署

共享模式打开链接即可聊天，不需要注册、登录或邀请码，不记录用户名和密码。浏览器自动获得签名匿名 Cookie，各浏览器的会话、长期记忆和工作文件单独保存，使用同一套 owner 管理的模型、SOUL 和工具权限。JoyAgent 内核和 Web 只使用 Python 3.11+ 标准库，无需前端构建；可选的外部 CLI Agent 需要单独安装运行时。

仓库的 `[web]` 默认是 `mode = "local"`，仅供本机回环访问。团队部署需要独立配置、私有会话签名目录和 HTTPS 入口。

根配置与包内默认聊天、决策工具统一使用百炼 Token Plan；下文保留的 IdeaLab 和千问配置用于其他部署示例，当前沙箱配置见下一节。复制示例时须替换完整模型及路由配置。个人 Token Plan 订阅不可共用，不能把本机套餐密钥直接部署到多人共用的服务；服务适用范围见 [Token Plan 文档](https://docs.bailian.console.aliyun.com/zh/model-studio/token-plan-personal-overview)。本机配置变更不会自动修改已有沙箱的私有配置。

## 当前沙箱

当前目标沙箱为 `ff6bb1ddfc424d4083fe1c9000612ffe`，创建于 **2026-11-02**。旧 HTTPS 签名链接于 **2026-11-04 15:40（Asia/Shanghai）** 到期；2026-11-04 已为同一沙箱更新入口，新链接有效期与沙箱到期时间一致，均为 **2026-11-09 15:47:19（Asia/Shanghai）**。链接有效期和沙箱生命周期分别管理，更新链接不会自动续期沙箱。到期前需更新入口、续期或迁移沙箱，并备份配置、会话签名密钥和用户数据；本项目不会自动续期。

2026-11-04 按 owner 要求将该沙箱的聊天与决策配置统一切换到百炼 Token Plan，接口为 `https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`，默认聊天模型为 `deepseek-v4.1-flash`。可选聊天模型为 `deepseek-v4.1-flash`、`qwen3.8-flash`、`qwen3.8-max`、`qwen3.7-plus`、`glm-5.3`、`glm-5.2`；独立决策工具启用 `decision-model-preview`，经同一网关的 `/systemone` 接口调用。聊天与决策共用 `BAILIAN_TOKEN_PLAN_API_KEY` 或版本目录之外的 `server-state/bailian-token-plan-credentials.toml`（权限 `600`），密钥不写入代码或网页。套餐切换时仅更新私有配置并重启服务，随后开放下述页面路由开关。

当前路由预设为 `simple_model = "deepseek-v4.1-flash"`、`complex_model = "qwen3.8-max"`、`min_probability = 0.8`，已同步本机、包内默认模板和沙箱私有配置。通过 `routing.enabled = true` 开放页面“自动路由”开关，`default_auto = false` 使首次打开默认关闭；用户开启后，简单任务、低置信度或分类失败使用 DeepSeek Flash，复杂类概率达到 80% 时使用 Qwen Max。开关与上次手动模型按浏览器保存，用户选择不修改服务器规则，也不影响其他用户。

页面开关发布版本为 `routing-switch-3601d1a3e358`，沙箱进程管理器 PID `1268287`。保留同期侧边栏更新后完成 Linux 903 项 Python 测试（896 通过、7 项跳过）和 130 项前端测试，再切换服务；健康检查、页面默认手动模型与静态资源校验通过。验证使用假模型，未调用真实模型服务。

切换聊天时，配置检查、网关 DNS/TLS 连接和网页默认模型检查通过；沙箱在 `umask 077` 下运行 895 项离线测试，888 项通过、7 项跳过。SSH 默认 `umask 002` 会使测试临时文件带有组写权限，首次运行触发了文件权限校验；复验未修改代码或放宽校验。随后统一 Decision 配置时，本机 902 项离线测试通过（887 通过、15 项跳过），本机与沙箱均通过凭据检查并重启；沙箱内部 HTTP 健康检查与工具目录检查通过，`decision_evaluate` 可用。没有发起真实模型请求，因此尚未验证当前 Token Plan 密钥的模型调用权限或额度。

2026-11-02 切换新实例时，NAS 上的 `/home/admin/workspace/joyagent` 部署文件、`agent-runtimes/v1` 和 `server-state` 均保留。新实例没有继承 Web 进程；已更新 HTTPS 入口与代理 Host 配置，恢复服务并部署“论文”页。新实例使用 Alibaba Cloud Linux 3，已从发行版仓库补装 `bubblewrap 0.4.0`，隔离执行测试通过。换代后应检查发行版依赖和 namespace 能力，不能仅凭文件保留判断服务已经恢复。

更早的一次换代曾丢失原 `/home/admin/.local/share/joyagent-server` 下的配置、签名密钥和会话，因此当前私有状态统一保存在 NAS 的 `/home/admin/workspace/joyagent/server-state`。浏览器匿名 Cookie 随域名隔离；换成新入口后进入新的访客空间，并不表示服务器上原有用户数据被删除。

当前访问方式是 **签名链接直接使用**。持有有效链接的人可以使用共享模型，应用不再设账号门禁；平台签名链接的鉴权和有效期仍由入口管理。匿名 Cookie 只用于数据隔离，不验证集团员工身份，也没有集团 SSO 对接。本文和 Git 仓库不保存实际签名链接。

以下是可复用的应用部署方法。生成签名链接或端口监听成功，都不能单独证明浏览器入口可用。2026-10-29 已在新实例验证完整 Python 回归、四种原生 CLI，以及公网 HTTPS 的首页、匿名 Cookie、CSRF、时间工具和隔离 MCP；内部 HTTP 的脚本执行、多会话、历史隔离和删除也通过。全部模型测试使用假响应，没有付费模型请求；未进行真实浏览器交互或新实例真实模型生成验收，DSH 首次集成异常与复验结果见[验证记录](verification.md)。

## 准备独立配置

在沙箱上的项目根目录复制一份配置，再编辑其中现有的 `[agent]`、`[model]`、`[decision]`、`[routing]`、`[web]` 段，不要重复添加同名 TOML 表：

```bash
cp agent.toml agent.shared.toml
```

配置示例：

```toml
[agent]
name = "joyagent"
soul = "SOUL.md"
policy = "policy.toml"
skills_dir = "skills"
workspace = "shared-workspace"
data_dir = "shared-data"
timezone = "Asia/Shanghai"

[model]
provider = "openai"
model = "qwen3.8-flash"
choices = ["qwen3.8-flash", "qwen3.8-max", "bailian/glm-5.2"]
base_url = "https://idealab.alibaba-inc.com/api/openai/v1"
api_key_env = "IDEALAB_API_TOKEN"
token_parameter = "max_tokens"
timeout_seconds = 120
max_tokens = 16384
credentials_file = "~/.config/joyagent/idealab-credentials.toml"

[decision]
enabled = true
base_url = "https://maas.qianwenaiapi.com/compatible-mode/v1"
model = "decision-model-preview"
api_key_env = "QWENCLOUD_API_KEY"
credentials_file = "~/.config/joyagent/qianwen-credentials.toml"
timeout_seconds = 15

[routing]
enabled = true
simple_model = "qwen3.8-flash"
complex_model = "qwen3.8-max"
min_probability = 0.8

[web]
mode = "shared"
public_origin = "https://joyagent.example.internal"
proxy_hosts = []
session_dir = "shared-sessions"
```

`public_origin` 必须替换为浏览器最终访问的精确 HTTPS origin，例如 `https://实际平台域名`，不带路径、查询串或末尾 `/`。若使用非默认端口，需写入相同端口。它用于校验浏览器请求来源，其主机部分也是默认允许的 Host，不能保留示例值。`proxy_hosts` 默认为空；只有入口会改写 Host 时，才按下文配置实际内部目标地址。

相对路径以配置文件所在目录为准。`data_dir` 和 `session_dir` 的父目录需要由管理员预先创建；`session_dir` 必须位于 workspace 和 data_dir 之外。共享数据根目录及已有用户目录必须属于服务用户、权限为 `700`，不能是符号链接；新建目录也使用 `700`。共享运行时会为每个浏览器匿名身份建立自己的 workspace，示例中的 `shared-workspace` 不作为团队共用文件目录。

内置文件、记忆、时间、只读技能和决策工具按 `policy.toml` 的白名单执行。默认白名单包含 `decision_evaluate`；已有部署需在 `tools.allow` 中加入该名称才会向成员提供。普通本地 MCP 不允许直接用于共享模式；需要执行命令、脚本或共享 MCP 时，按下面显式启用隔离执行器。

每轮聊天模型请求和工具调用预算由 owner 的 `policy.toml` 中 `[limits].max_steps`、`max_tool_calls` 控制，默认分别为 16 次、32 次。JoyAgent 与外部 Agent 共用这两个预算，执行中补充指令不会重置计数；整轮时限仍为 120 秒。修改后需重启服务。

### 自动选择聊天模型

上例开启 `[routing]` 后，网页提供“自动选择”。每轮普通聊天至多先调用一次千问决策模型，发送当前问题和最近少量用户、助手对话，不发送私有模型协议字段或服务凭据。只有分类为 `complex` 且该类别概率至少为 `min_probability`（默认 `0.8`）时使用 IdeaLab `qwen3.8-max`；简单任务、分类不确定或决策接口失败、超时都使用同一 IdeaLab 网关的 `qwen3.8-flash`，不自动重试。Web 结果和历史会显示选择及回退原因。

`min_probability` 对两类采用相同的确定性阈值。已有配置可暂时保留 `simple_min_probability` 作为兼容字段，但它也按新规则解释；建议将字段改名，不能同时配置两个名称，否则启动报错。

分类使用千问凭据与额度，后续聊天使用 IdeaLab 凭据与额度。路由判断受 `[decision].timeout_seconds` 的独立时限约束，默认 15 秒，并计入整轮时限；每轮最多一次，不占聊天模型调用次数或工具调用次数。作为普通工具执行的 `decision_evaluate` 仍计入工具预算。路由启用时必须同时启用 `[decision]`、在 policy 中授权 `decision_evaluate`，且两个目标都在 `model.choices` 中，否则启动报错。

手动选择实际模型会跳过分类，`/tool`、`/tools` 也不自动分类。所有通道共用 `Agent.chat` 与会话锁，本轮多步工具调用保持同一聊天模型。DeepSeek Flash 的协议兼容已就绪，IdeaLab 的准确 ID 是 `bailian/deepseek-v4.1-flash-1017`；当前 Key 的真实请求返回 HTTP 403“该模型需要授权”，因此默认不提供该选项。管理员开通此 Key 的模型权限后，可把该 ID 加入 `[model].choices`，重启并重新验收聊天及工具循环；模型页面不能代替凭据调用验证。

聊天及分类所取的对话历史默认限制为本次输入和同一会话此前两轮；owner 可在 `policy.toml` 的 `[limits]` 设置 `context_turns = 2`，旧配置缺省也为 2。分类继续只取有限的公开用户/助手文本，不发送工具结果；聊天保留当前执行中的完整工具消息组及补充指令。更早的聊天保留在 SQLite/JSONL，不随上下文裁剪删除。系统规则、工具定义与已保存长期偏好保持原有行为。Codex、pi、DSH 在窗口移动时清理并重建对应的原生会话和续接缓存，继续复用进程；更新服务后对已有会话同样生效。

2026-10-24 已通过正式服务的沙箱内部 HTTP 验证简单任务走 Flash、复杂任务走 Max，两条真实回复及路由结果均已持久化。验证范围为 JoyAgent 内核；真实浏览器端到端和四个外部 CLI 的真实模型 API 验收仍未完成，详见[验证记录](verification.md)。

### 独立决策工具

`[decision]` 控制 `decision_evaluate`，用于分类（`choice`）、是非判断（`noul`）和有序评分（`score`），通过 `/systemone` 调用 `decision-model-preview`。[接口说明](https://platform.qianwenai.com/docs/api-reference/decision-model-api)与[完整配置和示例](../README.md#决策分类是非判断与评分)。它使用自己的服务端配置，不替换聊天模型，也不修改 Web 的 `model.choices`。

决策网关、模型、超时和凭据由 owner 管理；成员只能传入业务状态与问题，不能在 Web 请求中指定网关或 API Key。上例使用独立的千问私有凭据文件，与聊天的 IdeaLab 凭据分开；服务端 `QWENCLOUD_API_KEY` 优先，文件的 `[openai].base_url` 需匹配决策网关。配置本身不验证密钥是否具备决策模型权限；需要以真实请求结果验收。停用决策工具前先关闭 `[routing]`，再将 `[decision].enabled` 设为 `false` 或从工具白名单移除名称，并重启服务。

成员可在 Web“执行工具”面板选择 `decision_evaluate`，或在 CLI、TUI、Web 输入同一条命令，统一经 `Agent.chat` 执行并保存历史：

```text
/tool decision_evaluate {"state":"支付失败","questions":{"department":{"type":"choice","instructions":"由哪个团队处理？","criteria":{"billing":"支付退款账单","technical":"软件故障"}}}}
```

这会发出一次真实决策模型请求，使用共享服务端额度；显式命令不调用额外聊天模型。在 JoyAgent 的自然语言聊天中，由模型选择该工具时，聊天请求与决策请求都会使用相应额度。外部 CLI 的原生工具集不自动接入此工具；选中其他 Agent 时仍可通过“执行工具”面板或 `/tool` 显式使用。决策调用受工具权限、每轮工具次数和超时限制。

### 命令、脚本与共享 MCP

要求 Linux、系统 `bubblewrap` / `prlimit`，以及可用的 user、mount、PID、network namespace。安装依赖由管理员完成，应用不自动安装，也不在隔离不可用时退回宿主执行。当前沙箱验证使用发行版 `bubblewrap 0.4.0`。Alibaba Cloud Linux 3 可通过 `sudo dnf install bubblewrap util-linux` 安装；Debian/Ubuntu 使用对应的 `apt` 软件包。安装后运行 `python3 -m unittest discover -s tests -p test_execution.py -v` 验证实际隔离能力。修改配置中现有的 `[execution]` 段：

```toml
[execution]
enabled = true
network = "none" # 默认断网；设为 "host" 时共用外层沙箱网络
timeout_seconds = 10
output_chars = 8000
memory_mb = 256
file_size_mb = 16
process_limit = 4096 # 当前共享服务的 owner 显式值；按账号的进程/线程计数检查 fork

[mcp.servers.demo]
enabled = true
shared = true
sandbox = true
command = "python3"
script = "/absolute/release/path/examples/mcp_server.py"
args = []
timeout_seconds = 15
```

在 `policy.toml` 的现有 `tools.allow` 中加入 `"command_run"`、`"script_run"`、`"mcp_demo_echo"`。`script` 必须是 workspace 外由服务用户或 root 管理且不可被组/其他用户写入的普通程序文件；不要使用访客上传的文件。单文件脚本只读挂载到 `/mcp/server.py`，依赖只能来自只读系统运行库。`command` 必须能解析到 `/usr` 下的系统程序；`args` 是脚本之后的附加参数。

每个命令使用独立隔离进程，当前访客 workspace 挂载到 `/workspace`，Python/Bash 可在此写文件。不挂载宿主 `/proc`、配置、记忆库或其他访客目录，也不传入模型密钥。超时或取消会清理进程；标准输出、标准错误、退出码和超时标志会返回到页面。共享 MCP 使用同样隔离方式，每个访客有独立的 MCP 进程，随运行时关闭而清理，禁止 `env` 和 `pass_env`。本机 Codex 的内部服务配置整理在 [MCP 清单](mcp-codex.md)，不会直接迁入共享服务。

`execution.network` 由服务管理员配置，支持两种模式：

| 值 | 执行环境的网络 |
| --- | --- |
| `"none"`（默认） | 使用独立网络 namespace，命令、脚本和共享 MCP 无法联网；外部 Agent 仍通过专用 relay 请求模型。 |
| `"host"` | 共用外层沙箱的网络 namespace，包括回环端口；只读提供外层的 DNS 配置与 TLS 根证书，沿用外层出站权限。 |

需要与外层沙箱一样联网时，把现有段中的 `network` 改为 `"host"` 后重启服务；此设置同时应用于命令、脚本、共享 MCP 和 Codex、pi、DSH 的新进程。文件、进程及访客工作区隔离仍保留，不自动继承外层代理环境变量或私有凭据。host 模式不是按域名放行的代理，外层沙箱能访问的网络和监听端口也能被工具访问。外层本身无法解析或访问的域名不会因这一设置恢复，应继续检查平台 DNS 和出站策略。

`process_limit` 可设为 4–4096，默认仍为 16。它通过 `RLIMIT_NPROC` 限制工作负载的 fork：检查整个真实 UID 的进程/线程计数，其他访客、常驻 Agent 及平台进程都会计入，不能理解为每个任务独立的进程配额，也不是为账号内所有进程统一设置上限。排查时当前共享实例同账号已有约 148 个线程，16 会使 Bash 启动后无法运行 `cat`、管道等外部命令。2026-10-29 经 owner 明确授权，本服务私有配置已调整为与原生 Agent 启动器一致的 4096 并重启生效；公网命令执行、文件读写、管道、Python 子进程和 Bash 脚本验证通过。根配置和包内默认配置仍为关闭执行、阈值 16。其他部署应由 owner 根据账号负载选择值，网页用户和模型不能修改它。

上述限制包含单进程地址空间和单文件大小，不等于所有子进程的总内存配额或工作区总磁盘配额，也不限制长期运行 MCP 的累计 CPU 时间。服务平台仍需配置容量、资源与访问管理。当前命令执行器不支持 macOS/Windows。

## 服务凭据与匿名会话

模型密钥由服务进程读取。将两套私有凭据文件单独部署到沙箱，不能依赖本机已有文件自动出现在远端。聊天使用 IdeaLab，自动路由分类和独立决策工具使用千问；以下占位符需在各自私有文件中替换：

```toml
# ~/.config/joyagent/idealab-credentials.toml
[openai]
base_url = "https://idealab.alibaba-inc.com/api/openai/v1"
api_key = "此处填 IdeaLab 聊天密钥"
```

```toml
# ~/.config/joyagent/qianwen-credentials.toml
[openai]
base_url = "https://maas.qianwenaiapi.com/compatible-mode/v1"
api_key = "此处填千问决策模型密钥"
```

目录权限为 `700`，文件为 `600`，由运行 joyagent 的系统用户拥有，且文件不能是符号链接。聊天优先读取服务端 `IDEALAB_API_TOKEN`，决策优先读取 `QWENCLOUD_API_KEY`，两者分别覆盖各自文件中的默认值。每个文件的 `[openai].base_url` 必须与对应网关一致，不能交换两套密钥。密钥不提交到 Git，不发给浏览器，也不保存为用户长期记忆。所有成员共用服务端模型配置和额度，没有按 Web 用户分别计费或配置 API Key 的入口。

`model` 是路由关闭时的默认模型；`choices` 决定网页“本轮模型”的手动选项，必须包含默认模型，最多 32 个不同 ID。路由开启时另提供默认的“自动选择”，不需要把 `auto` 加入 `choices`。所有实际聊天模型共用此段的 provider、地址、凭据和参数，需由管理员确认 Token 有相应权限。切换保留会话、记忆和文件；每轮固定使用所选模型，不影响其他浏览器。修改配置后重启服务生效，已被移除的浏览器选择会回到当前默认设置。

千问也可以作为可选聊天网关，见 [README 配置](../README.md#其他聊天网关与直连接口)。切换前备份服务配置，同时修改聊天网关、模型列表、环境变量和凭据文件路径，再重启服务；`[decision]` 保持自己的配置。模型 ID 随网关不同，例如 IdeaLab 的 `bailian/glm-5.2` 在千问使用 `glm-5.2`，路由目标也必须在新网关可用，否则先关闭路由。Web 下拉框只在当前聊天网关内选择模型。

首次启动自动在 `session_dir` 创建私有 `signing.key`，目录为 `700`、文件为 `600`，归服务用户所有，不能是符号链接。这里只保存匿名 Cookie 的签名密钥，不创建账号数据库，不存储用户名、密码或邀请码。密钥须保留以使服务重启后的 Cookie 继续有效，也不能提交到 Git 或发给浏览器。

匿名身份由随机 ID、到期时间及 HMAC 签名组成，有效期为 7 天。Cookie 使用 `Secure`、`HttpOnly`、`SameSite=Lax`，从聊天软件等外站点击链接时仍能带上已有身份；所有 POST 继续校验精确 Origin 与单独派生的 CSRF token。过期或清除 Cookie 后，刷新首页会获得新身份；同一浏览器配置下共用 Cookie 的标签页共享同一份数据。没有账号恢复、跨浏览器同步或退出登录入口。

## 启动与 HTTPS 入口

在项目根目录，以拥有配置及状态目录的服务用户执行：

```bash
python3 -m joyagent --config agent.shared.toml doctor
python3 -m joyagent --config agent.shared.toml web --host 0.0.0.0 --port 8765
```

`doctor` 只检查配置及凭据来源，并校验自动路由配置、显示简单和复杂任务的目标模型；不联网验证 API Key，不产生模型费用。正式运行时使用沙箱支持的进程托管方式，保证退出 SSH 后服务持续运行，并将配置文件路径写为绝对路径。

应用提供内部 HTTP 服务；HTTPS 由沙箱入口或反向代理终止。入口应保留浏览器的 `Origin`。默认只接受 `public_origin` 中的外部 Host；如果入口保留外部 Host，保持 `proxy_hosts = []` 即可。

OpenSandbox 的 ingress 会将 Host 改写为解析后的内部目标地址，见固定版本的 [上游请求重写源码](https://github.com/opensandbox-group/OpenSandbox/blob/f3f4a7f419e526de4c3deb250d72fc034986fc28/components/ingress/pkg/proxy/proxy.go#L226-L250)。遇到此类入口，管理员需核对入口实际转发给应用的 Host，再在现有 `[web]` 段显式加入对应 IP 和端口。例如，若实际目标为 `172.16.0.10:8765`：

```toml
proxy_hosts = ["172.16.0.10:8765"]
```

这里只允许最多 16 个精确、规范的 IP:端口。IPv4 使用点分十进制；IPv6 使用小写压缩形式并加方括号，例如 `[::1]:8765`。端口范围为 1–65535，不能写前导零。不接受域名、通配符、网段、URL、路径或用户信息，不能直接照抄示例地址。沙箱重建后内部 IP 可能改变，需重新核对并更新 `proxy_hosts`，然后重启服务。

2026-10-28 新实例的内部地址为 `172.16.2.173:8765`，恢复配置需使用此精确值；后续换代仍须重新检查。通过平台为新实例重新生成签名入口，再将其 HTTPS origin 写入私有配置的 `web.public_origin`，不能直接替换旧链接中的沙箱 ID。实际签名域名不提交到 Git。

此配置只增加明确允许的 Host 值；`public_origin` 始终保留浏览器的 HTTPS origin，所有 POST 仍必须携带与其完全一致的 Origin。匿名会话 Cookie 和 CSRF 校验始终保留；应用不会依据 `X-Forwarded-Host` 或 `Forwarded` 放宽校验。直接访问内部 HTTP 地址无法获得正常的安全 Cookie。签名链接的鉴权与过期仍由入口网关执行。

平台需完成入口端口映射、HTTPS 和签名链接生成。成员从自己的浏览器打开有效链接即可聊天；验收需确认自动获得匿名 Cookie、消息发送以及两个独立浏览器的数据隔离。模型连通性验证会使用服务端额度，离线验证应切换为 Demo，并将已有 `[routing].enabled`、`[decision].enabled` 都改为 `false`。

验收应检查聊天首页和应用 API。平台可能自行响应 `/healthz`，不能只凭该路径返回 200 判断 joyagent 已经就绪。

### 模型连接排查

如果出现 `Unexpected token '<' ... is not valid JSON`，说明浏览器把 HTML 响应当作 JSON 解析，不能仅凭这条信息认定模型故障或任务未执行。应核对请求的 HTTP 状态、入口响应和会话历史。网页使用一次 `POST /api/chat` 等待整轮结果，不轮询最终结果，也不设置额外的 10 秒聊天请求超时；原有整轮预算仍生效。失败后不自动重放消息或工具。HTML 和无效 JSON 使用固定提示并保留 HTTP 状态，不向页面展示原始响应正文。

为兼容升级前已打开的页面，服务端仍保留 `/api/chat/start` 与 `/api/chat/result`；刷新后的页面使用同步等待方式。兼容接口的异步任务由原有 Agent 运行时执行，沿用模型、工具权限及整轮预算；同一访客同一会话只允许一轮在途任务。提交与查询仍要求原有 Cookie、Origin/Host 和 CSRF 校验；其他访客无法查询该任务。相同访客、会话、本轮 ID 和请求内容在缓存保留期间只执行一次，内容冲突返回 409。完成结果最多保留 10 分钟，并受 256 条缓存上限约束；运行中的任务不被缓存淘汰。永久删除对话时同时清除该会话的结果缓存。缓存不跨应用重启持久化，任务记录仍以 SQLite 历史为准，刷新页面不会自动重发。

网页能打开不代表沙箱能连接模型。共享聊天会使用固定提示区分域名解析失败、安全连接失败、超时、凭据问题、额度不足及上游不可用；不会把异常原文、内部路径、密钥或上游响应正文返回给成员。未识别的错误仍显示“本轮未完成，请稍后重试或联系服务管理员。”。

外部 Agent 的本地模型请求通道满载与上游 HTTP 429 分别提示“本地模型请求通道繁忙”和“模型服务请求过于频繁”；调用预算用尽有独立提示。HTTP 401 表示认证失败，普通 HTTP 403 提示管理员检查模型权限、凭据及访问策略；IdeaLab 的 `403 / IRC-001` 是周期请求次数限流，同样显示“模型服务请求过于频繁”，不能据此要求更换密钥。共享模型的上游限额不等于当前会话显示的请求次数，工具调用后的续接也占模型请求次数。

服务日志使用 `Native model request failed: category=… source=…` 固定字段，上游 HTTP 错误额外记录经过整数校验的 `http_status`，来源仅为 `local_concurrency`、`upstream`、`credentials`、`protocol` 或 `budget`，每次模型桥激活只记录首次失败。不记录上游响应正文、URL 或凭据；IdeaLab 限流识别有界读取错误正文，仅解析 16 KiB 以内的 JSON 错误码，无法解析或未知错误码时保留 HTTP 分类。旧版本把 401 和 403 统一归为 `auth`，仅凭旧日志无法还原具体状态或确认密钥失效。Web 的“本轮未完成”是总状态，具体原因查看回复，聊天模型请求次数包含失败尝试，不包含 Decision 分类或决策工具自身的请求；不会自动重放已有工具操作。

IdeaLab 的兼容 `/api/openai/v1/models` 接口在 2026-11-04 的带凭据和匿名检查中返回相同列表，不能用于确认 AK 是否有效、是否具备指定模型权限或是否还有调用额度。需通过平台的接入管理和调用日志核对；工具访问其他网站返回 200 也不代表模型续接成功。触发周期限流后，应等待下一周期，或由管理员按实际用量评估调整该 AK 的上游限额；应用不自动切换密钥或绕过限流。

出现域名解析提示时，先在运行服务的沙箱中检查配置的模型域名是否能通过系统 DNS 解析，再核对平台的出站规则。只检查 DNS 或模型站点的未认证 HTTPS 连接不会生成模型回答；不要用真实聊天请求替代离线测试。不要通过硬编码 IP、关闭 TLS 校验或临时代理绕过平台网络限制。

2026-10-23 排查旧沙箱 `1dea2a5d89c2427eaf8b4f151950687b` 时，`api.deepseek.com` 和 `www.deepseek.com` 均无法通过系统 DNS 解析，`api.openai.com` 可以解析。在平台出站策略中明确放行 `api.deepseek.com` 后，经两个已配置 DNS 地址查询仍得到 NXDOMAIN。[OpenSandbox 的 DNS 代理](https://open-sandbox.ai/architecture/network/egress)会拦截 53 端口请求，并对拒绝的域名返回 NXDOMAIN；因此这不能证明两个真实上游都返回了该结果，也不能单凭当前动态规则排除平台更高优先级策略。

该 DeepSeek DNS 问题尚未恢复，排障交接见 [沙箱 DNS 故障记录](sandbox-dns-incident.md)。此前切换千问方案时，只读 `/models` 检查包含 `qwen3.8-flash`、`qwen3.8-max`、`glm-5.2`；模型列表不能单独证明当前密钥的生成权限，也不能代替真实聊天和工具调用验收。2026-10-24 已通过沙箱 Web 服务实际调用一次 `decision-model-preview`，分类、是非判断和评分均成功，详见[验证记录](verification.md)。千问聊天模型的真实生成仍未验收；`doctor` 只检查本地配置和凭据来源。

2026-10-24 的沙箱配置将聊天改回 IdeaLab，千问仅用于路由分类和独立决策工具；Flash 与 Max 都走 IdeaLab，两条自动路由已通过真实请求验证。IdeaLab 的 DeepSeek Flash 不直接访问 `api.deepseek.com`，当时失败原因是 IdeaLab 返回模型未授权的 HTTP 403，因此从当时的默认选项移除，未绕过授权或尝试其他别名。该记录不代表当前 Token Plan 配置的生成权限已验收，沙箱内部 HTTP 验证也不代表浏览器端到端验收。

更新签名链接时，若 origin 发生变化，必须同步修改 `web.public_origin` 并重启应用。旧主机名的匿名 Cookie 不会迁移，新主机名将建立新身份；即使旧数据完整保留，也不会自动关联到新浏览器身份。2026-11-04 的签名更新只替换此配置项并重启服务，未更换沙箱或删除原用户数据；新入口的首页、Origin 与 CSRF 校验已验证，未调用付费模型 API。

## 当前沙箱的进程运维

以下为本次恢复采用的部署位置，真实凭据和链接均不放入版本目录。`server-state` 位于持久化 NAS；目录必须属于服务用户且权限为 `700`，私有文件为 `600`，并继续遵守各数据目录的符号链接限制：

| 内容 | 沙箱路径 |
| --- | --- |
| 当前应用版本入口 | `/home/admin/workspace/joyagent/current`，指向具体 release 的符号链接 |
| 配置、匿名会话签名密钥、用户数据 | `/home/admin/workspace/joyagent/server-state` |
| 聊天与决策共用的私有凭据 | `/home/admin/workspace/joyagent/server-state/bailian-token-plan-credentials.toml` |
| 匿名会话签名目录 | `/home/admin/workspace/joyagent/server-state/sessions` |
| 当前服务配置 | `/home/admin/workspace/joyagent/server-state/agent.shared.toml` |
| 离线验证配置（按需创建） | `/home/admin/workspace/joyagent/server-state/agent.verify.toml`，使用独立 Demo 数据 |
| 进程管理状态与日志 | `/home/admin/workspace/joyagent/server-state/service` |

使用 Python 3.11+ 在沙箱执行。`ops/shared_service.py` 适用于支持 `/proc` 和 pidfd 的 Linux，启动后脱离终端，退出 SSH 不会结束服务；子进程异常退出后以 1–30 秒退避重启。它不负责开机自启、沙箱续期或 HTTPS 入口配置。NAS 保留程序和状态文件不会在新实例中自动启动进程，实例替换后仍需核对依赖、入口配置并启动服务。

启动和查询：

```bash
python3 /home/admin/workspace/joyagent/current/ops/shared_service.py start \
  --app-dir /home/admin/workspace/joyagent/current \
  --config /home/admin/workspace/joyagent/server-state/agent.shared.toml \
  --state-dir /home/admin/workspace/joyagent/server-state/service \
  --host 0.0.0.0 --port 8765

python3 /home/admin/workspace/joyagent/current/ops/shared_service.py status \
  --state-dir /home/admin/workspace/joyagent/server-state/service
```

停止：

```bash
python3 /home/admin/workspace/joyagent/current/ops/shared_service.py stop \
  --state-dir /home/admin/workspace/joyagent/server-state/service
```

日志位于 `service/service.log`，仅服务用户可读写。`status` 只报告进程管理器是否运行，不代替浏览器入口或模型连通性检查。

发布新版本时，必须先通过**当前仍在运行的旧版本**执行上述 `stop`，确认停止，再切换 `current` 符号链接，最后通过新版本执行 `start`。管理器的进程身份包含脚本的真实 release 路径；先切换链接再执行 `stop`，新版本不会把旧管理器认作自己启动的进程。配置及数据位于版本目录外，不随 release 切换。

旧页面仍可能使用异步兼容接口，因此没有在途 HTTP 连接不代表任务已经结束。安排停服前应等待运行中的任务完成；服务停止会取消尚未结束的任务并清理 Agent，结果缓存不跨重启保留，也不会自动重放。不能再仅凭端口没有 `ESTABLISHED` 连接判断可无损发布。

## 成员如何使用

已安装并启用 Codex 的共享服务使用 `[backends] default = "codex"` 作为默认 Agent。新会话和未手动指定 Agent 的会话默认选择 Codex；浏览器已保存的手动选择继续保留。macOS 本机 Web 的外部 Agent 执行器尚不受支持，不应将未安装或不可运行的 Codex 设置为本机默认。

顶部“对话”用于现有聊天，“AI 资讯”展示 AIhot 热榜前 10 条，再按发布时间倒序展示 AI 工具集、AI Agent Store 的资讯，合计最多 100 条；“源码仓库”按用途分类展示 AI 项目及开发资源，Claude、Kimi、智谱的官方仓库合并到对应分类中。源码目录随静态页面发布，不增加后台请求；旁边的“论文”提供 14 个研究方向的合集入口，仅 Agents 智能体展示 20 篇精选论文，各页签切换保留正在执行的对话。管理员在私有配置中增加以下设置并重启 Web，即可启用服务端定时抓取：

```toml
[news]
enabled = true
interval_seconds = 7200
```

每个 Web 服务共用一个抓取任务和 `agent.data_dir/news.sqlite3` 缓存。普通资讯每次与历史合并，再按发布时间倒序选取；当天不足时依次用前一天、更早的资讯补足。热榜按官方排名置顶并随最新榜单替换，最多占 10 条，普通资讯补足剩余位置；合计最多 100 条，按已知原文链接去重，超出的旧资讯从缓存中删除。例如热榜 10 条、今天普通资讯 80 条时，再保留昨天最新的 10 条。页面隐藏来源名称，保留标题、链接域名、日期和热榜排名；热榜标题使用官方接口提供的原文链接，已缓存的阅读页链接在启动时迁移；没有有效原文链接的条目不展示，不猜测地址或回退到聚合站。榜单更新时间不冒充文章发布日期。

AIhot 抓取地址固定为官方匿名只读接口 `https://aihot.news/api/v1/hot-topics`，无需密钥，进程内通过 ETag 避免重复下载未变榜单；部署环境需允许访问该 HTTPS 地址。关闭网页不会停止更新；Web 停服期间不抓取，重启按到期时间更新，不补发错过的多次抓取。升级新增来源时会立即安排一轮抓取。页面“刷新列表”只读取服务端缓存，来源异常时继续展示上次成功结果和失败提示。此任务不调用模型，且不读写访客会话。

“论文”独立配置，不与资讯共用条数限制。管理员在同一私有配置中添加：

```toml
[papers]
enabled = true
interval_seconds = 7200
```

论文页展示 14 个方向的社区合集链接及整理者，仅 Agents 智能体在页面列出 20 篇论文。服务优先读取 `https://huggingface.co/api/collections/Tempo14/agents-65c69e9e1d61ffef8641acd0` 的 paper 条目，再从 `https://huggingface.co/api/papers/{arxiv_id}` 获取所选论文摘要。合集超过 20 篇按发表时间截取；不足时从 `https://huggingface.co/api/daily_papers?sort=publishedAt&limit=100` 按收录时间补选标题含独立单词 agent、agents 或 agentic 的论文，并去重。规则无需模型分类，补选条目标明来源；其他 13 类不做服务端抓取。网络仅需这些 HF HTTPS 只读接口，无需密钥或额外依赖。标题直达 arXiv，另有 PDF、HF 讨论链接；服务不下载这些链接或执行论文内容，页面只显示论文的真实发表日期。

论文使用独立的 `agent.data_dir/papers.sqlite3`，首次启用或由原通用 100 篇列表升级时立即抓取，旧通用列表不再展示。之后沿用持久化调度，每两小时更新；成功后整体替换 20 篇，不滚入旧论文；接口异常、详情不匹配或无法凑满 20 篇时保留上次完整缓存。`GET /api/papers` 与资讯接口使用相同的会话、CSRF 和同源检查，只读取缓存，拒绝查询参数和刷新写请求，不创建访客运行时或调用模型。未配置 `[papers]` 的旧实例默认关闭；变更配置需重启 Web。

可选离线中文摘要：保持标题原文，只翻译摘要，并保留英文摘要的展开入口。该功能使用 [Argos Translate 的英译中 1.9 模型](https://github.com/argosopentech/argospm-index)（由 OPUS-MT 导出，模型包 README 标注原模型 CC-BY 4.0，作者 Jörg Tiedemann、Santhosh Thottingal），配合 CTranslate2 CPU 推理。机器翻译可能误译术语，不调用 LLM 或在线翻译 API。

依赖需管理员在 Linux 上预装到工作区、数据目录和访客 session 目录之外，不能在普通启动中自动安装。下面使用独立虚拟环境，不改变 JoyAgent 的标准库运行时；已在 Python 3.14 / x86_64、bubblewrap 0.4.0 上验证：

```sh
umask 022
python3 -m venv /opt/joyagent-paper-translator/venv
/opt/joyagent-paper-translator/venv/bin/python -m pip install --only-binary=:all: --index-url https://pypi.org/simple ctranslate2==4.8.4 sentencepiece==0.2.2
curl --fail --location https://data.argosopentech.com/argospm/v1/translate-en_zh-1_9.argosmodel -o /tmp/translate-en_zh-1_9.argosmodel
printf '%s\n' '433e7c4f034d87fbe2353161e05f18646d7999452f801a4e1f0378522b9850ab  /tmp/translate-en_zh-1_9.argosmodel' | sha256sum --check
# 仅在上一步校验通过后解压；保留模型包 README 的作者与许可证说明。
mkdir -p /opt/joyagent-paper-translator/package
python3 -m zipfile -e /tmp/translate-en_zh-1_9.argosmodel /opt/joyagent-paper-translator/package
chmod -R go-w /opt/joyagent-paper-translator
```

确认已安装 `/usr/bin/bwrap` 和 `/usr/bin/prlimit`，在已有 `[papers]` 节加入 `translation_runtime = "/opt/joyagent-paper-translator"` 后重启 Web。当前沙箱已安装在 `/home/admin/workspace/joyagent/paper-translator/argos-en-zh-v1`。运行库必须由当前服务账号或 root 管理，模型、分词器和解码代码变化会使旧翻译缓存失效。

翻译通过现有隔离执行器强制禁用网络，仅挂载只读系统库、翻译运行库、固定 worker 和临时空目录；不继承 API Key。当前每轮最多翻译 20 篇、2 个 CPU 计算线程、1 GiB 地址空间、600 秒超时，停止服务时终止并回收进程，不回退到宿主运行。首次启用也会翻译已缓存摘要，无需等待下一次抓取；每篇结果立即保存到 `papers.sqlite3`，重启复用，按当前 20 篇精选论文裁剪。摘要未变时更新或浏览器刷新不重新翻译，标题变化也不触发翻译。失败保留原文和已完成结果，页面显示进度与重试状态；PDF 内容不翻译。


1. 打开管理员提供的有效 HTTPS 签名链接。链接过期时联系管理员更新。
2. 在页头开启“自动路由”，由任务复杂度选择模型；关闭后恢复上次手动模型，可在“本轮模型”中修改。开关与模型按浏览器保存，刷新后保留，不影响其他用户。当前沙箱默认关闭，简单任务使用 DeepSeek Flash，复杂任务使用 Qwen Max。无需填写用户名、密码、邀请码或模型 API Key；当前会话正在回复或已归档时不能切换。回复和历史中可查看本轮路由结果。管理员设为 `routing.enabled = false` 时，页面开关禁用。
3. 在侧栏点击“新建会话”开始另一项任务，也可从活动列表切回旧会话；未手动命名时，标题取自第一条消息。一个会话处理中仍可新建、切换并在其他会话发送消息，结果和草稿各自保留，后台完成不会跳走当前会话。列表提示处理状态。执行期间也可在当前输入框点击“补充指令”：JoyAgent 下一安全步骤采用；外部 Agent 和显式工具在当前轮成功结束后自动续接，不中断命令或撤销已执行操作。失败或预算用尽会标记未应用，不自动重试。点击归档将旧会话移入“已归档”，消息和工具记录仍保留，恢复后可继续聊天。执行中的会话不能重复启动任务、归档或删除，其他会话可继续操作。归档状态存储在服务端，刷新页面仍有效。
4. 同一浏览器可继续读取自己的历史，不同会话共享该浏览器身份的长期记忆与工作区。换浏览器、清除 Cookie 或匿名会话到期后获得新身份，旧历史不会自动恢复。共用电脑请使用独立浏览器配置或临时窗口。
5. 在聊天输入框描述任务，由 Agent 调用获准的工具。也可输入 `/tools` 查看工具，或用 `/tool command_run {"command":"pwd; ls -la"}` 显式调用；显式执行不调用额外聊天模型，仍记录历史并执行权限、超时校验。工具可包含命令、Python/Bash 脚本、已启用的 MCP 或 `decision_evaluate`，以管理员配置为准；决策工具本身会发出真实模型请求。页面不再提供独立的工具执行表单，执行结果保存在会话中。
6. 管理员启用外部 Agent 后，可以选择 Codex、pi 或 DeepSeek Harness。第一次发送才启动对应进程，仅改选不会启动。同一访客、同一工作区、每种 Agent 共用一个进程；不同 Web 会话拥有独立原生会话 ID 和上下文，同一 Agent 串行处理请求。正常回复后保留进程，切回来补入缺失的对话和工具结果。处理中改选只影响下一轮；同一会话的历史与工作区继续保留。安装、权限和兼容边界见 [Agent 安装与切换](agent-backends.md)。

`/api/login`、`/api/register` 和 `/api/logout` 已移除。旧 `/login` 书签只会跳转到聊天首页。

升级后会自动为已有消息补齐会话元数据，旧 `default` 等会话无需手工导入。会话创建、归档和恢复使用相同的匿名身份、Origin 和 CSRF 校验，只作用于当前访客的数据库；归档不会清除 SQLite 消息或 JSONL 审计历史。

会话下方的“重命名”支持 1–80 个字符的单行名称，首尾空格自动去除。活动、归档及正在执行的会话均可重命名，名称保存在当前访客的 SQLite 中；刷新页面、重启服务和后续聊天都保留手动名称。该操作不改变会话编号、消息、草稿或 Agent 执行状态，不调用模型。旧数据库首次打开时自动增加内部自定义标题标记，无需手动迁移。

归档保留原生 Agent 会话，但进程和原生会话缓存仍受容量回收规则约束。活动及归档会话旁均有“删除”按钮，确认后先卸载并清理各 Agent 中对应的原生会话，再永久删除消息、工具记录、标题及对应审计副本，不能恢复。正常删除保留共享进程和其他会话的原生上下文；无法确认单会话清理完成时，会关闭该 Agent 的共享进程并清空其缓存，其他会话下次使用时重建。删除当前会话后打开其他活动会话，若无则新建空白会话。执行、排队或正在清理的会话暂不能再次删除；清理未完成时页面会提示重试。清理过程中也不能发送消息或归档。收起列表后仍可点击“会话列表”重新展开。

删除接口为 `POST /api/conversations/delete`，请求体必须为 `{"session_id":"会话编号","confirm":true}`，同样要求当前浏览器的 Cookie、CSRF token 和精确 Origin。只删除该访客自己的会话；未知编号返回 404，执行中返回 409，清理未完成返回 503，重试已删除的编号会继续完成清理。已删除会话的历史、发送消息、归档和恢复请求返回 410，不能通过旧标签页重新创建。

执行中补充接口为 `POST /api/steer`，请求包含 `session_id`、`turn_id`、`request_id`、`message`；`turn_id` 来自该浏览器发起 `/api/chat` 时生成的本轮编号。复用 Cookie、Origin、CSRF 校验，仅作用于当前访客已有的运行任务，不创建运行时、不另占聊天并发额度。重复的 `request_id` 与相同内容在本轮内幂等；编号不匹配或任务已结束返回 409，队列达到 8 条或累计字符上限返回 429。补充不会改变已选择的模型和 Agent，也不会重置本轮预算。服务异常终止前尚未消费的内存队列不保证恢复；正常结束时已应用和未应用内容都会落入会话历史。

## 隔离、容量与数据维护

服务不建立账号数据库；匿名运行数据按随机访客 ID 分开：

```text
shared-data/users/<user-id>/
  data/
    memory.sqlite3
    events.jsonl
    native_sessions/      # 外部 Agent 按需创建的私有临时状态
    ...
  workspace/
```

同名会话在不同浏览器身份之间隔离；一个身份的不同会话共享自己的长期记忆。模型配置、SOUL、policy 和技能指南由管理员统一管理，用户文件工具不能修改这些文件，也不能访问其他访客的 workspace 或状态目录。模型的私有续接字段会持久化以支持多步工具调用，但不会在 Web 历史接口中作为聊天内容返回。

单个进程全局最多同时处理 **8** 轮请求，每浏览器身份默认最多 **4** 个不同会话；同会话重复提交或超出容量立即返回繁忙，请等待后重试。等待同一种外部 Agent 的请求也占用额度，排队时间计入整轮超时。同访客的多个在途请求分别持有运行时使用计数，最后一个结束前不会被淘汰。最多缓存 **32** 个运行时，仅淘汰空闲实例，持久化数据不会因此删除。请只运行一个共享 Web 应用进程；目前没有跨进程的全局限流和运行时协调。

三个外部 Agent 共用常驻进程容量：同一访客、工作区、每种 Agent 最多一个进程，整个 Web 事件循环默认最多 **8** 个。管理员可在已有 `[backends]` 表中设置 `resident_limit = 8`，允许范围为 **1–32**。进程没有固定闲置过期时间；达到上限时只回收最久未使用的空闲进程，全部繁忙时拒绝新启动。每个进程最多缓存 **64** 个原生会话，满额时卸载最久未使用的原生会话，以后从 SQLite 历史重建；这不是 Web 会话总量限制，不会删除用户聊天记录。访客运行时淘汰和服务关闭会清理其原生进程与临时状态。

常驻进程每轮使用当前手选或自动路由选定的模型，整轮工具调用保持该模型。服务端只在当前轮次激活模型转发权限，空闲时拒绝模型请求；真实凭据不会进入子进程。失败、超时或取消会关闭该 Agent 的共享进程并清理缓存，该访客使用此 Agent 的各 Web 会话下次使用时从受预算约束的 SQLite 历史重建，不自动重试上轮操作。

Codex 使用服务生成的私有 `CODEX_HOME` 保存持久 thread，以支持删除指定线程，不使用 `ephemeral`；pi 使用私有 `--session-dir` 保存和切换独立会话，不使用 `--no-session`。这些文件位于访客的 `data_dir/native_sessions/` 内，属于可丢弃缓存，服务关闭时清理，服务重启后不保证保留原生 ID；不会接入个人或平台 Codex 的会话目录。Web 会话消息及工作区文件仍由服务端持久保存。

SQLite 是权威存储，JSONL 为可恢复的审计镜像。`memory_forget` 只删除当前检索记录，旧内容仍可能存在于聊天记录与审计历史中。停服后备份整个 `session_dir` 和 `data_dir`，并另行备份 owner 配置与私有凭据。丢失或更换签名密钥会使现有 Cookie 失效；Cookie 到期不等同于删除服务器历史和审计记录。

NAS 持久化不能替代独立备份：它用于在实例替换后保留已写入挂载目录的文件，不能找回此前只保存在旧实例本地的目录，也不能撤销误删或损坏。备份应放在独立受控位置，并实际验证配置、签名密钥、SQLite/JSONL 和工作区文件的恢复。

会话永久删除先完成指定原生会话的卸载与缓存清理，必要时关闭对应 Agent 进程以确保清理完成，再在一个 SQLite 事务中清除消息、会话元数据及对应审计 outbox，随后原子重建 `events.jsonl`。原生清理仍未完成时保留 SQLite 历史并返回可重试错误；镜像清理失败也不会报告删除成功，可重试或由后续启动恢复。数据库只保留无正文的会话编号和删除时间，阻止该编号被旧客户端重新使用。此操作不删除跨会话共享的长期记忆、工作区文件、已有备份或模型服务商记录，也不承诺擦除磁盘介质上的历史页；长期记忆检索删除仍遵循上段说明。

从旧账号版本迁移时，将 `auth_dir`、`invitation_file` 替换为 `session_dir`，停服并清理旧 `users.sqlite3` 及对应 WAL/SHM、已有账号库备份和邀请码文件。旧匿名身份不会根据用户名、请求参数或旧账号 Cookie 自动绑定历史账号；已有聊天数据仍保存在原目录，供 owner 管理，不向新的匿名访客公开。清理账号数据库不表示删除了聊天或审计历史。

共享服务适合受邀团队试用。当前并发限制不等同于每日 token 或费用配额；管理员应根据模型额度、磁盘容量和平台沙箱生命周期决定服务规模。

等待时的“处理进度”默认折叠，最多保留最近 5 条固定格式的执行状态。`GET /api/chat/progress?session_id=...&turn_id=...` 沿用访客会话与 CSRF 校验，按访客、会话和轮次隔离；不接收身份选择参数，不返回提示词、推理正文、工具参数、凭据或原始模型输出。仅当前可见会话每 1.2 秒读取一次，读取超时不取消或重放聊天请求；本轮结束后停止。服务端最多缓存 256 轮、每访客 16 轮，10 分钟后过期；永久删除会话时也清理进度缓存。
