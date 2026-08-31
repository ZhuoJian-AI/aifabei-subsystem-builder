---
name: aifabei-subsystem-builder
description: "让管理员 AI 一次性初始化爱法贝企业 ECS，让业务 AI 仅凭业务描述和本地 Git 新建、修改并直接部署跨部门子模块，同时按灼见六大契约接入 SaaS。用户提到爱法贝模块、企业 ECS、跨部门系统、AI CRUD、iframe 或灼见接入时使用。"
---

# 爱法贝企业模块搭建

用户只需要说明业务、参与部门和期望结果。不要要求用户选择框架、写命令、配置 Docker/Nginx、注册 GitHub 或使用 Coolify；这些由 AI 完成，并用业务语言汇报。

## 固定架构

```text
灼见 SaaS（中央控制面）
└─ 爱法贝企业大模块（逻辑聚合）
   └─ 模块系统（独立域名、本地 Git、数据库和发布单元）
      └─ 子模块（moduleKey，最小授权边界）
         └─ 页面、数据、AI Action、事件
            └─ 一个 owner 部门 + 多个协作/审批/使用部门
```

模块业务数据始终留在模块自己的数据库。灼见只保存登记、Manifest、授权、Action 目录、审计、事件游标和必要索引；禁止直接连接模块数据库。

GitHub 和 Coolify 不属于本 Skill 的企业模块开发、部署或更新链路。源码以企业 ECS 上的本地 Git 仓库为准，Docker 运行模块，Nginx 提供域名和 HTTPS。若需异地备份，使用 ECS 快照或企业指定的备份位置，不把远程 Git 作为业务用户前置条件。

## 六大平台契约

原生模块必须同时实现：

1. **Manifest**：声明子模块、参与部门建议、页面、Action 和事件。
2. **SSO**：接收灼见一次性 Ticket，建立模块自己的安全会话。
3. **授权**：按企业、子模块、页面、Action、部门/岗位/工作组/用户双重校验。
4. **页面上下文 Bridge**：向灼见上报当前页面、实体、筛选和选择摘要。
5. **Action**：页面和 AI 通过同一业务服务完成查询、创建、修改、删除、审批和导出。
6. **Event**：不同模块通过版本化、幂等事件交换业务变化，不共享数据库。

完整字段和安全规则见 [平台接入协议](references/platform-contract.md)。ECS 部署只负责让实现这六项契约的服务稳定运行，不新增第七个 SaaS 运行时契约。

## 先选择执行模式

根据用户现状自动选择，不把技术判断抛给小白：

1. **管理员一次性初始化 ECS**：读取 [管理员与服务器初始化](references/admin-bootstrap.md)，建立 Docker、本地 Git、Nginx、域名、HTTPS、安全规则、直接部署命令和不含密钥的环境档案。每台新 ECS 只做一次；不接入 GitHub 或 Coolify。
2. **新建原生模块系统**：只有业务确实需要独立域名、数据库、故障隔离或发布周期时才选。读取 [原生聚合与扩展](references/native-aggregation.md)、[平台接入协议](references/platform-contract.md) 和 [ECS 直接发布](references/direct-ecs-deployment.md)，优先运行 `scripts/scaffold_subsystem.py` 建立标准骨架，再实现业务页面、数据库和 Action。
3. **给现有系统增加子模块**：用户说“在这个模块里再加”“继续扩展当前系统”或新业务可沿用现有域名和数据库时优先选择。读取 [原生聚合与扩展](references/native-aggregation.md)，先运行 `scripts/inspect_subsystem.py --path <项目根目录> --json`；保留 `applicationSlug`、本地 Git、域名、接入密钥和数据卷，在同一 Manifest 的 `modules[]` 增加新的 `moduleKey`、页面和 Action。
4. **修改已有子模块**：先运行 `scripts/inspect_subsystem.py`，保留数据和现有能力，以兼容方式升级 Manifest 与业务代码，并部署到原域名。
5. **接入老系统**：只做 iframe、域名白名单和管理员授权；老系统未实现原生 SSO/Action 前，允许用户在 iframe 内额外登录一次，不伪装成已经打通数据。

如果无法判断是否需要新系统，先检查现有 Manifest、本地项目和环境档案。能在现有业务边界内实现时默认增加 `moduleKey`；只有需要独立故障域、数据隔离、域名或发布周期时才新建 `applicationSlug`。

## 开发前硬门槛

- 读取全局与项目 `AGENTS.md`。
- 以本 Skill 的 Schema 和版本化契约为唯一稳定依据，不依赖灼见私有数据库结构。
- 从空目录开发时先运行 `python <skill>/scripts/scaffold_subsystem.py --help`。
- 已有项目先运行 `python <skill>/scripts/inspect_subsystem.py --path <项目根目录> --json`。
- 检查结果已有相符 `applicationSlug` 时，不得通过新目录、新域名或名称后缀绕开扩展；新增子模块必须沿用原系统并保持已有 `moduleKey`、页面路由、Action 和数据迁移兼容。
- 每个项目必须是本地 Git 仓库；发布前提交本次修改并保持工作树干净。不得因为没有 GitHub 而省略版本、回滚和变更审查。
- 管理员交付的环境档案必须明确目标 ECS、域名后缀、资源限制、部署目录和登记凭证引用。业务 AI 不得把服务器密码、接入密钥或登记凭证写入 Git、日志或回复。

## 原生模块必须满足

- 固定端点：`/health`、Manifest、事件拉取、事件投递、Action 和 SSO。
- Manifest `version` 保持整数 `2`，新增能力用 `contractRevision` 表示；按 `schemas/manifest-v2.schema.json` 输出。
- 每个子模块恰好一个 owner 部门；每个参与部门必须显式声明 `pageKeys` 和 `actionKeys` 作为建议授权上限。平台管理员或企业管理员仍须确认，Manifest 不能自行扩权。
- 模块系统是部署边界，不是员工端唯一导航颗粒度。员工体验必须按“企业 → 子模块 → 页面”聚合；远端页面不重复灼见侧边栏、企业选择器或登录页。
- SSO 会话必须保存灼见签发的 `pageKeys`、`actionKeys` 和 `pageAccess`。未授权路由返回 403；页面按钮和 `/api/ui/actions/*` 必须再次校验页面与 Action allowlist。
- 页面按钮与 AI 调用同一个应用服务函数和权限判断。
- AI 工具必须同时通过用户、企业、应用、子模块、页面、Action 和管理员授权；`aiEnabled=false` 永不暴露给 AI。
- 查询、新增、修改、删除、审批、导出统一走 Action。修改和删除使用 `expectedVersion`；版本冲突返回 HTTP 409。
- 高风险操作声明 `requiresConfirmation=true`，校验确认声明、参数哈希和幂等 `requestId`；拒绝、过期和重复批准不得重复执行。
- iframe Bridge 只发送当前页面和选中实体摘要，不传 Token、Cookie、密码或整表数据；`postMessage` 禁止使用 `"*"`。
- 跨系统数据流使用版本化事件；目标系统按 `eventId` 幂等消费，不共享数据库。事件 `sequence` 必须跨容器/数据库重建仍单调不回退。
- 生产镜像只安装运行时依赖；测试与 Playwright 依赖拆到开发依赖文件；Dockerfile 自带不依赖额外系统包的 `/health` 检查。

## 本地 Git 与 ECS 直接部署

公网 ECS 按 [ECS 首次接入](references/ecs-first-access.md) 和 [ECS 直接发布](references/direct-ecs-deployment.md) 执行。一个模块系统一个域名；同一 ECS 可按域名运行多个容器；子模块使用路径和 `moduleKey`，不单独购买服务器或域名。

项目目录固定使用 `{companySlug}-{applicationSlug}`，例如 `aifabei-sample-review`。它是 ECS 上的本地 Git 仓库名，不是远程仓库名。正式目录不得携带版本、日期、环境或 `coldstart`；版本由 Git commit/tag 和 Manifest `contractRevision` 表达。

部署前依次运行：

```text
python <skill>/scripts/validate_source.py --path <模块项目目录>
python <skill>/scripts/validate_endpoint.py --base-url https://<模块域名>
python <skill>/scripts/e2e_acceptance.py --base-url https://<模块域名> --module-key <moduleKey> --page-key <pageKey> --query-action <actionKey>
```

首次发布和后续更新都部署同一本地 Git 仓库、同一 `applicationSlug`、域名、数据目录和接入密钥。成功后通知灼见重新同步 Manifest；新增子模块、页面、部门建议和 Action 默认为待授权。

管理员只在每台 ECS 初始化一次：安装运行底座、配置通配 DNS/HTTPS策略、建立目录、生成环境档案并安装该 ECS 所属企业的登记凭证。业务 AI 后续不得要求用户提供 GitHub/Coolify账号，也不得要求管理员逐项目配置基础设施。

## 失败处理

- 模板或说明歧义：修本 Skill、Schema、模板或验证脚本，再从空目录重测。
- 平台鉴权、Manifest、页面上下文、Action、事件或自动登记缺口：修 `ai-platform` 契约和测试，再重测。
- DNS、HTTPS、Nginx、Docker 或服务器资源问题：修管理员初始化/直接部署流程，不把服务器特例硬编码进业务代码。
- 新版本构建、启动或健康检查失败：保持数据目录，恢复上一健康 Git commit 对应的不可变镜像；禁止清空数据库解决问题。

## 冷启动角色与隔离

- 管理员 AI 只准备 Skill、企业档案、通配域名、ECS 运行底座和登记入口；不替业务 AI 编写业务流程。
- 每个业务 AI 只获得本 Skill、业务描述、自己的本地 Git 项目、目标域名和目标 ECS 的受控部署入口。不得读取另一业务 AI 的目录、服务器、日志或失败报告。
- 并行测试使用不同目录、容器、回环端口和数据目录；不能复制另一测试的代码、Secret 或数据库作为捷径。
- 管理员仅从公开端点、Docker/Nginx 状态和灼见登记结果进行黑盒验收。冷启动失败时先判断是 Skill、平台契约还是基础设施问题，不进入业务目录人工补写后宣称成功。

## 管理员接入回执

最终只向业务用户输出：系统名称与入口、子模块、参与部门、页面、AI 操作及确认要求、健康状态、灼见登记/同步状态和尚缺外部条件。密钥只报告“已配置/待配置”，绝不回显值。
