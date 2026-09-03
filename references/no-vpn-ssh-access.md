# 无 VPN 的 SSH 访问底座

本页只在管理员首次初始化、修复业务 AI 登录路径，或业务 AI 的 SSH `22` 在密码校验前失败时读取。目标不是让小白排查网络，而是让管理员预先把服务器做成“只有公网地址、root 账号和密码也能由 Codex 登录”。

## 结论与边界

默认方案是让 `sslh` 独占公网 TCP `443`，把 SSH 转发到回环 `22`，把 TLS/HTTPS 转发到回环 `8443` 的 Nginx：

```text
外部 Codex ── SSH → 公网IP:443 ─┐
                                ├─ sslh ── SSH → 127.0.0.1:22
浏览器/灼见 ─ HTTPS → 域名:443 ─┘       └─ TLS → 127.0.0.1:8443 (Nginx)
```

这不是 VPN，也不产生第二套账号或令牌。Codex 仍使用标准 SSH 和用户本次提供的 root 密码；网站继续使用标准 HTTPS。它解决的是客户端网络允许普通 TCP `443`、但限制 SSH `22` 的常见情况。

任何软件都不能靠密码绕过完全不存在的网络路由。如果业务网络连目标服务器的普通 TCP `443` 也不可达，本方案不成立。管理员必须选择业务网络可达的服务器地域，或先部署一个服务器主动连出的企业管理中继。平台没有已经部署并通过验收的中继时，不得在 Skill 中假装存在，也不得让小白安装 VPN。

## 管理员初始化

1. 在变更前创建云快照或等价恢复点，记录 Nginx、SSH、监听端口和防火墙现状。确认 `80/443` 已放行，`sshd` 在回环或本机 `22` 可用，现有 HTTPS 健康。
2. 安装发行版提供的 `sslh`。先检查实际二进制、systemd unit 和配置路径；不同发行版可能使用 `sslh`、`sslh-select` 或 `sslh-fork`，不得照抄不存在的路径。
3. 从 `nginx -T` 找出所有生效的公网 `listen 443 ...`。逐个备份原文件，将 TLS 监听改到 `127.0.0.1:8443`；同一虚拟主机的重复 IPv6 `443` 监听也必须处理，不能留下 Nginx 与 `sslh` 争抢公网 `443`。保留公网 `80`，用于 HTTP 跳转和 ACME HTTP-01。
4. 先运行 `nginx -t`，再重载 Nginx并确认 `127.0.0.1:8443` 提供原证书和虚拟主机。此时才启动 `sslh`：公网监听 `0.0.0.0:443`，SSH 目标为 `127.0.0.1:22`，TLS 目标为 `127.0.0.1:8443`。超时目标必须是 SSH，因为标准 SSH 客户端会先等待服务端 Banner。
5. 将 `sslh` 纳入 systemd 自动启动，并明确排在网络、SSH 和 Nginx 之后。不要把数据库、Redis、Docker 回环端口或文件目录暴露到公网。
6. 证书续期改用不会把 Nginx 重新改回公网 `443` 的流程，例如保留公网 `80` 的 HTTP-01 webroot；每次证书或 Nginx 变更后重新验证两条协议。

已有 `/etc/zhuojian/runtime.json` 的服务器不得重新签发 Runtime 凭证。完成外部验收后，只原子补充 `capabilities.passwordSshAccess=true` 和 `network.managementAccess`，保留原 `runtimeId`、组织、域名、存储配置与 Secret 引用。新服务器则在 `provision_runtime.py` 中传入 `--management-access-mode ssh-https-multiplex --management-access-verified`。

实现时以目标系统上 `sslh --help`、systemd unit 和 `nginx -T` 为准。典型参数语义如下，不能在未检查路径与现有监听时直接执行：

```text
sslh --foreground \
  --listen 0.0.0.0:443 \
  --ssh 127.0.0.1:22 \
  --tls 127.0.0.1:8443
```

## 硬验收

管理员控制台内的 `curl localhost`、Workbench 和命令助手都不算业务路径验收。管理员必须退出云控制台，在外部 Codex 中执行：

```text
python <skill>/scripts/validate_ssh_access.py \
  --host <ECS公网地址> \
  --ports 22,443 \
  --require-port 443 \
  --json
```

通过后再运行标准 SSH：

```text
ssh -p 443 root@<ECS公网地址>
```

只有出现密码提示、使用本次提供的密码成功执行只读命令，并且以下检查同时通过，才能在 Runtime 档案写入 `verified: true`：

- `443` 返回 SSH Banner；
- 同一公网 `443` 上的模块 HTTPS 证书、`/health` 和 Host 路由正常；
- 重启服务器后两条协议仍正常；
- 业务 AI 不需要云控制台、VPN、RAM、SSH 密钥或额外令牌。

本机预先存在代理时可以用于诊断，但不能用它代替“无 VPN”验收。探测在密码提示前超时属于网络路径失败；只有服务端明确返回 `Permission denied` 才能判断账号或密码失败。

## 回滚

任何一步失败时保持 SSH `22` 和当前业务容器不变：停止本次新增的 `sslh` unit，恢复本次备份的 Nginx 配置，运行 `nginx -t` 后重载，并再次验证原 HTTPS。只回滚本次明确修改的配置；不得重置系统、删除未知虚拟主机或清理业务数据。
