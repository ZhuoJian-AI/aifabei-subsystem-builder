# 灼见原生模块接入协议 v2.4–2.5

`version` 始终是整数 `2`；兼容增强写入字符串 `contractRevision`。`skill-version.json` 中的 `skillVersion` 只表示 Skill 发布版本，禁止写入子系统 Manifest，也不得借 Skill 自动更新改写项目的 `contractRevision`。

新系统固定声明 `contractRevision="2.5"`。维护已有系统时先读取 `subsystem.json`，只接受当前 Skill 明确支持的 `2.4` 或 `2.5` 并保持原值；未知版本立即停止。`2.4 → 2.5` 必须是用户明确要求的独立迁移，同时完成 Runtime、四类凭证、一次性 SSO 短码和完整端点验收，禁止只改版本号。维护 2.4 时使用本节标注的兼容规则；其余正文默认描述 2.5。

v2.5 把 Manifest、SSO、Action 和 Event 的凭证拆开，并把 SSO 改为平台保存、模块后端单次兑换的短码；业务目录字段与 v2.4 保持兼容。v2.4 继续使用单个 `ZHUOJIAN_INTEGRATION_SECRET`：Manifest/事件拉取使用静态 Bearer，同一密钥签发短时 SSO、Action 和 Event JWT；`auth` 使用 `algorithm="HS256"`。v2.4 普通维护不强制补齐 v2.5 的四类凭证、`authorization_code` SSO、静默刷新 Bridge 或标准分页导出；若业务需要这些新保证，应走显式 2.5 迁移。

## 标识和边界

- `enterprise.key`：Alphabet 的正式稳定标识是 `alphabet`。`aifabei` 仅供已有 Runtime 兼容；新 Manifest 必须写 `alphabet`。
- `applicationSlug`：独立模块系统、域名和发布单元。
- `moduleKey`：业务大模块中的子模块标识。
- `pageKey`：子模块内稳定页面/工作上下文，也是最小可见边界。
- `actionKey`：系统内全局唯一业务命令，也是最小操作边界。
- `departments[]`：开发、协作、审批和验收责任目录；每个子模块恰好一个 owner 部门。部门责任不产生员工访问权限。
- `accessRoles[]`：所需的“页面 + Action”权限组合建议。它不是子系统的角色，不创建角色、不绑定用户、不决定部门数据范围；Manifest 只声明真实业务需要的最少组合。
- SaaS 的组织结构固定为“企业 → 部门 → 用户”，不存在 Team 授权范围。部门只表示组织归属、数据范围与资源所有权，不因员工属于某部门就自动获得该部门资源的写权限。
- 用户组织归属为单一 `departmentId`，可拥有多个平台角色。页面、Action、工作空间与 AI 能力均由绑定角色取并集；每次访问时，SaaS 先筛出确实授予当前资源的角色，再只合并这些角色的数据范围。未授予当前资源的角色不能扩大它的 `effectiveDataScope`。
- 跨部门待办、审批和业务流程属于具体子系统。SaaS 只做角色授权、应用嵌入、Action 鉴权与明确目标的系统间事件投递，不集中创建或展示业务待办。
- 稳定标识仅使用小写字母、数字、点、下划线和短横线，不随显示文案变化。

## 固定端点

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/health` | 无副作用健康检查 |
| GET | `/api/integration/manifest` | 模块、部门、页面、Action 和事件目录 |
| GET | `/api/integration/events?after=&limit=` | 模块到 SaaS 的顺序增量事件 |
| POST | `/api/integration/event-deliveries` | SaaS 到目标模块的签名事件投递 |
| POST | `/api/integration/actions/{actionKey}` | 页面和 AI 共用业务命令出口 |
| GET | `/api/integration/sso?code=&redirect=&launch_nonce=` | iframe 单次短码换模块会话 |

Runtime 为每个系统自动生成四类不可混用的凭证：`zjmf_` 只用于 Manifest 和事件拉取，`zjss_` 只用于模块后端兑换 SSO 短码，`zjac_` 只验证 Action JWT，`zjev_` 只验证事件投递 JWT。它们不得跨系统复用，也不得使用灼见全局 JWT 密钥。业务负责人不接触这些值；Runtime 登记时一次提交，SaaS 只保存必要的哈希或加密值。

模块部署时还必须配置 `ZHUOJIAN_ORGANIZATION_ID`。SSO 兑换结果、Action JWT 和事件 JWT 都必须同时校验 `aud=applicationSlug` 与 `organizationId=ZHUOJIAN_ORGANIZATION_ID`；不能只检查字段非空。

## 平台 AI 与模块边界

灼见 SaaS 是唯一的平台 AI 控制面，负责模型供应商注册和 API Key 保管、模型路由与额度、Manifest/Action 目录、用户最终授权计算、AI 工具选择、短时 Action JWT 签发、确认流程和审计。模型供应商密钥不得进入 Alphabet 模块的源码、镜像、运行环境、数据库或前端，也不得通过 SSO、Bridge、Action 请求或事件传给模块。模块只提供业务能力，不需要知道平台使用哪个模型供应商。

平台 AI 的目标地址必须由已登记的模块 `baseUrl` 和固定路径组成：

```text
POST <registered-baseUrl>/api/integration/actions/<registered-actionKey>
Authorization: Bearer <short-lived-zhuojian-action-jwt>
```

请求体只允许携带本协议定义的 `requestId/moduleKey/pageKey/operation/expectedVersion/params`。不得让模型、用户输入或 `params` 提供 URL、IP、容器端口、数据库连接、路由覆盖或凭证来改变调用目标。平台 AI 不使用 SSO Cookie 代替 Action JWT，不通过 SSH/root 登录执行 CRUD，也不直接连接模块数据库。

ECS 管理员只需在 Runtime 初始化时建立通配域名、HTTPS `443` 和 Nginx 受控反向代理基础。每个模块部署时自动增加自己的域名路由，将公开的网页与固定集成端点代理到该模块容器；不开放数据库端口、Docker API、容器回环端口或通用管理后端。完成 Runtime 初始化后，未来模块不需要管理员逐个新增防火墙端口或批准版本。Runtime 登记 `baseUrl` 并同步 Manifest；合法候选自动生效，新应用先只授予内置“系统研发者”，普通业务员工仍由管理员首次绑定业务角色。

子系统不得为了平台 AI 重复建设聊天入口或保存平台模型 Key。如果未来确有独立的 OCR、视觉识别等模块专用模型能力，那是另一个由管理员明确批准的基础设施能力；它仍不得复用平台模型密钥，也不得绕过 Action 权限、确认和审计执行用户业务 CRUD。

### Action 如何物化为 AI 工具

每个 Action 自带生成基础工具所需的信息，SaaS 不为每个系统维护另一份 Codex Skill。平台登记 Manifest 后按以下规则生成动态工具：

| 动态工具部分 | Manifest 来源 | 规则 |
|---|---|---|
| 稳定内部身份 | `applicationSlug + actionKey` | 全平台唯一；不能因显示名称变化而改变 |
| 工具显示名 | 应用名、模块名和 Action `name` | 平台可规范化为模型供应商允许的函数名 |
| 工具说明 | `description` | 当前 SaaS 直接使用 `description`生成基础工具；`aiTool` 作为模块侧增强说明保留，平台尚未消费时不影响接入 |
| 模型可填写参数 | `inputSchema` | 仅业务参数；字段说明强烈推荐 |
| 返回值说明 | `resultSchema` | 描述模块真实返回值；SaaS 在把结果交给模型或文件执行器前必须按此 Schema 校验 |
| 权限与风险 | 页面 `actionKeys`、`aiEnabled`、`requiresConfirmation`、平台授权 | 不进入模型可改参数 |
| HTTP 封装 | 平台登记目录 | URL、JWT、`requestId/moduleKey/pageKey/operation` 由平台填写 |

`inputSchema` 描述 Action 请求体中的 `params`，不是整个 HTTP 请求。模型只生成业务参数；平台生成 `requestId`，从登记目录确定应用、模块、页面、Action 和操作类型。AI 可执行的 `create/update/delete/approve` 必须列出真实业务 `properties`、必填的目标或业务字段，并设置 `additionalProperties=false`，禁止使用空对象 Schema 让模型猜参数。`update/delete/approve` 所需 `expectedVersion` 必须来自最近一次获授权查询或 Bridge 页面上下文；没有可信版本时先查询或要求用户刷新，禁止让模型猜测版本号。

本 Skill 对 v2.5 Action 的最小构建要求是“描述 + 真实接口能力”：每个 Action 提供非空 `description`，以及 `actionKey/operation/inputSchema/resultSchema/aiEnabled/requiresConfirmation`；其中 `inputSchema` 是根类型为 `object` 的 JSON Schema，`resultSchema` 是 JSON Schema 对象。查询应支持必要筛选和 `limit`，默认只返回足够完成任务的数据，每条可修改记录返回 `dataVersion`。删除和审批必须确认，确认内容必须显示具体目标与参数，不能让用户确认一个空目标。这样平台不需要管理员逐条写说明，就能把已授权 Action 生成可靠的基础 AI 工具。

所有 Action 的返回值都不得包含服务器文件路径、数据库路径、连接串或备份位置；这些内部信息既不能写入 `resultSchema`，也不能通过嵌套字段送入 SaaS 或模型。

### 导出 Action 的统一数据集

`operation=export` 只返回权限过滤后的有界分页数据，不在业务 ECS 生成文件、数据库备份或服务器路径。返回 Schema 必须封闭并精确包含：

```text
snapshotId   # 同一次导出所有分页保持不变
snapshotAt   # ISO 8601 快照时间
columns[]    # key / label / type
rows[]       # 当前页记录，只含 columns 声明的字段
rowCount     # 当前权限与筛选条件下的总记录数
nextCursor   # 下一页不透明游标；末页为 null
```

首次调用由模型填写业务筛选条件，`limit` 由平台和子系统共同限制；后续调用只携带原筛选、`snapshotId` 和 `nextCursor`。游标必须短时有效，并绑定组织、员工、应用、Action、权限范围及快照，不能把页码、SQL、文件路径或权限范围明文交给模型。SaaS 的可信执行器负责连续读取分页并生成 Excel、Word、PPT、PDF 或文本产物，模型不得复述或拼接大批量行。权限中途被撤回、Schema 不匹配、游标失效或快照变化时整次生成失败，且不得交付半成品。

`artifact` 只能由 SaaS 工作空间文件服务成功提交后产生，必须带稳定 `fileId/versionId/workspaceId/canonicalPath`、文件大小、SHA-256 及应用/模块/页面/Action/请求/快照来源。模型正文、Action 文本和服务器路径都不能推断成文件。用户要求生成或导出文件时，没有成功文件工具结果和有效文件身份就必须明确失败，不能用 Markdown 表格冒充交付。

### SaaS 文件执行器与格式边界

基于当前业务数据生成文件时，平台必须优先提供一个复合工具 `business_export_to_workspace_file`，并在服务端固定执行以下链路：校验员工和当前页面 Action → 连续读取同一分页快照 → 校验每页 `resultSchema` → 在一次性沙箱生成文件 → 重新打开并验证 → 提交当前员工获权的工作空间 → 持久化 `artifact` → 返回文件卡片 → 标记完成。复合工具启用时，不得同时向模型暴露“普通 Action 查询 + 普通文件创建”这条可绕开的组合。组织、员工、应用、模块、页面和 Action 身份都由平台注入，模型不能提交或修改这些身份。

平台文件能力由唯一的能力注册表决定，不能由子系统 Manifest 声称。新产物默认使用 `.xlsx/.docx/.pptx/.pdf/.md/.txt`；`.csv/.tsv` 只有用户明确要求时输出。旧格式 `.xls/.doc/.ppt` 及 `.xlsb/.xlsm/.ods/.et/.docm/.rtf/.odt/.wps/.pptm/.pps/.ppsx/.odp/.dps` 可以按平台实际启用能力读取、预览和转换；编辑前生成现代格式副本，不原地覆盖旧文件。用户明确要求旧格式时，先生成并验证现代格式，再真实转换成旧格式，只提交最终产物，禁止只改扩展名。

宏和外部主动内容永不执行，外部链接不访问；无法证明宏包完整保留时，平台只生成不含宏的现代格式副本并用中文说明。CSV/TSV 不承载样式、公式或多个工作表，多工作表转换时必须明确指定工作表。PDF 只承诺生成、读取/OCR、合并、拆分、抽页和转换，不得宣称可以无损编辑任意 PDF 版式。Markdown 使用 CommonMark；大文本分页读取，不得静默截断。

模型只生成结构化内容和严格工具参数，真实文件由 SaaS 可信执行器产生。平台文件工具按操作拆分为 `*_create/inspect/edit/convert`（PDF 另含 `merge/split/extract`），嵌套字段必须是实际数组或对象，不能把 `sheets/slides/operations` 序列化成 JSON 字符串。模型供应商支持严格工具 Schema 时使用严格模式；无论供应商是否支持，SaaS 都必须再次校验并拒绝额外字段。

生成、转换和编辑必须在一次性沙箱内完成，并至少校验：非空和大小限制、扩展名/MIME/文件头一致、OOXML 包结构、Excel 工作表、Word 主要段落与表格、PPT 幻灯片数、PDF 页数和可渲染性、文本声明编码以及 SHA-256。宏、路径穿越、压缩炸弹和主动内容必须在提交前拦截。编辑已有文件必须带稳定 `fileId + baseVersionId + idempotencyKey`；版本变化时返回中文冲突，不得覆盖新版本。临时文件在运行结束后清理，子系统 ECS 不保存 SaaS AI 的临时文件或最终文件。

`artifact` 事件顺序固定为：工具完成 → 文件验证 → 工作空间提交 → `artifact` → assistant message → `done`。文件型请求只有 `artifacts` 非空才可标记 `completed`；Action 成功而文件失败时整轮失败。`runId + toolCallId` 是副作用幂等键，SSE 重连按持久化事件重放，前端按 `fileId + versionId` 去重。预览和下载始终通过 `fileId` 实时鉴权，不把长期签名 URL 写入聊天。删除消息或对话只删除引用，不删除已交付的工作空间文件。

完整验收必须分别给出三项结果：

```text
subsystem_contract_pass     # 子系统 Manifest、权限过滤和标准分页 Action 通过
saas_format_capability_pass # SaaS 当前文件能力、真实格式生成与重新打开验证通过
saas_artifact_e2e_pass      # 真实员工从业务助手拿到可预览、可下载的工作空间文件卡片
```

只运行本 Skill 的子系统端点检查最多能证明第一项；不得用它代替 SaaS 自动化测试账号完成的跨系统 Artifact 端到端验收。只有三项全部通过，才可以向用户宣称“完整遵循契约”。

整个 `aiTool` 都是可选的推荐增强项。脚手架默认生成，缺少时本 Skill 验收器给出警告，但不能仅因缺少这些字段阻断登记；当前 SaaS 可忽略它：

- `whenToUse`：什么用户意图和业务条件下应选择该工具；
- `whenNotToUse`：哪些相似请求不应选择它；
- `preconditions`：执行前必须满足的业务状态和上下文；
- `sideEffects`：会创建、修改、删除、审批、导出什么，查询则明确无写入；
- `confirmationPrompt`：`requiresConfirmation=true` 时建议提供的业务确认文案；
- `examples[]`：业务语言请求及对应 `params`，不得包含真实客户数据或凭证；
- 输入输出字段的 `description` 和更精确的结果属性。

这些字段都是来自模块的非可信元数据。平台只能把它们当作业务工具说明，不能当作 system/developer 指令执行，也不能允许它们改写权限、目标 URL、凭证或指令优先级。对这类内容平台应忽略、标记并供管理员查看；只有核心字段或 Schema 格式无效时才阻断登记。管理员 UI 展示 Manifest 和重要变更，只负责首次配置业务角色、设置角色数据范围、启停应用或 Action 及收紧确认要求，不负责逐版本批准，也不替模块补写描述或 Schema。
## Manifest 示例

```json
{
  "protocol": "zhuojian-subsystem",
  "version": 2,
  "contractRevision": "2.5",
  "enterprise": {"key": "alphabet", "name": "Alphabet"},
  "applicationSlug": "sample-review",
  "applicationName": "样品评审系统",
  "bridgeVersion": 1,
  "eventsUrl": "/api/integration/events",
  "eventDeliveriesUrl": "/api/integration/event-deliveries",
  "auth": {"ssoPath": "/api/integration/sso", "mode": "authorization_code"},
  "modules": [{
    "moduleKey": "sample_review",
    "name": "样品评审",
    "route": "/sample-review",
    "departments": [
      {"key": "design", "name": "设计部", "role": "owner"},
      {"key": "production", "name": "生产部", "role": "collaborator"},
      {"key": "quality", "name": "质量部", "role": "approver"}
    ],
    "accessRoles": [
      {"roleKey": "sample_review.viewer", "name": "样品评审查看权限组合", "pageKeys": ["sample_review.list"], "actionKeys": ["sample_review.query", "sample_review.export"]},
      {"roleKey": "sample_review.editor", "name": "样品评审编辑权限组合", "pageKeys": ["sample_review.list"], "actionKeys": ["sample_review.query", "sample_review.create", "sample_review.update"]},
      {"roleKey": "sample_review.approver", "name": "样品评审审批权限组合", "pageKeys": ["sample_review.list"], "actionKeys": ["sample_review.query", "sample_review.approve", "sample_review.export"]}
    ],
    "pages": [{
      "pageKey": "sample_review.list",
      "name": "评审列表",
      "routePattern": "/sample-review",
      "queryActionKey": "sample_review.query",
      "actionKeys": ["sample_review.query", "sample_review.create"],
      "contextSchema": {
        "type": "object",
        "properties": {"filters": {"type": "object"}, "selection": {"type": "object"}}
      },
      "aiSemantics": {
        "purpose": "查看和筛选样品评审记录。",
        "primaryEntities": ["sample_review"],
        "fieldSemantics": [{"field": "status", "meaning": "当前评审状态"}],
        "supportedIntents": ["说明本页用途", "查询样品评审记录"],
        "relatedPages": [],
        "businessTerms": [],
        "defaultQueryActionKey": "sample_review.query"
      }
    }],
    "actions": [{
      "actionKey": "sample_review.query",
      "name": "查询评审",
      "description": "查询当前用户权限范围内的样品评审记录，返回匹配条件的评审摘要和版本号。",
      "operation": "query",
      "aiEnabled": true,
      "requiresConfirmation": false,
      "aiTool": {
        "whenToUse": "用户需要查看、筛选或核对样品评审记录时使用。",
        "whenNotToUse": "用户要求新增、修改、删除或审批样品评审时不要使用。",
        "preconditions": ["用户已获得样品评审列表页面和查询 Action 权限。"],
        "sideEffects": "只读取样品评审数据，不产生业务写入。",
        "examples": [{
          "userRequest": "查看所有待评审的样品",
          "params": {"status": "reviewing"}
        }]
      },
      "inputSchema": {
        "type": "object",
        "additionalProperties": false,
        "properties": {
          "status": {"type": "string", "description": "评审状态筛选值，例如 reviewing"},
          "limit": {"type": "integer", "minimum": 1, "maximum": 500}
        }
      },
      "resultSchema": {
        "type": "object",
        "additionalProperties": false,
        "description": "样品评审查询结果",
        "properties": {
          "items": {"type": "array", "description": "匹配权限和筛选条件的评审摘要"}
        }
      }
    }],
    "events": {
      "publishes": ["design.sample_review.approved.v1"],
      "subscribes": []
    }
  }]
}
```

完整结构以 `schemas/manifest-v2.schema.json` 为准。部门声明只描述谁负责开发、协作和验收；`accessRoles` 是建议权限组合，不是模块角色，也不是模块自行颁发的权限。企业管理员参考该组合首次配置 SaaS 业务角色和部门数据范围。Runtime 同步通过后，内置“系统研发者”自动获得本企业、同一 SaaS 环境中该 Runtime 应用的完整页面、Action 和业务数据访问；它不包含 SaaS 管理后台、模型密钥、工作空间管理或跨企业权限。既有业务角色只在该应用现有授权上限内继承新增模块、页面和 Action；新应用不会自动授权普通业务角色。管理员明确移除的资源、停用的应用或 Action 必须跨后续同步保持，删除的 Manifest 资源立即从生效目录和授权中移除。事件声明只更新目录，不能自动创建投递路由。

## Runtime 候选与自动生效

Runtime 发布必须把“候选版本”和“生效版本”分开。首次登记只有在域名、企业身份、健康、凭证和完整 Manifest 校验全部通过后才激活应用；后续登记期间旧健康版本保持员工可用。候选失败只记录失败原因，不覆盖旧 Manifest、旧生效 commit、现有授权或管理员停用状态。

同一应用同一 commit 的重复登记必须幂等；多个候选并发时采用 latest-wins，只有当前 `requestedCommit` 对应候选能提交。较新的候选可以取代仍在验证的旧候选，迟到响应必须返回冲突，不能回写生效版本。合法候选激活时一次性更新 Manifest、Action 目录、Runtime 托管授权和生效 commit；任何一步失败都不允许出现半激活状态。

## 角色授权模型

平台员工只有一个主部门，但可以有多个角色。一个角色保存完整权限树：

```text
applicationSlug
└─ moduleKey
   └─ pageKey: view
      └─ actionKey: query/create/update/delete/export/approve
```

员工最终权限仍是角色并集，但数据范围不是全局一次合并：SaaS 对当前资源逐个筛选授权角色，再生成该资源的 `effectiveDataScope`（`unrestricted/include_self/own_only/department_ids`）。`departments[].role=owner` 只表示责任，不能替代授权。页面 `view` 与 Action 分开；模块的查询、导出、修改、删除和审批都必须执行本次收到的数据范围。旧系统可暂时按兼容模式接入，但必须清楚标记。

“系统研发者”是 SaaS 内置且系统托管的员工角色，稳定可见编码为 `zj-runtime-developer`。管理员只能把它绑定或解绑员工，不能改名、删除、停用或修改权限；平台不得把它实现成管理员账号。该角色对本企业 Runtime 管理应用使用不受部门限制的业务数据范围，但不能借此进入 SaaS 管理端、读取模型供应商密钥、管理工作空间或访问另一企业。

旧数据没有可用的负责部门或创建人字段时，不能仅新增空列就切换数据范围。升级必须先生成未归属清单，由业务负责人确认归属后回填，验证不同角色的允许与拒绝，再启用新契约。

## 双层鉴权

### iframe SSO

灼见先检查 `moduleKey` 的 `view` 权限，保存绑定用户会话、应用、模块、页面、`redirect`、`launch_nonce` 与 `auth_epoch` 的 120 秒单次短码，浏览器只把短码带到模块。模块后端使用自己的 `zjss_` 凭证 POST `/api/v1/subsystem-sso/exchange`；SaaS 原子消费后返回最终 claims。模块校验应用、企业、模块、跳转路径、nonce、有效期和页面/Action allowlist，把完整 claims 保存在服务端，只给浏览器设置短小的 `HttpOnly; Secure; SameSite=None; Partitioned` 会话标识，再 302 到不含短码的页面。短码只存哈希，刷新 iframe 必须重新签发。

SSO 兑换结果包含 SaaS 针对当前子模块计算的最终页面、操作 allowlist 和数据范围。模块不得用部门责任、`accessRoles` 建议值或用户本人的 `departmentId` 替代：

```json
{
  "roleIds": ["platform-role-id"],
  "effectiveDataScope": {
    "unrestricted": false,
    "include_self": false,
    "own_only": false,
    "department_ids": ["department-id"]
  },
  "pageKeys": ["sample_review.approval"],
  "actionKeys": ["sample_review.query", "sample_review.approve", "sample_review.export"],
  "pageAccess": {
    "sample_review.approval": {
      "permissions": ["view", "ai_query", "ai_approve", "export"],
      "actionKeys": ["sample_review.query", "sample_review.approve", "sample_review.export"],
      "dataScopes": {
        "view": {"unrestricted": false, "include_self": false, "own_only": false, "department_ids": ["department-id"]},
        "ai_query": {"unrestricted": false, "include_self": false, "own_only": false, "department_ids": ["department-id"]},
        "ai_approve": {"unrestricted": false, "include_self": true, "own_only": true, "department_ids": []},
        "export": {"unrestricted": false, "include_self": false, "own_only": false, "department_ids": ["department-id"]}
      },
      "actionDataScopes": {
        "sample_review.query": {"unrestricted": false, "include_self": false, "own_only": false, "department_ids": ["department-id"]},
        "sample_review.approve": {"unrestricted": false, "include_self": true, "own_only": true, "department_ids": []},
        "sample_review.export": {"unrestricted": false, "include_self": false, "own_only": false, "department_ids": ["department-id"]}
      }
    }
  }
}
```

模块必须把 allowlist 保存到安全会话，只向前端返回允许的页面与按钮；服务端路由、页面 Action 和页面上下文也必须逐次校验 `pageAccess`。页面读取使用 `dataScopes.view`，页面操作使用对应权限的 `dataScopes`，具体 Action 必须使用同名 `actionDataScopes`，不得把一个角色的宽数据范围拼到另一个角色的操作权限上。伪造 URL、前端显示错误或隐藏按钮均不能绕过服务端检查。

### Action

Action JWT 使用该系统专属 `zjac_` 密钥和 `typ=zhuojian-action`，至少包含用户、企业、`departmentId`、`departmentIds`、`roleIds`、当前 Action 的 `effectiveDataScope`、`moduleKey`、`pageKey`、`actionKey`、`operation`、`requestId` 和权限。模块不信任请求体中的身份或范围字段，并再次验证模块、页面、Action 和业务数据权限。

请求体：

```json
{
  "requestId": "stable-idempotency-id",
  "moduleKey": "sample_review",
  "pageKey": "sample_review.list",
  "operation": "query",
  "expectedVersion": null,
  "params": {"status": "reviewing"}
}
```

`requestId` 固定为平台生成的 8–128 位安全字符标识，首位为字母或数字，其余只允许字母、数字、点、下划线、冒号和短横线。子系统模板、Schema 与平台生成器必须使用同一限制，不能因拼接 Task、运行和工具名称而产生超长请求。

`update`、`delete` 和 `approve` 必须携带 `expectedVersion`，与当前实体版本不一致时返回 HTTP 409。模块按 `applicationSlug + requestId` 幂等保存结果，页面按钮和 Action HTTP 入口调用同一应用服务函数。

### 高风险确认

`requiresConfirmation=true` 的 Action 只有在 JWT 同时包含以下声明时执行：

```json
{
  "confirmed": true,
  "confirmationId": "uuid",
  "confirmedBy": "user-id",
  "confirmedAt": "RFC3339",
  "paramsHash": "sha256-canonical-json",
  "requestId": "same-request-id"
}
```

模块重新计算参数哈希，校验确认人与当前用户一致、确认未过期、requestId 匹配，并保证 `confirmationId` 只能消费一次。

## 应用语义地图与业务助手编排

每个 v2.5 AI 页面必须声明 `aiSemantics`。它是供 SaaS 结构化控制器使用的应用语义地图，只描述页面用途和关系，不是提示词，也不产生权限：

```json
{
  "aiSemantics": {
    "purpose": "汇总当前订单进度、交期风险和待处理问题。",
    "primaryEntities": ["production_order"],
    "fieldSemantics": [
      {"field": "due_date", "meaning": "客户承诺交期"}
    ],
    "supportedIntents": [
      "说明本页用途",
      "按明确条件查询风险订单",
      "基于当前权限数据生成文件"
    ],
    "relatedPages": [
      {
        "moduleKey": "factory_progress",
        "pageKey": "factory_progress.main",
        "relationship": "在本页发现工厂节点风险后，到工厂进度监测页查询或处理。"
      }
    ],
    "businessTerms": [
      {"term": "催办", "meaning": "推动当前节点负责人处理临期或逾期任务"}
    ],
    "defaultQueryActionKey": "progress_dashboard.query"
  }
}
```

- `purpose` 只说明页面解决什么问题；不得放模型提示、身份、权限结论、密钥或整表数据。
- `primaryEntities` 使用稳定业务实体标识；`fieldSemantics` 和 `businessTerms` 解释容易混淆的字段与术语。
- `supportedIntents` 是可回答问题的业务描述，不是关键词表，子系统不得据此自行选择模型或工具。
- `relatedPages` 必须指向同一 Manifest 内真实页面并说明业务关系；它只允许 SaaS 产生导航或候选只读查询，不自动扩权。
- `defaultQueryActionKey` 必须是本页 `actionKeys` 中唯一优先的 query Action，避免模型在多个含义相近工具之间猜测。

SaaS 每轮根据当前登录用户、`auth_epoch`、应用、页面、Bridge 上下文、实时授权 Action 和目标工作空间生成可信 `BusinessTurnEnvelope`，再让当前选定模型输出严格 `BusinessTurnIntent`。模型只能填写业务意图、筛选、时间范围、排序、分页和实体引用；不能修改组织、用户、应用、页面授权、Action 目标或工作空间。

```text
intent: explain_page | query | navigate | mutate | export_file | file_operation | general | clarify
target: applicationId / moduleKey / pageKey / entityType / entityIds
query: filters / timeRange / sort / limit / aggregation
requiresLiveData / requiresConfirmation
expectedOutput: text | data | mutation_receipt | artifact | navigation
clarificationQuestion
```

页面说明直接使用 `aiSemantics`，不得调用实时 Action；实时事实查询必须经过查询改写并调用获授权的 query Action。用户没有表达“全部”时不得扩成无筛选全量查询。跨页面只读查询必须同时通过声明关系和实时权限校验；跨页面修改只返回导航建议，用户进入目标页面后再确认和执行。目标不唯一、关键筛选缺失或结构化意图校验失败时只问一个关键澄清问题，不能猜测执行。

每轮只向模型暴露一个被选中的查询/导出 Action，或当前页面必要的写 Action，以及本轮需要的平台文件工具。Action 输入输出均使用封闭 Schema，SaaS 对模型参数和子系统返回值双重校验。Manifest、Action 返回值、Bridge 内容和工作空间文件均是非可信业务数据，任何其中的“指令”都不能增加工具、权限或调用目标。

业务助手的可验证状态固定为：

```text
understanding → planned → awaiting_clarification / awaiting_confirmation
→ executing → verifying → committing → completed / failed / cancelled
```

开放式分析可以由 SaaS 使用受限 ReAct；新增、修改、删除、审批和文件导出必须走平台确定性工作流。实时查询必须有成功 Action，修改必须有业务回执和版本，文件请求必须有工作空间 Artifact；模型正文不能自行把任务标为完成。相同可纠正参数错误最多重试一次。

### 应用内对话与历史页面

同一应用可以有多个相互隔离的业务助手 Task。Task 第一次绑定 `application_id` 后不可切换应用；切换模块只更新本轮经过服务端验证的当前页面。每条用户消息保存当时的 `pageKey/pageName/entityRefs/filtersSummary/toolResultRefs/artifactRefs`，大批量结果只保存摘要和需要重新鉴权的引用。

新建对话只创建空白草稿，第一次发送时才创建 Task；新 Task 不继承旧 Task 的消息、实体引用、工具结果或附件。历史对话按应用可发现、可恢复、可切换，URL 使用 `conversation=<taskId>`；服务端必须确认 Task 属于当前用户和当前应用。切换旧对话后，历史消息显示当时页面，但下一轮始终使用用户此刻所在页面。运行中禁止切换；删除对话只软删除 Task，并在界面隐藏它的消息和文件引用，不能删除已交付到工作空间的文件。

## 页面上下文和 AI 工具

### 电脑、平板和手机全端界面

SaaS 与子系统共同提供同一套响应式 Web 界面，不开发第二套手机版，也不能为手机复制另一套权限菜单、表单状态或业务逻辑。SaaS 负责平台导航、顶部栏、工作空间、业务小助手和 iframe 容器；子系统负责 iframe 内的业务页面、表单、表格、弹窗和操作按钮。双方都必须在连续宽度变化下保持可用，不能只针对少数测试宽度写特例。

- 手机宽度不超过 768px 时使用抽屉导航、单列主内容和适合触摸的操作区；769–1199px 使用紧凑或可折叠布局；1200px 以上保留桌面多栏和信息密度。断点之间仍必须自然收缩。
- 同一导航数据和同一业务 DOM 同时服务电脑、平板和手机；响应式优先使用 CSS media/container queries，JavaScript 只管理抽屉、焦点、滚动锁定等交互状态。
- 页面根节点不得出现非预期横向滚动。表格、权限矩阵、画布等确实需要宽度的内容可以在自己的容器内横向滚动，并保留表格语义、固定首列或滚动提示；导航、表单和主要按钮不得依赖横向滚动。
- 页面必须支持鼠标、键盘和触摸。必要操作不能只在 hover 时出现；主要触摸目标至少 44px，普通可点击目标不得小于 24px。抽屉和弹窗支持遮罩、显式关闭、Escape/浏览器返回、焦点约束和背景滚动锁定。
- 页面 viewport 必须包含 `width=device-width, initial-scale=1, viewport-fit=cover`。手机弹窗使用视口内的全屏或底部面板；输入区适配 `100dvh`、`100vh` 回退、`env(safe-area-inset-*)`、`VisualViewport` 变化和软键盘。旋转、浏览器前进后退、窗口缩放、200% 字体/页面缩放不得丢失表单、对话、Artifact 或未提交输入。
- 长应用名、模块名、文件名和中文错误需要换行或省略展示，并提供可访问的完整名称。iframe 高度跟随可视区域，避免平台和子系统同时产生整页滚动条，也不得因尺寸变化卸载 iframe 或重复请求。
- 子系统必须同时支持独立访问和嵌入 SaaS。独立访问保留自己的必要导航；`data-zhuojian-embedded` 生效时隐藏重复的系统级导航，只保留当前业务内容，不能出现第二套侧栏。
- 尊重 `prefers-reduced-motion`。响应式修改不得降低桌面端既有导航、信息密度、多栏、表格和业务流程的可用性。

真实验收覆盖全部 Manifest 页面，并至少包含 320×568、390×844、844×390、768×1024、1024×768、1280×720、1440×900 和 1920×1080；Chromium 覆盖桌面及 Android Chrome 行为，WebKit 覆盖 iPhone/iPad Safari 的动态高度、安全区和软键盘，桌面 Firefox 做基础回归。每页同时验收独立与嵌入模式、根节点溢出、菜单/返回/弹窗/表单/主要按钮、局部表格滚动、旋转、前进后退和 200% 缩放，并保存关键视口截图。任何员工页面在手机上不能完成对应桌面业务流程，或桌面因响应式修改退化，都必须阻断发布。

iframe 完成 SSO 会话恢复后，先发送一次绑定 `application_slug + launch_nonce` 的 `zhuojian:ready`，并在模块、页面、实体、筛选或选中项变化后发送同样绑定本次启动 nonce 的 `zhuojian:context`。发送方的 `targetOrigin` 必须来自已验证的 HTTPS 灼见来源，禁止使用 `"*"` 或用户可控查询参数。平台接收方还必须同时验证 `event.origin` 等于该应用登记来源、`event.source` 等于当前 iframe、`launch_nonce` 等于本次启动值，并校验消息类型、版本、应用、模块、页面和字段大小。浏览器的 iframe `load` 事件可能来自错误页，不能单独作为应用可用的证据：

```json
{
  "type": "zhuojian:ready",
  "version": 1,
  "application_slug": "sample-review",
  "launch_nonce": "本次 SSO 启动 nonce"
}
```

```json
{
  "type": "zhuojian:context",
  "version": 1,
  "enterprise_key": "alphabet",
  "application_slug": "sample-review",
  "launch_nonce": "本次 SSO 启动 nonce",
  "module_key": "sample_review",
  "page_key": "sample_review.list",
  "page_name": "评审列表",
  "route": "/sample-review",
  "entity_type": "sample_review",
  "entity_id": null,
  "filters": {"status": "reviewing"},
  "selection": {},
  "data_version": null
}
```

平台提供给 AI 的工具集合必须是：用户有效授权 ∩ 企业/应用 ∩ `moduleKey` ∩ `pageKey` ∩ 页面 `actionKeys` ∩ Manifest `aiEnabled` ∩ 管理员启用 Action。Bridge 不包含 Token、Cookie、密码、内部路径或整表数据。

业务助手只有在可信 `tool_result` 证明 `create/update/delete/approve` 已真实完成后，才向当前 iframe 发送局部刷新；普通问答、查询和文件导出不刷新。请求和结果都绑定应用、模块、页面、本次启动 nonce 和请求号：

```json
{
  "type": "zhuojian:refresh",
  "version": 1,
  "application_slug": "sample-review",
  "launch_nonce": "本次 SSO 启动 nonce",
  "module_key": "sample_review",
  "page_key": "sample_review.list",
  "request_id": "uuid"
}
```

```json
{
  "type": "zhuojian:refresh-result",
  "version": 1,
  "application_slug": "sample-review",
  "launch_nonce": "本次 SSO 启动 nonce",
  "module_key": "sample_review",
  "page_key": "sample_review.list",
  "request_id": "同一个 uuid",
  "status": "completed | deferred | failed",
  "data_version": "optional",
  "error": "失败时的简短中文原因"
}
```

子系统必须复用当前模块的数据加载函数，不得调用 `location.reload()` 或重建自身页面，不弹“已刷新”提示；并发请求合并为一次加载。存在未保存编辑时返回 `deferred` 并保留用户输入。SaaS 对来源、当前 iframe、应用、模块、页面、nonce 和请求号逐项校验；旧子系统不响应或超时时，平台只可在隐藏 iframe 完成新 SSO 和有效 `ready/context` 后原子替换，失败时继续保留旧页面。刷新期间业务助手抽屉、对话、滚动位置和输入内容必须保持挂载。

业务小助手不得混入其他应用、旧版 Skill、旧域名或长期记忆工具。用户询问当前、实时、数量、进度、异常或待办时，本轮必须有当前页面查询 Action 成功返回；调用失败就明确说明暂时无法确认，不得拿页面缓存、历史回答或记忆冒充实时结果。真实员工端验收还要核对本轮工具明细，出现越权工具或失败工具即不通过。

用户能在页面执行某个 Action，不代表 AI 自动拥有它。只有上述交集仍包含该 Action，且平台 AI 策略允许时，平台才可向模型暴露该工具并为本次调用签发 Action JWT。模块收到请求后必须再次验证 JWT 与 URL 中 `actionKey`、请求体中的模块/页面/操作完全一致；任何不一致都拒绝，不能因为请求来自灼见域名或通过 Nginx 就跳过鉴权。

## 事件

模块到 SaaS 的事件必须有严格递增且跨数据库重建不回退的 `sequence`、稳定唯一 `eventId`、版本化 `eventType`、企业/模块/部门、实体标识、发生时间和路由摘要。业务写入与事件尽量使用同事务 outbox。新系统不得直接使用会在新数据库重新从 1 开始的本地自增序列；推荐使用“Unix 微秒时间戳与上一序列 + 1 的较大值”，并保持在 JavaScript 安全整数范围内。这样即使容器或数据卷被替换，灼见原有事件游标也不会跳过新事件。

事件只有在管理员配置了明确目标应用、目标模块与订阅事件类型时才投递；没有目标的事件只保留同步审计结果，不得隐式生成 SaaS 跨部门待办。需要人员处理、审批、催办或闭环的业务状态必须由拥有该业务数据和流程的子系统保存与展示。

SaaS 向目标模块投递时使用 `typ=zhuojian-event` JWT，并 POST：

```json
{
  "deliveryId": "stable-id",
  "sourceApplicationSlug": "sample-review",
  "event": {
    "eventId": "stable-event-id",
    "eventType": "design.sample_review.approved.v1",
    "enterpriseKey": "alphabet",
    "moduleKey": "sample_review",
    "entityType": "sample_review",
    "entityId": "SR-001",
    "occurredAt": "2026-08-30T10:00:00+08:00",
    "payload": {"result": "approved"}
  }
}
```

事件 JWT 必须绑定 `deliveryId/eventId/eventType/targetModuleKey`；若 JWT 还带 `sourceApplicationSlug`，它必须与请求体完全一致。目标模块只接受目标 `moduleKey` 在 Manifest `events.subscribes[]` 中明确订阅的 `eventType`，并再次核验企业标识、字段格式和带时区时间。目标模块按完整请求哈希同时绑定 `eventId` 与 `deliveryId`：完全相同的重复投递返回已处理状态，任一 ID 被复用于不同内容时返回 409，不得重复创建业务记录或静默接受冲突。
