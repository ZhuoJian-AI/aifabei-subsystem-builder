# __APPLICATION_NAME__

灼见原生模块骨架，应用标识 `__APPLICATION_SLUG__`，子模块 `__MODULE_KEY__`，ECS 本地 Git 项目名 `__LOCAL_PROJECT_NAME__`。

1. 根据真实业务修改 `subsystem.json`、`app.py` 和页面。
2. 复制 `.env.example` 为本机 `.env`，填入随机 Secret；不得提交 `.env`。
3. 运行 `docker compose up --build`，确认 `/health` 和协议验收通过。

生产数据保留在模块自己的数据库；不要连接或复制灼见 SaaS 数据库。

如业务包含 Excel、Word、PPT、PDF、图片、音视频或其他持久文件，通过管理员提供的 Alphabet 文件网关上传和下载；数据库只保存 `objectKey` 与文件元数据。不得在代码或 `.env` 中配置 OSS AccessKey，也不得使用应用卷长期保存这些文件。
