# 部署到阿里云 ECS

使用 Linux ECS、公网 IP 或域名、Caddy 和 systemd，将 JoyAgent 部署为常驻 HTTPS 服务。浏览器访问 Caddy，Caddy 转发到 `127.0.0.1:8765`；无需将 Python 端口开放到公网。下面先验收完全离线的 Demo，再配置真实模型。**Demo 可用不代表真实模型或外部 Agent 已上线。**

## 准备资源与文件

- ECS 使用带 systemd 的 Linux，`/usr/bin/python3` 须为 Python 3.11+。基础 Web 不需要 Node.js、pip 依赖或前端构建。
- 配置公网 IP/EIP 及 HTTPS 入口；安全组和系统防火墙允许入口使用的 TCP 80/443，SSH 限定管理员来源，不开放 8765。使用域名时，A/AAAA 记录都应指向实际可用的地址；没有域名可采用下文的公网 IP 证书路线。
- 按 [Caddy 官方安装说明](https://caddyserver.com/docs/install)安装 Caddy。公网域名解析到服务器、80/443 可达且证书存储可持久写入后，Caddy 可自动申请和续期证书，见[自动 HTTPS 要求](https://caddyserver.com/docs/automatic-https)。公网 IP 路线使用下文明确核对的版本和配置。域名接入和备案要求按所选阿里云地域办理。
- 将项目源码上传到 `/opt/joyagent`，包括 `joyagent/`、`ops/`、`SOUL.md` 和 `skills/`。源码传输与私有配置、凭据、用户数据迁移分开进行；不要把本机凭据或整个工作目录直接公开上传。

模板在 [`ops/ecs`](../ops/ecs/)：`joyagent.service`、`Caddyfile.example`（域名）、`Caddyfile.ip.example`（公网 IPv4）、`agent.demo.toml`。示例域名 `agent.example.com` 和文档保留 IP `203.0.113.10` 都必须替换，不能实际用于签发。

以下命令用于**新 ECS 首次安装**；已有部署应保留现有配置和数据，不重复覆盖：

```bash
sudo useradd --system --user-group --home-dir /var/lib/joyagent --shell /usr/sbin/nologin joyagent
sudo install -d -o joyagent -g joyagent -m 700 /etc/joyagent /var/lib/joyagent
sudo install -o joyagent -g joyagent -m 600 /opt/joyagent/ops/ecs/agent.demo.toml /etc/joyagent/agent.toml
sudo install -o joyagent -g joyagent -m 600 /opt/joyagent/joyagent/defaults/policy.toml /etc/joyagent/policy.toml
sudo install -m 644 /opt/joyagent/ops/ecs/joyagent.service /etc/systemd/system/joyagent.service
sudo install -m 644 /opt/joyagent/ops/ecs/Caddyfile.example /etc/caddy/Caddyfile
sudoedit /etc/joyagent/agent.toml /etc/caddy/Caddyfile
```

如果 `joyagent` 用户已存在，核实其用途后复用。将两个文件的示例域名改成一致的真实域名：`web.public_origin` 填 `https://实际域名`，不能带路径或末尾 `/`。Caddy 站点名只填域名。配置中的路径已使用绝对路径，避免配置移到 `/etc` 后相对路径指向错误位置。

## 没有域名：直接使用公网 IP

截至 2026-10-31，Let’s Encrypt 已正式支持 IPv4/IPv6 证书，必须选择 `shortlived` profile，证书有效期 **160 小时**，不需要先购买域名。见[正式开放公告](https://letsencrypt.org/2026/01/15/6day-and-ip-general-availability)和[当前 profile 说明](https://letsencrypt.org/docs/profiles/)。本模板使用单个公网 IPv4；JoyAgent 的 `public_origin` 校验已支持 `https://公网IP`。

使用 **[Caddy 2.11.7 官方版本](https://github.com/caddyserver/caddy/releases/tag/v2.11.7)**，安装后运行 `caddy version` 核对。不能假定操作系统默认仓库中的旧版本具备相同 IP 证书能力。只把普通 Caddyfile 的域名替换为 IP 不够：默认 IP 站点可能使用本地 CA，需要显式选择 Let’s Encrypt ACME issuer 与 `profile shortlived`，见 [TLS issuer 配置](https://caddyserver.com/docs/caddyfile/directives/tls#issuers)。

在新安装的启动步骤之前，用 IP 模板替换 Caddy 配置：

```bash
sudo install -m 644 /opt/joyagent/ops/ecs/Caddyfile.ip.example /etc/caddy/Caddyfile
sudoedit /etc/caddy/Caddyfile /etc/joyagent/agent.toml
```

将 Caddyfile 中两处 `203.0.113.10` 改成实际公网 IPv4，并将 `web.public_origin` 改成同一个 `https://公网IPv4`，不带 `:443` 或末尾 `/`，`proxy_hosts` 保持空列表。全局 `default_sni` 让不发送 SNI 的 IP 客户端仍取得正确证书，见[官方参数说明](https://caddyserver.com/docs/caddyfile/options#default-sni)。若 ECS 公网 IP 通过 NAT 映射到私网网卡，不要增加 `bind 公网IP`；Caddy 监听网卡上实际可用的地址，证书和网页 URL 仍使用公网 IP，Python 后端保持回环监听。

首次签发按以下顺序进行，避免反复调试消耗生产签发额度：

1. **Staging 验证网络和挑战。**将 IP 模板中 `issuer acme` 的 `dir` 暂改为 `https://acme-staging-v02.api.letsencrypt.org/directory`；在已有全局 `{ ... }` 块中加入 `storage file_system /var/lib/caddy/ip-staging`，以独立保存测试证书。使用官方包的 `caddy` 服务账号时，先执行 `sudo install -d -o caddy -g caddy -m 700 /var/lib/caddy/ip-staging`；其他安装方式按实际服务账号调整。以该账号运行下文的配置验证，再启动服务，查看 Caddy 日志确认 ACME 验证与签发成功。Staging 证书不被普通浏览器信任，不能据此宣告 HTTPS 上线；不跳过浏览器警告，不使用 `curl -k`，也不将测试 CA 加入日常信任库。见 [Let’s Encrypt staging 说明](https://letsencrypt.org/docs/staging-environment/)与 [Caddy 存储配置](https://caddyserver.com/docs/caddyfile/options#storage)。
2. **Production 验收可信 HTTPS。**将 `dir` 恢复为模板的 `https://acme-v02.api.letsencrypt.org/directory`，移除临时 `storage` 行以使用正式持久存储。再次验证配置，并在首次上线阶段执行 `sudo systemctl restart caddy`，避免继续使用内存中的测试证书。日志确认正式签发成功后，用不跳过证书校验的 `curl --fail --show-error https://公网IPv4/healthz` 和普通公网浏览器验收；核对证书 SAN 包含该 IP、签发链受信任及到期时间，然后检查匿名 Cookie、聊天和 CSRF。
3. **保持自动续期。**Caddy 须常驻运行，证书存储持久可写，能出站访问 ACME 服务；用于验证的公网 TCP 80/443 需持续可达。IP 证书只能使用 HTTP-01 或 TLS-ALPN-01，不能使用 DNS-01，见[官方验证方式](https://letsencrypt.org/2025/07/01/issuing-our-first-ip-address-certificate)。Caddy 自动提前续期，默认剩余寿命约三分之一时尝试，也可能遵循 CA 的 ARI 窗口，见[续期规则](https://caddyserver.com/docs/caddyfile/options#renewal-window-ratio)。监测续期日志和证书到期时间，不采用每月才执行一次的续期任务。

公网 IP 变化后需同步更新 Caddy、`public_origin` 并重新签发，浏览器也会使用新的匿名身份。优先保持分配的公网地址稳定。若目标网络无法完成公开 CA 的验证，先处理端口映射和网络可达性，或改用域名路线；不要以关闭证书校验代替可信 HTTPS。

## 启动与无模型费用验收

```bash
cd /opt/joyagent
sudo -u joyagent /usr/bin/python3 -m joyagent --config /etc/joyagent/agent.toml doctor
sudo systemd-analyze verify /etc/systemd/system/joyagent.service
sudo -u caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
sudo systemctl daemon-reload
sudo systemctl enable --now joyagent
sudo systemctl enable --now caddy
sudo systemctl reload caddy
sudo systemctl status joyagent caddy --no-pager
```

`doctor` 只检查本地配置，不能代替网络、浏览器或隔离执行验收。检查内部服务时仍需传入配置的 Host，例如替换以下示例域名后执行；IP 路线将其替换为实际公网 IPv4，并在完成 Production 签发后执行外部 HTTPS 检查：

```bash
curl --fail --show-error -H 'Host: agent.example.com' http://127.0.0.1:8765/healthz
curl --fail --show-error https://agent.example.com/healthz
sudo journalctl -u joyagent -u caddy -n 100 --no-pager
```

用未连接公司网络的浏览器打开正式 HTTPS 地址，确认首页标识“离线 Demo”，发送“现在几点”，并用两个独立浏览器配置检查会话隔离。Demo 关闭了 Decision、自动路由、资讯抓取、命令执行、MCP 和三个外部 CLI，不调用真实模型或发送 IM/邮件。云主机、带宽等资源费用不属于模型费用。

systemd 直接托管前台 Python，支持重启恢复和开机启动；不要同时运行 `ops/shared_service.py start`。服务账号只需写 `/var/lib/joyagent`；会话目录自动以 700 创建。停服用 `systemctl stop joyagent`，升级前先等待正在执行的任务结束。

## 接入真实模型与外部 Agent

修改 `/etc/joyagent/agent.toml` 中现有的 `[model]`，配置可达的模型网关、准确模型 ID 和独立私有凭据文件，按 [README 模型配置](../README.md)填写。凭据目录权限为 700、文件为 600，归 `joyagent` 所有；不要放到源码、workspace 或网页里。先运行 `doctor`，重启后单独验收真实生成与工具调用；这一步会使用模型额度。

当前仓库的本机默认聊天和 Decision 统一使用百炼 Token Plan `token-plan.cn-beijing.maas.aliyuncs.com`，共用套餐凭据，自动路由关闭。本文共享 ECS 部署需单独配置允许共享的模型服务与凭据，不能复用个人 Token Plan 订阅。若选用 IdeaLab，阿里云 ECS 不自动具备集团网络访问能力，须在目标 ECS 核实 DNS、HTTPS 可达性和账号授权。换网关时同步修改 `model`、`choices` 和自动路由目标；未验证 Decision 前保持 `[decision]`、`[routing]` 关闭。

共享 Web 免登录，匿名 Cookie 用来隔离数据。原沙箱的签名链接门禁不会随代码迁移到 ECS；真实模型服务若只供指定成员使用，需在 HTTPS 入口配置相应访问控制后启用。

需要 Codex、pi 或 DeepSeek Harness 时，按 [Agent 安装与切换](agent-backends.md)安装固定运行库，并启用 Linux `bubblewrap`、`prlimit` 与可用的 user/mount/PID/network namespace。基础 Web 不要求这些依赖。当前运行库固定版本为 Node 24.22.1、Codex 0.142.5、pi 1.3.0、DSH 0.2.1-rc.2。

安装器及锁文件使用集团 npm 镜像 `registry.anpm.alibaba-inc.com`，普通 ECS 未必可达，不能直接假设安装命令成功。可在匹配 ECS 操作系统和 CPU 架构的受控 Linux 环境准备固定运行库后传输，或先适配并验证公网安装源；Mac 的 `node_modules` 不能直接迁到 Linux。运行库应放到如 `/opt/joyagent-agent-runtimes/v1` 的可读目录，配置 `backends.install_dir`。不得用关闭隔离来替代 namespace 修复。

启用外部 Agent 后应在实际 systemd 服务环境内验证隔离和读写权限。`execution.network = "host"` 会开放 ECS 本身可达的网络和回环服务；默认 `"none"` 下外部 Agent 仍能通过受控 relay 请求模型。常驻进程数量与 UID 进程限额需要按机器资源配置，不能直接把旧沙箱参数视为 ECS 容量承诺。

## 迁移历史和备份

等待旧服务任务结束并停服，备份完整 `session_dir`、`data_dir`、用户工作区，以及 owner 配置、SOUL、policy、skills 和私有凭据，再传到受控目标目录。恢复后调整绝对路径和文件归属；不要迁移旧 supervisor 的 PID/锁文件。SQLite 是权威存储，JSONL 是可恢复的审计镜像，应一起保留并验证恢复。

保留 `signing.key` 可使同一访问主机下仍有效的 Cookie 在重启后继续使用；**从沙箱域名换到自有域名或公网 IP 时，浏览器 Cookie 不会跨主机迁移，新身份不会自动关联旧历史**。文件迁移成功不等于历史已在新浏览器可见。需保留原始备份，再另行设计历史身份迁移；不要通过修改客户端用户 ID 暴露其他访客数据。

备份 `/var/lib/joyagent`、`/etc/joyagent` 及 Caddy 的持久证书状态。`memory_forget` 删除当前检索记录，不表示清除 SQLite/JSONL 中的全部历史。更多细节见[共享 Web 部署](deploy-shared-web.md)。
