# ECS 直接发布

本流程替代 GitHub 和 Coolify。业务 AI 在企业 ECS 的本地 Git 仓库中开发，通过 Docker、Nginx 和灼见 ECS 登记凭证完成发布与同步。小白只描述业务并确认结果。

## 不变量

- 项目真源默认是 `/srv/zhuojian/repositories/{companySlug}-{applicationSlug}` 的服务器本地 Git 仓库，不配置远程地址也能工作；业务负责人电脑或 GitHub 中的副本不能自动取代它。
- 一个 `applicationSlug` 永远复用同一项目目录、域名、回环端口、数据目录、原有契约凭证和容器名。新系统使用 `2.5` 四类分用凭证；维护现有 `2.4` 系统时保留单个历史凭证。
- 一个模块系统可包含多个 `moduleKey`；新增子模块不创建新域名、新项目目录或新数据库，除非确实需要独立故障/数据/发布边界。
- 生产容器只暴露一个 `127.0.0.1:<port>` 给 Nginx；数据库、Redis 和内部 API 不映射公网端口。
- 部署前必须有干净的本地 Git commit。镜像使用 commit SHA 标识，成功版本写入发布记录，禁止使用裸 `latest` 作为回滚依据。
- 模块 Secret 保存于 `/etc/zhuojian/apps/{applicationSlug}.env`，权限 `0600`，不进入项目目录、Git、日志或回复。
- 需要持久文件时默认建立 `/srv/zhuojian/data/<applicationSlug>/files`，把 `FILE_STORAGE_DRIVER=local` 和 `FILE_STORAGE_ROOT=/data/files` 写入模块 Secret。模块必须通过统一存储适配层访问稳定 `storageKey`。环境明确为 OSS 模式时，部署入口才向文件网关创建或复用项目身份并注入 `FILE_STORAGE_GATEWAY_URL` 和 `FILE_STORAGE_TOKEN`；模块永远不能获得 OSS AccessKey。

## 已有项目的开发基线

修改或增加功能前，先只读记录服务器项目的 Git HEAD、工作树状态和最近提交，再与当前可用的业务负责人本地副本或远程 Git 比较。比较完成前不得执行拉取、重置、覆盖目录或部署。

- 服务器提交领先、双方历史已经分叉，或服务器存在尚未同步的源码修改时，先从服务器拉取项目到独立工作区，并从服务器状态创建本次开发分支。本地已有的新功能只能合并到这个基线上，不能反向覆盖服务器。
- 服务器存在未提交修改时，先逐项区分源码与运行数据。经审查的源码用明确文件清单创建提交或补丁；`.env`、Secret、数据库、上传文件、日志、缓存、依赖目录和构建产物不得进入 Git，也不得随项目同步。
- 本地或远程提交只有在确认完整包含服务器 HEAD、且服务器没有额外源码修改时，才能作为更新后的基线。双方分叉时保留服务器行为，再合并本地新增功能；冲突不能靠覆盖目录解决。
- 只有负责人明确指定另一份权威源码，或服务器经核实只有镜像、编译产物、生成文件而没有可验证的源码仓库时，才不采用服务器基线。例外必须在交付回复中说明。

任何情况下都不得使用 `git reset --hard`、强制推送或删除服务器项目目录来完成同步。运行容器内的临时手改不是可信源码；发现这类改动时先报告差异，不能悄悄复制回项目。

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
   /srv/zhuojian/data/<applicationSlug>/files/.tmp/
   /etc/zhuojian/apps/<applicationSlug>.env
   /etc/nginx/conf.d/zhuojian-<enterprise>-<applicationSlug>.conf
   ```

7. Runtime 为新系统生成 Manifest、SSO、Action、Event 四类 `2.5` 项目凭证和 `SESSION_SECRET`；维护已有 `2.4` 系统时复用 `ZHUOJIAN_INTEGRATION_SECRET`，不得自动升级。Secret 写入受控文件，普通更新不由业务 AI 手工轮换。含文件能力时按环境档案注入存储配置。后续更新复用原存储模式和稳定 `storageKey`；从硬盘迁移 OSS 必须单独执行。
8. 构建 `zhuojian/<enterprise>/<applicationSlug>:<commitSHA>`。已有系统在切换前由 Runtime 自动通知 SaaS 进入发布闸门；此时员工入口和 Action 临时关闭。随后启动新容器并挂载固定数据目录，先从回环地址检查 `/health`，再原子切换 Nginx；新容器不健康时恢复旧容器、取消闸门并继续旧版本。
9. 为 `https://<applicationSlug>.<domainSuffix>` 写入 Nginx Host 路由并签发/复用 HTTPS 证书。验证证书、`frame-ancestors`、Host 隔离、`/health` 和 Manifest。
10. 运行 `validate_endpoint.py` 和 `e2e_acceptance.py`。它们只算登记前技术预检，不得冒充真实员工 SSO 验收；任一项失败都不得登记版本。
11. 使用 `scripts/publish_subsystem.py` 登记当前 Git commit、`baseUrl`、镜像引用和 Runtime 管理的当前契约凭证；脚本根据 Manifest 在 `2.4` 单凭证与 `2.5` 四凭证之间选择，从受控 Secret 文件读取且不打印。灼见检查域名、组织、健康与 Manifest 后创建/复用应用，不自动创建授权。新系统返回 `pending_review` 属于正常结果，需管理员核对差异并启用后才对员工开放。

## 后续更新

```text
比较服务器 Git 与其他副本；服务器较新或有独有源码时先从服务器建立基线
→ 读取服务器现有 Git、subsystem.json 和 Manifest
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

回滚同样必须经过发布闸门并重新登记，不能只换本地容器：

```text
zhuojian-runtime rollback <applicationSlug> [--commit <commitSHA>]
python <skill>/scripts/publish_subsystem.py \
  --project-path <项目目录> \
  --base-url https://<applicationSlug>.<domainSuffix> \
  --use-running-release
```

第一条成功后状态是 `awaiting_platform_registration`，第二条把实际运行的旧 commit、Manifest 和凭证重新交给 SaaS 核对。登记成功或管理员批准前，SaaS 保持关闭；不得把本地健康误报成平台已经可用。

## 发布登记接口

业务 AI 的首次发布和后续更新使用同一个接口：

```text
POST /api/v1/ecs-publisher/modules/register
Authorization: Bearer <ECS Runtime 登记凭证>
```

请求字段：

- `application_slug`、`application_name`：从已验证的 Manifest 取得；
- `base_url`：必须严格等于 `https://{applicationSlug}.{runtime.domainSuffix}`；
- `2.5` 请求使用 `credentials`：包含 Runtime 管理的 `manifest_access_token`、`sso_exchange_token`、`action_signing_secret`、`event_signing_secret`，四值有固定类型前缀且互不相同；
- `2.4` 兼容请求使用 `integration_secret`：值来自已有 `ZHUOJIAN_INTEGRATION_SECRET`。两种字段必须且只能出现一种；
- `source_commit`：干净本地 Git 的完整 commit SHA；
- 镜像引用由 Runtime 从真实运行容器核对后自动登记，业务 AI 不填写；
- `release_metadata`：不超过 64 KiB 的非敏感部署摘要。

平台根据 Runtime 凭证自动锁定 `organizationId`、`enterpriseKey` 和域名后缀，业务 AI 不能改写这些身份。返回 `healthy` 表示已同步；返回 `pending_review` 表示代码部署成功但新清单尚未获管理员批准，两者都不是自动授权。返回 `failed` 时模块继续独立运行，但灼见不把该版本当作成功版本。

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
- `registration`：登记凭证失效、域名超范围或平台接口缺失，模块可保持本地运行，但 SaaS 入口维持关闭并标记“等待平台登记”，不自动扩大凭证。
- `storage`：ECS、本地 Git、数据库或固定文件目录存在丢失风险时停止发布并完成同一恢复点的快照/备份；本地目录、磁盘阈值或权限未验证时不得把文件写入容器层或公开静态目录。
- `object-storage`：环境明确选择 OSS 时，Bucket、网关或系统前缀授权未通过则停止发布或迁移；不得把 OSS 凭证交给业务 AI，也不得静默切回本地模式。
