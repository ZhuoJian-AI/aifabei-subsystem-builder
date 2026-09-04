# __APPLICATION_NAME__

灼见原生模块骨架，应用标识 `__APPLICATION_SLUG__`，子模块 `__MODULE_KEY__`，ECS 本地 Git 项目名 `__LOCAL_PROJECT_NAME__`。

1. 根据真实业务修改 `subsystem.json`、`app.py` 和页面。
2. 复制 `.env.example` 为本机 `.env`，填入随机 Secret；不得提交 `.env`。
3. 运行 `docker compose up --build`，确认 `/health` 和协议验收通过。

生产数据保留在模块自己的数据库；不要连接或复制灼见 SaaS 数据库。

持久文件统一通过 `storage.py` 的 `StorageAdapter` 使用稳定 `storageKey`：默认写入 `/data/files`，管理员启用企业 OSS 后，Runtime 会把 `FILE_STORAGE_DRIVER` 切换为 `oss-gateway` 并自动向容器注入内部网关地址与本系统令牌。业务代码、业务负责人和业务 AI 都不需要 Bucket、对象前缀或 OSS AccessKey。选择了网关但配置不完整时应用会明确启动失败，不会静默退回硬盘。

模板的“业务文件”示例已经通过同一 Adapter 完成上传、列表、下载与可重试删除；浏览器只接触 `fileId`，数据库内部记录 `storageKey`、后端、文件名、MIME、大小、SHA-256 和业务归属。扩展附件能力时复用该模式，不要直接读写 `/data/files`，也不要把绝对路径、Bucket 或临时签名 URL 存入数据库。

运行存储单元测试：

```text
pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```
