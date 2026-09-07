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

from contract_versions import require_supported_contract_revision
from publish_subsystem import load_app_environment


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


def persistent_file_inventory(paths: list[Path]) -> dict[str, tuple[int, int]]:
    """Capture file size and mtime without reading or changing business data."""

    inventory: dict[str, tuple[int, int]] = {}
    for root in paths:
        resolved = root.expanduser().resolve()
        if not resolved.exists():
            continue
        candidates = [resolved] if resolved.is_file() else resolved.rglob("*")
        for candidate in candidates:
            if candidate.is_file():
                stat = candidate.stat()
                inventory[str(candidate)] = (stat.st_size, stat.st_mtime_ns)
    return inventory


def validate_export_dataset(result: dict, expected_snapshot_id: str | None) -> tuple[str, str | None]:
    required = {"snapshotId", "snapshotAt", "columns", "rows", "rowCount", "nextCursor"}
    if set(result) != required:
        raise SystemExit("export Action 未返回标准分页数据集")
    snapshot_id = result.get("snapshotId")
    if not isinstance(snapshot_id, str) or not snapshot_id:
        raise SystemExit("export Action 的 snapshotId 无效")
    if expected_snapshot_id is not None and snapshot_id != expected_snapshot_id:
        raise SystemExit("export Action 分页过程中更换了 snapshotId")
    if not isinstance(result.get("snapshotAt"), str) or not result["snapshotAt"]:
        raise SystemExit("export Action 的 snapshotAt 无效")
    if not isinstance(result.get("columns"), list) or not all(
        isinstance(column, dict)
        and isinstance(column.get("key"), str)
        and column["key"]
        and isinstance(column.get("label"), str)
        and column["label"]
        for column in result["columns"]
    ):
        raise SystemExit("export Action 的 columns 无效")
    if not isinstance(result.get("rows"), list) or not all(
        isinstance(row, dict)
        and all(value is None or isinstance(value, (str, int, float, bool)) for value in row.values())
        for row in result["rows"]
    ):
        raise SystemExit("export Action 的 rows 必须是扁平标量记录")
    if isinstance(result.get("rowCount"), bool) or not isinstance(result.get("rowCount"), int):
        raise SystemExit("export Action 的 rowCount 无效")
    next_cursor = result.get("nextCursor")
    if next_cursor is not None and (not isinstance(next_cursor, str) or len(next_cursor) < 20):
        raise SystemExit("export Action 的 nextCursor 必须是不透明游标")
    return snapshot_id, next_cursor


def load_runtime_values(env_file: Path, environment_names: tuple[str, ...]) -> dict[str, str]:
    values = load_app_environment(env_file) if env_file.is_file() else {}
    for name in environment_names:
        value = os.getenv(name, "")
        if value:
            values[name] = value
    return values


def require_v25_credentials(values: dict[str, str]) -> dict[str, str]:
    expected = {
        "manifest_access_token": ("ZHUOJIAN_MANIFEST_ACCESS_TOKEN", "zjmf_"),
        "sso_exchange_token": ("ZHUOJIAN_SSO_EXCHANGE_TOKEN", "zjss_"),
        "action_signing_secret": ("ZHUOJIAN_ACTION_SIGNING_SECRET", "zjac_"),
        "event_signing_secret": ("ZHUOJIAN_EVENT_SIGNING_SECRET", "zjev_"),
    }
    credentials: dict[str, str] = {}
    for field, (name, prefix) in expected.items():
        value = values.get(name, "")
        if len(value) < 40 or not value.startswith(prefix):
            raise SystemExit(f"v2.5 Runtime 应用凭证缺少或类型错误：{name}")
        credentials[field] = value
    if len(set(credentials.values())) != len(credentials):
        raise SystemExit("v2.5 四类应用凭证不得复用")
    return credentials


def main() -> int:
    parser = argparse.ArgumentParser(description="按现有 2.4/2.5 契约验收 Manifest、SSO 与页面感知 Action")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--module-key", required=True)
    parser.add_argument("--page-key", required=True)
    parser.add_argument("--query-action", required=True)
    parser.add_argument("--export-action", help="可选：验收标准分页导出 Action")
    parser.add_argument("--export-page-limit", type=int, default=100)
    parser.add_argument(
        "--persistent-path",
        action="append",
        default=[],
        type=Path,
        help="可重复：导出前后必须完全不变的业务 ECS 持久目录或文件",
    )
    parser.add_argument("--app-env-file", type=Path, help="Runtime 管理的应用凭证文件；默认按域名推断")
    parser.add_argument("--organization-id-env", default="ZHUOJIAN_ORGANIZATION_ID")
    parser.add_argument("--legacy-secret-env", default="ZHUOJIAN_INTEGRATION_SECRET")
    args = parser.parse_args()

    base = args.base_url.rstrip("/") + "/"
    hostname = urlsplit(base).hostname or ""
    inferred_slug = hostname.split(".", 1)[0]
    env_file = args.app_env_file or Path("/etc/zhuojian/apps") / f"{inferred_slug}.env"
    runtime_values = load_runtime_values(
        env_file,
        (
            "ZHUOJIAN_MANIFEST_ACCESS_TOKEN",
            "ZHUOJIAN_SSO_EXCHANGE_TOKEN",
            "ZHUOJIAN_ACTION_SIGNING_SECRET",
            "ZHUOJIAN_EVENT_SIGNING_SECRET",
            args.legacy_secret_env,
            args.organization_id_env,
        ),
    )
    manifest_token = runtime_values.get("ZHUOJIAN_MANIFEST_ACCESS_TOKEN", "")
    manifest_token_kind = "2.5" if manifest_token else ""
    if not manifest_token:
        manifest_token = runtime_values.get(args.legacy_secret_env, "")
        manifest_token_kind = "2.4" if manifest_token else ""
    if not manifest_token:
        raise SystemExit("缺少 Manifest 接入凭证；未读取或输出任何凭证值")
    organization_id = runtime_values.get(args.organization_id_env, "")
    if not organization_id:
        raise SystemExit("Runtime 管理的应用环境缺少 organization UUID")

    status, manifest = json_request(
        urljoin(base, "api/integration/manifest"),
        token=manifest_token,
    )
    if status != 200:
        raise SystemExit(f"Manifest 请求失败：HTTP {status}")
    try:
        contract_revision = require_supported_contract_revision(manifest.get("contractRevision"))
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if contract_revision != manifest_token_kind:
        raise SystemExit(
            f"contractRevision={contract_revision} 与当前凭证类型不一致；"
            "验收不会猜测或自动升级版本"
        )
    auth = manifest.get("auth")
    if contract_revision == "2.5" and (
        not isinstance(auth, dict)
        or auth.get("ssoPath") != "/api/integration/sso"
        or auth.get("mode") != "authorization_code"
        or "algorithm" in auth
    ):
        raise SystemExit("v2.5 Manifest 未声明一次性 authorization_code SSO")
    if contract_revision == "2.4" and (
        not isinstance(auth, dict)
        or auth.get("ssoPath") != "/api/integration/sso"
        or auth.get("algorithm") != "HS256"
        or "mode" in auth
    ):
        raise SystemExit("v2.4 Manifest 未声明 HS256 票据 SSO")
    if contract_revision == "2.5":
        credentials = require_v25_credentials(runtime_values)
        action_secret = credentials["action_signing_secret"]
    else:
        legacy_secret = runtime_values.get(args.legacy_secret_env, "")
        if len(legacy_secret) < 32:
            raise SystemExit(f"v2.4 缺少有效的 {args.legacy_secret_env}")
        action_secret = legacy_secret
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

    export_action = None
    if args.export_action:
        export_action = next(
            (item for item in (module or {}).get("actions", []) if item.get("actionKey") == args.export_action),
            None,
        )
        if (
            not export_action
            or export_action.get("operation") != "export"
            or export_action.get("requiresConfirmation")
            or args.export_action not in page.get("actionKeys", [])
        ):
            raise SystemExit("Manifest 中找不到匹配的无确认 export Action")

    now = int(time.time())
    user_id = f"acceptance-{uuid4().hex[:12]}"
    if contract_revision == "2.5":
        # v2.5 must not accept a module-self-signed legacy SSO ticket. A real
        # authorization code is issued and consumed by SaaS during admin E2E.
        legacy_status, _ = json_request(
            urljoin(base, "api/integration/sso")
            + "?"
            + urlencode({"ticket": "legacy-self-signed-ticket", "redirect": page["routePattern"]}),
            token="",
        )
        if legacy_status not in {400, 401, 404, 422}:
            raise SystemExit("v2.5 模块仍接受旧式自签 SSO ticket")
        sso_summary = "authorization_code_shape_verified;real_sso_pending_admin_acceptance"
    else:
        sso_claims = {
            "iss": "zhuojian-saas",
            "aud": manifest["applicationSlug"],
            "typ": "zhuojian-sso",
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
            "moduleKey": args.module_key,
            "permissions": ["view", "ai_query"],
            "pageKeys": [args.page_key],
            "actionKeys": [args.query_action],
            "pageAccess": {
                args.page_key: {
                    "permissions": ["view", "ai_query"],
                    "actionKeys": [args.query_action],
                },
            },
            "jti": uuid4().hex,
            "iat": now,
            "exp": now + 60,
        }
        sso_url = urljoin(base, "api/integration/sso") + "?" + urlencode({
            "ticket": jwt(action_secret, sso_claims),
            "redirect": page["routePattern"],
        })
        sso_status, _ = json_request(sso_url, token="")
        if sso_status != 302:
            raise SystemExit(f"v2.4 SSO 票据交换失败：HTTP {sso_status}")
        sso_summary = "legacy_ticket_accepted"
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
        token=jwt(action_secret, action_claims),
        method="POST",
        body=request_body,
    )
    if status != 200:
        raise SystemExit(f"页面感知 query Action 失败：HTTP {status}，{result.get('detail', '')}")

    export_summary = "not_requested"
    if export_action is not None:
        if contract_revision != "2.5":
            # The historical contract did not define the standard dataset or
            # its input shape. Preserve it without pretending that v2.5 export
            # guarantees were verified or forcing a contract migration.
            export_summary = "v2.4_present;standard_dataset_not_required"
        else:
            before_files = persistent_file_inventory(args.persistent_path)
            snapshot_id: str | None = None
            cursor: str | None = None
            page_count = 0
            seen_cursors: set[str] = set()
            while True:
                page_count += 1
                if page_count > args.export_page_limit:
                    raise SystemExit("export Action 分页超过验收上限")
                export_request_id = f"acceptance-export-{uuid4().hex}"
                export_claims = {
                    **action_claims,
                    "actionKey": args.export_action,
                    "operation": "export",
                    "permissions": ["view", "export"],
                    "requestId": export_request_id,
                    "jti": uuid4().hex,
                    "iat": int(time.time()),
                    "exp": int(time.time()) + 60,
                }
                export_params = {"limit": min(100, max(1, args.export_page_limit))}
                if cursor is None:
                    export_params["filters"] = {}
                else:
                    export_params["cursor"] = cursor
                export_body = {
                    "requestId": export_request_id,
                    "moduleKey": args.module_key,
                    "pageKey": args.page_key,
                    "operation": "export",
                    "expectedVersion": None,
                    "params": export_params,
                }
                export_status, export_result = json_request(
                    urljoin(base, f"api/integration/actions/{quote(args.export_action, safe='')}"),
                    token=jwt(action_secret, export_claims),
                    method="POST",
                    body=export_body,
                )
                if export_status != 200:
                    raise SystemExit(
                        f"分页 export Action 失败：HTTP {export_status}，{export_result.get('detail', '')}"
                    )
                snapshot_id, cursor = validate_export_dataset(export_result, snapshot_id)
                if cursor is None:
                    break
                if cursor in seen_cursors:
                    raise SystemExit("export Action 重复返回同一个游标")
                seen_cursors.add(cursor)
            if persistent_file_inventory(args.persistent_path) != before_files:
                raise SystemExit("export Action 在业务 ECS 持久目录中创建或修改了文件")
            export_summary = f"standard_dataset_verified:{page_count}_pages"

    print(json.dumps({
        "status": "pre_registration_only",
        "contractRevision": contract_revision,
        "applicationSlug": manifest["applicationSlug"],
        "moduleKey": args.module_key,
        "pageKey": args.page_key,
        "actionKey": args.query_action,
        "sso": sso_summary,
        "legacySelfSignedSso": "rejected" if contract_revision == "2.5" else "accepted",
        "realSso": (
            "pending_admin_acceptance"
            if contract_revision == "2.5"
            else "technical_ticket_verified"
        ),
        "query": "technical_path_executed_with_local_test_signature",
        "export": export_summary,
        "subsystem_contract_pass": True,
        "saas_format_capability_pass": "not_run",
        "saas_artifact_e2e_pass": "not_run",
    }, ensure_ascii=False))
    print("技术预检未输出凭证、未执行写操作，也不代表真实员工 SSO 已通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
