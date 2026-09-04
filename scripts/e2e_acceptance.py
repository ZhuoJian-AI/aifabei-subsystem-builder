#!/usr/bin/env python3
"""Exercise read-only SSO and a page-scoped query action against a deployed subsystem."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import time
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urljoin
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4


class RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise HTTPError(req.full_url, code, "redirect captured", headers, fp)


OPENER = build_opener(RejectRedirects)


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def jwt(secret: str, claims: dict) -> str:
    header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = b64(json.dumps(claims, separators=(",", ":")).encode())
    signature = b64(hmac.new(secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
    return f"{header}.{payload}.{signature}"


def json_request(url: str, *, token: str, method: str = "GET", body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}", "User-Agent": "Aifabei-E2E/2.1"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    try:
        with OPENER.open(Request(url, headers=headers, data=data, method=method), timeout=20) as response:
            return response.status, json.load(response)
    except HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode())
        except Exception:  # noqa: BLE001
            detail = {"detail": str(exc)}
        return exc.code, detail


def main() -> int:
    parser = argparse.ArgumentParser(description="验收 SSO 和页面感知只读 AI Action")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--module-key", required=True)
    parser.add_argument("--page-key", required=True)
    parser.add_argument("--query-action", required=True)
    parser.add_argument("--token-env", default="ZHUOJIAN_INTEGRATION_SECRET")
    parser.add_argument("--secret-env", default="ZHUOJIAN_INTEGRATION_SECRET")
    parser.add_argument("--organization-id-env", default="ZHUOJIAN_ORGANIZATION_ID")
    args = parser.parse_args()
    token, secret = os.getenv(args.token_env, ""), os.getenv(args.secret_env, "")
    organization_id = os.getenv(args.organization_id_env, "")
    if not token or len(secret) < 32 or not organization_id:
        raise SystemExit(
            f"请通过 {args.token_env}、{args.secret_env} 和 {args.organization_id_env} "
            "提供模块接入凭证与真实企业 organization UUID。"
        )
    base = args.base_url.rstrip("/") + "/"
    status, manifest = json_request(urljoin(base, "api/integration/manifest"), token=token)
    if status != 200:
        raise SystemExit(f"Manifest 请求失败：HTTP {status}")
    module = next((item for item in manifest.get("modules", []) if item.get("moduleKey") == args.module_key), None)
    action = next((item for item in (module or {}).get("actions", []) if item.get("actionKey") == args.query_action), None)
    page = next((item for item in (module or {}).get("pages", []) if item.get("pageKey") == args.page_key), None)
    if not module or not action or not page or args.query_action not in page.get("actionKeys", []):
        raise SystemExit("Manifest 中找不到匹配的 module/page/query action 关系。")
    if action.get("operation") != "query" or action.get("requiresConfirmation"):
        raise SystemExit("e2e_acceptance 默认只执行无需确认的 query Action。")

    now, user_id = int(time.time()), f"acceptance-{uuid4().hex[:12]}"
    sso_claims = {
        "iss": "zhuojian-saas", "aud": manifest["applicationSlug"], "typ": "zhuojian-sso",
        "sub": user_id, "organizationId": organization_id, "departmentId": "acceptance-dept",
        "departmentIds": ["acceptance-dept"], "roleIds": ["acceptance-role"],
        "effectiveDataScope": {
            "unrestricted": False, "include_self": False, "own_only": False,
            "department_ids": ["acceptance-dept"],
        },
        "teamId": None, "moduleKey": args.module_key, "permissions": ["view", "ai_query"],
        "pageKeys": [args.page_key], "actionKeys": [args.query_action],
        "pageAccess": {
            args.page_key: {
                "permissions": ["view", "ai_query"],
                "actionKeys": [args.query_action],
            },
        },
        "jti": uuid4().hex, "iat": now, "exp": now + 60,
    }
    # SSO must land on the exact authorized page, not a coarse application home.
    sso_url = urljoin(base, "api/integration/sso") + "?" + urlencode({"ticket": jwt(secret, sso_claims), "redirect": page["routePattern"]})
    sso_status, _ = json_request(sso_url, token="")
    if sso_status != 302:
        raise SystemExit(f"SSO 票据交换失败：HTTP {sso_status}")

    request_id = f"acceptance-query-{uuid4().hex}"
    action_claims = {
        "iss": "zhuojian-saas", "aud": manifest["applicationSlug"], "typ": "zhuojian-action",
        "sub": user_id, "organizationId": organization_id, "departmentId": "acceptance-dept",
        "departmentIds": ["acceptance-dept"], "roleIds": ["acceptance-role"],
        "effectiveDataScope": {
            "unrestricted": False, "include_self": False, "own_only": False,
            "department_ids": ["acceptance-dept"],
        },
        "teamId": None, "moduleKey": args.module_key, "pageKey": args.page_key,
        "actionKey": args.query_action, "operation": "query", "permissions": ["view", "ai_query"],
        "requestId": request_id, "jti": uuid4().hex, "iat": now, "exp": now + 60,
    }
    request_body = {
        "requestId": request_id, "moduleKey": args.module_key, "pageKey": args.page_key,
        "operation": "query", "expectedVersion": None, "params": {},
    }
    status, result = json_request(
        urljoin(base, f"api/integration/actions/{quote(args.query_action, safe='')}"),
        token=jwt(secret, action_claims), method="POST", body=request_body,
    )
    if status != 200:
        raise SystemExit(f"页面感知 query Action 失败：HTTP {status}，{result.get('detail', '')}")
    print(json.dumps({
        "status": "PASS", "applicationSlug": manifest["applicationSlug"], "moduleKey": args.module_key,
        "pageKey": args.page_key, "actionKey": args.query_action, "sso": "accepted", "query": "executed",
    }, ensure_ascii=False))
    print("验收脚本没有输出 Secret，也没有执行 create/update/delete。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
