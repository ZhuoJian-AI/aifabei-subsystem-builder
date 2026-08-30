# __APPLICATION_NAME__ 开发规则

- 本系统属于爱法贝企业，应用标识 `__APPLICATION_SLUG__`，当前子模块 `__MODULE_KEY__`。
- 页面按钮和 AI Action 必须调用同一个业务服务函数，禁止两套业务规则。
- 数据只写本模块数据库；跨模块使用版本化事件，不直连其他数据库。
- 修改和删除要求 `expectedVersion`；高风险 Action 要求灼见确认声明和幂等 requestId。
- 不提交 `.env`、密码、Token、私钥、生产数据或数据库文件。
- 不删除已有数据卷；变更前后运行单元测试、协议验证和浏览器冒烟。
