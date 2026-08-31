# Coolify 发布闭环

业务 AI 不直接登录 Coolify，也不持有 Coolify API Token。管理员只在每家公司首次初始化时把目标服务器、项目、环境、GitHub App Source 和域名后缀登记到灼见；之后 `publish_subsystem.py` 完成整条链路。

```text
本地校验并提交
→ 灼见创建/复用 {companySlug}-{moduleSlug} 私有仓库
→ 使用短时单仓库 Token push main
→ 灼见按企业部署档案创建/复用 Coolify Application
→ 平台注入接入 Secret、组织 ID、SaaS Origin 和 /data 持久卷
→ 固定到本次 Git commit 并触发部署
→ Docker HEALTHCHECK + 公网 /health 验证
→ 读取 Manifest 并自动创建/更新企业应用索引
→ 同步子模块、页面、部门建议、Action 和事件
→ 返回入口、Coolify 资源、部署记录和平台应用 ID
```

## 管理员一次性前置条件

1. Coolify 已把企业 ECS 作为远程 Server 管理，且只开放 80/443。
2. Coolify 中存在专门承载该企业模块的 Project/Environment。
3. Coolify 的 GitHub App Source 能读取灼见组织中新创建的私有仓库。
4. 通配 DNS（如 `*.aifabei.staging.zhuojianai.com`）已指向该 ECS。
5. 灼见中央 Backend Secret 已配置 `COOLIFY_API_URL` 和 `COOLIFY_API_TOKEN`；Token 不下发企业 ECS。
6. 管理员通过灼见部署档案接口保存稳定 `runtime_key`、`server_uuid`、`project_uuid`、环境、`github_app_uuid` 和 `domain_suffix`；多台 ECS 分别登记，且标记一个默认 Runtime。
7. 企业发布机仅保存本企业 organization-scoped `ZHUOJIAN_PUBLISH_KEY`。

一个企业可配置一台服务器承载多个模块系统，也可配置多个 Runtime 分散到多台 ECS。业务 AI 只能使用环境档案里的 `ZHUOJIAN_RUNTIME_KEY`；模块首次发布后绑定该 Runtime，普通更新不能迁移 Server。平台根据组织部署档案选择 Server，因此业务 AI 无法把代码发布到其他企业服务器。

## 失败边界

- `source`：Coolify 无法读取私有仓库。检查 GitHub App Source，不向业务 AI 分发 PAT。
- `build`：Dockerfile 或依赖失败。业务 AI 修代码并提交；保留原应用和数据卷。
- `health`：容器未监听 `0.0.0.0:8000` 或 `/health` 非 200。
- `routing`：通配 DNS、80/443 或证书问题，由管理员修底座。
- `contract`：Manifest、事件或 Action 契约不合格，修业务代码或 Skill。

如果已经有健康版本，新版本失败时平台把同一个 Coolify Application 回退到上一次成功 commit；不删除持久卷、不复制数据库、不在中央 SaaS 运行企业模块。没有历史健康版本时只标记失败并返回建议。

## 幂等更新

- 同一 `applicationSlug` 永远复用同一仓库、Coolify Application、域名和 `/data` 卷。
- 新子模块只更新同一个 Manifest 的 `modules[]`，成功部署后灼见自动重新同步，员工导航按新 `moduleKey/pageKey` 展示。
- 重复提交同一个健康 commit 不会创建新资源。
- 权限不会由 Manifest 自动授予；管理员仍在灼见按部门、岗位、工作组或用户确认。
