# Alphabet 企业文件存储

本规范让不懂技术的负责人正常提出“上传 Excel”“保存 PDF”“导出文件”等业务需求，后续创建系统的 AI 自动使用企业 OSS。负责人不选择 Bucket、不创建 RAM、不接触 AccessKey，也不需要理解对象存储。

## 固定边界

- Alphabet 在每个环境、每个 ECS 所在地域创建或绑定一个专用私有 OSS Bucket。同一 ECS 上的多个 Alphabet 系统共用它，不为每个系统重复创建 Bucket。
- Bucket 必须与 ECS 同地域。已有 Bucket 若属于其他企业、其他环境、平台公共资产或不同地域，不得直接复用。
- 一个系统固定使用 `apps/<applicationSlug>/`；常用子前缀为 `uploads/`、`exports/` 和 `generated/`。OSS 前缀是逻辑路径，无需管理员提前创建文件夹。
- OSS 保存用户上传和系统生成的持久文件。数据库保存业务数据及文件元数据；ECS 数据盘保存数据库、Docker、本地 Git、临时处理文件和缓存。
- 禁止用 OSS 或 ossfs 承载数据库文件、Docker 卷、Git 仓库或高频随机写数据。

## 管理员一次性初始化

管理员 AI 在业务负责人开始创建系统前完成一次：

1. 读取 ECS 控制台确认地域，创建或绑定同地域 Bucket。名称建议使用 `alphabet-<environment>-<region>-files-<唯一后缀>`。
2. 选择标准存储；测试或非关键数据可选本地冗余，生产关键数据优先同城冗余。Bucket ACL 保持私有，并开启阻止公共访问。
3. 为文件网关配置仅限该 Bucket 和 `apps/` 前缀的最小权限。优先使用 ECS RAM 角色；无法使用角色时，将专用凭证只保存在 `/etc/zhuojian/oss-gateway.env`，目录权限 `0700`、文件权限 `0600`。
4. 在 ECS 上部署一个共享文件网关，只绑定 Docker 私有网络或 `127.0.0.1`。网关持有 OSS 权限，业务系统和负责人都不持有 OSS AccessKey。
5. 文件网关必须支持幂等创建项目身份，并将每个项目令牌锁定为 `apps/<applicationSlug>/`；系统不能提交任意 Bucket、企业或其他系统的对象前缀。
6. 将不含密钥的存储信息写入 `/etc/zhuojian/runtime.json`，运行 `scripts/validate_storage_profile.py`，再用两个不同 `applicationSlug` 验证互相不能读、写、删文件。

管理员最终只报告 Bucket 和网关“已配置/待配置”，不能在回复、日志、Git 或业务项目中显示凭证。

## 环境档案

```json
{
  "capabilities": {
    "objectStorage": true
  },
  "objectStorage": {
    "provider": "aliyun-oss",
    "mode": "gateway-signed-url",
    "bucket": "alphabet-staging-<region>-files-<suffix>",
    "region": "<ECS所在地域ID>",
    "rootPrefix": "apps",
    "gatewayBaseUrl": "http://127.0.0.1:<文件网关端口>",
    "credentialRef": "/etc/zhuojian/oss-gateway.env",
    "verified": true
  }
}
```

环境档案可供业务 AI 判断能力，但 `credentialRef` 指向的文件只允许网关进程和管理员读取。业务应用仅接收：

```text
STORAGE_GATEWAY_URL=<企业文件网关地址>
STORAGE_PROJECT_TOKEN=<只允许当前 applicationSlug 的项目令牌>
```

## 新建或更新系统

业务描述出现任何持久文件需求时，业务 AI 自动执行：

1. 读取 `runtime.json`，确认 `objectStorage.verified=true`；不向负责人询问 OSS 方案。
2. 由受控部署入口调用文件网关的管理员接口，按 `applicationSlug` 幂等创建或复用项目身份，得到当前系统的受限令牌并写入该系统的 `0600` Secret 文件。
3. 上传前由业务后端检查用户和业务权限，再调用网关申请短时上传 URL；浏览器直接上传文件，文件正文不经过业务服务器。
4. 下载或删除前再次检查业务权限，再通过网关申请短时 URL或执行删除。数据库只保存 `objectKey`、文件名、大小、MIME、校验和、上传者和业务对象。
5. OCR、解析、预览和转码可下载到临时目录，任务结束后必须清理；输入和输出的持久副本仍在 OSS。

OSS 或网关不可用时必须明确返回“文件服务暂不可用”，不得把新文件悄悄写到 ECS 长期目录。系统没有持久文件需求时，不需要注入项目令牌。

## 验收

- Bucket 私有且阻止公共访问，地域与 ECS 相同。
- 网关健康，应用容器中没有 OSS AccessKey。
- 每种实际使用的文件类型至少完成一次上传和下载。
- 对象位于 `apps/<applicationSlug>/`，另一个系统的令牌无法访问。
- 数据库保存对象引用和元数据；重建容器后文件仍可访问。
- 上传后普通应用数据卷没有按文件大小持续增长。
