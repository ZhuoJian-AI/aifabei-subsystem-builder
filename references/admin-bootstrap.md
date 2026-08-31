# 管理员与服务器初始化

本模式供灼见管理员 AI 使用。目标是把企业提供的 ECS 变成可重复部署的模块运行环境，并生成业务 AI 能读取、但不含任何密钥的环境档案。

## 输入与边界

- 允许输入：云控制台会话、ECS 地址、一次性或长期管理员凭据、目标企业、测试域名、Coolify 项目/团队。
- 凭据只能通过受控会话、Secret 管理或交互式隐藏输入使用；环境档案、Git、日志和回复不得出现密码、Token 或私钥。
- 默认保留服务器上全部既有容器、虚拟主机、数据库和数据卷。新资源使用唯一 `zhuojian-<enterprise>-<application>` 标签。
- 管理员 AI 只建立底座和发布入口，不替业务 AI 编写业务流程。
- 远程仓库与发布身份按 [仓库命名与自动发布](repository-publishing.md) 配置；业务用户和业务 AI 不需要 GitHub 账号。

## 初始化流程

1. 只读记录实例区域、公网/内网地址、系统版本、CPU、内存、Swap、磁盘、Docker、监听端口、反向代理、容器、网络、数据卷和现有域名。
2. 确认目标域名真实解析到该 ECS。一个系统一个域名；多个系统可共享 ECS，但必须使用独立容器、回环端口、网络和数据卷。
3. 只公开 80/443；数据库、Redis 和内部 API 不发布到宿主机。SSH 沿用已有管理策略，不为方便测试扩大公网范围。
4. 将 ECS 加入管理员指定的 Coolify Team。首次可使用用户授权的 root 会话；自动部署应切换为 Coolify 专用部署密钥。
5. 建立 HTTPS 和 Host 路由，验证两个不同域名不会进入同一容器。不得用本机 hosts 文件冒充 DNS 完成。
6. 在灼见为该企业登记 Coolify 部署档案：`server_uuid`、`project_uuid`、Environment、`github_app_uuid` 和通配域名后缀。验证中央 Backend 的 Coolify Token 可操作该目标；Token 本身不写入企业档案或 ECS。
7. 生成 `zhuojian-environment.json`，只写非敏感能力和标识；密钥由灼见中央发布服务直接写入 Coolify Secret，档案只引用名字。闭环见 [Coolify 发布闭环](deployment-closed-loop.md)。

管理员 AI 已取得短时灼见管理员 JWT 后，不要求用户手填接口，直接运行：

```text
python <skill>/scripts/configure_deployment_profile.py --organization-id <组织UUID> --runtime-key hk-01 --default-runtime --server-uuid <Coolify Server UUID> --project-uuid <Coolify Project UUID> --github-app-uuid <Coolify GitHub Source UUID> --domain-suffix aifabei.staging.zhuojianai.com
```

JWT 只通过临时环境变量 `ZHUOJIAN_ADMIN_TOKEN` 传入，命令成功后立即从进程环境清除；不得写入环境档案。
同一企业有多台 ECS 时为每台登记不同 `runtime-key`；环境档案用 `ZHUOJIAN_RUNTIME_KEY` 指定该开发环境，未指定时使用 `--default-runtime` 标记的目标。已有模块更新时固定沿用最初 Runtime，发布命令不能顺手搬服务器。

### 资源预检与小规格服务器

- 把“可以运行已构建镜像”和“可以在本机从源码构建镜像”分开判断。交付业务 AI 前必须记录可用内存、Swap、磁盘余量和允许的并发构建数。
- 本机源码构建的推荐基线是至少 2 GiB 内存；低于 2 GiB 且没有 Swap 时，环境档案必须标记 `sourceBuild=false`，不得让业务 AI 直接触发 Python/Node 依赖构建。
- 1 GiB 轻量服务器只允许以下二选一：由管理员先配置受控 Swap 并验证一次串行构建，或使用中央流水线/镜像仓库构建后仅在目标服务器拉取运行。Swap 属于服务器底座，不得由业务 AI 临时创建。
- 小规格服务器的 `maxConcurrentBuilds` 固定为 `1`；测试、Playwright、Schema 校验器等在开发机或流水线执行，不进入生产镜像构建。
- 构建期间若 SSH、HTTPS、命令助手同时无响应，管理员按基础设施故障处理：停止继续下发部署，保留卷，恢复实例后检查 OOM、构建进程和容器状态。不得进入业务目录手工修改代码来掩盖底座问题。
- 磁盘必须同时容纳当前镜像、下一版镜像和构建缓存；余量不足时停止发布并清理本次可确认的构建缓存，禁止全局 prune 或删除未知卷。

### Coolify 健康检查约束

- 模块 Dockerfile 必须定义不依赖额外系统包的 `HEALTHCHECK`，推荐使用项目运行时自带的 Python/Node 请求 `/health`。
- Dockerfile 已有健康检查时，Coolify Application 设置 `health_check_enabled=false`，保留镜像检查；不要让 Coolify 用镜像里不存在的 `curl` 或 `wget` 覆盖它。
- 如果明确启用 Coolify 健康检查，镜像必须实际包含它调用的命令，并在冷启动验收中从最终镜像内执行一次。
- `requirements.txt`/生产镜像只放运行时依赖；pytest、Playwright、Schema 校验器等写入独立的开发依赖文件，不得把浏览器测试栈安装进小规格 ECS 的生产容器。

## 环境档案

```json
{
  "schemaVersion": 1,
  "enterpriseKey": "aifabei",
  "environment": "staging",
  "runtimeId": "aifabei-hk-01",
  "deployment": {
    "provider": "coolify",
    "controlPlane": "zhuojian-central",
    "serverId": "<opaque-id>",
    "projectId": "<opaque-id>",
    "environment": "production",
    "runtimeKey": "aifabei-hk-01",
    "githubSourceId": "<opaque-id>",
    "profileConfigured": true
  },
  "domains": {"suffix": "aifabei.staging.zhuojianai.com", "httpsRequired": true},
  "capabilities": {
    "docker": true,
    "compose": true,
    "persistentVolumes": true,
    "sourceBuild": true,
    "maxConcurrentBuilds": 1
  },
  "resources": {"memoryMiB": 4096, "swapMiB": 0, "diskFreeGiB": 20},
  "network": {"publicPorts": [80, 443], "privateServicePortsOnly": true},
  "sourceControl": {
    "provider": "github",
    "owner": "ZhuoJian-AI",
    "visibility": "private",
    "repositoryPattern": "{companySlug}-{moduleSlug}",
    "publisher": "zhuojian-central",
    "publisherBaseUrl": "https://ai-platform.staging.zhuojianai.com",
    "publisherCredentialRef": "/etc/zhuojian/publisher.key"
  },
  "secretRefs": ["ZHUOJIAN_INTEGRATION_SECRET", "/etc/zhuojian/publisher.key", "SESSION_SECRET"],
  "platformBindings": ["ZHUOJIAN_ORGANIZATION_ID"],
  "verifiedAt": "<RFC3339>"
}
```

业务 AI 读取档案后只选择 `applicationSlug`、容器名称和持久卷，不需要重新登录阿里云或理解 DNS、安全组和 Coolify。

## 验收与回滚

- 验收：DNS、HTTPS、`/health`、Host 隔离、Coolify部署记录、容器健康和数据库端口不公网暴露。
- 记录新增 DNS record ID、安全组 rule ID、Coolify resource ID、容器/卷标签和证书域名。
- 回滚只删除本次新增且带精确标签的 DNS、规则、Coolify资源和空测试卷；不运行 Docker 全局 prune，不删除已有卷。
