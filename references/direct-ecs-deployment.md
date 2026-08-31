# ECS 直接发布

本流程替代 GitHub 和 Coolify。业务 AI 在企业 ECS 的本地 Git 仓库中开发，通过 Docker、Nginx 和灼见 ECS 登记凭证完成发布与同步。小白只描述业务并确认结果。

## 不变量

- 项目真源是 `/srv/zhuojian/repositories/{companySlug}-{applicationSlug}` 的本地 Git 仓库，不配置远程地址也能工作。
- 一个 `applicationSlug` 永远复用同一项目目录、域名、回环端口、数据目录、接入密钥和容器名。
- 一个模块系统可包含多个 `moduleKey`；新增子模块不创建新域名、新项目目录或新数据库，除非确实需要独立故障/数据/发布边界。
- 生产容器只暴露一个 `127.0.0.1:<port>` 给 Nginx；数据库、Redis 和内部 API 不映射公网端口。
- 部署前必须有干净的本地 Git commit。镜像使用 commit SHA 标识，成功版本写入发布记录，禁止使用裸 `latest` 作为回滚依据。
- 模块 Secret 保存于 `/etc/zhuojian/apps/{applicationSlug}.env`，权限 `0600`，不进入项目目录、Git、日志或回复。

## 首次发布

1. 读取 `/etc/zhuojian/runtime.json`，确认企业、组织 UUID、域名后缀、目录、端口范围、构建能力和登记凭证引用。
2. 用 `scripts/scaffold_subsystem.py` 在规范项目目录建立骨架；若目录已存在，改为检查现有项目，禁止覆盖。
3. 在开发代码前先设计 Action 目录；页面按钮和 `/api/integration/actions/{actionKey}` 必须调用同一个业务服务函数。
4. 初始化本地 Git，提交可审查的初始版本。不得创建 GitHub 仓库、GitLab 仓库或 Coolify Application。
5. 运行源码、Schema 和业务测试。失败时修代码，不发布半成品。
6. 为 `applicationSlug` 分配并持久记录一个未占用回环端口，建立：

   ```text
   /srv/zhuojian/deployments/<applicationSlug>/release.json
   /srv/zhuojian/data/<applicationSlug>/
   /etc/zhuojian/apps/<applicationSlug>.env
   /etc/nginx/conf.d/zhuojian-<enterprise>-<applicationSlug>.conf
   ```

7. 首次生成独立 `ZHUOJIAN_INTEGRATION_SECRET` 和 `SESSION_SECRET`，写入 Secret 文件；后续更新复用，轮换必须与灼见协调，不能随部署自动更换。
8. 构建 `zhuojian/<enterprise>/<applicationSlug>:<commitSHA>`，启动新容器并挂载固定数据目录。先从回环地址检查 `/health`，再原子切换 Nginx；新容器不健康时恢复旧容器和旧镜像。
9. 为 `https://<applicationSlug>.<domainSuffix>` 写入 Nginx Host 路由并签发/复用 HTTPS 证书。验证证书、`frame-ancestors`、Host 隔离、`/health` 和 Manifest。
10. 使用 `scripts/publish_subsystem.py` 和 ECS Runtime 登记凭证向灼见登记当前 Git commit、`baseUrl`、`applicationSlug`、镜像引用和模块接入密钥；凭证只从环境档案引用的 Secret 文件读取，模块接入密钥只从环境变量读取，二者都不打印。灼见检查域名后缀、组织、健康与 Manifest 后创建/复用企业应用并同步，默认不创建任何 grant。
11. 运行 `validate_endpoint.py` 和 `e2e_acceptance.py`，输出管理员接入回执。

## 后续更新

```text
读取现有本地 Git、subsystem.json 和 Manifest
→ 保留 applicationSlug、域名、端口、数据目录和 Secret
→ 以数据库迁移兼容旧数据
→ 页面与 Action 回归测试
→ 本地 Git commit
→ 构建 commit SHA 镜像
→ 新容器健康后切换
→ 失败则回到上一健康镜像
→ 请求灼见同步同一个应用的 Manifest
```

Manifest 同步负责让灼见看到新增、修改或停用的子模块、页面、Action 和事件。新增能力默认为待授权；已有 grant 不得因为 Manifest 更新而自动扩大。

## 发布登记接口

业务 AI 的首次发布和后续更新使用同一个接口：

```text
POST /api/v1/ecs-publisher/modules/register
Authorization: Bearer <ECS Runtime 登记凭证>
```

请求字段：

- `application_slug`、`application_name`：从已验证的 Manifest 取得；
- `base_url`：必须严格等于 `https://{applicationSlug}.{runtime.domainSuffix}`；
- `integration_secret`：当前模块自己的 `ZHUOJIAN_INTEGRATION_SECRET`；
- `source_commit`：干净本地 Git 的完整 commit SHA；
- `image_ref`：可选的不可变镜像引用；
- `release_metadata`：不超过 64 KiB 的非敏感部署摘要。

平台根据 Runtime 凭证自动锁定 `organizationId`、`enterpriseKey` 和域名后缀，业务 AI不能在请求体中改写这些身份。平台随后读取 Manifest、创建或复用企业应用、保存模块接入配置并执行同步；返回 `healthy` 才算登记成功。返回 `failed` 时模块继续独立运行，但灼见不把失败版本当作成功版本。

查询当前 Runtime 自己发布的模块使用：

```text
GET /api/v1/ecs-publisher/modules/{applicationSlug}
Authorization: Bearer <ECS Runtime 登记凭证>
```

## 自动登记的最小权限

管理员初始化 ECS 时安装的登记凭证必须绑定：

- 一个 `organizationId`；
- 一个企业 `enterpriseKey`；
- 一个允许的域名后缀；
- 可选的固定 ECS 公网地址；
- 仅 `register/sync module` 能力。

登记服务必须拒绝 localhost、私网/元数据地址、非 HTTPS、跨企业组织 ID、后缀外域名、危险重定向和不合格 Manifest。凭证不能创建授权、调用业务 Action、读取其他应用或操作服务器。

如果目标灼见环境尚未部署 Alembic `0048_ecs_publisher_runtime` 和 `/api/v1/ecs-publisher` 路由，发布应停在“模块健康、等待平台升级”，不得退回 GitHub/Coolify或向业务用户索要平台管理员 Token。平台升级后直接重跑发布登记，无需重建模块。

## 故障边界

- `build`：依赖或 Dockerfile 失败，保留当前健康容器和数据，修代码后重新提交。
- `health`：容器未监听 `0.0.0.0:8000`、环境变量缺失或 `/health` 非 200，拒绝切换 Nginx。
- `routing`：DNS、80/443、证书或 Nginx 问题，修管理员底座，不修改业务数据。
- `contract`：Manifest、SSO、Bridge、Action 或 Event 不合格，修业务代码或 Skill。
- `registration`：登记凭证失效、域名超范围或平台接口缺失，模块保持运行但标记“未接入灼见”，不自动扩大凭证。
- `storage`：ECS、本地 Git 或数据库存在丢失风险时停止发布并完成快照/备份；不得以远程 Git 缺失为由跳过备份。
