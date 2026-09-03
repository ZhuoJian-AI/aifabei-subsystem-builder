---
name: aifabei-subsystem-builder
description: "让管理员 AI 一次性初始化 Alphabet 企业 ECS，让业务 AI 仅凭业务描述和本地 Git 新建、修改并直接部署跨部门子模块；文件默认使用固定数据盘并保留迁移 OSS 的能力，同时按灼见六大契约接入 SaaS。用户提到 Alphabet 模块、企业 ECS、跨部门系统、AI CRUD、文件迁移、iframe 或灼见接入时使用。"
---

# Alphabet 企业模块搭建

公司对外及用户可见名称固定为 `Alphabet`。为兼容已登记的组织、域名和项目，技术稳定标识、Skill ID 与仓库名继续沿用 `aifabei`；不得把稳定标识误当作公司显示名称。

用户只需要说明业务、参与部门和期望结果。不要要求用户选择框架、写命令、配置 Docker/Nginx、注册 GitHub 或使用 Coolify；这些由 AI 完成，并用业务语言汇报。

管理员首次接入服务器时，小白只需直接提供服务器公网地址、root 账号和密码，或提供已经登录的云控制台页面；这已经构成本次登录与初始化授权。AI 必须读取 [ECS 首次接入](references/ecs-first-access.md) 并自动完成登录：优先尝试公网 SSH，连接在密码校验前超时或拒绝时自动改走云控制台 Workbench，轻量应用服务器还可用命令助手自行诊断。不得把 SSH、端口、VPN、密钥对或命令行排障重新交给小白；只有所有可用登录路径都失败时，才报告唯一、具体且必须由用户完成的控制台动作。

## 固定架构

```text
灼见 SaaS（中央控制面）
└─ Alphabet 企业大模块（逻辑聚合）
   └─ 模块系统（独立域名、本地 Git、数据库和发布单元）
      └─ 子模块（moduleKey）
         └─ 页面（pageKey）与 Action（actionKey）
            ├─ 一个 owner 部门 + 多个协作部门（开发与验收责任）
            └─ 多个 accessRole（员工使用授权建议）
```

模块业务数据始终留在模块自己的数据库。灼见只保存登记、Manifest、授权、Action 目录、审计、事件游标和必要索引；禁止直接连接模块数据库。

用户上传或系统生成的 Excel、Word、PPT、PDF、图片、音视频、压缩包和其他持久文件不进入数据库或容器可写层。新环境默认进入模块独立的 ECS 固定数据目录；管理员以后可迁移到 Alphabet 企业 OSS。无论使用硬盘还是 OSS，业务代码必须经过同一存储适配层并保存稳定 `storageKey`，使前端和业务接口在迁移时不变。负责人不需要了解阿里云、Bucket、RAM、AccessKey 或服务器路径。完整规则及迁移步骤见 [Alphabet 文件存储与 OSS 迁移](references/object-storage.md)。

GitHub 和 Coolify 不属于本 Skill 的企业模块开发、部署或更新链路。源码以企业 ECS 上的本地 Git 仓库为准，Docker 运行模块，Nginx 提供域名和 HTTPS。若需异地备份，使用 ECS 快照或企业指定的备份位置，不把远程 Git 作为业务用户前置条件。

## 六大平台契约

原生模块必须同时实现：

1. **Manifest**：声明子模块、参与部门责任、角色建议、页面、Action 和事件。
2. **SSO**：接收灼见一次性 Ticket，建立模块自己的安全会话。
3. **授权**：原生系统只按角色计算 `applicationSlug → moduleKey → pageKey → actionKey`；用户可有多个角色，权限取并集。
4. **页面上下文 Bridge**：向灼见上报当前页面、实体、筛选和选择摘要。
5. **Action**：页面和 AI 通过同一业务服务完成查询、创建、修改、删除、审批和导出。
6. **Event**：不同模块通过版本化、幂等事件交换业务变化，不共享数据库。

完整字段和安全规则见 [平台接入协议](references/platform-contract.md)。ECS Publisher 只是管理员初始化和发布登记通道，不是第七个业务契约；它不能替代 Manifest、SSO、授权、Bridge、Action 或 Event。

## 先选择执行模式

根据用户现状自动选择，不把技术判断抛给小白：

1. **管理员一次性初始化 ECS**：读取 [管理员与服务器初始化](references/admin-bootstrap.md) 和 [Alphabet 文件存储与 OSS 迁移](references/object-storage.md)，建立 Docker、本地 Git、Nginx、域名、HTTPS、安全规则、固定数据目录、磁盘阈值和备份，并运行 `scripts/provision_runtime.py` 安装最小权限登记凭证与无密钥环境档案。默认使用本地硬盘；只有管理员明确要求时才初始化 OSS 和文件网关。每台新 ECS 只做一次；不接入 GitHub 或 Coolify。
2. **新建原生模块系统**：只有业务确实需要独立域名、数据库、故障隔离或发布周期时才选。读取 [原生聚合与扩展](references/native-aggregation.md)、[平台接入协议](references/platform-contract.md) 和 [ECS 直接发布](references/direct-ecs-deployment.md)，优先运行 `scripts/scaffold_subsystem.py` 建立标准骨架，再实现业务页面、数据库和 Action。
3. **给现有系统增加子模块**：用户说“在这个模块里再加”“继续扩展当前系统”或新业务可沿用现有域名和数据库时优先选择。读取 [原生聚合与扩展](references/native-aggregation.md)，先运行 `scripts/inspect_subsystem.py --path <项目根目录> --json`；保留 `applicationSlug`、本地 Git、域名、接入密钥和数据卷，在同一 Manifest 的 `modules[]` 增加新的 `moduleKey`、页面和 Action。
4. **修改已有子模块**：先运行 `scripts/inspect_subsystem.py`，保留数据和现有能力，以兼容方式升级 Manifest 与业务代码，并部署到原域名。
5. **接入老系统**：只做 iframe、域名白名单和管理员授权；老系统未实现原生 SSO/Action 前，允许用户在 iframe 内额外登录一次，不伪装成已经打通数据。

如果无法判断是否需要新系统，先检查现有 Manifest、本地项目和环境档案。能在现有业务边界内实现时默认增加 `moduleKey`；只有需要独立故障域、数据隔离、域名或发布周期时才新建 `applicationSlug`。

## 开发前硬门槛

- 读取全局与项目 `AGENTS.md`。
- 网络可访问时，只读查看灼见公开源码 `https://github.com/ZhuoJian-AI/ai-platform`，重点参考 `llm_router/backend/app/api/ecs_publisher.py`、`app/services/subsystem_*` 与企业应用前端调用链，并记录参考 commit；不得要求 GitHub 账号、Token 或 push 权限。无法访问时不得阻塞开发，仍以本 Skill 的六大契约和 Schema 为最终标准。
- 以本 Skill 的 Schema 和版本化契约为唯一稳定依据，不依赖灼见私有数据库结构。
- 从空目录开发时先运行 `python <skill>/scripts/scaffold_subsystem.py --help`。
- 已有项目先运行 `python <skill>/scripts/inspect_subsystem.py --path <项目根目录> --json`。
- 检查结果已有相符 `applicationSlug` 时，不得通过新目录、新域名或名称后缀绕开扩展；新增子模块必须沿用原系统并保持已有 `moduleKey`、页面路由、Action 和数据迁移兼容。
- 每个项目必须是本地 Git 仓库；发布前提交本次修改并保持工作树干净。不得因为没有 GitHub 而省略版本、回滚和变更审查。
- 管理员交付的环境档案必须明确目标 ECS、域名后缀、资源限制、部署目录和登记凭证引用。业务 AI 不得把服务器密码、接入密钥或登记凭证写入 Git、日志或回复。
- 业务需求只要出现附件、上传、下载、导入、导出、图片、音视频或办公文档，就自动实现统一存储适配层；不要询问负责人选择硬盘还是 OSS。读取环境档案：`local-managed` 时使用固定数据目录，`oss-gateway` 时使用文件网关。两种模式都未验证时只向管理员报告“文件存储待初始化”，不得让负责人登录阿里云、创建 RAM 或手工设置路径。

## 原生模块必须满足

- 固定端点：`/health`、Manifest、事件拉取、事件投递、Action 和 SSO。
- Manifest `version` 保持整数 `2`，新增能力用 `contractRevision` 表示；按 `schemas/manifest-v2.schema.json` 输出。
- 每个子模块恰好一个 owner 部门；`departments[]` 只声明开发、协作、审批和验收责任，绝不直接授予员工权限。
- 每个子模块必须声明 `accessRoles[]`，其中 `roleKey/pageKeys/actionKeys` 只是给管理员的角色建议。平台管理员或企业管理员确认并映射到平台角色后才生效；Manifest 不能自行扩权。
- 一个用户只能归属一个组织部门，但可以拥有多个角色。部门回答“这个人属于哪里”和默认数据上下文；角色回答“这个人能看哪个大模块、子模块、页面，能执行哪些页面按钮和 AI Action”。跨部门协作通过增加角色实现，不得把用户挂到多个部门，也不得直接给部门或个人颁发原生模块权限。
- 模块系统是部署边界，不是员工端唯一导航颗粒度。员工体验必须按“企业 → 子模块 → 页面”聚合；远端页面不重复灼见侧边栏、企业选择器或登录页。
- SSO 会话必须保存灼见签发的 `pageKeys`、`actionKeys` 和 `pageAccess`。未授权路由返回 403；页面按钮和 `/api/ui/actions/*` 必须再次校验页面与 Action allowlist。
- 页面按钮与 AI 调用同一个应用服务函数和权限判断。
- AI 工具必须同时通过用户、企业、应用、子模块、页面、Action 和管理员授权；`aiEnabled=false` 永不暴露给 AI。
- 查询、新增、修改、删除、审批、导出统一走 Action。修改和删除使用 `expectedVersion`；版本冲突返回 HTTP 409。
- 高风险操作声明 `requiresConfirmation=true`，校验确认声明、参数哈希和幂等 `requestId`；拒绝、过期和重复批准不得重复执行。
- iframe Bridge 只发送当前页面和选中实体摘要，不传 Token、Cookie、密码或整表数据；`postMessage` 禁止使用 `"*"`。
- 跨系统数据流使用版本化事件；目标系统按 `eventId` 幂等消费，不共享数据库。事件 `sequence` 必须跨容器/数据库重建仍单调不回退。
- 生产镜像只安装运行时依赖；测试与 Playwright 依赖拆到开发依赖文件；Dockerfile 自带不依赖额外系统包的 `/health` 检查。
- 含持久文件的系统必须通过统一存储适配层读写。默认本地模式使用 `FILE_STORAGE_DRIVER=local` 和 `FILE_STORAGE_ROOT=/data/files`；OSS 模式才使用 `STORAGE_GATEWAY_URL` 和本系统专属 `STORAGE_PROJECT_TOKEN`。数据库只保存 `storageKey`、`storageBackend`、文件名、MIME、大小、SHA-256 及业务归属，不保存绝对路径或签名 URL。任一模式故障时明确失败，禁止静默切换后端。

## 本地 Git 与 ECS 直接部署

公网 ECS 按 [ECS 首次接入](references/ecs-first-access.md) 和 [ECS 直接发布](references/direct-ecs-deployment.md) 执行。一个模块系统一个域名；同一 ECS 可按域名运行多个容器；子模块使用路径和 `moduleKey`，不单独购买服务器或域名。

项目目录固定使用 `{companySlug}-{applicationSlug}`，例如 `aifabei-sample-review`。它是 ECS 上的本地 Git 仓库名，不是远程仓库名。正式目录不得携带版本、日期、环境或 `coldstart`；版本由 Git commit/tag 和 Manifest `contractRevision` 表达。

部署前依次运行：

```text
python <skill>/scripts/validate_source.py --path <模块项目目录>
python <skill>/scripts/validate_storage_profile.py --runtime-profile /etc/zhuojian/runtime.json
python <skill>/scripts/validate_endpoint.py --base-url https://<模块域名>
python <skill>/scripts/e2e_acceptance.py --base-url https://<模块域名> --module-key <moduleKey> --page-key <pageKey> --query-action <actionKey>
python <skill>/scripts/publish_subsystem.py --project-path <模块项目目录> --base-url https://<模块域名>
```

前四项验证通过、容器和 Nginx 已切换到健康版本后，最后一项通过 `POST /api/v1/ecs-publisher/modules/register` 登记当前 Git commit 并同步 Manifest。含持久文件时还必须运行 `validate_source.py --requires-file-storage` 并完成真实上传、下载、重建容器后读取及未授权访问拒绝测试；已启用 OSS 时追加 `--requires-object-storage`。首次发布和后续更新都部署同一本地 Git 仓库、同一 `applicationSlug`、域名、数据目录、存储键和接入密钥；新增子模块、页面、角色建议和 Action 默认为待授权，参与部门变化不会自动改变员工权限。

管理员只在每台 ECS 初始化一次：安装运行底座、配置通配 DNS/HTTPS 策略、建立固定数据目录、磁盘阈值和备份，并用管理员会话调用 `POST /api/v1/ecs-publisher/organizations/{organizationId}/runtimes`。默认本地存储不需要阿里云账号、RAM 或文件网关。管理员以后选择 OSS 时，再一次性绑定同地域私有 Bucket 和网关；迁移按文件清单、SHA-256、分批切换和回滚窗口执行。负责人不获得平台管理员 Token 或 OSS AccessKey，也不需要逐项目配置基础设施。

## 失败处理

- 模板或说明歧义：修本 Skill、Schema、模板或验证脚本，再从空目录重测。
- 平台鉴权、Manifest、页面上下文、Action、事件或自动登记缺口：修 `ai-platform` 契约和测试，再重测。
- DNS、HTTPS、Nginx、Docker 或服务器资源问题：修管理员初始化/直接部署流程，不把服务器特例硬编码进业务代码。
- 本地固定数据目录、权限、磁盘阈值或备份未验证：停止含文件能力的首次发布，修管理员底座；不得写入容器可写层或公开静态目录。
- 环境明确选择 OSS 但 Bucket、文件网关、系统前缀或签名权限未验证：停止发布或迁移，不得把 AccessKey 交给业务 AI，也不得静默改回本地模式。
- 新版本构建、启动或健康检查失败：保持数据目录，恢复上一健康 Git commit 对应的不可变镜像；禁止清空数据库解决问题。

## 冷启动角色与隔离

- 管理员 AI 只准备 Skill、企业档案、通配域名、ECS 运行底座和登记入口；不替业务 AI 编写业务流程。
- 每个业务 AI 只获得本 Skill、业务描述、自己的本地 Git 项目、目标域名和目标 ECS 的受控部署入口。不得读取另一业务 AI 的目录、服务器、日志或失败报告。
- 并行测试使用不同目录、容器、回环端口和数据目录；不能复制另一测试的代码、Secret 或数据库作为捷径。
- 管理员仅从公开端点、Docker/Nginx 状态和灼见登记结果进行黑盒验收。冷启动失败时先判断是 Skill、平台契约还是基础设施问题，不进入业务目录人工补写后宣称成功。

## 管理员接入回执

最终只向业务用户输出：系统名称与入口、子模块、开发/验收责任部门、建议角色、页面、AI 操作及确认要求、健康状态、灼见登记/同步状态、文件能力状态和尚缺外部条件。明确说明“建议角色待管理员映射和授权”；只用“本地文件存储已就绪”“OSS 已就绪”或“文件存储待管理员处理”描述状态，绝不向负责人展示服务器路径、Bucket、对象前缀或密钥。
