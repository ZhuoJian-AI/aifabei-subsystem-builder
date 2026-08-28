---
name: aifabei-subsystem-builder
description: "让爱法贝业务同事只描述需求，由 AI 新建、修改、部署或接入独立模块系统，并与灼见 SaaS v2 协议兼容。一个系统可包含多个子模块，每个子模块可由多个部门共同参与；页面按钮与 AI 共用操作接口。用户提到爱法贝模块、跨部门系统、ECS、内网部署、iframe 或灼见接入时使用。"
---

# 爱法贝模块系统搭建

用户只负责说清业务、参与部门和期望结果。不要让用户选择框架、写命令、配置 Docker、解释 iframe 或手工拼接口；这些由 AI 完成并用业务语言汇报。

## 固定业务层级

```text
爱法贝企业
└─ 模块系统（一个域名、一次部署）
   └─ 多个子模块（moduleKey，最小授权单位）
      └─ 多个参与部门及角色
         └─ 页面、数据和操作
```

- 模块系统是独立仓库、域名、数据库和发布单元。
- 子模块是灼见菜单、授权和 AI 操作的最小边界。
- 每个子模块必须有且只有一个 `owner` 部门，可有多个协作、审批或使用部门。
- 模块业务数据留在模块数据库；灼见只保存登记、授权、操作目录、事件游标和必要索引。

## 执行入口

按用户意图直接选择，不要求用户理解技术差异：

1. **新建**：从业务描述建立最小可用系统。
2. **修改**：原地修改现有系统，不覆盖业务代码或重建数据库。
3. **接入**：补齐 v2 协议并部署，最后输出管理员接入回执。

开始前读取全局与项目 `AGENTS.md`，再运行：

```text
python <本 Skill 目录>/scripts/inspect_subsystem.py --path <仓库根目录>
```

## 接入标准

实施前读取 [平台接入 v2 协议](references/platform-contract.md)。系统必须提供：

- `GET /health`
- `GET /api/integration/manifest`
- `GET /api/integration/events`
- `POST /api/integration/actions/{actionKey}`
- `GET /api/integration/sso?ticket=...`
- 页面上下文 `zhuojian:context` Bridge

关键规则：

- 页面按钮与 AI 调用同一个业务命令处理器和权限校验。
- `aiEnabled=false` 的操作不能提供给 AI。
- 高风险操作标记 `requiresConfirmation=true`，由灼见取得用户确认后调用。
- action 按 `requestId` 幂等；重复请求返回同一结果。
- 模块同时验证企业、用户、部门、`moduleKey`、`actionKey` 和操作权限。
- 不接受前端自行声明的身份；只信任灼见签发的短期票据。
- iframe、事件和 action 不传整表数据，不直连其他模块数据库。

## 部署判断

按 [ECS 与内网接入](references/ecs-first-access.md) 执行：

- 公网 ECS：优先使用 Coolify，测试域名为 `{applicationSlug}.aifabei.staging.zhuojianai.com`，必须启用 HTTPS。
- 同一 ECS 可部署多个模块系统；按域名路由到不同容器，不能为每个项目另买服务器。
- 内网服务器：可以完成开发和本地部署，但没有经过授权的公网网关或隧道时，明确报告“等待网络接入”，不得声称已接入灼见。
- root 密码只允许交互式隐藏输入，绝不写入文件、命令参数、日志或回复。已经公开过的密码视为失效。

## 管理员接入回执

部署并通过验证后，只向用户输出以下业务回执，不要求管理员查看源码或数据库：

```text
模块系统：<名称>
入口地址：https://<applicationSlug>.aifabei.staging.zhuojianai.com
清单地址：https://<域名>/api/integration/manifest
协议版本：2
子模块：<moduleKey + 名称列表>
参与部门：<部门 + 角色列表>
AI 操作：<actionKey + 是否需确认列表>
健康检查：通过/未通过
连接密钥：已安全配置/等待管理员配置（绝不回显值）
平台状态：等待管理员登记/已登记
```

Skill 不得自动登录灼见管理员后台、自动创建授权或推断部门映射。管理员在灼见“接入模块系统”向导中粘贴域名和连接密钥，核对模块及部门后确认登记。

## 完成标准

- 运行项目自身测试、构建、Compose 校验和健康检查。
- 运行 `validate_endpoint.py`，确认 v2 清单、部门归属、操作 Schema 和事件游标。
- 用真实浏览器验证页面、iframe、单点登录、同一页面操作和 AI 操作。
- 高风险操作验证确认、拒绝、过期和重复批准。
- 告诉业务用户：系统解决什么、谁参与、页面和 AI 能做什么、是否上线、还缺哪一项外部条件。
