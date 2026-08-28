# 平台接入最小协议

## 业务对象与稳定标识

平台固定使用“企业 → 模块 → 参与部门 → 页面/数据/操作”的层级：

- `enterpriseKey`：企业稳定标识，爱法贝环境使用平台分配的企业 key。
- `applicationSlug`：独立部署系统的稳定标识。
- `moduleKey`：业务模块稳定标识，是开发、授权、接入和聚合的主要边界。
- `departments`：模块参与部门列表；每项至少有稳定 `key`、名称和职责。一个模块允许一个或多个部门。
- `actionKey`：模块内稳定操作标识。页面按钮与 AI 调用复用同一业务命令。

名称可以调整，稳定标识不要随页面文案改变。后续新增页面、操作或参与部门时更新清单即可；中央 SaaS 应动态读取清单，不要求逐项人工硬编码。

## iframe 上下文

子系统页面在模块切换、实体选择、筛选变化和成功保存后发送：

```json
{
  "type": "zhuojian:context",
  "version": 1,
  "enterprise_key": "aifabei",
  "application_slug": "stable-application-slug",
  "route": "/current-module",
  "module_key": "stable_module_key",
  "module_name": "业务人员看得懂的名称",
  "department_keys": ["design", "production"],
  "entity_type": "style",
  "entity_id": "203A023",
  "filters": {},
  "selection": {},
  "data_version": "optional-change-version"
}
```

消息不得含 Token、Cookie、密钥、密码、整份业务数据或内部文件路径。中央平台校验 iframe source、配置域名、版本和大小。

## 集成清单

`GET /api/integration/manifest` 至少返回协议名与版本、企业、应用 slug、模块、参与部门、页面、操作、事件接口地址和 Bridge 版本。例如：

```json
{
  "protocol": "zhuojian-subsystem",
  "version": 2,
  "enterprise": {"key": "aifabei", "name": "爱法贝"},
  "applicationSlug": "sample-review",
  "bridgeVersion": 1,
  "eventsUrl": "/api/integration/events",
  "modules": [{
    "key": "sample_review",
    "name": "样衣评审",
    "route": "/sample-review",
    "departments": [
      {"key": "design", "name": "设计部", "role": "owner"},
      {"key": "production", "name": "生产部", "role": "collaborator"}
    ],
    "actions": [{
      "key": "sample_review.approve",
      "name": "通过评审",
      "method": "POST",
      "path": "/api/integration/actions/sample_review.approve",
      "permission": "sample_review.approve",
      "requiresConfirmation": true
    }]
  }]
}
```

当前模块聚合协议版本为 `2`。`departments` 使用列表，即使当前只有一个部门也不要降级为单值 `department`。`role` 可使用 `owner`、`collaborator`、`approver`、`consumer` 或项目定义的稳定角色。

## 人与 AI 共用操作

模块内所有会改变业务状态的关键操作都进入统一命令层：

1. 页面按钮调用模块自己的命令处理器。
2. `POST /api/integration/actions/{actionKey}` 在完成平台身份、企业、部门、模块权限和输入校验后，调用同一个命令处理器。
3. AI 小助手从 manifest 读取允许的操作，不通过模拟点击绕过业务权限。
4. 操作请求携带稳定的 `requestId`；重复请求返回同一业务结果，避免 AI 重试造成重复写入。
5. 高风险操作在 manifest 标记 `requiresConfirmation: true`，中央 SaaS 获得用户确认后才调用。

建议请求体包含：

```json
{
  "requestId": "stable-idempotency-id",
  "moduleKey": "sample_review",
  "entityType": "style",
  "entityId": "203A023",
  "input": {"result": "approved"}
}
```

返回业务结果、实体版本和所产生事件的 `eventId`，但不返回 Token、内部路径或无关整表数据。

## 业务事件

`GET /api/integration/events?after=<sequence>&limit=<n>` 返回：

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
    "dataVersion": "version",
    "payload": {"result": "approved"}
  }],
  "nextAfter": 18,
  "hasMore": false
}
```

事件与业务写入尽量在同一数据库事务内形成 outbox。消费者只有处理成功后才保存 `nextAfter`，并按 `eventId` 幂等。事件只放路由所需摘要；完整数据通过受控查询 API 获取。

## 鉴权和权限

- 系统间调用使用独立 Bearer Token 或后续统一网关签名，不复用 root 密码。
- 中央 SaaS 决定企业/部门/模块可见性和操作授权；模块系统仍校验每次写操作权限。
- 平台数据库不继承模块业务表。中央层只保存企业、模块及参与部门注册、授权、操作目录、事件游标、幂等记录、跨模块待办和必要索引。
- 清单新增内容可以自动发现；删除或改变稳定标识属于破坏性变更，应先标记弃用并保留兼容期。
