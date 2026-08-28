# 灼见模块系统接入协议 v2

## 稳定标识

- `enterprise.key`：企业稳定标识，爱法贝使用 `aifabei`。
- `applicationSlug`：独立模块系统标识，也是测试子域名前缀。
- `moduleKey`：系统内子模块标识，是最小授权单位。
- `departments[].key`：与灼见部门 slug 对应；每个子模块恰好一个 `owner`。
- `actionKey`：系统内全局唯一的业务操作标识。

显示名称可以修改，稳定标识不能随页面文案改变。
稳定标识只使用小写字母、数字、点、下划线和短横线，并以字母或数字开头。

## 固定端点

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/health` | 无副作用健康检查，返回 2xx |
| GET | `/api/integration/manifest` | 读取模块、部门和操作目录 |
| GET | `/api/integration/events?after=&limit=` | 顺序增量事件 |
| POST | `/api/integration/actions/{actionKey}` | 页面和 AI 共用的业务命令出口 |
| GET | `/api/integration/sso?ticket=&redirect=` | iframe 短票据换模块会话 |

Manifest 和事件使用独立静态 Bearer Token。SSO 与 action 使用灼见以同一接入密钥签发的 60 秒 HS256 JWT，但不能使用灼见全局用户 JWT 密钥。

## Manifest

```json
{
  "protocol": "zhuojian-subsystem",
  "version": 2,
  "enterprise": {"key": "aifabei", "name": "爱法贝"},
  "applicationSlug": "sample-review",
  "applicationName": "样衣协同系统",
  "bridgeVersion": 1,
  "eventsUrl": "/api/integration/events",
  "auth": {"ssoPath": "/api/integration/sso", "algorithm": "HS256"},
  "modules": [{
    "moduleKey": "sample_review",
    "name": "样衣评审",
    "route": "/sample-review",
    "departments": [
      {"key": "design", "name": "设计部", "role": "owner"},
      {"key": "production", "name": "生产部", "role": "collaborator"}
    ],
    "actions": [{
      "actionKey": "sample_review.approve",
      "name": "通过评审",
      "description": "确认当前样衣评审通过",
      "operation": "update",
      "aiEnabled": true,
      "requiresConfirmation": true,
      "inputSchema": {
        "type": "object",
        "properties": {"styleId": {"type": "string"}},
        "required": ["styleId"]
      },
      "resultSchema": {"type": "object"}
    }]
  }]
}
```

`operation` 只能是 `query/create/update/delete/export`。清单新增内容会被自动发现但不会自动授权；移除 action 时平台将其停用。

## 双层鉴权

### iframe SSO

灼见先检查用户的子模块 `view` 权限，再生成 `typ=zhuojian-sso` 的短票据。模块必须验证：

- HS256 签名、`iss=zhuojian-saas`、`aud=applicationSlug`、`typ`、`exp`。
- `organizationId`、`sub`、`moduleKey` 和 `permissions`。
- `jti` 只能消费一次；保存到短期缓存直到票据过期。
- `redirect` 只能是模块站内相对路径。

验证后建立 `HttpOnly; Secure; SameSite=Lax` 的短期模块会话，再 302 到不含票据的页面。

### action 身份

灼见调用 action 时使用 `typ=zhuojian-action` 的 60 秒 JWT，额外包含 `actionKey`、`operation`、`requestId`。模块不得信任请求体中的身份字段，必须从 JWT 读取身份并再次检查模块权限。

## 人与 AI 共用命令

页面按钮和 `/api/integration/actions/{actionKey}` 必须调用同一个应用服务函数。平台请求体：

```json
{
  "requestId": "stable-idempotency-id",
  "moduleKey": "sample_review",
  "params": {"styleId": "203A023"}
}
```

模块按 `applicationSlug + requestId` 保存幂等结果。响应仅返回当前操作结果及事件摘要，不返回 Token、内部路径或无关整表数据。

## 事件

```json
{
  "items": [{
    "sequence": 18,
    "eventId": "stable-unique-id",
    "eventType": "design.sample_review.completed",
    "enterpriseKey": "aifabei",
    "moduleKey": "sample_review",
    "departmentKeys": ["design", "production"],
    "entityType": "style",
    "entityId": "203A023",
    "action": "completed",
    "occurredAt": "2026-08-27T16:30:00+08:00",
    "payload": {"result": "approved"}
  }],
  "nextAfter": 18,
  "hasMore": false
}
```

事件序号严格递增，`eventId` 全局稳定。事件与业务写入尽量使用同事务 outbox；事件只包含路由所需摘要。

## iframe Bridge

页面在模块、实体或筛选变化后发送：

```json
{
  "type": "zhuojian:context",
  "version": 1,
  "enterprise_key": "aifabei",
  "application_slug": "sample-review",
  "route": "/sample-review",
  "module_key": "sample_review",
  "module_name": "样衣评审",
  "department_keys": ["design", "production"],
  "entity_type": "style",
  "entity_id": "203A023",
  "filters": {},
  "selection": {},
  "data_version": "optional-version"
}
```

Bridge 不得包含 Token、Cookie、密码、整份业务数据或内部文件路径。模块响应头的 CSP `frame-ancestors` 只允许灼见正式与测试域名。
