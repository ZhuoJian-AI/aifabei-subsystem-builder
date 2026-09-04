# 灼见原生模块接入协议 v2.4

`version` 始终是整数 `2`；兼容增强写入字符串 `contractRevision`。当前灼见 SaaS 已支持到 `2.4`，本 Skill 的新建和升级系统固定声明 `contractRevision="2.4"`，不自行发明更高版本号。本 Skill 在 v2.4 可兼容字段上要求 Action 提供业务描述和接口能力，供 SaaS 生成基础 AI 工具；其余说明字段用于提升模型选择和调用质量，不作为接入门槛。

## 标识和边界

- `enterprise.key`：企业稳定标识，Alphabet 沿用 `aifabei`，不因显示名称变更而改写。
- `applicationSlug`：独立模块系统、域名和发布单元。
- `moduleKey`：业务大模块中的子模块标识。
- `pageKey`：子模块内稳定页面/工作上下文，也是最小可见边界。
- `actionKey`：系统内全局唯一业务命令，也是最小操作边界。
- `departments[]`：开发、协作、审批和验收责任目录；每个子模块恰好一个 owner 部门。部门责任不产生员工访问权限。
- `accessRoles[]`：v2.4 所需的“页面 + Action”权限组合建议。它不是子系统的角色，不创建角色、不绑定用户、不决定部门数据范围；管理员只把其中的页面和 Action 授权给 SaaS 中已有的平台角色。当前管理员界面要求处理每个建议，因此 Manifest 只声明真实业务需要的最少权限组合，不预制猜测性角色。
- 用户组织归属为单一 `departmentId`，可拥有多个平台角色。SaaS 把全部有效角色合并为 `applicationSlug/moduleKey/pageKey/actionKey` 权限树和 `effectiveDataScope`。后者表示该用户可查看全部数据、本人数据或哪些部门的数据。跨部门协作通过增加角色实现，不通过多部门成员关系、部门直授或个人直授实现。
- 稳定标识仅使用小写字母、数字、点、下划线和短横线，不随显示文案变化。

## 固定端点

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/health` | 无副作用健康检查 |
| GET | `/api/integration/manifest` | 模块、部门、页面、Action 和事件目录 |
| GET | `/api/integration/events?after=&limit=` | 模块到 SaaS 的顺序增量事件 |
| POST | `/api/integration/event-deliveries` | SaaS 到目标模块的签名事件投递 |
| POST | `/api/integration/actions/{actionKey}` | 页面和 AI 共用业务命令出口 |
| GET | `/api/integration/sso?ticket=&redirect=` | iframe 一次性票据换模块会话 |

每个模块系统只有一个独立的 `ZHUOJIAN_INTEGRATION_SECRET`：Manifest 和事件拉取把它作为静态 Bearer Token；SSO 使用 120 秒、严格一次性的 HS256 JWT，Action 和事件投递使用 60 秒 HS256 JWT。SSO Ticket 消费后必须立即建立模块会话并重定向到不含 Ticket 的业务路由；平台刷新或重新挂载 iframe 时必须重新签发，禁止缓存或复用启动 URL。这个密钥只属于当前模块系统，不得跨系统复用，也不得复用灼见全局用户 JWT 密钥。管理员接入界面因此只需要填写一次“接入凭证”。

模块部署时还必须配置 `ZHUOJIAN_ORGANIZATION_ID`，并把它绑定到灼见中该企业的真实 organization UUID。所有 SSO、Action 和事件投递 JWT 都必须同时校验 `aud=applicationSlug` 与 `organizationId=ZHUOJIAN_ORGANIZATION_ID`；不能只检查“organizationId 非空”。

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
| 返回值说明 | `resultSchema` | 描述模块返回值，供模块验收和后续平台增强使用；当前 SaaS 动态工具不依赖它做返回值校验 |
| 权限与风险 | 页面 `actionKeys`、`aiEnabled`、`requiresConfirmation`、平台授权 | 不进入模型可改参数 |
| HTTP 封装 | 平台登记目录 | URL、JWT、`requestId/moduleKey/pageKey/operation` 由平台填写 |

`inputSchema` 描述 Action 请求体中的 `params`，不是整个 HTTP 请求。模型只生成业务参数；平台生成 `requestId`，从登记目录确定应用、模块、页面、Action 和操作类型。`update/delete` 所需 `expectedVersion` 必须来自最近一次获授权查询或 Bridge 页面上下文；没有可信版本时先查询或要求用户刷新，禁止让模型猜测版本号。

本 Skill 对 v2.4 Action 的最小构建要求只有“描述 + 接口能力”：每个 Action 提供非空 `description`，以及 `actionKey/operation/inputSchema/resultSchema/aiEnabled/requiresConfirmation`；其中 `inputSchema` 是根类型为 `object` 的 JSON Schema，`resultSchema` 是 JSON Schema 对象。这不改变协议版本，只保证平台不需要管理员逐条写说明，就能把已授权 Action 生成基础 AI 工具。

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
  "contractRevision": "2.4",
  "enterprise": {"key": "aifabei", "name": "Alphabet"},
  "applicationSlug": "sample-review",
  "applicationName": "样品评审系统",
  "bridgeVersion": 1,
  "eventsUrl": "/api/integration/events",
  "eventDeliveriesUrl": "/api/integration/event-deliveries",
  "auth": {"ssoPath": "/api/integration/sso", "algorithm": "HS256"},
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

完整结构以 `schemas/manifest-v2.schema.json` 为准。部门声明只描述谁负责开发、协作和验收；`accessRoles` 是建议权限组合，不是模块角色，也不是模块自行颁发的权限。平台管理员或企业管理员参考该组合，把页面和 Action 授权给 SaaS 中已有的平台角色后才生效，部门数据范围也一律由平台角色设置。同步新增权限组合建议、页面、Action 和事件时只登记为“待授权”。已批准 Action 的操作类型、输入 Schema、AI 开关、确认要求或业务含义发生变化时，平台必须显示差异并重新置为待审核；只补充名称、描述、`aiTool` 推荐说明或结果说明时记录差异即可，不必自动停用。删除 Action 时平台停用目录项，不自动扩权。

## 角色授权模型

平台员工只有一个组织部门，但可以有多个角色。一个角色保存完整权限树：

```text
applicationSlug
└─ moduleKey
   └─ pageKey: view
      └─ actionKey: query/create/update/delete/export/approve
```

员工最终权限是全部启用角色权限树与数据范围的并集。`departments[].role=owner` 仅表示该部门负责需求、开发验收或变更确认，不能替代平台角色授权。页面 `view` 与 Action 分开：看见页面不表示能修改数据，也不表示 AI 可以调用页面中的 Action。角色的数据范围由 SaaS 合并为 `effectiveDataScope`，包含 `unrestricted/include_self/own_only/department_ids`；模块查询、导出、修改、删除和审批均必须对业务记录执行该范围。没有 v2.4 角色授权 Manifest 的旧系统可以继续按整站 iframe 或旧契约兼容授权，但必须在管理员界面标记为兼容模式，不能冒充已经完成 v2.4 角色授权。

旧数据没有可用的负责部门或创建人字段时，不能仅新增空列就切换数据范围。升级必须先生成未归属清单，由业务负责人确认归属后回填，验证不同角色的允许与拒绝，再启用新契约。

## 双层鉴权

### iframe SSO

灼见先检查 `moduleKey` 的 `view` 权限，再签发 `typ=zhuojian-sso` 短票据。模块验证签名、`iss=zhuojian-saas`、`aud=applicationSlug`、`typ`、`exp`、企业、用户、`moduleKey`、权限和一次性 `jti`。`redirect` 必须是站内相对路径且命中获授权页面。成功后建立 `HttpOnly; Secure; SameSite=Lax` 会话并 302 到不含票据的页面。

v2.4 SSO 票据必须包含 SaaS 根据平台角色计算的最终页面、操作 allowlist 和数据范围。模块不得用 Manifest 的部门责任、`accessRoles` 建议值或用户本人的 `departmentId` 替代平台最终授权：

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
      "actionKeys": ["sample_review.query", "sample_review.approve", "sample_review.export"]
    }
  }
}
```

模块必须把 allowlist 保存到安全会话，只向前端返回允许的页面与按钮；服务端路由、页面 Action 和页面上下文也必须逐次校验 `pageAccess`。伪造 URL、前端显示错误或隐藏按钮均不能绕过服务端检查。

### Action

Action JWT 使用 `typ=zhuojian-action`，至少包含用户、企业、`departmentId`、`departmentIds`、`roleIds`、`effectiveDataScope`、`moduleKey`、`pageKey`、`actionKey`、`operation`、`requestId` 和权限。`operation` 可为 `query/create/update/delete/export/approve`；其中 `approve` 对应独立的 `ai_approve` 权限。模块不信任请求体中的身份或范围字段，并再次验证当前会话/令牌对模块、页面、Action 和业务数据的权限。

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

`update` 和 `delete` 必须携带 `expectedVersion`，与当前实体版本不一致时返回 HTTP 409。模块按 `applicationSlug + requestId` 幂等保存结果，页面按钮和 Action HTTP 入口调用同一应用服务函数。

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

iframe 在模块、页面、实体、筛选或选中项变化后发送。`postMessage` 的 `targetOrigin` 必须从 `document.referrer` 解析并验证为 HTTPS 灼见父页面来源，或使用服务器下发的同等白名单；禁止使用 `"*"`，也禁止把来源值放进用户可控查询参数：

```json
{
  "type": "zhuojian:context",
  "version": 1,
  "enterprise_key": "aifabei",
  "application_slug": "sample-review",
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

用户能在页面执行某个 Action，不代表 AI 自动拥有它。只有上述交集仍包含该 Action，且平台 AI 策略允许时，平台才可向模型暴露该工具并为本次调用签发 Action JWT。模块收到请求后必须再次验证 JWT 与 URL 中 `actionKey`、请求体中的模块/页面/操作完全一致；任何不一致都拒绝，不能因为请求来自灼见域名或通过 Nginx 就跳过鉴权。

## 事件

模块到 SaaS 的事件必须有严格递增且跨数据库重建不回退的 `sequence`、稳定唯一 `eventId`、版本化 `eventType`、企业/模块/部门、实体标识、发生时间和路由摘要。业务写入与事件尽量使用同事务 outbox。新系统不得直接使用会在新数据库重新从 1 开始的本地自增序列；推荐使用“Unix 微秒时间戳与上一序列 + 1 的较大值”，并保持在 JavaScript 安全整数范围内。这样即使容器或数据卷被替换，灼见原有事件游标也不会跳过新事件。

SaaS 向目标模块投递时使用 `typ=zhuojian-event` JWT，并 POST：

```json
{
  "deliveryId": "stable-id",
  "sourceApplicationSlug": "sample-review",
  "event": {
    "eventId": "stable-event-id",
    "eventType": "design.sample_review.approved.v1",
    "enterpriseKey": "aifabei",
    "moduleKey": "sample_review",
    "entityType": "sample_review",
    "entityId": "SR-001",
    "occurredAt": "2026-08-30T10:00:00+08:00",
    "payload": {"result": "approved"}
  }
}
```

事件 JWT 必须绑定 `deliveryId/eventId/eventType/targetModuleKey`；若 JWT 还带 `sourceApplicationSlug`，它必须与请求体完全一致。目标模块只接受目标 `moduleKey` 在 Manifest `events.subscribes[]` 中明确订阅的 `eventType`，并再次核验企业标识、字段格式和带时区时间。目标模块按完整请求哈希同时绑定 `eventId` 与 `deliveryId`：完全相同的重复投递返回已处理状态，任一 ID 被复用于不同内容时返回 409，不得重复创建业务记录或静默接受冲突。
