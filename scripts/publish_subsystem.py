#!/usr/bin/env python3
"""Publish a healthy local-Git subsystem release into ZhuoJian SaaS."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def call_json(
    url: str,
    token: str,
    method: str = "GET",
    body: dict | None = None,
) -> dict:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body else None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "Aifabei-ECS-Publisher/1.0",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    try:
        with urlopen(
            Request(url, data=data, method=method, headers=headers), timeout=30
        ) as response:
            return json.load(response)
    except HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:1000]
        raise SystemExit(f"接口返回 HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise SystemExit(f"接口连接失败: {exc.reason}") from exc


def git(project: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(project), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode:
        raise SystemExit(result.stderr.strip() or "本地 Git 命令失败")
    return result.stdout.strip()


def load_object(path: Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"无法读取{label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"{label}必须是 JSON 对象: {path}")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(
        description="业务 AI 将当前本地 Git 版本登记并同步到灼见"
    )
    parser.add_argument("--project-path", type=Path, required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument(
        "--runtime-profile",
        type=Path,
        default=Path("/etc/zhuojian/runtime.json"),
    )
    parser.add_argument("--runtime-credential-file", type=Path)
    parser.add_argument(
        "--integration-secret-env", default="ZHUOJIAN_INTEGRATION_SECRET"
    )
    parser.add_argument("--image-ref")
    parser.add_argument("--release-metadata", type=Path)
    args = parser.parse_args()

    project = args.project_path.resolve()
    if not project.is_dir():
        raise SystemExit(f"项目目录不存在: {project}")
    if git(project, "status", "--porcelain"):
        raise SystemExit("发布被拒绝：本地 Git 工作树不干净，请先审查并提交本次修改")
    commit = git(project, "rev-parse", "HEAD").lower()

    profile = load_object(args.runtime_profile, "Runtime 环境档案")
    platform_url = str((profile.get("platform") or {}).get("baseUrl") or "").rstrip("/")
    if urlsplit(platform_url).scheme != "https":
        raise SystemExit("Runtime 环境档案中的 platform.baseUrl 必须是 HTTPS")
    credential_value = str(
        (profile.get("deployment") or {}).get("registrationCredentialRef") or ""
    ).strip()
    if args.runtime_credential_file is None and not credential_value:
        raise SystemExit("Runtime 环境档案缺少登记凭证引用")
    credential_ref = args.runtime_credential_file or Path(credential_value)
    try:
        runtime_credential = credential_ref.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SystemExit(f"无法读取 Runtime 登记凭证: {credential_ref}") from exc
    integration_secret = os.getenv(args.integration_secret_env, "").strip()
    if not runtime_credential or not integration_secret:
        raise SystemExit(
            f"Runtime 登记凭证和环境变量 {args.integration_secret_env} 均必须已配置"
        )

    base_url = args.base_url.rstrip("/")
    parsed = urlsplit(base_url)
    domain_suffix = str((profile.get("domains") or {}).get("suffix") or "").lower()
    if parsed.scheme != "https" or not parsed.hostname:
        raise SystemExit("模块入口必须是公网 HTTPS URL")
    if not domain_suffix or not parsed.hostname.endswith("." + domain_suffix):
        raise SystemExit("模块域名不在 Runtime 允许的域名后缀内")

    manifest = call_json(
        base_url + "/api/integration/manifest", integration_secret
    )
    application_slug = str(manifest.get("applicationSlug") or "")
    application_name = str(manifest.get("applicationName") or "")
    enterprise_key = str((manifest.get("enterprise") or {}).get("key") or "")
    if not application_slug or not application_name:
        raise SystemExit("Manifest 缺少 applicationSlug/applicationName")
    if parsed.hostname != f"{application_slug}.{domain_suffix}":
        raise SystemExit("Manifest applicationSlug 与模块域名不一致")
    if enterprise_key != str(profile.get("enterpriseKey") or ""):
        raise SystemExit("Manifest enterprise.key 与 Runtime 企业不一致")

    metadata = load_object(args.release_metadata, "发布元数据") if args.release_metadata else {}
    result = call_json(
        platform_url + "/api/v1/ecs-publisher/modules/register",
        runtime_credential,
        "POST",
        {
            "application_slug": application_slug,
            "application_name": application_name,
            "base_url": base_url,
            "integration_secret": integration_secret,
            "source_commit": commit,
            "image_ref": args.image_ref,
            "release_metadata": metadata,
        },
    )
    summary = {
        "status": result.get("status"),
        "applicationSlug": result.get("application_slug"),
        "applicationId": result.get("application_id"),
        "contractRevision": result.get("contract_revision"),
        "sourceCommit": result.get("last_success_commit"),
        "authorization": "unchanged; new capabilities require administrator approval",
    }
    if result.get("status") != "healthy":
        summary["error"] = result.get("last_error") or "contract verification failed"
        print(json.dumps(summary, ensure_ascii=False))
        return 2
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
