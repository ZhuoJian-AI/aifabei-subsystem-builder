---
name: aifabei-subsystem-builder
description: "让管理员 AI 初始化爱法贝企业开发环境，让业务 AI 从业务描述新建、修改、部署或接入跨部门子模块，并与灼见 SaaS 原生协议兼容。用户提到爱法贝模块、企业 ECS、跨部门系统、AI CRUD、iframe 或灼见接入时使用。"
---

# 爱法贝企业模块搭建

用户只需要说明业务、参与部门和期望结果。不要要求用户选择框架、写命令、配置 Docker、注册 GitHub 或手工拼接口；这些由 AI 完成，并用业务语言汇报。

## 固定架构

```text
灼见 SaaS（中央控制面）
└─ 爱法贝企业大模块（逻辑聚合）
   └─ 模块系统（独立域名、仓库、数据库、发布单元）
      └─ 子模块（moduleKey，最小授权边界）
         └─ 页面、数据、AI Action、事件
            └─ 一个 owner 部门 + 多个协作/审批/使用部门
```

模块业务数据始终留在模块自己的数据库。灼见只保存登记、Manifest、授权、Action 目录、审计、事件游标和必要索引；禁止直接连接模块数据库。

## 先选择执行模式

根据用户现状自动选择，不把技术判断抛给小白：

1. **管理员初始化**：用户提供新 ECS、云控制台或现有 Coolify 权限。读取 [管理员与服务器初始化](references/admin-bootstrap.md)，建立 Docker、远程部署、域名、HTTPS、安全规则和不含密钥的环境档案；创建远程仓库或发布代码时还必须读取 [仓库命名与自动发布](references/repository-publishing.md)。
2. **原生模块开发**：用户描述新业务。读取 [平台接入协议](references/platform-contract.md)，优先运行 `scripts/scaffold_subsystem.py` 建立标准骨架，再实现业务页面、数据库和 Action。
3. **修改原生模块**：先运行 `scripts/inspect_subsystem.py`，保留数据和现有能力，以兼容方式升级 Manifest 与业务代码。
4. **接入老系统**：只做 iframe、域名白名单和管理员授权；老系统未实现原生 SSO/Action 前，允许用户在 iframe 内额外登录一次，不伪装成已经打通数据。

## 开发前硬门槛

- 读取全局与项目 `AGENTS.md`。
- 新原生模块参考灼见公开源码 `https://github.com/ZhuoJian-AI/ai-platform`，记录参考提交；以本 Skill 的 Schema 和版本化契约为准，不依赖平台私有数据库结构。
- 从空目录开发时先运行 `python <skill>/scripts/scaffold_subsystem.py --help`。
- 已有项目先运行 `python <skill>/scripts/inspect_subsystem.py --path <项目根目录> --json`。
- GitHub 不是业务用户前置条件。业务 AI 只使用本地 Git；首次发布时调用 `scripts/publish_subsystem.py`，由灼见中央发布服务创建私有仓库并签发仅限该仓库、短时有效的推送 Token。GitHub App 私钥不得进入 Skill、企业 ECS、仓库、日志或回复，规则见 [仓库命名与自动发布](references/repository-publishing.md)。

## 原生模块必须满足

- 固定端点：`/health`、Manifest、事件拉取、事件投递、Action 和 SSO。
- Manifest `version` 保持整数 `2`，新增能力用 `contractRevision` 表示；按 `schemas/manifest-v2.schema.json` 输出。
- 每个子模块恰好一个 owner 部门；每个参与部门必须显式声明 `pageKeys` 和 `actionKeys` 作为建议授权上限，页面必须声明 `pageKey`、路由、上下文 Schema 和允许的 Action。平台管理员或企业管理员仍须确认，Manifest 不能自行扩权。
- SSO 会话必须保存灼见签发的 `pageKeys`、`actionKeys` 和 `pageAccess`。子系统只渲染获授权页面，直接访问未授权路由返回 403；页面按钮和 `/api/ui/actions/*` 还必须再次校验当前页面与 Action 都在会话 allowlist 中。只校验模块级 `permissions` 不合格。
- 页面按钮与 AI 调用同一个应用服务函数和权限判断。
- AI 工具必须同时通过用户、企业、应用、子模块、页面、Action 和管理员授权；`aiEnabled=false` 永不暴露给 AI。
- 查询、新增、修改、删除、导出统一走 Action。修改和删除使用 `expectedVersion`；版本冲突返回 HTTP 409。
- 高风险操作声明 `requiresConfirmation=true`，校验灼见确认声明、参数哈希和幂等 `requestId`；拒绝、过期和重复批准不得重复执行。
- iframe Bridge 只发送当前页面和选中实体的摘要，不传 Token、Cookie、密码或整表数据；`postMessage` 的 `targetOrigin` 必须取经 HTTPS 校验的灼见父页面来源，禁止使用 `"*"`。
- 跨系统数据流使用版本化事件；目标系统按 `eventId` 幂等消费，不共享数据库。事件 `sequence` 必须跨容器/数据库重建仍单调不回退，禁止从 1 重新开始的裸自增游标；按 `references/platform-contract.md` 使用 Unix 微秒序列。
- 生产镜像只安装运行时依赖，测试与 Playwright 依赖必须拆到开发依赖文件；Dockerfile 自带 `/health` 检查时，管理员配置 Coolify 保留镜像检查，禁止用不存在的 `curl/wget` 覆盖。

## 部署与登记

公网 ECS 按 [ECS 与内网接入](references/ecs-first-access.md) 执行。每个模块系统一个域名，同一 ECS 可按域名运行多个容器；子模块使用路径和 `moduleKey`，不单独购买服务器或域名。

正式仓库名固定为 `{companySlug}-{moduleSlug}`。爱法贝示例为 `aifabei-sample-review`；公司标识只在仓库名前出现一次，正式名称不得携带版本、日期、环境或 `coldstart`。验收仓库才允许 `{companySlug}-{moduleSlug}-coldstart-v{n}`，且必须标记为测试资源。不要因为升级创建 `-v2` 新仓库，版本使用 Git commit、tag 和 Manifest `contractRevision` 表达。

部署后依次运行：

```text
python <skill>/scripts/validate_source.py --path <模块项目目录>
python <skill>/scripts/validate_endpoint.py --base-url https://<模块域名>
python <skill>/scripts/e2e_acceptance.py --base-url https://<模块域名> --module-key <moduleKey> --page-key <pageKey> --query-action <actionKey>
```

首次发布或后续更新由业务负责人明确说“发布/部署”后运行：

```text
python <skill>/scripts/publish_subsystem.py --path <模块项目目录>
```

脚本读取 `ZHUOJIAN_PLATFORM_URL`，并优先读取环境变量 `ZHUOJIAN_PUBLISH_KEY`；未设置时自动读取管理员一次性安装到 `/etc/zhuojian/publisher.key` 的企业级发布凭证。业务用户不需要登录 GitHub，脚本会自动查找或创建规范仓库并推送当前提交。管理员只在每家公司首次初始化时安装一次凭证，之后该公司的新模块和更新都不再要求管理员参与。缺少公司环境档案或发布凭证时停在“等待管理员首次初始化”，不得向用户索要 GitHub 账号或把 GitHub App 私钥复制到 ECS。

接入密钥只通过环境变量或交互式隐藏输入传递，绝不写入命令参数、仓库、日志或回复。管理员可在灼见“接入模块系统”向导登记；已有安全管理员 Token 时，也可运行 `scripts/register_subsystem.py --help` 自动完成发现、创建和同步。业务 AI 不自行授予部门权限。

## 失败处理

- 模板或说明歧义：修本 Skill、Schema、模板或验证脚本，再从空目录重测。
- 平台鉴权、Manifest、页面上下文、Action 或事件缺口：修 `ai-platform` 契约和测试，再重测。
- DNS、HTTPS、反向代理或容器问题：修管理员初始化流程，不把服务器特例硬编码进业务代码。

冷启动验收时禁止人工替业务 AI 补写业务代码。失败尝试保留报告，清理仅限本次带标签的隔离资源，然后换全新无上下文 AI 重跑。

## 管理员接入回执

最终只向业务用户输出：系统名称与入口、子模块、参与部门、页面、AI 操作及确认要求、健康状态、平台登记状态和尚缺外部条件。密钥只报告“已配置/待配置”，绝不回显值。
