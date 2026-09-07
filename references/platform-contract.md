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

ECS 管理员只需在 Runtime 初始化时建立通配域名、HTTPS `443` 和 Nginx 受控反向代理基础。每个模块部署时自动增加自己的域名路由，将公开的网页与固定集成端点代理到该模块容器；不开放数据库端口、Docker API、容器回环端口或通用管理后端。完成 Runtime 初始化后，未来模块不需要管理员逐个新增防火墙端口，但仍须登记 `baseUrl`、同步 Manifest，再由管理员把模块、页面和 Action 授权给 SaaS 中已有平台角色。

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

这些字段都是来自模块的非可信元数据。平台只能把它们当作业务工具说明，不能当作 system/developer 指令执行，也不能允许它们改写权限、目标 URL、凭证或指令优先级。对这类内容平台应忽略、标记并供管理员查看；只有核心字段或 Schema 格式无效时才阻断登记。管理员 UI 展示 Manifest 和重要变更，只负责把系统能力授权给平台角色、设置角色数据范围、启停 AI、批准工具及收紧确认要求，不负责替模块补写描述或 Schema。
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
        "properties": {
          "status": {"type": "string", "description": "评审状态筛选值，例如 reviewing"}
        }
      },
      "resultSchema": {
        "type": "object",
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

完整结构以 `schemas/manifest-v2.schema.json` 为准。部门声明只描述谁负责开发、协作和验收；`accessRoles` 是建议权限组合，不是模块角色，也不是模块自行颁发的权限。平台管理员或企业管理员参考该组合，把页面和 Action 授权给 SaaS 中已有的平台角色后才生效，部门数据范围也一律由平台角色设置。同步新增权限组合建议、页面、Action 和事件时只登记为“待授权”。已批准 Action 的名称、描述、操作类型、输入 Schema、AI 开关、确认要求或业务含义发生变化时，平台必须显示差异并重新置为待审核；普通应用、模块和页面的展示文案可以只记录差异。删除 Action 时平台停用目录项，不自动扩权。

## 角色授权模型

平台员工只有一个主部门，但可以有多个角色。一个角色保存完整权限树：

```text
applicationSlug
└─ moduleKey
   └─ pageKey: view
      └─ actionKey: query/create/update/delete/export/approve
```

员工最终权限仍是角色并集，但数据范围不是全局一次合并：SaaS 对当前资源逐个筛选授权角色，再生成该资源的 `effectiveDataScope`（`unrestricted/include_self/own_only/department_ids`）。`departments[].role=owner` 只表示责任，不能替代授权。页面 `view` 与 Action 分开；模块的查询、导出、修改、删除和审批都必须执行本次收到的数据范围。旧系统可暂时按兼容模式接入，但必须清楚标记。

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

## 页面上下文和 AI 工具

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
