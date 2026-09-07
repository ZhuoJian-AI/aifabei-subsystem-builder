# Alphabet 模块系统搭建

这个仓库装着**两份配套的 skill**，覆盖一个业务模块从「说不清的想法」到「上线接入」的全程。

根 skill 同时规定 Alphabet 企业级文件底座：Runtime 尚未验收 OSS 时，新系统使用 ECS 固定数据目录；管理员一次性配置同地域私有 OSS 和企业文件网关并通过真实探针后，后来首次初始化的系统会自动获得独立的 `apps/<applicationSlug>/` 前缀和项目身份。管理员无需预知系统数量或逐项目发令牌，业务负责人和业务 AI 的正常创建、修改、部署流程也无需申请、填写、复制或输出阿里云、Bucket、RAM、AccessKey 或项目令牌。每个系统第一次 `ensure-app` 时会冻结实际存储后端；Runtime 已纳管系统保持 release 记录的 `local-managed` 或 `oss-gateway`，未纳管的旧容器原样保留，迁移或导入必须另走清单、校验和与回滚流程。

每台业务服务器的负责人只在第一次提供 `IP + root + 密码`。首次登录后，AI 会为当前 Codex 环境建立经过验证的服务器专用 SSH 访问记忆；以后维护直接登录，不再向负责人索要账号密码。

管理员只需让业务电脑通过日常 VPN 验证一次标准 SSH `22`；只有 `22` 受限时才建立 SSH/HTTPS `443` 共用入口。负责人此后只把公网地址、root 账号和密码交给 Codex，并保持 VPN 已启用；不需要阿里云账号或已登录的控制台。

```
业务负责人有个想法
        │
        ▼
  ┌──────────────┐
  │   模块需求/   │   只管「想清楚」
  │              │   问成一份开工计划书 · 全程不出现技术词
  └──────┬───────┘
         │   交棒物 = 开工计划书 + 原始材料 + 阻塞项
         ▼
  ┌──────────────────────────┐
  │  仓库根目录（本 skill）    │   只管「做出来」
  │  多部门组成模块 · 多模块   │   建模块 · 部署 · 接入灼见 SaaS
  │  聚合企业 · 接入灼见 SaaS  │
  └──────────────────────────┘
```

---

## 目录

| 位置 | 是什么 | 谁维护 |
|---|---|---|
| **仓库根**（`SKILL.md` / `references/` / `scripts/`） | Alphabet 模块系统搭建 —— **做出来 + 部署 + 接入** | Alphabet |
| **`模块需求/`** | 把一个想法**问成一份能开工的计划书** —— 访谈 + 判断 + 画图 | 灼见 |

⚠️ **根目录那份的位置没有变。** clone 下来仍然可以直接当 skill 用，
`模块需求/` 只是多出来的一个子目录，不影响它。

---

## 怎么装

两份是**独立的 skill**，各装各的。公开仓库不需要 GitHub 账号；Codex 的目录是 `~/.codex/skills`：

```bash
# 做出来那份（仓库根）
cp -r <clone 下来的目录>              ~/.codex/skills/aifabei-subsystem-builder/

# 想清楚那份（子目录）
cp -r <clone 下来的目录>/模块需求      ~/.codex/skills/模块需求/
```

装「做出来」那份时，`模块需求/` 子目录可以一起带着，不影响使用 —— 它不会被当成根 skill 的内容。

不想使用 Git，也可以从公开 Release 完成一次引导安装。Windows PowerShell：

```powershell
$updater = Join-Path ([IO.Path]::GetTempPath()) 'aifabei-skill-updater.py'
Invoke-WebRequest 'https://github.com/ZhuoJian-AI/aifabei-subsystem-builder/releases/latest/download/update_skill.py' -OutFile $updater
py $updater --install-dir "$HOME/.codex/skills/aifabei-subsystem-builder"
Remove-Item -LiteralPath $updater
```

安装引导版后，每次调用根 Skill 会检查公开 GitHub Release 的同主版本稳定更新；不跟踪 `main`，不需要 GitHub 登录。更新成功后，AI 会先读取新版 `CHANGELOG.md` 中与当前 `skillVersion` 对应的记录，再按新版规则继续。断网或校验失败时保留本地版本继续工作，跨主版本只提示管理员。

`skillVersion=1.x` 是 Skill 自身版本；业务系统 Manifest 的 `contractRevision=2.4/2.5` 是 SaaS 接入版本。自动更新 Skill 永远不会替现有系统升级接入契约。

维护者发布稳定版时，需要同时更新 `skill-version.json` 和 `CHANGELOG.md`，测试通过并合并到 `main` 后再发布同版本 GitHub Release。只合并 `main` 不会触发用户自动更新。

---

## 两份怎么衔接

**交接物是一份开工计划书**，里面有：

- 这事该不该做、**现在能不能开工**（差什么，具体到人和动作）
- **架构图**：数据从哪来 · 几个页面 · 谁用 · AI 那条旁路的边界
- **每个页面的线框** + 每条排法的业务理由
- 会在哪崩（每条都指得回访谈里的某个缺口）
- S0…S6 怎么走，每步「算完成」都能判断
- 折叠区：谁参与 · 数据从哪来 · 验收清单 · 访谈留底

**前一份不碰技术选型** —— 用什么做、放在哪、怎么部署，全部留给后一份决定，
理由写在计划书里：「承接方手上有什么、熟什么，他比我们清楚」。

**后一份不用回头问业务** —— 计划书的唯一完成标准就是这个：
> 交给一个完全不了解这摊业务的人，他能不能不回头问业务问题就开工？

---

## 目录纪律

- **每份 skill 一个独立目录，互不引用对方的内容**
- 需要提到对方时**写指针**（「见另一份的 X」），不要抄过来 —— 抄成第二份必然漂移
- 各自的 `AGENTS.md`（Codex 版）都由脚本生成，**不要手改**
