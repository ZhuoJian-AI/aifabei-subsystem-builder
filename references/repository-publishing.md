# 仓库命名与自动发布

本规则供灼见管理员 AI 或发布服务使用。业务用户不需要 GitHub 账号，业务 AI 也不得持有组织级 Token、GitHub App 私钥或跨仓库写权限。

## 身份与职责

- 业务 AI：在本地目录开发、测试和提交本地 Git，输出发布回执；不得自行创建组织仓库、授予权限或保存发布凭据。
- 管理员 AI / 灼见发布服务：校验项目后，使用已安装在目标组织的 GitHub App 或等价机器身份创建私有仓库、推送代码、绑定 Coolify 并登记灼见。
- 平台或企业管理员：首次安装发布机器身份、限定可访问组织和仓库范围；日常发布不要求管理员逐次登录 GitHub。

## 唯一命名规范

```text
companySlug  = 企业稳定英文标识，例如 aifabei
moduleSlug   = 模块系统稳定英文标识，例如 sample-review
repository   = {companySlug}-{moduleSlug}
```

这里的 `moduleSlug` 就是 Manifest 的 `applicationSlug`，不是系统内部的 `moduleKey`。两段标识均只允许小写英文字母、数字和单连字符，必须以字母或数字开头和结尾，不允许连续连字符。`moduleSlug` 不得再次包含 `companySlug` 前缀。

正式示例：

```text
aifabei-sample-review
aifabei-production-handoff
aifabei-chair-library
```

正式仓库名不得包含：

- 环境：`dev`、`staging`、`prod`；
- 时间：日期、年份；
- 版本：`v2`、`v24`；
- 验收标记：`test`、`demo`、`coldstart`；
- 部门名称：部门是 Manifest 授权主体，不是仓库边界。

验收测试是唯一例外，命名为 `{companySlug}-{moduleSlug}-coldstart-v{n}`。验收仓库必须保持私有并加测试标记，不能冒充正式模块；通过验收后创建或更新无测试后缀的正式仓库。

## 发布不变量

1. 默认目标组织从 `zhuojian-environment.json` 的 `sourceControl.owner` 读取；没有配置时使用管理员明确指定的组织，不让业务用户选择。
2. 所有企业模块仓库默认 `private`。公开灼见平台参考源码不等于公开企业业务代码或数据。
3. 创建前按完整仓库名查询。已存在则验证企业、模块和远程地址后更新；不得用 `-2`、`-new` 或版本后缀绕过重名。
4. 仓库描述使用“企业名称 · 模块名称 · 灼见原生模块”，并添加 `company-{companySlug}`、`zhuojian-native-module`、`contract-v2` topics；验收仓库额外添加 `acceptance-test`。
5. 每个正式模块系统对应一个仓库、一个 Coolify Application、一个持久卷集合和一个模块域名。一个仓库可包含该系统内多个 `moduleKey`，不为每个页面或部门再建仓库。
6. 升级沿用同一仓库，通过 commit、tag、部署记录和 `contractRevision` 追踪；不得因平台升级复制仓库。
7. 推送前运行源码、端点和端到端验证。只有管理员明确授权后才能发生创建仓库、push、部署或 SaaS 登记等外部写入。

## 环境档案字段

管理员初始化时把非敏感发布规则写入环境档案：

```json
{
  "sourceControl": {
    "provider": "github",
    "owner": "ZhuoJian-AI",
    "visibility": "private",
    "repositoryPattern": "{companySlug}-{moduleSlug}",
    "publisher": "admin-service"
  }
}
```

档案只记录发布能力和目标，不包含 GitHub App 私钥、安装令牌、PAT 或 SSH 私钥。
