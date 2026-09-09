# SaaS 受控专业 AI 契约 v1.0.9

## Task

- 允许 Alphabet 子系统在不持有模型供应商密钥的前提下使用 OCR、图片比较/分类、语音转写、结构化抽取和业务预测。
- 将模型执行、身份和权限复核、输入暂存及清理、结果 Schema 校验留在 SaaS；子系统只显示并校正草稿，人工确认后再调用普通 Action。

## Changed behavior

- `platformAiCapability` 成为 v2.5 Action 的可选受控声明，只允许六种固定能力、声明的输入类型和强制人工确认。
- 顶层 Skill 只增加一条结果边界；详细 Bridge、草稿、撤权、失败关闭和验收规则位于平台契约。
- 原生模板可按业务需要选择能力，发送绑定当前应用、模块、页面、Action、请求号和启动 nonce 的 Bridge 请求；AI 草稿不会自动保存。
- Schema、语义校验器和源码校验器拒绝写操作伪装、开放结果 Schema、缺失人工确认、未绑定 Bridge 及子系统直连供应商。
- 技术预检新增专业 AI 契约与 SaaS 员工端到端状态，未运行时诚实返回 `not_run`。

## Verification

- `python -m pytest -q`: `182 passed, 41 skipped`.
- `python C:/Users/王鑫涛/.codex/skills/.system/skill-creator/scripts/quick_validate.py .`: `Skill is valid!`.
- 生成的专业 AI 示例子系统通过源码契约校验；普通原生模板继续通过自身安全与恢复测试。
- `git diff --check`: passed.

## Decisions and risks

- 能力声明是草稿契约，不授予权限，也不允许模型选择 provider、model、组织或写入目标。
- 真实员工的模型调用必须由 SaaS 环境完成；子系统端点预检不会冒充 SaaS 端到端通过。
- 本次未修改任何既有业务子系统，也未改变契约版本 `2.5`。

## Remaining work

- SaaS 对应实现需独立合并、部署并使用已声明专业 AI 能力的测试子系统完成真实员工端到端验收。
- 合并最新 `origin/main` 后从合并提交构建并发布 GitHub Release `v1.0.9`，再验证公开更新器。
