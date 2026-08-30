# 灼见原生模块接入协议 v2.1

`version` 始终是整数 `2`；兼容增强写入字符串 `contractRevision`。平台必须兼容没有 `contractRevision` 和 `pages` 的 v2.0 模块。

## 标识和边界

- `enterprise.key`：企业稳定标识，爱法贝为 `aifabei`。
- `applicationSlug`：独立模块系统、域名和发布单元。
- `moduleKey`：子模块及最小授权单位；每个子模块恰好一个 owner 部门。
- `pageKey`：子模块内稳定页面/工作上下文。
- `actionKey`：系统内全局唯一业务命令。
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

每个模块系统只有一个独立的 `ZHUOJIAN_INTEGRATION_SECRET`：Manifest 和事件拉取把它作为静态 Bearer Token；SSO、Action 和事件投递用同一密钥签发 60 秒 HS256 JWT。这个密钥只属于当前模块系统，不得跨系统复用，也不得复用灼见全局用户 JWT 密钥。管理员接入界面因此只需要填写一次“接入凭证”。

模块部署时还必须配置 `ZHUOJIAN_ORGANIZATION_ID`，并把它绑定到灼见中该企业的真实 organization UUID。所有 SSO、Action 和事件投递 JWT 都必须同时校验 `aud=applicationSlug` 与 `organizationId=ZHUOJIAN_ORGANIZATION_ID`；不能只检查“organizationId 非空”。

## Manifest 示例

```json
{
  "protocol": "zhuojian-subsystem",
  "version": 2,
  "contractRevision": "2.1",
  "enterprise": {"key": "aifabei", "name": "爱法贝"},
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

完整结构以 `schemas/manifest-v2.schema.json` 为准。同步新增页面、Action 和事件时只登记为“待授权”；删除 Action 时平台停用目录项，不自动扩权。

## 双层鉴权

### iframe SSO

灼见先检查 `moduleKey` 的 `view` 权限，再签发 `typ=zhuojian-sso` 短票据。模块验证签名、`iss=zhuojian-saas`、`aud=applicationSlug`、`typ`、`exp`、企业、用户、`moduleKey`、权限和一次性 `jti`。`redirect` 必须是站内相对路径。成功后建立 `HttpOnly; Secure; SameSite=Lax` 会话并 302 到不含票据的页面。

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

iframe 在模块、页面、实体、筛选或选中项变化后发送：

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

模块到 SaaS 的事件必须有递增 `sequence`、稳定唯一 `eventId`、版本化 `eventType`、企业/模块/部门、实体标识、发生时间和路由摘要。业务写入与事件尽量使用同事务 outbox。

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
