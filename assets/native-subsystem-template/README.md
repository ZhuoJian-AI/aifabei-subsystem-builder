# __APPLICATION_NAME__

灼见原生模块骨架，应用标识 `__APPLICATION_SLUG__`，子模块 `__MODULE_KEY__`，ECS 本地 Git 项目名 `__LOCAL_PROJECT_NAME__`。

1. 根据真实业务修改 `subsystem.json`、`app.py` 和页面。
2. 复制 `.env.example` 为本机 `.env`，填入随机 Secret；不得提交 `.env`。
3. 运行 `docker compose up --build`，确认 `/health` 和协议验收通过。

生产数据保留在模块自己的数据库；不要连接或复制灼见 SaaS 数据库。

持久文件统一通过 `storage.py` 的 `StorageAdapter` 使用稳定 `storageKey`：尚未首次部署的系统采用 Runtime 当时的默认后端；`ensure-app` 会把实际选择冻结到该系统的发布记录。管理员后来启用企业 OSS 后，新系统会获得 `oss-gateway` 配置和本系统令牌，已有系统不会暗中换后端，必须走显式、验收过的迁移。业务代码、业务负责人和业务 AI 都不需要 Bucket、对象前缀或 OSS AccessKey。选择了网关但配置不完整时应用会明确启动失败，不会静默退回硬盘。

模板的“业务文件”示例已经通过同一 Adapter 完成上传、列表、下载与可恢复删除；浏览器只接触 `fileId` 和版本，数据库内部记录 `storageKey`、后端、文件名、MIME、大小、SHA-256 和业务归属。上传必须提供准确 `Content-Length`，Runtime 用所有系统共享的 `/run/zhuojian/upload.lock` 串行保护请求缓冲与后端提交，并在写入前、逐块写入和提交前同时检查实时 90% 使用率与 5 GiB 磁盘保留线。完整接收正文后，数据库以紧邻后端提交的时间记录 `uploading`，后台再按大小和 SHA-256 收敛“对象已提交但响应丢失”等中断状态；失败项会退避而不堵住后续记录，本地临时文件只存在于保留的 staging 目录并在宽限期后清理。删除必须先由当前 SaaS 会话取得一次性确认声明，再把相同的 `requestId`、参数哈希和 `expectedVersion` 交给删除路由；对象删除和数据库提交之间即使重启，后台 lease 恢复器也会幂等完成。扩展附件能力时复用该模式，不要直接读写 `/data/files`，也不要把绝对路径、Bucket 或临时签名 URL 存入数据库。

Action 的 `requestId` 是 8–128 位受限字符标识。模板在同一个 SQLite 事务里完成业务写入、Outbox、确认消费和幂等结果，两个并发的相同请求只执行一次；请求标识一旦绑定到 Action、用户、参数和版本，就不能换参数复用。页面高风险按钮与平台 Action 共用这套服务端保护，浏览器弹窗本身不算授权。

浏览器侧所有会改数据的 `/api/ui/*` 请求还必须带本系统自己的精确 `Origin`；Runtime 通过 `ZHUOJIAN_PUBLIC_ORIGIN` 注入固定 HTTPS origin。灼见父页面和同一公司下的其他子域不能借浏览器会话调用这些接口。

运行存储单元测试：

```text
pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```
