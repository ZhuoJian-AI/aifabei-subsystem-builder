# 企业交接 Skill 自动同步

## 目的

`aifabei-subsystem-builder` 是所有企业共用的 SaaS 与 ECS 契约。某家公司、某台服务器、某个既有系统或数据通道的特殊事实放在独立交接 Skill 中。业务负责人首次只接收总 Skill，并为新服务器提供一次 `IP + root + 密码`；不要求其创建 GitHub 仓库、选择交接包或维护版本。

所有稳定版由唯一公共仓库 `ZhuoJian-AI/zhuojian-enterprise-skills` 发布。一个仓库可以包含多个独立 Skill；每个 Skill 保留自己的 `skillVersion`、更新记录、压缩包和 SHA-256。

## 每轮更新

在登录任何服务器前运行：

```text
python <skill>/scripts/update_skill.py
python <skill>/scripts/update_managed_skills.py installed
```

第一条更新总 Skill；第二条只更新本机已经安装并出现在稳定目录中的交接 Skill。更新失败时保留旧版；跨主版本只报告，不自动替换。

任一命令输出更新后，立即读取输出路径中的 `skill-version.json`、对应 `CHANGELOG.md` 版本段和新版 `SKILL.md`。同一轮不得继续使用已被替换的旧指令。

## 登录后的识别

管理员初始化 ECS 时，非敏感 Runtime 档案固定记录 `enterpriseKey` 与 `runtimeId`。登录并通过 `zhuojian-runtime doctor` 后读取 `/etc/zhuojian/runtime.json`，不得输出其中的秘密引用或其他配置。然后运行：

```text
python <skill>/scripts/update_managed_skills.py resolve \
  --enterprise-key <enterpriseKey> \
  --runtime-id <runtimeId> \
  --host <本次登录地址>
```

匹配顺序是 `runtimeId`、主机地址、企业级无服务器限定规则。主机地址用于首次兜底；长期归属以 ECS 档案为准。用户顺手给出的公司名称只作一致性核对，不能覆盖服务器档案。二者不一致时停止选择交接 Skill并报告管理员。

`resolve` 会安装尚不存在的交接 Skill，也会升级同主版本的旧副本。输出 `MANAGED_SKILLS_UPDATED` 后，立即读取输出路径中的交接 `SKILL.md` 和本次任务明确要求的 references；交接 Skill 补充总契约，不能取代或放宽总契约。

目录没有匹配项时继续使用总 Skill，并明确说明“管理员尚未为这台 ECS 登记专属交接 Skill”。目录声明了匹配项但下载、校验或安装失败时，不得猜测服务器特有规则；已有旧版可以继续使用，首次安装失败则停止依赖特殊环境的操作并通知管理员。

## 管理员发布

管理员只维护一个公共仓库。新增公司或服务器时：

1. 创建公司前缀的唯一 Skill 名称，例如 `alphabet-daoxun-data-bridge`。
2. 在交接 Skill 的 `skill-version.json` 写入 `managedBy: ZhuoJian-AI/zhuojian-enterprise-skills`。
3. 在稳定目录中登记 `enterpriseKeys`、`runtimeIds` 和必要的 `hosts`；服务器专属 Skill 至少登记 `runtimeIds` 或 `hosts`，避免仅凭公司名称加载全部交接规则。
4. 构建独立 ZIP、计算 SHA-256，并作为同一个 GitHub Release 的资产发布。
5. 不把 ECS 密码、SSH 私钥、数据库凭证、令牌或客户数据放进仓库或发布包。

旧 `ZhuoJian-AI/aifabei-subsystem-builder` 仓库只提供一次同主版本桥接发布，使已有负责人自动切换到总仓库。桥接完成后，后续稳定版全部由企业 Skills 总仓库发布。
