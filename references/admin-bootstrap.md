# 管理员与服务器初始化

本模式供灼见管理员 AI 使用。目标是把 Alphabet 提供的一台 ECS 初始化为可重复运行多个模块系统的环境。每台 ECS 只初始化一次；业务负责人和业务 AI 不需要 GitHub、Coolify、阿里云控制台或手工配置 Nginx。

## 输入与边界

- 允许输入：服务器公网地址与 root 账号密码，或已登录的云控制台会话；后续平台登记再需要 Alphabet 组织 UUID、域名后缀和灼见管理员会话。收到服务器地址与 root 凭证后，AI 按 [ECS 首次接入](ecs-first-access.md) 自动登录，不把连接排障交给小白。默认本地文件存储不要求 OSS；管理员可在现在或以后明确选择绑定 Alphabet OSS。
- 密码、SSH 密钥、模块接入密钥和 ECS 登记凭证不得进入本地 Git、环境档案、日志或回复。
- 默认保留服务器全部既有容器、虚拟主机、数据库和数据目录。新资源使用 `zhuojian-<enterprise>-<application>` 标识。
- 管理员 AI 只建立运行底座、域名规则和登记入口，不替业务 AI 编写业务流程。
- 测试阶段可按用户明确授权使用 root；正式环境优先建立只允许调用受控部署命令的 `zhuojian-deploy` 用户，不要求业务用户理解该账号。

## 一次性初始化流程

1. 只读记录实例区域、公网/内网地址、系统版本、CPU、内存、Swap、磁盘、Git、Docker、Nginx、监听端口、容器、数据目录、现有域名和备份状态。
2. 确认通配 DNS `*.<企业域名后缀>` 解析到该 ECS。一个模块系统一个子域名；多个系统共享 ECS 时使用独立容器、回环端口和数据目录。
3. 按 [Alphabet 文件存储与 OSS 迁移](object-storage.md) 初始化默认本地文件存储：建立固定数据目录、模块隔离、磁盘阈值和数据库加文件的一致性备份。只有管理员明确要求时才创建或绑定同地域私有 Bucket 并部署文件网关。
4. 安装或核验 Git、Docker、Docker Compose、Nginx 和 HTTPS 证书工具。不得安装 GitHub CLI、GitHub App、Coolify Agent 或 Coolify Server 作为本流程依赖。
5. 只公开 80/443；数据库、Redis、文件网关和模块内部端口只绑定 Docker 网络或 `127.0.0.1`。SSH 沿用管理员批准的来源范围。
6. 建立固定目录并限制权限：

   ```text
   /srv/zhuojian/repositories/   ECS 本地 Git 仓库
   /srv/zhuojian/deployments/    容器与版本记录
   /srv/zhuojian/data/           模块持久数据
   /srv/zhuojian/backups/        数据和配置备份
   /etc/zhuojian/                环境档案与 Secret 引用
   /etc/nginx/conf.d/             每个模块的 Host 路由
   ```

7. 安装受控直接部署入口。它只能在以上目录内创建/更新指定 `applicationSlug`，分配回环端口、建立固定文件目录、构建不可变镜像、生成 Nginx 虚拟主机、检查 HTTPS/健康和回滚本次发布；OSS 模式才创建或复用存储项目身份。不得运行全局 Docker prune、删除未知卷或重启无关服务。
8. 管理员在灼见为该企业签发一枚 **ECS Runtime 登记凭证**。优先运行 `scripts/provision_runtime.py`，让它调用平台接口并分别写入凭证和环境档案；不得复制到命令参数、终端回显或回复。凭证文件固定为 `/etc/zhuojian/runtime-registration.key`，权限为目录 `0700`、文件 `0600`。它只允许把该域名后缀下、该组织的健康模块登记/重新同步到灼见，不允许部署代码、管理服务器、授予权限或访问其他企业。
9. 生成不含密钥的 `/etc/zhuojian/runtime.json`，然后用两个最小测试应用验证域名隔离、HTTPS、`/health`、Manifest、登记链路、本地上传/下载、目录隔离、磁盘阈值和重建容器后读取。OSS 模式再验证真实对象读写及跨系统前缀拒绝。测试资源使用独立名称和数据目录，不碰已有项目。

## 环境档案

```json
{
  "schemaVersion": 2,
  "enterpriseKey": "aifabei",
  "organizationId": "<灼见组织UUID>",
  "environment": "staging",
  "runtimeId": "aifabei-hk-01",
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
    "publicPorts": [80, 443],
    "privateServicePortsOnly": true
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
    "ZHUOJIAN_INTEGRATION_SECRET",
    "SESSION_SECRET"
  ],
  "verifiedAt": "<RFC3339>"
}
```

业务 AI 只读取这份非敏感档案，选择尚未占用的 `applicationSlug`，在固定目录开发和发布。本地模式不包含 OSS 凭证；以后切换 OSS 时，`credentialRef` 指向的文件只由管理员和网关读取。不得要求负责人登录阿里云、GitHub、Coolify或手工编辑 Nginx。

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
  --runtime-key aifabei-hk-01 \
  --enterprise-key aifabei \
  --environment staging \
  --domain-suffix aifabei.staging.zhuojianai.com \
  --public-address <ECS公网IP> \
  --storage-mode local \
  --storage-verified
```

管理员以后切换 OSS 时使用 `--storage-mode oss`，并额外传入 `--storage-bucket`、`--storage-region` 和 `--storage-gateway-url`。不得让业务负责人运行这条命令。

平台管理员可用以下接口查看、停用或轮换，业务 AI 不得调用：

```text
GET   /api/v1/ecs-publisher/organizations/{organizationId}/runtimes
PATCH /api/v1/ecs-publisher/organizations/{organizationId}/runtimes/{runtimeId}
POST  /api/v1/ecs-publisher/organizations/{organizationId}/runtimes/{runtimeId}/rotate-credential
```

轮换后旧凭证立即失效。新凭证仍只显示一次，必须先原子写入临时 `0600` 文件，再替换正式文件；轮换不应改变模块域名、本地 Git、数据目录或接入密钥。

## 资源预检

- 区分“能运行镜像”和“能在本机从源码构建”。低于 2 GiB 且没有受控 Swap 时标记 `sourceBuild=false`，停止直接源码发布并报告需要管理员扩容或提供企业内部镜像构建位置；业务 AI 不得临时创建 Swap。
- 小规格服务器 `maxConcurrentBuilds` 固定为 `1`。测试、Playwright和 Schema 校验在开发目录执行，不进入生产镜像。
- 磁盘必须同时容纳当前镜像、下一镜像、本地 Git 和备份。余量不足时停止发布，只能清理本次可确认的构建缓存，禁止全局 prune 或删除未知卷。
- Dockerfile 的 `/health` 检查必须使用镜像实际具备的运行时命令，不得假设存在 `curl` 或 `wget`。
- 本地模式下磁盘还要容纳附件和导出文件。默认使用率达到 80%告警；达到 90%或剩余不足 5 GiB 时停止新上传和发布，但保持既有文件可读。不得自动删除未知文件。
- 对象存储也不替代 ECS 数据盘。数据库、Docker、本地 Git、构建缓存和临时处理始终需要磁盘余量。

## 验收与回滚

- 验收：通配 DNS、HTTPS、两个 Host 不串站、Docker 健康、Nginx 配置、数据库端口不公网暴露、ECS 登记凭证只能登记本企业且不会自动授权；本地文件目录固定挂载、权限隔离、磁盘阈值与一致性备份有效。OSS 模式追加检查 Bucket 私有且同地域、文件网关健康和跨系统对象拒绝。
- 记录新增 DNS record ID、安全组 rule ID、Nginx 文件、容器、数据目录和证书域名。
- 回滚只删除本次新增且带精确标识的测试容器、Nginx 文件和空测试目录；不删除已有 Git 仓库、业务数据或未知卷。
- 本地 Git 和业务数据与 ECS 同盘时必须配置 ECS 快照或企业指定的异地备份；GitHub 不作为必需备份目标。
