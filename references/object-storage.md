# Alphabet 文件存储与 OSS 迁移

本规范让不懂技术的负责人正常提出“上传 Excel”“保存 PDF”“导出文件”等需求。新环境默认使用 ECS 数据盘；管理员明确完成 OSS 初始化后才切换 OSS。负责人不选择存储方案、不登录阿里云、不创建 RAM，也不接触 AccessKey。

## 模式选择

- `local-managed`：默认模式。持久文件进入 `/srv/zhuojian/data/<applicationSlug>/files/`，由模块后端鉴权后读写。
- `oss-gateway`：可选升级模式。管理员部署同地域私有 OSS 和文件网关后启用，一个系统固定使用 `apps/<applicationSlug>/`。
- 已有系统已经使用 OSS、S3 或其他稳定对象存储时保留现状，不得为了套用默认值迁回本地磁盘。
- 存储模式必须来自 `/etc/zhuojian/runtime.json`，不得在运行中因 OSS 故障、本地目录故障或磁盘不足而静默切换。

OSS 只保存附件、导出物和生成文件，不承载数据库、Docker 卷、Git 仓库或高频随机写数据。无论哪种模式，数据库、Docker、本地 Git、构建缓存和临时处理仍需要磁盘空间。

## 从第一天就遵守的可迁移契约

任何包含持久文件的系统都必须实现一个单一 `StorageAdapter`（名称可随语言调整），业务代码不得直接拼接磁盘路径、OSS 地址或 Bucket 名称。

统一接口至少覆盖：

```text
put(stream, metadata) -> storageKey
open(storageKey) -> stream 或短时下载地址
delete(storageKey)
exists(storageKey)
stat(storageKey) -> size, checksum, mime
```

数据库为每个文件保存：

```text
fileId
storageKey
storageBackend        # local 或 oss，迁移期间允许并存
originalName
mimeType
sizeBytes
checksumSha256
uploaderId
businessType / businessId
createdAt
```

约束：

- `storageKey` 使用不可猜测的稳定相对键，例如 `uploads/2026/09/<uuid>`；不得保存 `/srv/...` 绝对路径、临时路径、Bucket 名称或带过期时间的签名 URL。
- 浏览器只使用 `fileId` 调用模块的上传、下载和删除接口；不得把 `/data/files` 配成公开静态目录。
- 模块后端先校验当前用户、组织、页面和业务对象权限，再调用存储适配层。
- 上传先写同一文件系统中的 `.tmp/`，校验大小、类型和 SHA-256 后原子改名；失败必须清理临时文件。
- 删除业务记录和删除文件必须可重试。先标记待删除，再由后台任务删除文件，避免数据库成功而文件操作失败造成不可恢复的不一致。
- OCR、解析、预览和转码只使用临时目录，任务结束后清理；需要长期保留的结果重新通过适配层保存。
- 业务源码和 Git 中禁止出现 OSS AccessKey。`.env.example` 只能出现变量名和占位值。

建议统一环境变量：

```text
FILE_STORAGE_DRIVER=local|oss
FILE_STORAGE_ROOT=/data/files
STORAGE_GATEWAY_URL=
STORAGE_PROJECT_TOKEN=
```

本地模式只需要前两项。后两项仅由管理员在启用 OSS 时写入服务器 Secret，业务负责人不填写，真实值不进入 Git、日志或回复。

## 默认本地模式

管理员初始化 ECS 时：

1. 建立 `/srv/zhuojian/data/<applicationSlug>/files/.tmp/`，随模块固定数据目录一起挂载为 `/data`；容器内使用 `/data/files`。
2. 目录仅允许运行该模块的用户或容器读写。不同 `applicationSlug` 不共享文件目录。
3. 将 `FILE_STORAGE_DRIVER=local` 和 `FILE_STORAGE_ROOT=/data/files` 写入 `/etc/zhuojian/apps/<applicationSlug>.env`，权限 `0600`。
4. 为数据库和整个 `/srv/zhuojian/data/<applicationSlug>/` 配置同一恢复点的快照或异地备份。只备份数据库、不备份文件不算可恢复。
5. 每次上传前检查磁盘。默认使用率达到 80%时告警；达到 90%或剩余空间不足 5 GiB 时拒绝新上传并返回“文件存储空间不足，请联系管理员”。既有文件仍可读取，禁止自动删除未知文件腾空间。
6. 管理员可按服务器容量调整阈值，但必须在 `runtime.json` 中记录，不能由业务用户选择。

本地模式环境档案示例：

```json
{
  "capabilities": {
    "fileStorage": true,
    "objectStorage": false
  },
  "fileStorage": {
    "provider": "local-disk",
    "mode": "local-managed",
    "root": "/srv/zhuojian/data",
    "pathTemplate": "{applicationSlug}/files",
    "warningUsedPercent": 80,
    "stopUploadUsedPercent": 90,
    "minimumFreeGiB": 5,
    "verified": true
  }
}
```

## 可选 OSS 模式

只有管理员明确要求启用 OSS 时才执行：

1. 创建或绑定与 ECS 同地域的企业专用私有 Bucket；其他企业、环境、平台公共资产或不同地域的 Bucket 不得复用。
2. 选择标准存储；测试或非关键数据可选本地冗余，生产关键数据优先同城冗余。ACL 保持私有并开启阻止公共访问。
3. 为文件网关配置仅限该 Bucket 和 `apps/` 前缀的最小权限。凭证只保存在 `/etc/zhuojian/oss-gateway.env`，目录权限 `0700`、文件权限 `0600`。
4. 文件网关只绑定 Docker 私有网络或回环地址，为每个 `applicationSlug` 幂等创建受限项目身份。应用不得获得 OSS AccessKey。
5. 将 OSS 模式写入环境档案，完成真实上传、下载、删除和跨系统隔离验收后才设置 `verified=true`。

OSS 模式继续兼容以下档案：

```json
{
  "capabilities": {
    "fileStorage": true,
    "objectStorage": true
  },
  "fileStorage": {
    "provider": "aliyun-oss",
    "mode": "oss-gateway",
    "verified": true
  },
  "objectStorage": {
    "provider": "aliyun-oss",
    "mode": "gateway-signed-url",
    "bucket": "alphabet-production-<region>-files-<suffix>",
    "region": "<ECS所在地域ID>",
    "rootPrefix": "apps",
    "gatewayBaseUrl": "<Docker私网地址或回环地址>",
    "credentialRef": "/etc/zhuojian/oss-gateway.env",
    "verified": true
  }
}
```

## 从硬盘迁移到 OSS

迁移由管理员 AI 执行，业务负责人只确认维护窗口和验收结果。不得边复制边直接改数据库，也不得在未校验完整性时删除本地文件。

### 1. 预检和冻结基线

1. 确认应用已经通过统一 `StorageAdapter` 访问文件，数据库没有依赖绝对路径或永久 URL。若仍有散落的文件读写，先重构并保持本地模式上线验证。
2. 备份数据库、Secret 和 `/srv/zhuojian/data/<applicationSlug>/files/`，记录备份时间和恢复方法。
3. 统计数据库文件记录数、本地文件数、总字节数、孤儿文件和缺失文件。缺失或重复映射必须先处理。
4. 为每个文件生成或核对 SHA-256，形成只含 `fileId/storageKey/size/checksum` 的迁移清单；清单不得包含用户 Token、AccessKey 或签名 URL。
5. 确认 OSS Bucket、网关、项目前缀和容量预算，并用测试对象完成上传、下载、删除。

### 2. 复制和校验

1. 将本地 `<storageKey>` 上传到 `apps/<applicationSlug>/<storageKey>`。保留原 `storageKey`，不要改文件名来表示版本。
2. 上传程序必须可断点续跑和幂等：目标对象存在且大小与 SHA-256 一致时跳过；不一致时报告冲突，不覆盖未知对象。
3. 每个对象上传后核对大小和 SHA-256。OSS ETag 不能普遍当作文件 MD5，尤其是分片上传；以迁移清单中的 SHA-256 为准。
4. 复制阶段数据库仍保持 `storageBackend=local`，用户继续从本地读取。

### 3. 增量收口和切换

根据业务停机容忍度选择：

- 小系统：进入只读维护窗口，停止新上传，复制最后增量并全量校验后切换。
- 不能停上传的系统：先发布“新文件双写、读取优先原后端”的过渡版本；双写任何一端失败都返回失败并记录待补偿任务。待存量复制完成后再收口增量。

切换时：

1. 只把已验证文件的 `storageBackend` 从 `local` 更新为 `oss`，分批提交并记录批次；不要一次无条件更新整表。
2. 设置 `FILE_STORAGE_DRIVER=oss`，由管理员注入 `STORAGE_GATEWAY_URL` 和 `STORAGE_PROJECT_TOKEN`，重启新容器。
3. 通过原来的业务下载地址抽样和批量校验；前端 URL、业务 API 和 `fileId` 不应改变。
4. 保留读取回退：标记为 `oss` 的对象读取失败时只记录并报警，不自动永久改回本地；管理员可按批次回滚数据库标记和应用配置。

### 4. 观察、回滚与清理

1. 至少观察一个完整业务周期，检查新上传、下载、删除、导入、导出、OCR/预览任务及权限拒绝。
2. 回滚只需恢复上一版本容器、`FILE_STORAGE_DRIVER=local` 和对应数据库迁移批次；本地原文件在观察期内保持只读，不得提前删除。
3. 只有在数据库记录数、对象数、总字节、SHA-256、业务抽样和备份恢复演练全部通过，且管理员明确确认后，才进入本地清理。
4. 清理按迁移清单逐个删除已验证文件，禁止对数据根目录执行递归清空。先移到受控隔离目录并保留一个约定周期，再按精确清单删除。
5. 清理完成后仍保留数据库备份、迁移清单、失败清单、切换批次和回滚记录；Secret 和 AccessKey 不进入这些记录。

## 验收

### 本地模式

- 重建容器后文件仍可访问，文件真实位于固定宿主机数据目录，而非容器可写层。
- 另一个系统不能访问当前系统目录。
- 未授权用户不能仅凭路径下载或删除文件。
- 数据库只保存稳定键和元数据；源码中不存在绝对宿主机路径或 OSS AccessKey。
- 磁盘阈值、告警、拒绝上传和数据库加文件的一致性备份已经验证。

### OSS 模式或迁移完成

- Bucket 私有、阻止公共访问且与 ECS 同地域；应用容器中没有 OSS AccessKey。
- 每种实际文件类型完成上传和下载，另一个系统的令牌无法访问。
- 数据库引用、对象数量、总字节和 SHA-256 与迁移清单一致。
- 重建容器后文件仍可访问，普通应用数据卷不再按附件大小持续增长。
