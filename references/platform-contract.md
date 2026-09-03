# 灼见原生模块接入协议 v2.4

`version` 始终是整数 `2`；兼容增强写入字符串 `contractRevision`。平台必须兼容没有 `contractRevision` 和 `pages` 的 v2.0 模块。

## 标识和边界

- `enterprise.key`：企业稳定标识，Alphabet 沿用 `aifabei`，不因显示名称变更而改写。
- `applicationSlug`：独立模块系统、域名和发布单元。
- `moduleKey`：业务大模块中的子模块标识。
- `pageKey`：子模块内稳定页面/工作上下文，也是最小可见边界。
- `actionKey`：系统内全局唯一业务命令，也是最小操作边界。
- `departments[]`：开发、协作、审批和验收责任目录；每个子模块恰好一个 owner 部门。部门责任不产生员工访问权限。
- `accessRoles[]`：模块 AI 建议的平台角色、页面和 Action 上限。它只用于管理员映射/确认，不会自动创建有效授权。
- 用户组织归属为单一 `departmentId`，可拥有多个平台角色。原生模块的最终授权只从角色合并为 `applicationSlug/moduleKey/pageKey/actionKey` 权限树；跨部门协作通过增加角色实现，不通过多部门成员关系、部门直授或个人直授实现。
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
      {"roleKey": "sample_review.designer", "name": "样品设计负责人", "suggestedDepartmentKey": "design", "pageKeys": ["sample_review.list"], "actionKeys": ["sample_review.query", "sample_review.create", "sample_review.update"]},
      {"roleKey": "sample_review.quality_approver", "name": "样品质量审批员", "suggestedDepartmentKey": "quality", "pageKeys": ["sample_review.list"], "actionKeys": ["sample_review.query", "sample_review.approve", "sample_review.export"]}
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
      "operation": "query",
      "aiEnabled": true,
      "requiresConfirmation": false,
      "inputSchema": {"type": "object"},
      "resultSchema": {"type": "object"}
    }],
    "events": {
      "publishes": ["design.sample_review.approved.v1"],
      "subscribes": []
    }
  }]
}
```

完整结构以 `schemas/manifest-v2.schema.json` 为准。部门声明只描述谁负责开发、协作和验收；`accessRoles` 是建议角色，不是模块自行颁发的权限。平台管理员或企业管理员将建议映射到平台角色并逐页确认 Action 后才生效。同步新增角色建议、页面、Action 和事件时只登记为“待授权”；删除 Action 时平台停用目录项，不自动扩权。

## 角色授权模型

平台员工只有一个组织部门，但可以有多个角色。一个角色保存完整权限树：

```text
applicationSlug
└─ moduleKey
   └─ pageKey: view
      └─ actionKey: query/create/update/delete/export/approve
```

员工最终权限是全部启用角色权限树的并集。`departments[].role=owner` 仅表示该部门负责需求、开发验收或变更确认，不能替代平台角色授权。页面 `view` 与 Action 分开：看见页面不表示能修改数据，也不表示 AI 可以调用页面中的 Action。旧系统没有 v2.4 Manifest 时可以继续按整站 iframe 兼容授权，但必须在管理员界面标记为兼容模式。

## 双层鉴权

### iframe SSO

灼见先检查 `moduleKey` 的 `view` 权限，再签发 `typ=zhuojian-sso` 短票据。模块验证签名、`iss=zhuojian-saas`、`aud=applicationSlug`、`typ`、`exp`、企业、用户、`moduleKey`、权限和一次性 `jti`。`redirect` 必须是站内相对路径且命中获授权页面。成功后建立 `HttpOnly; Secure; SameSite=Lax` 会话并 302 到不含票据的页面。

v2.4 SSO 票据还必须包含管理员基于平台角色计算出的最终页面和操作 allowlist，模块不得用 Manifest 的部门责任或 `accessRoles` 建议值替代平台最终授权：

```json
{
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

Action JWT 使用 `typ=zhuojian-action`，至少包含用户、企业、部门、`moduleKey`、`pageKey`、`actionKey`、`operation`、`requestId` 和权限。`operation` 可为 `query/create/update/delete/export/approve`；其中 `approve` 对应独立的 `ai_approve` 权限。模块不信任请求体中的身份字段，并再次验证当前会话/令牌对模块、页面和 Action 的权限。

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

目标模块按 `eventId`/`deliveryId` 幂等消费，返回已接受或已处理状态；不得因为重复投递重复创建业务记录。
