---
name: aifabei-subsystem-builder
description: "让 AI 用业务需求和服务器登录信息，在 Alphabet 企业 ECS 上新建、修改并部署业务系统，自动接入灼见 SaaS 的登录、角色授权、业务操作和文件存储。用户提到 Alphabet 系统、企业 ECS、跨部门模块、OSS 迁移、iframe 或灼见接入时使用。"
---

# Alphabet 企业业务系统

对外名称和正式企业标识都使用 `Alphabet` / `alphabet`；Skill 名称中的 `aifabei` 只作历史兼容。

每次调用本 Skill 时，同一轮先运行一次 `python <skill>/scripts/update_skill.py`。若输出 `SKILL_UPDATED`，先读取新版 `skill-version.json` 对应的 `CHANGELOG.md` 版本记录，再重新读取新版 `SKILL.md` 和本次所需参考文件后继续；其他结果直接使用本地版本，细节见 [Skill 稳定版更新](references/skill-updates.md)。

业务负责人只需提供三样东西：想做什么、谁使用，以及首次出现的新服务器的 `IP + root + 密码`。这次提供即代表已授权当前 Codex 环境长期登录该服务器；首次成功后建立并复用本机 SSH 访问记忆，后续禁止再次向负责人索要账号密码。不要让其准备阿里云、OSS、GitHub、模型供应商或平台令牌，也不要让其选择技术方案。

## 自动判断

- 用户明确说“初始化服务器”时，执行管理员模式。
- 其他情况均执行业务模式。
- 优先扩展服务器上的现有系统；只有确实需要独立域名、数据库、发布周期或故障隔离时才新建系统。
- 暂时只能 iframe 接入的旧系统要标记“兼容模式”，不能说成已经完成原生接入。

## 业务模式

1. 按 [服务器长期访问记忆](references/server-access-memory.md) 查找已有访问档案，再按 [ECS 首次接入](references/ecs-first-access.md) 登录。新服务器先试 SSH `22`，再试管理员配置的 `443`；连接超时不代表密码错误。
2. 运行 `zhuojian-runtime doctor`。若提示服务器尚未初始化，停止部署，只告诉用户“请企业管理员先初始化这台服务器”。
3. 检查服务器上的现有项目。若服务器源码比本地/Git 副本更新、分叉或含未同步改动，先从服务器建立开发基线，禁止用落后副本覆盖；例外见 [ECS 直接发布](references/direct-ecs-deployment.md)。读取 `subsystem.json` 识别并保留已有 `2.4` 或 `2.5` 接入版本；未知版本停止，不能猜测或只改版本号。能扩展就扩展；新项目使用内置模板创建。细则见 [原生聚合与扩展](references/native-aggregation.md)。
4. 完成业务页面、数据库和操作能力，并按 [平台接入协议](references/platform-contract.md) 接入。通用业务助手由 SaaS 提供；页面需要 OCR、语音转写、图片判断等专业 AI 时也由 SaaS 受控执行，子系统不保存模型密钥。
5. 使用 Runtime 当前提供的文件存储；不要询问用户 Bucket、令牌或服务器目录。需求涉及上传、附件、导入导出或持久文件时，读取 [文件存储与 OSS 迁移](references/object-storage.md)，校验源码时加 `--requires-file-storage`；只有 Runtime 已启用 OSS 时再加 `--requires-object-storage`。
6. 完成测试、部署和平台登记。Runtime 自动处理域名、目录和平台接入信息，AI 不读取、不复制、不展示这些秘密。
7. Runtime 校验通过后自动登记并生效，不等待管理员逐版本审核。新系统先只自动授权“系统研发者”；其他员工仍按企业管理员已绑定的业务角色访问，既有角色只在原权限上限内继承新增能力，管理员停用始终优先。

常用入口如下，参数以 `--help` 为准：

```text
zhuojian-runtime doctor
zhuojian-runtime preflight <applicationSlug>
zhuojian-runtime ensure-app <applicationSlug>
python <skill>/scripts/validate_source.py --path <项目目录>
zhuojian-runtime deploy <applicationSlug> --issue-certificate
python <skill>/scripts/publish_subsystem.py --help
python <skill>/scripts/validate_endpoint.py --help
python <skill>/scripts/e2e_acceptance.py --help
```

## 必须实现的结果

- 员工从灼见 SaaS 登录，系统自己不再创建一套员工账号和角色。
- SaaS 组织结构只使用“企业 → 部门 → 用户”，访问权限由用户绑定的一个或多个平台角色取并集；不要创建 Team，也不要把子系统待办变成 SaaS 的跨部门待办。
- 企业管理员只需绑定“系统研发者”或配置业务角色；Runtime 更新不需要重复审核。多角色可以取并集，但每个页面和操作的数据范围必须单独计算，不能互相借权限。
- 页面按钮和 SaaS AI 调用同一套业务服务；修改、删除等高风险操作必须经过版本校验、确认和防重复执行。
- 系统文件统一经过模板存储层，当前可先用磁盘，以后可以按清单迁移 OSS。
- 基于业务数据生成文件时，必须走“子系统结构化数据 → SaaS 文件执行器 → 当前员工工作空间”链路；出现可预览、可下载的工作空间文件卡片后才算完成。
- 每个 AI 页面必须声明用途、核心实体、可回答问题、关联页面和 Action 语义；SaaS 负责结构化意图、查询改写、对话隔离和工具编排，子系统不得自行处理模型路由。
- OCR、语音转写、图片比较/分类、结构化抽取和业务预测等专业 AI 必须通过 SaaS 受控能力返回可校正草稿；人工确认后才调用普通 Action 写入业务记录，子系统不保存或直连模型供应商密钥。
- 所有员工页面必须同时兼容电脑、平板和手机；导航及主要操作不得被裁切，只有表格、画布等必要内容可以在自身容器内横向滚动。
- 新系统使用契约 `2.5`；已有 `2.4/2.5` 系统的普通维护保持原版本。`2.4 → 2.5` 只在用户明确要求迁移时执行。登录、权限和平台接入的技术细节由模板、Runtime 与 [平台接入协议](references/platform-contract.md) 自动落实，不转嫁给业务用户。

## 管理员模式

管理员只需为每台 ECS 初始化一次。按 [管理员与服务器初始化](references/admin-bootstrap.md) 完成 Runtime、Docker、域名、数据目录、监控、备份和平台登记；网络问题按 [SSH、VPN 与代理访问](references/ssh-access.md) 排查。以后业务 AI 可以自行创建和更新系统；管理员只需按人员职责绑定“系统研发者”或业务角色，必要时可停用系统或 Action。

OSS 不是前置条件。未配置时使用磁盘；以后由管理员按 [文件存储与 OSS 迁移](references/object-storage.md) 迁移，不在普通发布过程中暗中切换。

## 交付回复

只说明系统入口、做成的功能、使用角色、自动发布状态、健康状态和文件存储状态；若员工尚无业务角色要明确说明。不要展示服务器路径、Bucket、对象前缀或任何凭证。
