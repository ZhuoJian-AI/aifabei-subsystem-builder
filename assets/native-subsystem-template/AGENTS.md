# __APPLICATION_NAME__ 开发规则

- 本系统属于 Alphabet 企业，应用标识 `__APPLICATION_SLUG__`，当前子模块 `__MODULE_KEY__`。
- 页面按钮和 AI Action 必须调用同一个业务服务函数，禁止两套业务规则。
- 数据只写本模块数据库；跨模块使用版本化事件，不直连其他数据库。
- 修改和删除要求 `expectedVersion`；高风险 Action 要求灼见确认声明和幂等 requestId。
- 所有持久文件只通过 `storage.py` 的 `StorageAdapter` 读写并在数据库保存稳定 `storageKey`；不得直接拼接本地路径、调用 OSS SDK，或读取 Bucket、AccessKey 和服务端对象前缀。
- `FILE_STORAGE_DRIVER=local` 与 `oss-gateway` 的业务行为必须一致；OSS 网关地址和项目令牌仅由 Runtime 注入，故障时明确失败，禁止静默回退到另一后端。
- 不提交 `.env`、密码、Token、私钥、生产数据或数据库文件。
- 不删除已有数据卷；变更前后运行单元测试、协议验证和浏览器冒烟。
