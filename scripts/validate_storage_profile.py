#!/usr/bin/env python3
"""Validate the non-secret Alphabet object-storage runtime profile."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit


BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
REQUIRED_FIELDS = {
    "provider", "mode", "bucket", "region", "rootPrefix",
    "gatewayBaseUrl", "credentialRef", "verified",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="校验 Alphabet ECS 的企业文件存储档案")
    parser.add_argument("--runtime-profile", required=True, help="例如 /etc/zhuojian/runtime.json")
    args = parser.parse_args()
    try:
        raw = sys.stdin.read() if args.runtime_profile == "-" else Path(args.runtime_profile).expanduser().read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"无法读取环境档案：{exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit("环境档案顶层必须是 JSON 对象")

    failures: list[str] = []
    capabilities = payload.get("capabilities")
    storage = payload.get("objectStorage")
    if not isinstance(capabilities, dict) or capabilities.get("objectStorage") is not True:
        failures.append("capabilities.objectStorage 必须为 true")
    if not isinstance(storage, dict):
        failures.append("缺少 objectStorage 档案")
        storage = {}
    missing = sorted(field for field in REQUIRED_FIELDS if storage.get(field) in (None, ""))
    if missing:
        failures.append("objectStorage 缺少字段：" + ", ".join(missing))
    if storage.get("provider") != "aliyun-oss":
        failures.append("objectStorage.provider 必须为 aliyun-oss")
    if storage.get("mode") != "gateway-signed-url":
        failures.append("objectStorage.mode 必须为 gateway-signed-url")
    if storage.get("rootPrefix") != "apps":
        failures.append("objectStorage.rootPrefix 必须为 apps")
    if storage.get("verified") is not True:
        failures.append("objectStorage.verified 必须为 true")
    bucket = storage.get("bucket")
    if isinstance(bucket, str) and not BUCKET_RE.fullmatch(bucket):
        failures.append("objectStorage.bucket 不是有效的 Bucket 名称")
    gateway = storage.get("gatewayBaseUrl")
    if isinstance(gateway, str):
        parsed = urlsplit(gateway)
        loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if not parsed.hostname or (parsed.scheme != "https" and not (parsed.scheme == "http" and loopback)):
            failures.append("gatewayBaseUrl 必须是 HTTPS，或 ECS 回环地址上的 HTTP")
    credential_ref = storage.get("credentialRef")
    if isinstance(credential_ref, str) and not credential_ref.startswith("/etc/zhuojian/"):
        failures.append("credentialRef 必须位于 /etc/zhuojian/，且不能指向项目目录")

    if failures:
        print("STORAGE PROFILE VALIDATION FAILED")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("STORAGE PROFILE VALIDATION PASS: Alphabet OSS gateway profile is configured")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
