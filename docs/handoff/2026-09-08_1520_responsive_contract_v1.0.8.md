# 全端响应式契约 v1.0.8

## Task

- 在不增加小白操作步骤、不修改子系统契约版本的前提下，让以后由 Skill 创建和更新的子系统同时兼容电脑、平板与手机。
- 保留并整合 `1.0.7` 的 Runtime 自动发布能力，使用下一个稳定补丁版本 `1.0.8`。

## Changed behavior

- 顶层 `SKILL.md` 只增加一条全端可用结果规则，复杂要求仍位于按需读取的平台契约。
- `platform-contract.md` 明确 SaaS 与子系统各自的响应式责任、同 DOM 原则、嵌入/独立双模式、触摸/键盘/鼠标、局部表格滚动、安全区、动态视口、软键盘、旋转和浏览器矩阵。
- 原生子系统模板内置桌面侧栏、平板紧凑导航和手机抽屉，使用同一份导航与业务组件；嵌入模式继续隐藏重复导航。
- 源码校验器新增高可信 viewport、`viewport-fit=cover` 和根最小宽度拦截，并对可疑固定宽度、表格容器及 hover-only 交互给出定位警告。
- 端到端验收结果新增 `responsive_acceptance_pass` 与逐页面、逐模式、逐引擎、逐视口待验收明细；注册前不会伪造真实员工 SSO 浏览器结果。
- Skill 版本更新为 `1.0.8`；`contractRevision` 继续为 `2.5`，Manifest Schema 没有新增自我声明字段。

## Verification

- `python -m pytest -q`: `176 passed, 41 skipped`.
- `python C:/Users/王鑫涛/.codex/skills/.system/skill-creator/scripts/quick_validate.py .`: `Skill is valid!`.
- `git diff --check`: passed.
- SaaS 侧独立的 Playwright 验收已在 Chromium、WebKit、Firefox 和 8 组视口中全部通过，为模板/契约提供真实浏览器基准。

## Decisions and risks

- 静态分析只阻止高可信错误；合法的大表格、画布或宽组件由真实浏览器验收决定，避免只凭字符串误伤。
- Python 模板浏览器测试在未安装相应 Playwright 浏览器的环境中会跳过该引擎；稳定发布前的 SaaS 三引擎套件提供强制浏览器证据。
- 本 Skill 变更不授权或执行任何既有业务子系统的部署。

## Remaining work

- 合并最新 `origin/main` 后，从干净的合并后 `main` 构建并校验稳定包，发布 GitHub Release `v1.0.8`，再做公开无登录下载/更新检查。
