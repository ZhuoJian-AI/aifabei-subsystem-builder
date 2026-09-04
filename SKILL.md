---
name: aifabei-subsystem-builder
description: "让管理员 AI 一次性初始化 Alphabet 企业 ECS 和可选的企业 OSS 网关，让业务 AI 仅凭业务描述和服务器登录信息新建、修改并直接部署跨部门子模块；运行底座自动为未来系统分配域名、文件前缀和项目凭证，同时按灼见六大契约接入 SaaS。用户提到 Alphabet 模块、企业 ECS、跨部门系统、AI CRUD、文件迁移、OSS、iframe 或灼见接入时使用。"
---

# Alphabet 企业模块搭建

公司对外及用户可见名称固定为 `Alphabet`。为兼容已登记的组织、域名和项目，技术稳定标识、Skill ID 与仓库名继续沿用 `aifabei`；不得把稳定标识误当作公司显示名称。

用户只需要说明业务、参与部门和期望结果。不要要求用户选择框架、写命令、配置 Docker/Nginx、注册 GitHub 或使用 Coolify；这些由 AI 完成，并用业务语言汇报。

严格区分“管理员首次初始化”和“业务 AI 日常使用”。管理员首次初始化可以使用公网 SSH 或已经登录的云控制台，并按 [SSH、VPN 与代理访问](references/ssh-access.md) 验证至少一条业务连接路径。初始化完成后，业务负责人只需在当前任务中把服务器公网地址、root 账号和密码交给 Codex；AI 按 [ECS 首次接入](references/ecs-first-access.md) 自动尝试 SSH `22`、再尝试已配置的 SSH `443`。业务电脑已有 VPN 或本机代理时，AI 自行复用该网络路径；不得要求小白提供阿里云账号、控制台页面、RAM、密钥对或命令行操作。AI 只在真正出现的 SSH 密码提示中转交密码，不把密码放进命令参数、脚本、文件、Git、日志或回复，也不在任务结束后另行保存。

VPN 或代理只解决“网络能否到服务器”，不会改变 root 账号和密码。探测在出现密码提示前超时，不得误报为密码错误；只有服务端明确返回 `Permission denied` 才属于凭证失败。SSH `443` 与 HTTPS 复用是 `22` 受限时的可选回退，不是每台 ECS 的强制改造或交付门槛。

## 固定架构

```text
灼见 SaaS（中央控制面）
└─ Alphabet 企业大模块（逻辑聚合）
   └─ 模块系统（独立域名、本地 Git、数据库和发布单元）
      └─ 子模块（moduleKey）
         └─ 页面（pageKey）与 Action（actionKey）
            ├─ 一个 owner 部门 + 多个协作部门（开发与验收责任）
            └─ 多个 accessRole（供管理员参考的权限组合，不是子系统角色）
```

模块业务数据始终留在模块自己的数据库。灼见只保存登记、Manifest、授权、Action 目录、审计、事件游标和必要索引；禁止直接连接模块数据库。

平台 AI 固定属于灼见 SaaS 控制面。模型供应商注册、API Key、模型路由、额度、AI 编排、Action 凭证签发和审计都由平台管理员在 SaaS 底座管理；Alphabet 模块不得要求业务负责人填写模型供应商或 API Key，不得保存或复用平台模型密钥，也不得为了让平台 AI 工作而在模块内再搭一套聊天 AI。模块只负责在 Manifest 中完整声明并实现可调用的业务 Action。SaaS 把 Action 动态物化为 AI 工具，不为每个系统另写 Codex Skill，也不要求管理员在 UI 中逐条编写工具说明。平台 AI 只能从已登记 Manifest 中选择 `aiEnabled=true` 且当前用户最终授权允许的 Action，再以短时 `zhuojian-action` 凭证调用模块。

平台 AI 不登录 ECS、不使用 root/SSH、不连接模块数据库，也不能在请求体中指定任意后端 URL。调用目标只能由已登记的模块 `baseUrl` 与固定路径 `/api/integration/actions/{actionKey}` 组成；Action 凭证放在 `Authorization: Bearer`，请求体只传契约字段和业务参数。公网只开放 Nginx 的 HTTPS `443`，由 Nginx 按模块域名反向代理到对应容器；数据库、Docker 管理端口、容器回环端口和任意内部管理接口不得暴露给 SaaS 或 AI。

用户上传或系统生成的 Excel、Word、PPT、PDF、图片、音视频、压缩包和其他持久文件不进入数据库或容器可写层。`/etc/zhuojian/runtime.json` 只决定**尚未初始化的新系统**采用哪种默认后端；第一次 `ensure-app` 会把实际选择冻结到该系统的 release 记录。未完成 OSS 初始化时进入模块独立的 ECS 固定数据目录；管理员一次性验收企业 OSS 网关后，后来新建的系统自动使用 `apps/<applicationSlug>/`，无需管理员预建文件夹或逐项目发令牌。Runtime 已纳管的系统继续使用 release 记录的 `local-managed` 或 `oss-gateway`；尚未纳管、使用 `signed-upload` 等旧方案的容器保持原样，完成专项审查和导入前不得直接交给 Runtime 更新。任何后端变化都必须由管理员另行执行带清单、校验和回滚点的显式迁移。业务代码始终经过同一存储适配层并保存稳定 `storageKey`，使前端和业务接口在迁移时不变。负责人不需要了解阿里云、Bucket、RAM、AccessKey、项目令牌或服务器路径。完整规则及迁移步骤见 [Alphabet 文件存储与 OSS 迁移](references/object-storage.md)。

GitHub 和 Coolify 不属于本 Skill 的企业模块开发、部署或更新链路。源码以企业 ECS 上的本地 Git 仓库为准，Docker 运行模块，Nginx 提供域名和 HTTPS。若需异地备份，使用 ECS 快照或企业指定的备份位置，不把远程 Git 作为业务用户前置条件。

## 六大平台契约

原生模块必须同时实现：

1. **Manifest**：声明子模块、参与部门责任、权限组合建议、页面、Action 和事件。
2. **SSO**：接收灼见一次性 Ticket，建立模块自己的安全会话。
3. **授权**：灼见 SaaS 只以平台角色计算 `applicationSlug → moduleKey → pageKey → actionKey` 与部门数据范围；用户可有多个角色，权限取并集。模块不建第二套角色，只复验 SaaS 签发的最终权限与数据范围。
4. **页面上下文 Bridge**：向灼见上报当前页面、实体、筛选和选择摘要。
5. **Action**：页面和 AI 通过同一业务服务完成查询、创建、修改、删除、审批和导出。
6. **Event**：不同模块通过版本化、幂等事件交换业务变化，不共享数据库。

完整字段和安全规则见 [平台接入协议](references/platform-contract.md)。ECS Publisher 只是管理员初始化和发布登记通道，不是第七个业务契约；它不能替代 Manifest、SSO、授权、Bridge、Action 或 Event。

## 先选择执行模式

根据用户现状自动选择，不把技术判断抛给小白：

1. **管理员一次性初始化 ECS**：读取 [管理员与服务器初始化](references/admin-bootstrap.md)、[SSH、VPN 与代理访问](references/ssh-access.md) 和 [Alphabet 文件存储与 OSS 迁移](references/object-storage.md)，建立 Docker、本地 Git、Nginx、域名、安全规则、固定数据目录、磁盘阈值和备份，并先从业务实际使用的外部 Codex 验证 VPN 下的标准 SSH `22` 或可选的 HTTPS/SSH `443` 共用入口；再以本地模式运行 `scripts/provision_runtime.py`。该脚本会在请求平台签发前真实执行本地文件写入、读取、删除和磁盘余量检查，通过后才安装最小权限登记凭证与无密钥环境档案。若管理员选择企业 OSS，随后安装文件网关并用匿名读取拒绝、`apps/*` 边界拒绝、真实对象读写删除、双应用隔离和临时身份撤销探针切换 Runtime 默认存储；禁止用布尔参数跳过验收。每台 ECS 和每套企业 OSS 只初始化一次；不接入 GitHub 或 Coolify。
2. **新建原生模块系统**：只有业务确实需要独立域名、数据库、故障隔离或发布周期时才选。读取 [原生聚合与扩展](references/native-aggregation.md)、[平台接入协议](references/platform-contract.md) 和 [ECS 直接发布](references/direct-ecs-deployment.md)，优先运行 `scripts/scaffold_subsystem.py` 建立标准骨架，再实现业务页面、数据库和 Action。
3. **给现有系统增加子模块**：用户说“在这个模块里再加”“继续扩展当前系统”或新业务可沿用现有域名和数据库时优先选择。读取 [原生聚合与扩展](references/native-aggregation.md)，先运行 `scripts/inspect_subsystem.py --path <项目根目录> --json`；保留 `applicationSlug`、本地 Git、域名、接入密钥和数据卷，在同一 Manifest 的 `modules[]` 增加新的 `moduleKey`、页面和 Action。
4. **修改已有子模块**：先运行 `scripts/inspect_subsystem.py --path <项目根目录> --json`，保留数据和现有能力，以兼容方式升级 Manifest 与业务代码，并部署到原域名。
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
- 模型供应商注册和密钥只存在于灼见 SaaS 底座。项目源码、镜像、部署环境和模块数据库不得出现 `OPENAI_API_KEY`、`ANTHROPIC_API_KEY`、`DASHSCOPE_API_KEY`、`AZURE_OPENAI_API_KEY`、`GEMINI_API_KEY`、`DEEPSEEK_API_KEY` 等平台模型凭证。业务 AI 只实现业务 Action，不向负责人索取模型供应商、Key、Base URL 或模型名称。
- 业务需求只要出现附件、上传、下载、导入、导出、图片、音视频或办公文档，就自动实现统一存储适配层；不要询问负责人选择硬盘还是 OSS。读取环境档案：`local-managed` 时使用固定数据目录，`oss-gateway` 时使用文件网关。两种模式都未验证时只向管理员报告“文件存储待初始化”，不得让负责人登录阿里云、创建 RAM 或手工设置路径。
- 新系统的文件上传必须保留模板提供的精确 `Content-Length`、全 ECS 共享上传锁、双副本容量门禁、`uploading` 校验恢复与 `pending` 删除恢复。业务 AI 不得为了“支持流式上传”去掉这些状态和锁；对象操作与数据库提交之间发生超时或重启时，系统必须靠后台短超时、小批次任务自动收敛，不能依赖负责人保存原请求编号。
- 业务 AI 不申请、填写、复制或输出 Bucket、RAM AccessKey、网关 Secret 或项目令牌；正常流程也不需要主动读取这些值。它在项目首次干净提交后运行 `zhuojian-runtime ensure-app <applicationSlug>`；Runtime 为首次初始化的系统选择当前默认后端并冻结到 release：OSS 模式下自动生成该系统身份并直接注入容器，本地模式下自动建立固定数据目录。重复执行必须复用同一身份和已冻结后端。若管理员已暂停或永久撤销该系统，命令必须失败而不是自动恢复。

## 原生模块必须满足

- 固定端点：`/health`、Manifest、事件拉取、事件投递、Action 和 SSO。
- Manifest `version` 保持整数 `2`，本 Skill 新建或升级的原生系统必须使用当前 SaaS 已支持的 `contractRevision="2.4"` 并按 `schemas/manifest-v2.schema.json` 输出。不得自行声明平台尚未支持的更高版本号。
- 每个子模块恰好一个 owner 部门；`departments[]` 只声明开发、协作、审批和验收责任，绝不直接授予员工权限。
- 每个子模块必须声明 v2.4 兼容字段 `accessRoles[]`；它只是“页面 + Action”权限组合建议，不是子系统的角色、不创建平台角色、不绑定用户，也不决定部门数据范围。当前 SaaS 管理员界面会要求逐项处理这些建议，因此只声明真实业务需要的最少权限组合，不生成猜测性角色。管理员把对应页面和 Action 直接授权给 SaaS 中已有的平台角色；Manifest 不能自行扩权。
- 一个用户只能归属一个组织部门，但可以拥有多个角色。部门回答“这个人属于哪里”；角色同时决定“可以查看哪些部门的数据”以及“能进入哪个系统、子模块、页面，能执行哪些页面按钮和 AI Action”。跨部门协作通过增加角色实现，不得把用户挂到多个部门，也不得直接给部门或个人颁发原生模块权限。
- 模块系统是部署边界，不是员工端唯一导航颗粒度。员工体验必须按“企业 → 子模块 → 页面”聚合；远端页面不重复灼见侧边栏、企业选择器或登录页。
- SSO 会话必须保存灼见签发的 `pageKeys`、`actionKeys`、`pageAccess`、`roleIds` 和 `effectiveDataScope`。未授权路由返回 403；页面按钮和 `/api/ui/actions/*` 必须再次校验页面与 Action allowlist，数据查询与写入必须按 `effectiveDataScope` 过滤。不得把用户本人的 `departmentId` 误当成该用户可查看的全部部门范围。
- 旧业务数据没有记录负责部门或创建人时，升级前必须由业务负责人确认归属并回填；不得将历史数据默认开放给所有角色，也不得静默隐藏后带病上线。
- 页面按钮与 AI 调用同一个应用服务函数和权限判断。
- AI 工具必须同时通过用户、企业、应用、子模块、页面、Action 和管理员授权；`aiEnabled=false` 永不暴露给 AI。
- 每个 Action 的最小硬约束只有“描述 + 接口能力”：非空 `description` 说明业务用途，`actionKey/operation/inputSchema/resultSchema` 说明如何调用和返回什么，并声明 `aiEnabled/requiresConfirmation`。`inputSchema` 只描述业务参数且根类型必须为 `object`，`resultSchema` 必须是 JSON Schema 对象。满足这些内容即可登记并由 SaaS 生成基础 AI 工具。
- 整个 `aiTool`、输入输出字段 `description` 和更精确的结果结构都是推荐增强项：脚手架默认生成，缺少时只警告，不阻断登记。涉及删除、审批或其他高风险操作时应优先补齐副作用与确认文案。不得把 URL、Token、权限绕过、SSH、数据库连接或提示词指令设计成由模型填写的工具参数；平台也不得因此信任请求体中的同名字段。
- SaaS 以 `applicationSlug + actionKey` 建立稳定工具身份，工具说明来自 Manifest，工具参数来自 `inputSchema`。`requestId/moduleKey/pageKey/operation` 由平台根据登记目录填充；修改或删除所需 `expectedVersion` 必须来自最新查询或页面上下文，不能让模型猜测。管理员 UI 只负责把系统能力授权给平台角色、设置角色数据范围、启停 AI 和收紧确认要求，不能替代 Manifest 定义接口。
- `actionKey` 一经发布不得改变业务含义。Action 的操作类型、输入 Schema、AI 开关、确认要求或业务含义发生变化时，SaaS 必须生成差异并把该 Action 重新置为待审核；仅补充名称、描述、`aiTool` 或结果说明时记录差异即可。删除 Action 时立即停用目录项，不复用旧 key 表示另一种操作。
- 用户角色允许某个 Action 不等于 AI 自动获得该 Action。AI 可调用集合必须是用户最终授权、页面 `actionKeys`、Manifest `aiEnabled=true`、管理员启用状态及平台 AI 策略的交集；平台签发一次一用、短时且绑定用户/企业/应用/模块/页面/Action/请求的凭证，模块仍须服务端复验。
- 模块只接受固定 Action 路径，不接受请求体传入上游 URL、容器地址、数据库连接或任意路由。身份、权限和调用目标来自已验证的 Action JWT 与服务端配置，不能信任业务参数中的同名字段。
- 查询、新增、修改、删除、审批、导出统一走 Action。修改和删除使用 `expectedVersion`；版本冲突返回 HTTP 409。
- 高风险操作声明 `requiresConfirmation=true`，校验确认声明、参数哈希和幂等 `requestId`；拒绝、过期和重复批准不得重复执行。
- iframe Bridge 只发送当前页面和选中实体摘要，不传 Token、Cookie、密码或整表数据；`postMessage` 禁止使用 `"*"`。
- 跨系统数据流使用版本化事件；目标系统按 `eventId` 幂等消费，不共享数据库。事件 `sequence` 必须跨容器/数据库重建仍单调不回退。
- 生产镜像只安装运行时依赖；测试与 Playwright 依赖拆到开发依赖文件；Dockerfile 自带不依赖额外系统包的 `/health` 检查。
- 含持久文件的系统必须通过统一存储适配层读写。本地模式使用 `FILE_STORAGE_DRIVER=local` 和 `FILE_STORAGE_ROOT=/data/files`；OSS 模式由 Runtime 注入 `FILE_STORAGE_DRIVER=oss-gateway`、`FILE_STORAGE_GATEWAY_URL` 和本系统专属 `FILE_STORAGE_TOKEN`。数据库只保存内部 `storageKey`、`storageBackend`、文件名、MIME、大小、SHA-256 及业务归属，浏览器只接触不可猜测的 `fileId`，不保存或暴露绝对路径、Bucket 或签名 URL。任一模式故障时明确失败，禁止静默切换后端。

## 本地 Git 与 ECS 直接部署

公网 ECS 按 [ECS 首次接入](references/ecs-first-access.md) 和 [ECS 直接发布](references/direct-ecs-deployment.md) 执行。一个模块系统一个域名；同一 ECS 可按域名运行多个容器；子模块使用路径和 `moduleKey`，不单独购买服务器或域名。

项目目录固定使用 `{companySlug}-{applicationSlug}`，例如 `aifabei-sample-review`。它是 ECS 上的本地 Git 仓库名，不是远程仓库名。正式目录不得携带版本、日期、环境或 `coldstart`；版本由 Git commit/tag 和 Manifest `contractRevision` 表达。

部署前依次运行：

```text
zhuojian-runtime doctor
zhuojian-runtime preflight <applicationSlug>
zhuojian-runtime ensure-app <applicationSlug>
python <skill>/scripts/validate_source.py --path <模块项目目录>
python <skill>/scripts/validate_storage_profile.py --runtime-profile /etc/zhuojian/runtime.json
python <skill>/scripts/validate_ssh_access.py --host <ECS公网地址> --ports 22,443
zhuojian-runtime deploy <applicationSlug> --issue-certificate
python <skill>/scripts/validate_endpoint.py --base-url https://<模块域名>
python <skill>/scripts/e2e_acceptance.py --base-url https://<模块域名> --module-key <moduleKey> --page-key <pageKey> --query-action <actionKey>
python <skill>/scripts/publish_subsystem.py --project-path <模块项目目录> --base-url https://<模块域名>
```

除最后登记外的基础检查、源码检查、部署和公网验收全部通过，且容器和 Nginx 已切换到健康版本后，才通过 `POST /api/v1/ecs-publisher/modules/register` 登记当前 Git commit 并同步 Manifest。`validate_source.py` 必须确认模块没有平台模型供应商凭证；`validate_endpoint.py` 只对缺少 Action 描述、接口字段或输入 Schema 根类型错误等无法生成基础工具的情况阻断，对 `aiTool` 等增强项只输出警告；`e2e_acceptance.py` 必须模拟平台签发合法的页面级 Action 凭证，经公开 HTTPS 域名和 Nginx 执行一次无副作用 query Action，不能用容器地址或本机端口代替。该通用脚本只验证接入链路；项目还必须在隔离测试数据上覆盖部门 A、部门 B 和本人三种范围，验证查询/导出/写操作的允许与拒绝，且页面与 AI Action 结果一致。含持久文件时还必须运行 `validate_source.py --requires-file-storage` 并完成真实上传、下载、重建容器后读取及未授权访问拒绝测试；已启用 OSS 时追加 `--requires-object-storage`。首次发布和后续更新都部署同一本地 Git 仓库、同一 `applicationSlug`、域名、数据目录、存储键和接入密钥；新增子模块、页面、Action、操作类型、输入 Schema、AI 开关或确认要求等能力变化默认为待审核/待授权，单纯补充说明文案只记录差异，参与部门变化不会自动改变员工权限。

环境档案声明 `requiresVpn=true`，或本轮实测只有 VPN/代理路径成功时，发布前的 SSH 复验必须复用同一条成功路径。若当前 Codex 使用本机 HTTP 代理，Banner 探测追加 `--proxy-url http://<本机地址>:<端口>`，交互式登录运行 `scripts/ssh_via_http_proxy.py`；AI 完成这些技术操作，不把代理配置或命令抛给业务负责人。实测路径与 Runtime 档案不一致时继续使用已验证路径，但在回执中要求管理员修正档案，不得重新退回必然失败的直连探测。

管理员只在每台 ECS 初始化一次：安装运行底座、配置通配 DNS/HTTPS 策略、建立固定数据目录、磁盘阈值和备份，并用管理员会话调用 `POST /api/v1/ecs-publisher/organizations/{organizationId}/runtimes`。管理员选择 OSS 时，再为该企业和环境一次性绑定同地域私有 Bucket、仅限 `apps/*` 的 RAM 身份和网关，然后运行真实验收探针。此后未来系统的前缀和项目令牌由 Runtime 自动产生；管理员无需预知或逐项目配置基础设施，只需在系统登记后到 SaaS 把系统、模块、页面和 Action 授权给已有平台角色，并设置该角色可查看的部门数据范围。换公司、换环境或换 Bucket 时必须重新做这一次企业级初始化。负责人不获得平台管理员 Token 或 OSS AccessKey。

## 失败处理

- 模板或说明歧义：修本 Skill、Schema、模板或验证脚本，再从空目录重测。
- 平台鉴权、Manifest、页面上下文、Action、事件或自动登记缺口：修 `ai-platform` 契约和测试，再重测。
- 平台尚未配置模型供应商、无法根据最终授权生成 AI 工具目录、无法签发 Action 凭证或无法记录调用审计：报告“平台 AI 底座待管理员处理”；不得转而把模型 Key 填进模块或让 AI 使用 SSH、数据库连接执行 CRUD。
- Manifest 缺少 Action `description` 或接口能力字段：停止登记并由业务 AI 补齐；缺少整个 `aiTool`、禁用场景、前置条件、副作用、示例、确认文案或字段说明时给出改进警告，但不阻断登记，也不要求管理员在 UI 中替系统逐条补写。
- DNS、HTTPS、Nginx、Docker 或服务器资源问题：修管理员初始化/直接部署流程，不把服务器特例硬编码进业务代码。
- 本地固定数据目录、权限、磁盘阈值或备份未验证：停止含文件能力的首次发布，修管理员底座；不得写入容器可写层或公开静态目录。
- 环境明确选择 OSS 但 Bucket、文件网关、系统前缀或签名权限未验证：停止发布或迁移，不得把 AccessKey 交给业务 AI，也不得静默改回本地模式。
- 已有 Runtime 必须走 `configure-oss-gateway` 原地升级，禁止重新运行 `provision_runtime.py` 或轮换 Runtime 登记凭证。已有 OSS release 时不得直接改 Bucket/地域，必须先走显式迁移。
- 新版本构建、启动或健康检查失败：保持数据目录，恢复上一健康 Git commit 对应的不可变镜像；禁止清空数据库解决问题。

## 冷启动角色与隔离

- 管理员 AI 只准备 Skill、企业档案、通配域名、ECS 运行底座和登记入口；不替业务 AI 编写业务流程。
- 每个业务 AI 只获得本 Skill、业务描述、自己的本地 Git 项目、目标域名和目标 ECS 的受控部署入口。不得读取另一业务 AI 的目录、服务器、日志或失败报告。
- 并行测试使用不同目录、容器、回环端口和数据目录；不能复制另一测试的代码、Secret 或数据库作为捷径。
- 管理员仅从公开端点、Docker/Nginx 状态和灼见登记结果进行黑盒验收。冷启动失败时先判断是 Skill、平台契约还是基础设施问题，不进入业务目录人工补写后宣称成功。

## 管理员接入回执

最终只向业务用户输出：系统名称与入口、子模块、开发/验收责任部门、建议权限组合、页面、AI 操作及确认要求、健康状态、灼见登记/同步状态、文件能力状态和尚缺外部条件。明确说明“页面和 Action 待管理员授权给 SaaS 中已有平台角色，并配置角色的部门数据范围”；只用“本地文件存储已就绪”“OSS 已就绪”或“文件存储待管理员处理”描述状态，绝不向负责人展示服务器路径、Bucket、对象前缀或密钥。
