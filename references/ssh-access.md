# SSH、VPN 与代理访问

本页供管理员首次初始化、修复业务 AI 登录路径，或业务 AI 在密码校验前无法连接时读取。目标是让负责人针对新服务器只提供一次公网地址、root 账号和密码；AI 建立 [服务器长期访问记忆](server-access-memory.md) 后永久复用，VPN、代理、端口选择和 SSH 命令都由 AI 处理。

## 一眼判断

- 透明 VPN 已启用：像普通网络一样先连接 SSH `22`，无需特殊命令。
- 当前 Codex 只有本机 HTTP 代理：Banner 探测使用 `validate_ssh_access.py --proxy-url http://<本机地址>:<端口>`，交互式 SSH 运行本 Skill 的 `ssh_via_http_proxy.py`；它会封装 `ProxyCommand` 和 Windows/macOS/Linux 的路径引号。
- `22` 不通但服务器已经配置 SSH/HTTPS `443` 复用：改连 `443`，账号和密码不变。
- 直连和当前 VPN/代理都没有收到 SSH Banner：这是网络路径问题。只请负责人确认 VPN 已启用；不要反复提交密码，也不要把问题说成 root 或密码失效。

只有服务端明确返回 `Permission denied`，或云端登录页面明确提示密码错误，才能归类为凭证失败。密码只在交互式 SSH 提示中输入，不进入命令参数、脚本、环境变量、Git、日志或回复。

## 业务 AI 自动连接

先运行 `server_access_memory.py resolve`。已有访问档案时直接运行 `login`，不得再次要求密码；只有新服务器没有档案时才继续下列首次连接。先遵守当前 Codex 的网络规则。用户、服务器档案或已知地域表明必须使用 VPN/代理时，第一轮就走规定路径；否则才先用直接网络探测 `22` 和已配置的 `443`：

```text
python "<skill>/scripts/validate_ssh_access.py" --host <ECS公网地址> --ports 22,443 --json
```

直连失败且当前环境规定使用无认证 HTTP 代理时，AI 自行重试：

```text
python "<skill>/scripts/validate_ssh_access.py" --host <ECS公网地址> --ports 22,443 --proxy-url http://127.0.0.1:7897 --json
```

代理地址只是示例；优先使用当前 Codex 的环境规则或已知本机代理，不扫描任意端口。脚本拒绝在命令行携带代理用户名或密码。

探测返回 `selectedPort` 后，透明 VPN 使用标准 SSH。首次见到主机时自动记录新主机密钥；若已记录的密钥后来发生变化则停止，并交给管理员核对服务器身份：

```text
ssh -o StrictHostKeyChecking=accept-new -o PasswordAuthentication=yes -o KbdInteractiveAuthentication=yes -o PreferredAuthentications=password,keyboard-interactive -o PubkeyAuthentication=no -o NumberOfPasswordPrompts=1 -p <selectedPort> root@<ECS公网地址>
```

本机 HTTP 代理使用以下封装脚本，避免业务 AI 自己拼接含空格或中文路径的 `ProxyCommand`：

```text
python "<skill>/scripts/ssh_via_http_proxy.py" --proxy-url http://127.0.0.1:7897 --host <ECS公网地址> --port <selectedPort>
```

上述首次登录入口显式禁用公钥尝试并优先密码/键盘交互，避免本机 SSH 配置或大量 Agent 密钥在密码提示前耗尽认证次数。启动带 PTY 的 SSH，等真正出现密码提示后，再通过标准输入提交当前对话已提供的初始密码。Banner 探测不发送账号或密码；密码不得进入命令参数、脚本、临时文件、环境变量、Git、日志或回复。登录后立即按 [服务器长期访问记忆](server-access-memory.md) 建立专用密钥，后续 `login` 固定使用该密钥并禁止回退到密码认证。

本轮登录实测需要代理、但 Runtime 档案仍写 `requiresVpn=false` 时，继续复用已经成功的代理路径完成工作，并在管理员回执中标记档案需要修正；不得因档案过时而重新退回失败的直连路径。

## 管理员一次性验收

管理员只需从业务电脑实际使用的外部 Codex 路径完成一次 Banner 探测和真实 root 登录：

1. VPN 下标准 SSH `22` 成功时，保留现有 Nginx/HTTPS，不安装 `sslh`。
2. `22` 被业务网络限制、而普通 TCP `443` 可达时，才选择下面的 `443` 复用方案。
3. Runtime 档案写入实际成功的 `mode`、`connectionOrder` 和 `businessAiPort`；依赖 VPN/受管代理时写入 `requiresVpn=true`，不依赖时写入 `false`。
4. `verified=true` 只表示已从真实业务路径读取 SSH Banner 并用 root 密码登录成功，不要求关闭 VPN。

`provision_runtime.py` 按 Alphabet 当前前提默认写入 `requiresVpn=true`；只有管理员已经完成不依赖 VPN 的外部实测时，才传 `--no-management-access-requires-vpn`。

业务负责人不需要阿里云账号、RAM、Workbench、SSH 密钥或额外令牌。长期访问失效时由管理员恢复，不能再次向负责人索要账号密码。若验证失败，管理员可以使用自己的云控制台修复服务器端监听或防火墙，但不能把控制台当作业务 AI 的日常连接方式。

## 可选的 SSH/HTTPS `443` 复用

只有管理员确认需要时，才让 `sslh` 独占公网 TCP `443`，把 SSH 转发到回环 `22`，把 TLS/HTTPS 转发到回环 `8443` 的 Nginx：

```text
外部 Codex ── SSH → 公网IP:443 ─┐
                                ├─ sslh ── SSH → 127.0.0.1:22
浏览器/灼见 ─ HTTPS → 域名:443 ─┘       └─ TLS → 127.0.0.1:8443 (Nginx)
```

它不产生第二套账号或令牌。实现步骤：

1. 先创建云快照或等价恢复点，记录 Nginx、SSH、监听端口、防火墙和 `sslh` 原有启用/运行状态。确认现有 HTTPS 与本机 `sshd` 健康。
2. 安装发行版提供的 `sslh`，检查真实二进制、systemd unit 和配置路径。
3. 从 `nginx -T` 找出所有公网 `listen 443`，逐个备份后改为 `127.0.0.1:8443`；同时处理重复 IPv6 监听，保留公网 `80` 供跳转和 ACME HTTP-01。
4. 运行 `nginx -t`，重载并确认回环 `8443` 提供原证书和 Host 路由，再启动公网 `0.0.0.0:443` 的 `sslh`。SSH 目标为 `127.0.0.1:22`，TLS 目标为 `127.0.0.1:8443`，超时目标设为 SSH。
5. 将 `sslh` 纳入 systemd 自动启动并排在网络、SSH、Nginx 之后；每次 Nginx 或证书变更后复验 SSH 和 HTTPS。
6. 从业务实际 VPN/代理路径运行 `validate_ssh_access.py --require-port 443`，再交互式登录并验证同一 `443` 上的 HTTPS、证书、`/health` 和 Host 路由。

已有 `/etc/zhuojian/runtime.json` 的服务器不得重新签发 Runtime 凭证。只原子更新 `network.managementAccess` 和 `capabilities.passwordSshAccess=true`，保留原 `runtimeId`、组织、域名、存储配置与 Secret 引用。

## 回滚

`443` 复用的任一步失败时，先停止本次 watchdog/timer，再停止新增的 `sslh`，恢复其变更前的启用与运行状态和本次备份的 Nginx 配置；运行 `nginx -t` 后重载并验证原 HTTPS。只回滚本次明确修改的配置，不重置系统、不删除未知虚拟主机或业务数据。
