# Alphabet 契约退役 Team 范围

- 执行者：Codex
- 任务：`refactor/retire-team-scope`
- 版本：Skill `1.0.2`，契约仍为 `2.5`

## 已完成

- `SKILL.md` 只增加一条简短结果规则：SaaS 组织是企业、部门、用户，权限按角色取并集，不存在 Team 或平台跨部门待办。
- 平台契约明确部门表示归属、数据范围与资源所有权，业务待办和审批由子系统负责；无目标的事件只审计，不生成 SaaS 待办。
- Manifest Schema 和端点校验器拒绝 `team/teams/teamId/team_id`；SSO 与 Action 验收不再发送 Team claim。
- 不增加小白操作步骤，契约版本保持 `2.5`。

## 验证

- `python -m pytest -q`：`154 passed, 41 skipped`，另有 `23` 个子测试通过。
- `quick_validate.py .`：`Skill is valid!`。

## 剩余发布步骤

- 与执行时最新 `origin/main` 整合、合并 GitHub `main`。
- 从干净 main 构建并核验 `1.0.2` 发布包，创建稳定 GitHub Release，再验证公开免登录安装并同步本机已安装 Skill。

## 决定

- Team 兼容迁移属于 SaaS 实现细节，不写入小白使用步骤。
- `departments[]` 继续表示责任部门，`accessRoles[]` 继续表示角色授权建议，不创建 Team。
