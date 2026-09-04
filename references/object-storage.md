# Alphabet 文件存储与 OSS 迁移

本规范让不懂技术的负责人正常提出“上传 Excel”“保存 PDF”“导出文件”等需求。Runtime 首次登记先使用 ECS 固定数据盘；管理员一次性完成企业 OSS 网关并通过真实验收后，OSS 自动成为未来系统的默认后端。负责人不选择存储方案、不登录阿里云、不创建 RAM，也不接触 Bucket、AccessKey 或项目令牌。

## 模式选择

- `local-managed`：Runtime 尚未验收 OSS 时的安全初始模式。持久文件进入 `/srv/zhuojian/data/<applicationSlug>/files/`，由模块后端鉴权后读写。
- `oss-gateway`：管理员部署同地域私有 OSS 和文件网关并通过真实探针后的默认模式。每个未来系统由 Runtime 自动获得 `apps/<applicationSlug>/` 和自己的项目身份；OSS 不要求也不应预先创建“文件夹”。
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
FILE_STORAGE_DRIVER=local|oss-gateway
FILE_STORAGE_ROOT=/data/files
FILE_STORAGE_GATEWAY_URL=
FILE_STORAGE_TOKEN=
```

本地模式只需要前两项。后两项由 Runtime 的 root-only 网关管理命令为每个系统自动生成并直接注入容器；管理员和业务负责人都不逐项目填写，真实值不进入 Git、日志或回复。旧变量 `STORAGE_GATEWAY_URL/STORAGE_PROJECT_TOKEN` 只为已有系统兼容，新项目使用上面的 `FILE_STORAGE_*` 名称。

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
3. 创建一个无控制台登录的 RAM 用户，只给文件网关使用；每个公司/环境一枚网关身份，不是每个项目一枚 AccessKey。策略必须同时把列举限制在 `apps`/`apps/*`，并把对象操作限制在该 Bucket 的 `apps/*`：

   ```json
   {
     "Version": "1",
     "Statement": [
       {
         "Effect": "Allow",
         "Action": "oss:ListObjects",
         "Resource": "acs:oss:*:*:<企业Bucket>",
         "Condition": {"StringLike": {"oss:Prefix": ["apps", "apps/*"]}}
       },
       {
         "Effect": "Allow",
         "Action": [
           "oss:GetObject", "oss:PutObject", "oss:DeleteObject",
           "oss:ListParts", "oss:AbortMultipartUpload"
         ],
         "Resource": "acs:oss:*:*:<企业Bucket>/apps/*"
       }
     ]
   }
   ```

   OSS 资源 ARN 的地域字段固定为 `*`；实际地域由 Endpoint 和 Bucket 决定。不要把 `oss-cn-hongkong` 填进 ARN 的地域位置。
4. 管理员只在创建 AccessKey 时接触一次明文，通过不回显的临时文件传进服务器，最终写成 root:root `0600` 的 `/etc/zhuojian/oss-gateway.env`。内容格式如下；不得把真实值放进命令参数、Git、截图、日志或回复：

   ```text
   OSS_ENDPOINT=https://oss-<地域ID>.aliyuncs.com
   OSS_BUCKET=<企业Bucket>
   OSS_ACCESS_KEY_ID=<仅管理员写入>
   OSS_ACCESS_KEY_SECRET=<仅管理员写入>
   ```

5. 将 `assets/admin-runtime/gateway/` 安全传到服务器后，以 root 从该目录运行 `sh ./install.sh`。网关固定安装在 `/opt/zhuojian/storage-gateway`，只加入 `zhuojian-storage` Docker 私网，不发布主机端口；应用容器永远不获得 OSS AccessKey。
6. 已有 Runtime 只运行下面的一次性原地升级，绝对不要重跑 `provision_runtime.py`：

   ```text
   zhuojian-runtime configure-oss-gateway \
     --bucket <企业Bucket> \
     --region <阿里云地域ID>
   ```

   该命令会真实执行 PUT、GET、DELETE、两个临时应用同名对象隔离和临时身份撤销。探针失败时不改 Runtime；成功后才原子写入 `verified=true` 并把 `oss-gateway` 设为未来应用默认模式。
7. 新系统不再需要管理员。业务 AI 在本地 Git 首次干净提交后执行 `zhuojian-runtime ensure-app <applicationSlug>`；Runtime 幂等生成项目身份并写入 `/etc/zhuojian/apps/<applicationSlug>.storage.env`，部署时自动注入。命令不输出令牌，重复执行复用原身份；被管理员暂停或撤销的身份不会被自动复活。

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
    "mode": "gateway-api-v1",
    "bucket": "alphabet-production-<region>-files-<suffix>",
    "region": "<ECS所在地域ID>",
    "rootPrefix": "apps",
    "gatewayBaseUrl": "http://zhuojian-storage-gateway:8080",
    "credentialRef": "/etc/zhuojian/oss-gateway.env",
    "verified": true
  }
}
```

## 从硬盘迁移到 OSS

迁移由管理员 AI 执行，业务负责人只确认维护窗口和验收结果。当前模板提供按记录后端读取、稳定 `fileId/storageKey` 和可重试删除所需的基础能力，但不把一个通用脚本伪装成适合所有旧系统的全自动迁移器；管理员必须先根据该系统的真实表结构生成迁移清单和可重试作业。不得边复制边直接改数据库，也不得在未校验完整性时删除本地文件。

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

1. 只把已验证文件的 `storageBackend` 从 `local` 更新为 `oss-gateway`，分批提交并记录批次；不要一次无条件更新整表。
2. 由 Runtime 生成/复用该系统身份并把 `FILE_STORAGE_DRIVER=oss-gateway`、`FILE_STORAGE_GATEWAY_URL` 和 `FILE_STORAGE_TOKEN` 注入新容器；不得人工复制令牌。
3. 通过原来的业务下载地址抽样和批量校验；前端 URL、业务 API 和 `fileId` 不应改变。
4. 保留读取回退：标记为 `oss` 的对象读取失败时只记录并报警，不自动永久改回本地；管理员可按批次回滚数据库标记和应用配置。

### 4. 观察、回滚与清理

1. 至少观察一个完整业务周期，检查新上传、下载、删除、导入、导出、OCR/预览任务及权限拒绝。
2. 回滚只需恢复上一版本容器、`FILE_STORAGE_DRIVER=local` 和对应数据库迁移批次；本地原文件在观察期内保持只读，不得提前删除。
3. 只有在数据库记录数、对象数、总字节、SHA-256、业务抽样和备份恢复演练全部通过，且管理员明确确认后，才进入本地清理。
4. 清理按迁移清单逐个删除已验证文件，禁止对数据根目录执行递归清空。先移到受控隔离目录并保留一个约定周期，再按精确清单删除。
5. 清理完成后仍保留数据库备份、迁移清单、失败清单、切换批次和回滚记录；Secret 和 AccessKey 不进入这些记录。

Runtime 的普通 `backup` 只覆盖应用数据库、本地数据和恢复配置，不复制 OSS 对象，也不保证数据库与 OSS 在同一时刻快照。OSS 系统必须另外配置管理员认可的对象版本、清单或异地保护策略，并在恢复演练中验证数据库引用与对象集合一致；不得把“本地备份成功”描述为“OSS 文件已经备份”。

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
