#!/usr/bin/env python3
"""Save one enterprise's non-secret Coolify target in ZhuoJian."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


def main() -> int:
    parser = argparse.ArgumentParser(description="为企业登记一次性的 Coolify 部署档案")
    parser.add_argument("--platform-url", default=os.environ.get("ZHUOJIAN_PLATFORM_URL", ""))
    parser.add_argument("--organization-id", required=True)
    parser.add_argument("--runtime-key", default="default", help="企业内稳定运行环境标识")
    parser.add_argument("--server-uuid", required=True)
    parser.add_argument("--project-uuid", required=True)
    parser.add_argument("--environment-name", default="production")
    parser.add_argument("--environment-uuid")
    parser.add_argument("--destination-uuid")
    parser.add_argument("--github-app-uuid", required=True)
    parser.add_argument("--domain-suffix", required=True, help="不带 *.，如 aifabei.staging.zhuojianai.com")
    parser.add_argument("--use-build-server", action="store_true")
    parser.add_argument("--default-runtime", action="store_true", help="设为未指定 runtime 时的默认目标")
    args = parser.parse_args()

    token = os.environ.get("ZHUOJIAN_ADMIN_TOKEN", "").strip()
    if not args.platform_url.strip() or not token:
        parser.error("需要 ZHUOJIAN_PLATFORM_URL 和临时环境变量 ZHUOJIAN_ADMIN_TOKEN")
    endpoint = urllib.parse.urljoin(
        args.platform_url.rstrip("/") + "/",
        f"api/v1/module-publisher/organizations/{urllib.parse.quote(args.organization_id, safe='')}/deployment-profile",
    )
    parsed = urllib.parse.urlparse(endpoint)
    if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        parser.error("平台管理接口必须使用 HTTPS")
    body = {
        "runtime_key": args.runtime_key,
        "server_uuid": args.server_uuid,
        "project_uuid": args.project_uuid,
        "environment_name": args.environment_name,
        "environment_uuid": args.environment_uuid,
        "destination_uuid": args.destination_uuid,
        "github_app_uuid": args.github_app_uuid,
        "domain_suffix": args.domain_suffix,
        "use_build_server": args.use_build_server,
        "is_default": args.default_runtime,
        "is_active": True,
    }
    request = urllib.request.Request(
        endpoint,
        method="PUT",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
            "user-agent": "ZhuoJian-Deployment-Profile/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("detail")
        except (UnicodeDecodeError, json.JSONDecodeError):
            detail = None
        print(f"登记失败：{detail or f'HTTP {exc.code}'}", file=sys.stderr)
        return 1
    except urllib.error.URLError:
        print("登记失败：无法连接灼见平台", file=sys.stderr)
        return 1
    print(json.dumps({
        "status": "configured",
        "organizationId": result.get("organization_id"),
        "runtimeKey": result.get("runtime_key"),
        "serverUuid": result.get("server_uuid"),
        "projectUuid": result.get("project_uuid"),
        "domainSuffix": result.get("domain_suffix"),
        "deployerConfigured": result.get("deployer_configured"),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
