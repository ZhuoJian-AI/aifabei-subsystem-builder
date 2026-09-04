#!/usr/bin/env python3
"""Validate an Alphabet local-disk or OSS file-storage runtime profile."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path, PurePosixPath


BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
REGION_RE = re.compile(r"^[a-z0-9][a-z0-9-]+$")
GATEWAY_URL = "http://zhuojian-storage-gateway:8080"
CREDENTIAL_REF = "/etc/zhuojian/oss-gateway.env"
OSS_REQUIRED_FIELDS = {
    "provider", "mode", "bucket", "region", "rootPrefix",
    "gatewayBaseUrl", "credentialRef", "verified",
}
LOCAL_REQUIRED_FIELDS = {
    "provider", "mode", "root", "pathTemplate", "warningUsedPercent",
    "stopUploadUsedPercent", "minimumFreeGiB", "verified",
}


def _absolute_posix(value: object) -> bool:
    return isinstance(value, str) and PurePosixPath(value).is_absolute()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="校验 Alphabet ECS 的本地硬盘或 OSS 文件存储档案"
    )
    parser.add_argument("--runtime-profile", required=True, help="例如 /etc/zhuojian/runtime.json")
    parser.add_argument(
        "--require-mode",
        choices=("local", "oss"),
        help="可选：要求环境必须使用指定存储模式",
    )
    args = parser.parse_args()
    try:
        raw = (
            sys.stdin.read()
            if args.runtime_profile == "-"
            else Path(args.runtime_profile).expanduser().read_text(encoding="utf-8")
        )
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"无法读取环境档案：{exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit("环境档案顶层必须是 JSON 对象")

    failures: list[str] = []
    capabilities = payload.get("capabilities")
    file_storage = payload.get("fileStorage")
    object_storage = payload.get("objectStorage")
    legacy_oss = not isinstance(file_storage, dict) and isinstance(object_storage, dict)
    if not isinstance(capabilities, dict) or (
        capabilities.get("fileStorage") is not True
        and not (legacy_oss and capabilities.get("objectStorage") is True)
    ):
        failures.append("capabilities.fileStorage 必须为 true")

    # Backward compatibility for an already-provisioned OSS runtime.
    if not isinstance(file_storage, dict) and isinstance(object_storage, dict):
        file_storage = {
            "provider": "aliyun-oss",
            "mode": "oss-gateway",
            "verified": object_storage.get("verified"),
        }
    if not isinstance(file_storage, dict):
        failures.append("缺少 fileStorage 档案")
        file_storage = {}

    provider = file_storage.get("provider")
    mode = file_storage.get("mode")
    if provider == "local-disk" and mode == "local-managed":
        selected_mode = "local"
        missing = sorted(
            field for field in LOCAL_REQUIRED_FIELDS
            if file_storage.get(field) in (None, "")
        )
        if missing:
            failures.append("fileStorage 缺少字段：" + ", ".join(missing))
        if not _absolute_posix(file_storage.get("root")):
            failures.append("fileStorage.root 必须是绝对 POSIX 路径")
        if file_storage.get("pathTemplate") != "{applicationSlug}/files":
            failures.append(
                "fileStorage.pathTemplate 必须为 {applicationSlug}/files"
            )
        warning = file_storage.get("warningUsedPercent")
        stop = file_storage.get("stopUploadUsedPercent")
        minimum = file_storage.get("minimumFreeGiB")
        if not isinstance(warning, int) or not 1 <= warning < 100:
            failures.append("warningUsedPercent 必须是 1–99 的整数")
        if not isinstance(stop, int) or not 1 <= stop < 100:
            failures.append("stopUploadUsedPercent 必须是 1–99 的整数")
        if isinstance(warning, int) and isinstance(stop, int) and warning >= stop:
            failures.append("warningUsedPercent 必须小于 stopUploadUsedPercent")
        if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 1:
            failures.append("minimumFreeGiB 必须是大于或等于 1 的整数")
        if isinstance(capabilities, dict) and capabilities.get("objectStorage") is True:
            failures.append("本地模式下 capabilities.objectStorage 必须为 false")
    elif provider == "aliyun-oss" and mode == "oss-gateway":
        selected_mode = "oss"
        if not isinstance(object_storage, dict):
            failures.append("OSS 模式缺少 objectStorage 档案")
            object_storage = {}
        missing = sorted(
            field for field in OSS_REQUIRED_FIELDS
            if object_storage.get(field) in (None, "")
        )
        if missing:
            failures.append("objectStorage 缺少字段：" + ", ".join(missing))
        if object_storage.get("provider") != "aliyun-oss":
            failures.append("objectStorage.provider 必须为 aliyun-oss")
        if object_storage.get("mode") != "gateway-api-v1":
            failures.append("objectStorage.mode 必须为 gateway-api-v1")
        if object_storage.get("rootPrefix") != "apps":
            failures.append("objectStorage.rootPrefix 必须为 apps")
        bucket = object_storage.get("bucket")
        if isinstance(bucket, str) and not BUCKET_RE.fullmatch(bucket):
            failures.append("objectStorage.bucket 不是有效的 Bucket 名称")
        region = object_storage.get("region")
        if isinstance(region, str) and not REGION_RE.fullmatch(region):
            failures.append("objectStorage.region 不是有效的阿里云地域 ID")
        gateway = object_storage.get("gatewayBaseUrl")
        if isinstance(gateway, str) and gateway != GATEWAY_URL:
            failures.append(f"gatewayBaseUrl 必须为受管 Docker 私网地址 {GATEWAY_URL}")
        credential_ref = object_storage.get("credentialRef")
        if isinstance(credential_ref, str) and credential_ref != CREDENTIAL_REF:
            failures.append(f"credentialRef 必须为 {CREDENTIAL_REF}")
        if isinstance(capabilities, dict) and capabilities.get("objectStorage") is not True:
            failures.append("OSS 模式下 capabilities.objectStorage 必须为 true")
    else:
        selected_mode = "unknown"
        failures.append(
            "fileStorage 必须选择 local-disk/local-managed 或 aliyun-oss/oss-gateway"
        )

    if file_storage.get("verified") is not True:
        failures.append("fileStorage.verified 必须为 true")
    if selected_mode == "oss" and isinstance(object_storage, dict):
        if object_storage.get("verified") is not True:
            failures.append("objectStorage.verified 必须为 true")
    if args.require_mode and selected_mode != args.require_mode:
        failures.append(
            f"当前存储模式为 {selected_mode}，但要求 {args.require_mode}"
        )

    if failures:
        print("STORAGE PROFILE VALIDATION FAILED")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print(
        "STORAGE PROFILE VALIDATION PASS: "
        f"Alphabet file storage mode is {selected_mode}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
