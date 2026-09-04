#!/usr/bin/env python3
"""Run pre-registration technical checks without claiming real employee SSO."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4

from publish_subsystem import load_app_credentials, load_app_environment


class RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HTTPError(req.full_url, code, "redirect captured", headers, fp)


OPENER = build_opener(ProxyHandler({}), RejectRedirects())


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def jwt(secret: str, claims: dict) -> str:
    header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = b64(json.dumps(claims, separators=(",", ":")).encode())
    signature = b64(hmac.new(secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
    return f"{header}.{payload}.{signature}"


def json_request(
    url: str,
    *,
    token: str,
    method: str = "GET",
    body: dict | None = None,
) -> tuple[int, dict]:
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    headers = {"Accept": "application/json", "User-Agent": "Aifabei-E2E/2.5"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if data is not None:
        headers["Content-Type"] = "application/json"
    try:
        with OPENER.open(Request(url, headers=headers, data=data, method=method), timeout=20) as response:
            payload = response.read(4 * 1024 * 1024 + 1)
            if len(payload) > 4 * 1024 * 1024:
                raise SystemExit("验收响应超过 4 MiB")
            return response.status, json.loads(payload)
    except HTTPError as exc:
        try:
            detail = json.loads(exc.read(64 * 1024).decode())
        except Exception:  # noqa: BLE001
            detail = {"detail": str(exc)}
        return exc.code, detail


def main() -> int:
    parser = argparse.ArgumentParser(description="验收 v2.5 Manifest 与页面感知只读 AI Action")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--module-key", required=True)
    parser.add_argument("--page-key", required=True)
    parser.add_argument("--query-action", required=True)
    parser.add_argument("--app-env-file", type=Path, help="Runtime 管理的应用凭证文件；默认按域名推断")
    parser.add_argument("--organization-id-env", default="ZHUOJIAN_ORGANIZATION_ID")
    args = parser.parse_args()

    base = args.base_url.rstrip("/") + "/"
    hostname = urlsplit(base).hostname or ""
    inferred_slug = hostname.split(".", 1)[0]
    env_file = args.app_env_file or Path("/etc/zhuojian/apps") / f"{inferred_slug}.env"
    credentials = load_app_credentials(env_file)
    environment = load_app_environment(env_file)
    organization_id = os.getenv(args.organization_id_env, "") or environment.get(args.organization_id_env, "")
    if not organization_id:
        raise SystemExit("Runtime 管理的应用环境缺少 organization UUID")

    status, manifest = json_request(
        urljoin(base, "api/integration/manifest"),
        token=credentials["manifest_access_token"],
    )
    if status != 200 or manifest.get("contractRevision") != "2.5":
        raise SystemExit(f"v2.5 Manifest 请求失败：HTTP {status}")
    module = next(
        (item for item in manifest.get("modules", []) if item.get("moduleKey") == args.module_key),
        None,
    )
    action = next(
        (item for item in (module or {}).get("actions", []) if item.get("actionKey") == args.query_action),
        None,
    )
    page = next(
        (item for item in (module or {}).get("pages", []) if item.get("pageKey") == args.page_key),
        None,
    )
    if not module or not action or not page or args.query_action not in page.get("actionKeys", []):
        raise SystemExit("Manifest 中找不到匹配的 module/page/query Action")
    if action.get("operation") != "query" or action.get("requiresConfirmation"):
        raise SystemExit("验收只执行无需确认的 query Action")

    # v2.5 must not accept a module-self-signed legacy SSO ticket.
    legacy_status, _ = json_request(
        urljoin(base, "api/integration/sso")
        + "?"
        + urlencode({"ticket": "legacy-self-signed-ticket", "redirect": page["routePattern"]}),
        token="",
    )
    if legacy_status not in {400, 401, 404, 422}:
        raise SystemExit("模块仍接受旧式自签 SSO ticket")

    now = int(time.time())
    user_id = f"acceptance-{uuid4().hex[:12]}"
    request_id = f"acceptance-query-{uuid4().hex}"
    action_claims = {
        "iss": "zhuojian-saas",
        "aud": manifest["applicationSlug"],
        "typ": "zhuojian-action",
        "sub": user_id,
        "organizationId": organization_id,
        "departmentId": "acceptance-dept",
        "departmentIds": ["acceptance-dept"],
        "roleIds": ["acceptance-role"],
        "effectiveDataScope": {
            "unrestricted": False,
            "include_self": False,
            "own_only": False,
            "department_ids": ["acceptance-dept"],
        },
        "teamId": None,
        "moduleKey": args.module_key,
        "pageKey": args.page_key,
        "actionKey": args.query_action,
        "operation": "query",
        "permissions": ["view", "ai_query"],
        "requestId": request_id,
        "jti": uuid4().hex,
        "iat": now,
        "exp": now + 60,
    }
    request_body = {
        "requestId": request_id,
        "moduleKey": args.module_key,
        "pageKey": args.page_key,
        "operation": "query",
        "expectedVersion": None,
        "params": {},
    }
    status, result = json_request(
        urljoin(base, f"api/integration/actions/{quote(args.query_action, safe='')}"),
        token=jwt(credentials["action_signing_secret"], action_claims),
        method="POST",
        body=request_body,
    )
    if status != 200:
        raise SystemExit(f"页面感知 query Action 失败：HTTP {status}，{result.get('detail', '')}")
    print(json.dumps({
        "status": "pre_registration_only",
        "applicationSlug": manifest["applicationSlug"],
        "moduleKey": args.module_key,
        "pageKey": args.page_key,
        "actionKey": args.query_action,
        "legacySelfSignedSso": "rejected",
        "realSso": "pending_admin_acceptance",
        "query": "technical_path_executed_with_local_test_signature",
    }, ensure_ascii=False))
    print("技术预检未输出凭证、未执行写操作，也不代表真实员工 SSO 已通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
