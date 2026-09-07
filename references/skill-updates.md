# Skill 稳定版更新

`skillVersion` 是这份 Codex Skill 自身的发布版本；`contractRevision` 是业务系统与 SaaS 的接入契约。两者互不替代，Skill 更新不得改写任何项目的 `subsystem.json`。

每次调用 Skill 时，同一轮只运行一次：

```text
python <skill>/scripts/update_skill.py
```

- `SKILL_UPDATE_CURRENT`：直接继续。
- `SKILL_UPDATED`：先读取新版 `skill-version.json`，再读取 `CHANGELOG.md` 中与该 `skillVersion` 对应的版本记录，最后重新读取新版 `SKILL.md` 和本次需要的参考文件后继续，不能混用新旧指令。
- `SKILL_UPDATE_MAJOR_AVAILABLE`：跨主版本不自动安装，只提醒管理员。
- `SKILL_UPDATE_WARNING`：网络或校验失败；现有本地版本保持不变，显示一句中文提示后继续任务。

更新器只读取公开 GitHub Release 的稳定版，不读取 `main`。它验证 Skill 身份、稳定 SemVer、SHA-256、压缩路径、必要文件，以及压缩包内是否存在与 `skillVersion` 完全匹配的有效更新记录；任一校验失败都不安装。在同一磁盘的临时目录准备完成后才替换，替换失败会恢复旧目录。检测到 `.git` 开发工作树时不自动覆盖，开发者仍使用 Git 更新。

没有 GitHub 账号也可以下载公开 Release；网络必须能访问 GitHub。旧副本没有更新器时，需要按仓库 README 手工执行一次引导安装。以后 `1.x` 内的稳定版自动更新；`2.x` 等跨主版本由管理员明确升级。

新系统始终生成 `contractRevision=2.5`。维护已有系统时先读取 `subsystem.json`，只接受当前 Skill 明确支持的版本并保持原值。`2.4 → 2.5` 是独立迁移任务，必须由用户明确要求，并同时迁移凭证、SSO、Runtime 配置和端点验收，禁止只改版本号。

维护者发布稳定版时，必须先更新 `skill-version.json`，并在 `CHANGELOG.md` 增加同版本章节；测试通过并合并到 `main` 后，才能从该合并提交构建并发布同版本 GitHub Release。只更新 `main` 不会让用户自动更新。
