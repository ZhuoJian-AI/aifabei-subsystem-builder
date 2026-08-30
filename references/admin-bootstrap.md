# 管理员与服务器初始化

本模式供灼见管理员 AI 使用。目标是把企业提供的 ECS 变成可重复部署的模块运行环境，并生成业务 AI 能读取、但不含任何密钥的环境档案。

## 输入与边界

- 允许输入：云控制台会话、ECS 地址、一次性或长期管理员凭据、目标企业、测试域名、Coolify 项目/团队。
- 凭据只能通过受控会话、Secret 管理或交互式隐藏输入使用；环境档案、Git、日志和回复不得出现密码、Token 或私钥。
- 默认保留服务器上全部既有容器、虚拟主机、数据库和数据卷。新资源使用唯一 `zhuojian-<enterprise>-<application>` 标签。
- 管理员 AI 只建立底座和发布入口，不替业务 AI 编写业务流程。
- 远程仓库与发布身份按 [仓库命名与自动发布](repository-publishing.md) 配置；业务用户和业务 AI 不需要 GitHub 账号。

## 初始化流程

1. 只读记录实例区域、公网/内网地址、系统版本、磁盘、Docker、监听端口、反向代理、容器、网络、数据卷和现有域名。
2. 确认目标域名真实解析到该 ECS。一个系统一个域名；多个系统可共享 ECS，但必须使用独立容器、回环端口、网络和数据卷。
3. 只公开 80/443；数据库、Redis 和内部 API 不发布到宿主机。SSH 沿用已有管理策略，不为方便测试扩大公网范围。
4. 将 ECS 加入管理员指定的 Coolify Team。首次可使用用户授权的 root 会话；自动部署应切换为 Coolify 专用部署密钥。
5. 建立 HTTPS 和 Host 路由，验证两个不同域名不会进入同一容器。不得用本机 hosts 文件冒充 DNS 完成。
6. 生成 `zhuojian-environment.json`，只写非敏感能力和标识；密钥写入 Coolify Secret/环境变量，由档案中的 `secretRefs` 引用名字。

### Coolify 健康检查约束

- 模块 Dockerfile 必须定义不依赖额外系统包的 `HEALTHCHECK`，推荐使用项目运行时自带的 Python/Node 请求 `/health`。
- Dockerfile 已有健康检查时，Coolify Application 设置 `health_check_enabled=false`，保留镜像检查；不要让 Coolify 用镜像里不存在的 `curl` 或 `wget` 覆盖它。
- 如果明确启用 Coolify 健康检查，镜像必须实际包含它调用的命令，并在冷启动验收中从最终镜像内执行一次。
- `requirements.txt`/生产镜像只放运行时依赖；pytest、Playwright、Schema 校验器等写入独立的开发依赖文件，不得把浏览器测试栈安装进小规格 ECS 的生产容器。

## 环境档案

```json
{
  "schemaVersion": 1,
  "enterpriseKey": "aifabei",
  "environment": "staging",
  "runtimeId": "aifabei-hk-01",
  "deployment": {"provider": "coolify", "serverId": "<opaque-id>"},
  "domains": {"suffix": "aifabei.staging.zhuojianai.com", "httpsRequired": true},
  "capabilities": {"docker": true, "compose": true, "persistentVolumes": true},
  "network": {"publicPorts": [80, 443], "privateServicePortsOnly": true},
  "sourceControl": {
    "provider": "github",
    "owner": "ZhuoJian-AI",
    "visibility": "private",
    "repositoryPattern": "{companySlug}-{moduleSlug}",
    "publisher": "admin-service"
  },
  "secretRefs": ["ZHUOJIAN_INTEGRATION_SECRET", "SESSION_SECRET"],
  "platformBindings": ["ZHUOJIAN_ORGANIZATION_ID"],
  "verifiedAt": "<RFC3339>"
}
```

业务 AI 读取档案后只选择 `applicationSlug`、容器名称和持久卷，不需要重新登录阿里云或理解 DNS、安全组和 Coolify。

## 验收与回滚

- 验收：DNS、HTTPS、`/health`、Host 隔离、Coolify部署记录、容器健康和数据库端口不公网暴露。
- 记录新增 DNS record ID、安全组 rule ID、Coolify resource ID、容器/卷标签和证书域名。
- 回滚只删除本次新增且带精确标签的 DNS、规则、Coolify资源和空测试卷；不运行 Docker 全局 prune，不删除已有卷。
