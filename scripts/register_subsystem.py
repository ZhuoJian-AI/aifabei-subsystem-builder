#!/usr/bin/env python3
"""Admin-only discovery and registration helper for ZhuoJian SaaS."""

from __future__ import annotations

import argparse
import json
import os
from urllib.error import HTTPError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


def call(url: str, token: str, method: str, body: dict | None = None) -> dict:
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": "Aifabei-Registrar/2.1"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    try:
        with urlopen(Request(url, data=data, headers=headers, method=method), timeout=30) as response:
            return json.load(response) if response.status != 204 else {}
    except HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:1000]
        raise SystemExit(f"灼见 API 返回 HTTP {exc.code}: {detail}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="管理员发现并登记灼见原生模块")
    parser.add_argument("--platform-url", default="https://ai-platform.staging.zhuojianai.com")
    parser.add_argument("--organization-id", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--admin-token-env", default="ZHUOJIAN_ADMIN_TOKEN")
    parser.add_argument("--integration-token-env", default="ZHUOJIAN_SUBSYSTEM_TOKEN")
    parser.add_argument("--apply", action="store_true", help="真正创建并同步；省略时只做发现")
    args = parser.parse_args()
    admin_token, integration_token = os.getenv(args.admin_token_env, ""), os.getenv(args.integration_token_env, "")
    if not admin_token or not integration_token:
        raise SystemExit("管理员 Token 和模块接入 Token 必须通过环境变量提供。")
    api = args.platform_url.rstrip("/") + "/api/v1/"
    discovery = call(
        urljoin(api, f"organizations/{args.organization_id}/applications/discover"), admin_token, "POST",
        {"base_url": args.base_url, "auth_token": integration_token},
    )
    summary = {"status": "discovered", "name": discovery["suggested_name"], "slug": discovery["suggested_slug"], "modules": len(discovery.get("modules", []))}
    if not args.apply:
        print(json.dumps(summary, ensure_ascii=False))
        print("尚未创建应用；管理员核对部门映射和默认不授权原则后，加 --apply 执行登记。")
        return 0
    created = call(
        urljoin(api, f"organizations/{args.organization_id}/applications"), admin_token, "POST",
        {"name": discovery["suggested_name"], "slug": discovery["suggested_slug"], "entry_url": discovery["entry_url"], "display_mode": "embedded", "is_active": True, "assistant_enabled": True},
    )
    app_id = created["id"]
    call(urljoin(api, f"applications/{app_id}/integration"), admin_token, "PUT", {
        "manifest_url": discovery["manifest_url"], "auth_token": integration_token, "sync_enabled": True,
    })
    synced = call(urljoin(api, f"applications/{app_id}/integration/sync"), admin_token, "POST")
    print(json.dumps({**summary, "status": "registered", "applicationId": app_id, "sync": synced.get("status"), "grants": "none"}, ensure_ascii=False))
    print("应用已发现和同步，但没有自动授予任何部门权限。请管理员在权限页面确认。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
