#!/usr/bin/env python3
"""Provision one least-privilege ZhuoJian ECS publisher runtime."""

from __future__ import annotations

import argparse
import json
import os
import stat
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def call_json(url: str, token: str, body: dict) -> dict:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Aifabei-Runtime-Provisioner/1.0",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            return json.load(response)
    except HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:1000]
        raise SystemExit(f"灼见 API 返回 HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise SystemExit(f"无法连接灼见 API: {exc.reason}") from exc


def secure_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, stat.S_IRUSR | stat.S_IWUSR)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
    except OSError:
        path.unlink(missing_ok=True)
        raise
    if os.name != "nt":
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def _https_platform(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname:
        raise argparse.ArgumentTypeError("platform-url 必须是公网 HTTPS 地址")
    return value.rstrip("/")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="管理员为一台企业 ECS 签发最小权限 Runtime 登记凭证"
    )
    parser.add_argument(
        "--platform-url",
        type=_https_platform,
        default="https://ai-platform.staging.zhuojianai.com",
    )
    parser.add_argument("--organization-id", required=True)
    parser.add_argument("--runtime-key", required=True)
    parser.add_argument("--enterprise-key", default="aifabei")
    parser.add_argument(
        "--environment",
        choices=("development", "staging", "production"),
        default="staging",
    )
    parser.add_argument("--domain-suffix", required=True)
    parser.add_argument("--public-address")
    parser.add_argument("--admin-token-env", default="ZHUOJIAN_ADMIN_TOKEN")
    parser.add_argument(
        "--profile-out",
        type=Path,
        default=Path("/etc/zhuojian/runtime.json"),
    )
    parser.add_argument(
        "--credential-out",
        type=Path,
        default=Path("/etc/zhuojian/runtime-registration.key"),
    )
    args = parser.parse_args()

    admin_token = os.getenv(args.admin_token_env, "").strip()
    if not admin_token:
        raise SystemExit(f"管理员 Token 必须通过环境变量 {args.admin_token_env} 提供")
    for output in (args.profile_out, args.credential_out):
        if output.exists():
            raise SystemExit(f"拒绝覆盖已有文件: {output}")

    endpoint = (
        f"{args.platform_url}/api/v1/ecs-publisher/organizations/"
        f"{args.organization_id}/runtimes"
    )
    payload = {
        "runtime_key": args.runtime_key,
        "enterprise_key": args.enterprise_key,
        "environment": args.environment,
        "domain_suffix": args.domain_suffix,
        "public_address": args.public_address,
    }
    result = call_json(endpoint, admin_token, payload)
    credential = result.get("credential")
    profile = result.get("runtime_profile")
    runtime = result.get("runtime") or {}
    if not isinstance(credential, str) or not credential or not isinstance(profile, dict):
        raise SystemExit("灼见 API 未返回有效的 Runtime 凭证和环境档案")

    profile.setdefault("deployment", {})["registrationCredentialRef"] = str(
        args.credential_out
    )
    secure_write(args.credential_out, credential + "\n")
    try:
        secure_write(
            args.profile_out,
            json.dumps(profile, ensure_ascii=False, indent=2) + "\n",
        )
    except OSError:
        raise SystemExit(
            f"环境档案写入失败；Runtime 凭证已安全保存在 {args.credential_out}，"
            "不要重新签发，请修复目录权限后手工保存本次档案。"
        )

    print(
        json.dumps(
            {
                "status": "provisioned",
                "runtimeId": str(runtime.get("id") or ""),
                "runtimeKey": runtime.get("runtime_key") or args.runtime_key,
                "organizationId": args.organization_id,
                "domainSuffix": args.domain_suffix,
                "profile": str(args.profile_out),
                "credential": "installed",
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
