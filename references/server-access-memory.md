# 服务器长期访问记忆

## 对负责人的承诺

负责人针对一台新服务器首次提供 `IP + root + 密码`，即视为明确授权当前 Codex 环境长期登录该服务器处理本 Skill 范围内的建设、维护和部署。此后无论刷新、换任务还是隔天继续，都先复用本机访问档案，禁止再次向负责人索要 root 账号或密码。

长期访问记忆由“服务器连接档案 + 该服务器专用 SSH 密钥 + OpenSSH 已确认的主机身份”组成，不依赖把初始密码保存到文件。档案只保存在当前 Codex 环境，不进入 Skill、业务仓库、Git、日志或回复。

## 每次登录

先查已有档案：

```text
python <skill>/scripts/server_access_memory.py resolve --host <服务器IP> --user root
```

- 返回 `SERVER_ACCESS_READY`：直接运行 `login`，不得再尝试密码登录或询问负责人。
- 返回 `SERVER_ACCESS_MISSING`：只有当前对话里已经出现该新服务器的初始密码时，才执行首次建立流程。
- 负责人以前提供过、但当前环境没有可用档案且对话中也没有原密码：停止并说明“这台服务器的长期访问需要企业管理员恢复”，不得向小白负责人重新索要密码、云账号、控制台或密钥。

已有档案的登录入口：

```text
python <skill>/scripts/server_access_memory.py login --host <服务器IP> --user root
```

脚本固定使用档案中的端口、路由和专用密钥，并禁止回退到密码认证。主机身份变化、密钥被撤销或网络不可达时按实际层级报错；不得把连接失败说成密码错误。

## 首次建立

首次只由 AI 完成，负责人除了已经给出的 `IP + root + 密码` 不再操作：

1. 按 [SSH、VPN 与代理访问](ssh-access.md) 探测 `22/443`，选择真正返回 SSH Banner 的端口和路由。
2. 运行 `prepare` 生成该服务器专用的 Ed25519 密钥；输出中的公钥可以安装到服务器，私钥只留在本机访问目录：

   ```text
   python <skill>/scripts/server_access_memory.py prepare --host <服务器IP> --user root
   ```

3. 启动带 PTY 的首次 SSH，只在真实密码提示中提交负责人本次已经给出的密码。在服务器上将 `prepare` 返回的完整公钥去重追加到 `/root/.ssh/authorized_keys`，并确保目录权限为 `0700`、文件权限为 `0600`。密码不得进入命令参数、脚本、临时文件、环境变量、Git、日志或回复。
4. 退出密码会话，通过专用密钥发起一次全新的批处理登录并执行 `true`；只有成功后才写入访问档案：

   ```text
   python <skill>/scripts/server_access_memory.py verify \
     --host <服务器IP> --user root --port <已验证端口> \
     [--proxy-url http://127.0.0.1:7897] [--requires-vpn]
   ```

5. 再运行一次 `resolve`，必须得到 `SERVER_ACCESS_READY`。从这一刻起不再使用或索要初始密码。

公钥安装必须去重，不能清空或覆盖服务器已有的 `authorized_keys`。不得修改其他用户、关闭密码登录、改变 SSH 监听端口或替管理员做额外安全策略调整。

## 失效与恢复

- 网络或 VPN 不通：按网络问题处理，只请负责人确认日常 VPN 已开启，不询问密码。
- 主机密钥变化：停止登录并交给企业管理员核对服务器是否重装，不自动接受新身份。
- 专用密钥丢失、权限损坏或被服务器撤销：交给企业管理员恢复长期访问；仍不向小白负责人索要账号密码。
- 用户明确要求撤销长期访问时，企业管理员从服务器删除对应公钥，并删除当前 Codex 环境中的该主机访问档案和专用私钥。
