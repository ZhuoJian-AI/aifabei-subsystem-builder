# __APPLICATION_NAME__ 开发规则

- 本系统属于 Alphabet 企业，应用标识 `__APPLICATION_SLUG__`，当前子模块 `__MODULE_KEY__`。
- 页面按钮和 AI Action 必须调用同一个业务服务函数，禁止两套业务规则。
- 数据只写本模块数据库；跨模块使用版本化事件，不直连其他数据库。
- 修改、审批和删除要求 `expectedVersion`；Action 的 `requestId` 必须满足模板 schema，并绑定用户、参数和版本，业务提交、Outbox、确认消费与幂等结果必须同事务完成。
- 页面高风险按钮与平台 Action 共用服务端确认校验；必须检查一次性 `confirmationId`、确认人、五分钟时效和规范 JSON 参数哈希。文件删除也不得提供绕过该流程的裸路由。
- 所有持久文件只通过 `storage.py` 的 `StorageAdapter` 读写并在数据库保存稳定 `storageKey`；不得直接拼接本地路径、调用 OSS SDK，或读取 Bucket、AccessKey 和服务端对象前缀。
- `FILE_STORAGE_DRIVER=local` 与 `oss-gateway` 的业务行为必须一致；OSS 网关地址和项目令牌仅由 Runtime 注入，故障时明确失败，禁止静默回退到另一后端。
- 文件上传必须保留模板的准确 `Content-Length`、全主机共享 flock、实时使用率加双副本容量门禁、紧邻后端提交的 `uploading` 元数据、专用 staging 和带退避的后台校验恢复；不得改回无长度流式请求、先写对象后写数据库、扫描业务键清理临时文件，或让坏记录堵死恢复队列。文件删除必须保留 pending lease 和后台幂等恢复。
- 不提交 `.env`、密码、Token、私钥、生产数据或数据库文件。
- 平台接入固定使用 v2.5 四类凭证：Manifest/事件拉取只用 `zjmf_`，SSO 换码只用 `zjss_`，Action JWT 只用 `zjac_`，事件投递 JWT 只用 `zjev_`；禁止兼容回单一共享 Secret。
- SSO 入口只接收 `code`、`redirect`、`launch_nonce`，由后端向固定 `ZHUOJIAN_SAAS_ORIGIN` 换取声明并严格复验绑定关系和 120 秒时效；浏览器不得直接持有项目凭证。
- 完整 SSO claims 只存服务端 `browser_sessions`；页面查看和 Action 必须调用 SaaS session-check 取得当前资源数据范围并在撤权时失败关闭。
- 通用 create/update 禁止直接写 `status` 等受控状态；审批、删除和其他状态迁移只能走对应 Action，其中审批和删除不得取消确认。
- 不删除已有数据卷；变更前后运行单元测试、协议验证和浏览器冒烟。
- 不把业务值拼入 `innerHTML`；使用 DOM 节点和 `textContent`。运行 Uvicorn 时关闭 access log，避免一次性 SSO ticket 进入查询字符串日志。
- 所有会改变状态的 `/api/ui/*` 请求必须校验浏览器 `Origin` 与 Runtime 注入的本系统公网 origin 完全一致；SaaS 父页面或其他业务子域都不能作为 API origin。
