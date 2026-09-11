# 企业交接 Skill 自动同步 v1.1.0

## Task

- 保留业务负责人首次只提供 `IP + root + 密码` 的简单交接方式。
- 管理员初始化 ECS 后，只向负责人分发 `aifabei-subsystem-builder`；总 Skill 根据服务器企业、Runtime 或主机自动取得专属交接 Skill。
- 让已有 `1.x` 安装通过旧仓库的一次稳定发布迁移到唯一企业 Skills 总仓库。

## Changed behavior

- 总 Skill 每轮先更新自己，再更新本机已有交接 Skills；登录已初始化 ECS 后，以 `runtimeId`、主机地址和企业级规则解析并安装缺失交接 Skill。
- 新增 `scripts/update_managed_skills.py`，验证稳定目录身份、独立 SemVer、SHA-256、压缩路径、必要文件和版本更新记录，并以原子替换和失败回滚保护旧版。
- `skillVersion=1.1.0` 的 `update_skill.py` 已切换到 `ZhuoJian-AI/zhuojian-enterprise-skills`；旧仓库的 `v1.1.0` Release 只承担从 `1.0.9` 的最后一次桥接。
- 总仓库已通过 PR #1 建立，首个稳定 Release `bundle-v1.0.0` 包含总 Skill `1.1.0` 与 `alphabet-daoxun-data-bridge` `1.0.0`。

## Verification

- `python -m pytest -q`: `188 passed, 41 skipped`.
- `python C:/Users/王鑫涛/.codex/skills/.system/skill-creator/scripts/quick_validate.py .`: `Skill is valid!`.
- `python -m compileall -q scripts tests`: passed.
- `git diff --check`: passed.
- 从总仓库公开 `releases/latest/download/update_skill.py` 无登录全新安装：`SKILL_UPDATED installed 1.1.0`。
- 按 `enterpriseKey=alphabet + host=8.218.208.205` 解析：`MANAGED_SKILLS_UPDATED installed alphabet-daoxun-data-bridge 1.0.0`。

## Decisions and risks

- 用户口述公司名只作一致性核对；长期归属以 ECS 的 `enterpriseKey + runtimeId` 为准，主机地址用于首次兜底。
- 服务器专属 Skill 不会仅凭公司名匹配，避免同公司不同 ECS 规则串装。
- 本次没有修改业务系统、服务器、数据库、OSS 或 SaaS 接入契约；新系统仍使用契约 `2.5`。

## Remaining work

- 合并本分支后，从合并后的干净 `main` 构建并发布旧仓库 `v1.1.0` 桥接 Release。
- 发布后从公开旧仓库 `v1.0.9` 副本执行真实自动升级，确认它能迁移到总仓库并安装道讯交接 Skill。
