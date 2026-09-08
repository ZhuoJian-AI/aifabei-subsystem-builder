# 管理员与服务器初始化

本模式供灼见管理员 AI 使用。目标是把 Alphabet 提供的一台 ECS 初始化为可重复运行多个模块系统的环境。每台 ECS 只初始化一次；业务负责人和业务 AI 不需要 GitHub、Coolify、阿里云控制台或手工配置 Nginx。

## 输入与边界

- 允许输入：新服务器首次提供一次公网地址与 root 账号密码，或已登录的云控制台会话；后续平台登记再需要 Alphabet 组织 UUID、域名后缀和灼见管理员会话。收到服务器地址与 root 凭证后，AI 按 [服务器长期访问记忆](server-access-memory.md) 建立可复用登录，再按 [ECS 首次接入](ecs-first-access.md) 和 [SSH、VPN 与代理访问](ssh-access.md) 验证业务电脑实际使用的 VPN/代理路径。以后不得再次向业务负责人索要账号密码。标准 SSH `22` 已通过时不必改造 Nginx 或安装 `sslh`；只有 `22` 受限且确有需要时才配置 SSH/HTTPS `443` 复用。管理员可在现在或以后一次性绑定 Alphabet 企业 OSS；业务 AI 永远不需要阿里云账号、Bucket、RAM、AccessKey 或逐项目令牌。
- 密码、SSH 密钥、模块项目凭证和 ECS 登记凭证不得进入本地 Git、环境档案、日志或回复。
- 默认保留服务器全部既有容器、虚拟主机、数据库和数据目录。新资源使用 `zhuojian-<enterprise>-<application>` 标识。
- 管理员 AI 只建立运行底座、域名规则和登记入口，不替业务 AI 编写业务流程。

## 一次性初始化流程

1. 只读记录实例区域、公网/内网地址、系统版本、CPU、内存、Swap、磁盘、Git、Docker、Nginx、监听端口、容器、数据目录、现有域名和备份状态。
2. 确认通配 DNS `*.<企业域名后缀>` 解析到该 ECS。一个模块系统一个子域名；多个系统共享 ECS 时使用独立容器、回环端口和数据目录。
3. 按 [Alphabet 文件存储与 OSS 迁移](object-storage.md) 确定文件策略。Runtime 首次登记固定使用 `local-managed`；后续主机工具会建立数据目录、模块隔离、磁盘阈值和数据库加文件的一致性备份。管理员可以先准备同地域私有 Bucket 和 RAM 网关身份，但只有 Runtime 与主机工具安装完成后，才安装文件网关并通过真实验收切换默认模式。
4. 安装或核验 Git、Docker、Docker Compose、Nginx 和 HTTPS 证书工具。不得安装 GitHub CLI、GitHub App、Coolify Agent 或 Coolify Server 作为本流程依赖。
5. Web 只公开 80/443；管理连接优先使用业务 VPN 可达的标准 SSH `22`。只有 `22` 在业务路径上不可用时，才按 [SSH、VPN 与代理访问](ssh-access.md) 让公网 `443` 同时承载 HTTPS 和 SSH，并把 Nginx TLS 后端改为回环 `8443`。数据库、Redis、文件网关和模块内部端口只绑定 Docker 网络或 `127.0.0.1`。
6. 建立固定目录并限制权限：

   ```text
   /srv/zhuojian/repositories/   ECS 本地 Git 仓库
   /srv/zhuojian/deployments/    容器与版本记录
   /srv/zhuojian/data/           模块持久数据
   /srv/zhuojian/backups/        数据和配置备份
   /etc/zhuojian/                环境档案与 Secret 引用
   /etc/nginx/conf.d/             每个模块的 Host 路由
   ```

7. 在签发任何 Runtime 凭证前，先从业务实际使用的外部 Codex（允许启用 VPN/代理）验证 SSH Banner，并在真实 PTY 密码提示中完成一次 root 登录。只有已验证的连接路径才能写进 Runtime 档案；不能把云控制台登录误记为业务 AI 已可连接。
8. 管理员在灼见为该企业签发一枚 **ECS Runtime 登记凭证**。运行 `scripts/provision_runtime.py`：脚本先检查目标目录、目标文件、所有权和权限，再对固定数据目录执行真实写入、读取、删除和磁盘余量探针；全部通过后才调用平台接口，并分别原子写入 `/etc/zhuojian/runtime-registration.key` 与不含密钥的 `/etc/zhuojian/runtime.json`。不得把凭证复制到命令参数、终端回显或回复。凭证文件权限固定为目录 `0700`、文件 `0600`。它只允许把该域名后缀下、该组织的健康模块登记/重新同步到灼见，并触发平台固定的“系统研发者”托管授权和既有角色有界继承；不允许自选角色、提升权限、部署代码、管理服务器或访问其他企业。
9. Runtime 档案和登记凭证已经落盘后，把 `assets/admin-runtime/host/` 传到 ECS 并以 root 在该目录运行 `sh ./install.sh`，安装 `zhuojian-runtime` 受控直接部署入口。标准 SSH `22` 已验证时不得传 443 参数；只有管理员已另外安装并实测 SSH/HTTPS 复用后，才运行 `sh ./install.sh --enable-ssh-https-multiplex --public-address <ECS公网地址>`。入口只能在固定目录内创建/更新指定 `applicationSlug`，分配回环端口、建立固定文件目录、构建不可变镜像、生成 Nginx 虚拟主机、检查 HTTPS/健康和回滚本次发布；不得运行全局 Docker prune、删除未知卷或重启无关服务。
10. 管理员选择 OSS 时，再按 [Alphabet 文件存储与 OSS 迁移](object-storage.md) 安装 `assets/admin-runtime/gateway/`，写入企业级 root-only 凭证，并运行 `zhuojian-runtime configure-oss-gateway`。只有匿名读取拒绝、`apps/*` 外的列举/读/写/删全部拒绝、真实 PUT/GET/DELETE、双应用隔离和临时身份撤销全部通过后，尚未初始化的新系统默认存储才切换成 `oss-gateway`；已有 release 不迁移。
11. 用两个独立的最小测试应用验证域名隔离、HTTPS、`/health`、Manifest、登记链路、本地上传/下载、目录隔离、磁盘阈值和重建容器后读取。OSS 模式必须额外完成网关重启复验、新应用自动分配独立网络/前缀/身份，以及另一应用和匿名请求均无法读取。测试资源使用独立名称和数据目录，不碰已有项目。

## 环境档案

```json
{
  "schemaVersion": 2,
  "enterpriseKey": "alphabet",
  "organizationId": "<灼见组织UUID>",
  "environment": "staging",
  "runtimeId": "alphabet-hk-01",
  "deployment": {
    "provider": "direct-ecs",
    "repositoriesRoot": "/srv/zhuojian/repositories",
    "deploymentsRoot": "/srv/zhuojian/deployments",
    "dataRoot": "/srv/zhuojian/data",
    "backupsRoot": "/srv/zhuojian/backups",
    "nginxConfigRoot": "/etc/nginx/conf.d",
    "registrationCredentialRef": "/etc/zhuojian/runtime-registration.key"
  },
  "domains": {
    "suffix": "aifabei.staging.zhuojianai.com",
    "wildcardDnsVerified": true,
    "httpsRequired": true
  },
  "capabilities": {
    "localGit": true,
    "docker": true,
    "compose": true,
    "nginx": true,
    "persistentData": true,
    "fileStorage": true,
    "objectStorage": false,
    "sourceBuild": true,
    "maxConcurrentBuilds": 1
  },
  "resources": {
    "memoryMiB": 4096,
    "swapMiB": 0,
    "diskFreeGiB": 20,
    "appPortRange": [18000, 18999]
  },
  "network": {
    "publicPorts": [22, 80, 443],
    "privateServicePortsOnly": true,
    "managementAccess": {
      "mode": "standard-ssh",
      "host": "<ECS公网IP或域名>",
      "connectionOrder": [22],
      "businessAiPort": 22,
      "requiresVpn": true,
      "requiresCloudConsole": false,
      "verified": true
    }
  },
  "fileStorage": {
    "provider": "local-disk",
    "mode": "local-managed",
    "root": "/srv/zhuojian/data",
    "pathTemplate": "{applicationSlug}/files",
    "warningUsedPercent": 80,
    "stopUploadUsedPercent": 90,
    "minimumFreeGiB": 5,
    "verified": true
  },
  "platform": {
    "baseUrl": "https://ai-platform.staging.zhuojianai.com",
    "registrationEnabled": true
  },
  "secretRefs": [
    "/etc/zhuojian/runtime-registration.key",
    "/etc/zhuojian/apps/{applicationSlug}.env",
    "SESSION_SECRET"
  ],
  "verifiedAt": "<RFC3339>"
}
```

业务 AI 只读取这份非敏感档案，选择尚未占用的 `applicationSlug`，在固定目录开发和发布。本地模式不包含 OSS 凭证；切换 OSS 后，业务 AI 只运行 `zhuojian-runtime ensure-app/deploy`，不申请、填写、复制或输出 `credentialRef`、RAM AccessKey 或项目令牌。由于日常连接当前使用 root，这是一条工作流约束而不是对 root 的技术权限承诺；真正需要主机级秘密隔离时必须另设受限部署账号。不得要求负责人登录阿里云、GitHub、Coolify或手工编辑 Nginx。

## SaaS Runtime 接口

管理员的一次性签发使用：

```text
POST /api/v1/ecs-publisher/organizations/{organizationId}/runtimes
Authorization: Bearer <平台管理员会话 Token>
```

请求字段为 `runtime_key`、`enterprise_key`、`environment`、`domain_suffix` 和可选 `public_address`。响应包含 `runtime`、只出现一次的 `credential` 与不含密钥的 `runtime_profile`。调用前必须确认凭证和档案目标文件不存在；不得覆盖旧文件后重新签发造成正在运行的发布链路失效。

推荐命令（Token 只放临时环境变量）：

```text
python <skill>/scripts/provision_runtime.py \
  --organization-id <组织UUID> \
  --runtime-key alphabet-hk-01 \
  --enterprise-key alphabet \
  --environment staging \
  --domain-suffix aifabei.staging.zhuojianai.com \
  --public-address <ECS公网IP> \
  --management-access-mode standard-ssh \
  --management-access-requires-vpn \
  --management-access-verified \
  --storage-mode local
```

`provision_runtime.py` 只负责第一次 Runtime 登记，并且只接受 `--storage-mode local`。它会自己完成真实本地存储探针；历史兼容参数 `--storage-verified` 不会跳过或代替探针。绝不能用人工布尔值直接声明本地目录或 OSS 可用。

若平台请求已经发出但客户端未收到确定响应，不得盲目重试创建。先用同一个管理员会话调用 Runtime 列表接口，按 `runtime_key`、组织和环境查找：没有记录才能重试创建；已有记录时使用平台的轮换接口取得一次新凭证并安全替换本机文件，不得再创建第二个 Runtime。若平台调用成功但本机原子写入失败，先保留错误现场、修复目标目录权限，再按列表查询与轮换流程恢复，禁止把可能已签发的凭证写入日志。

若 `/etc/zhuojian/runtime.json` 已存在，绝对不要再次运行该脚本或重新签发登记凭证。管理员按 [Alphabet 文件存储与 OSS 迁移](object-storage.md) 安装企业网关后，只执行一次原地升级：

```text
zhuojian-runtime configure-oss-gateway \
  --bucket <企业私有Bucket> \
  --region <阿里云地域ID>
```

该命令会先检查网关、秘密文件和 Docker 私网，再运行匿名读取拒绝、`apps/*` 外列举/读/写/删拒绝、真实对象读写删除、两个临时应用隔离和临时身份撤销探针。任一项失败时保持原 Runtime 档案和既有应用不变；通过后才把 `oss-gateway` 设置成尚未初始化的新系统的默认模式。Runtime 已纳管的 `local-managed`/`oss-gateway` release 继续使用自己记录的后端；使用既有签名上传等不同结构、尚未由 Runtime 纳管的容器只保持原样，完成专项导入前不得用普通 `ensure-app/deploy` 接管。

平台管理员可用以下接口查看、停用或轮换，业务 AI 不得调用：

```text
GET   /api/v1/ecs-publisher/organizations/{organizationId}/runtimes
PATCH /api/v1/ecs-publisher/organizations/{organizationId}/runtimes/{runtimeId}
POST  /api/v1/ecs-publisher/organizations/{organizationId}/runtimes/{runtimeId}/rotate-credential
```

轮换后旧的 ECS 登记凭证立即失效。新值只显示一次，必须先原子写入临时 `0600` 文件，再替换正式文件；这项操作不改变模块域名、本地 Git、数据目录或各系统项目凭证。

## 资源预检

- 区分“能运行镜像”和“能在本机从源码构建”。低于 2 GiB 且没有受控 Swap 时标记 `sourceBuild=false`，停止直接源码发布并报告需要管理员扩容或提供企业内部镜像构建位置；业务 AI 不得临时创建 Swap。
- 小规格服务器 `maxConcurrentBuilds` 固定为 `1`。测试、Playwright和 Schema 校验在开发目录执行，不进入生产镜像。
- 磁盘必须同时容纳当前镜像、下一镜像、本地 Git 和备份。余量不足时停止发布，只能清理本次可确认的构建缓存，禁止全局 prune 或删除未知卷。
- Dockerfile 的 `/health` 检查必须使用镜像实际具备的运行时命令，不得假设存在 `curl` 或 `wget`。
- 本地模式下磁盘还要容纳附件和导出文件。默认使用率达到 80%告警；达到 90%或剩余不足 5 GiB 时停止新上传和发布，但保持既有文件可读。不得自动删除未知文件。
- 对象存储也不替代 ECS 数据盘。数据库、Docker、本地 Git、构建缓存和临时处理始终需要磁盘余量。

## 验收与回滚

- 验收：外部 Codex 通过业务实际使用的网络路径（允许 VPN/代理）在标准 `22` 或已配置的 `443` 读取 SSH Banner，并通过交互式 root 密码登录；同时 HTTPS、通配 DNS、两个 Host 不串站、Docker 健康、Nginx 配置、数据库端口不公网暴露、ECS 登记凭证只能登记本企业，且除固定的“系统研发者”托管授权和既有角色有界继承外不能修改权限；本地文件目录固定挂载、权限隔离、真实读写删除、磁盘阈值与一致性备份有效。OSS 模式追加检查 Bucket 私有且同地域、匿名读取被拒绝、RAM 对 `apps/*` 外的列举/读/写/删全部拒绝、网关不暴露公网端口、真实 PUT/GET/DELETE、双应用隔离、临时身份撤销、重启后复验和新应用自动 `ensure-app`。每个新 OSS 应用只和网关共享自己的 Docker 网络，不和其他应用共享存储网络。
- 记录新增 DNS record ID、安全组 rule ID、Nginx 文件、容器、数据目录和证书域名。
- 回滚只删除本次新增且带精确标识的测试容器、Nginx 文件和空测试目录；不删除已有 Git 仓库、业务数据或未知卷。
- 本地 Git 和业务数据与 ECS 同盘时必须配置 ECS 快照或企业指定的异地备份；GitHub 不作为必需备份目标。
