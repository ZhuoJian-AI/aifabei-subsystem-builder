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
10. 使用 ECS 登记凭证向灼见登记 `baseUrl`、`applicationSlug` 和模块接入密钥；凭证只通过 Secret 文件或临时环境读取，不打印。灼见检查域名后缀、组织、健康与 Manifest 后创建/复用企业应用并同步，默认不创建任何 grant。
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

## 自动登记的最小权限

管理员初始化 ECS 时安装的登记凭证必须绑定：

- 一个 `organizationId`；
- 一个企业 `enterpriseKey`；
- 一个允许的域名后缀；
- 可选的固定 ECS 公网地址；
- 仅 `register/sync module` 能力。

登记服务必须拒绝 localhost、私网/元数据地址、非 HTTPS、跨企业组织 ID、后缀外域名、危险重定向和不合格 Manifest。凭证不能创建授权、调用业务 Action、读取其他应用或操作服务器。

如果灼见尚未提供该受限登记接口，发布应停在“模块健康、等待管理员首次登记”，不得退回 GitHub/Coolify或向业务用户索要平台管理员 Token。管理员登记一次后，后续 Manifest 更新仍可由平台定时同步。

## 故障边界

- `build`：依赖或 Dockerfile 失败，保留当前健康容器和数据，修代码后重新提交。
- `health`：容器未监听 `0.0.0.0:8000`、环境变量缺失或 `/health` 非 200，拒绝切换 Nginx。
- `routing`：DNS、80/443、证书或 Nginx 问题，修管理员底座，不修改业务数据。
- `contract`：Manifest、SSO、Bridge、Action 或 Event 不合格，修业务代码或 Skill。
- `registration`：登记凭证失效、域名超范围或平台接口缺失，模块保持运行但标记“未接入灼见”，不自动扩大凭证。
- `storage`：ECS、本地 Git 或数据库存在丢失风险时停止发布并完成快照/备份；不得以远程 Git 缺失为由跳过备份。
