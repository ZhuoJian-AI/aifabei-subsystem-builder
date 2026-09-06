#!/usr/bin/env python3
"""Validate a deployed protocol-v2 subsystem without exposing its token."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from publish_subsystem import load_app_credentials


class RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HTTPError(req.full_url, code, "重定向不被接入协议允许", headers, fp)


OPENER = build_opener(ProxyHandler({}), RejectRedirects())
STABLE_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
VALIDATION_WARNINGS: list[str] = []


def get_json(url: str, token: str) -> dict:
    headers = {"Accept": "application/json", "User-Agent": "Aifabei-Contract-Validator/2.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with OPENER.open(Request(url, headers=headers), timeout=15) as response:
        if not 200 <= response.status < 300:
            raise SystemExit(f"{url} 返回 HTTP {response.status}")
        payload = response.read(4 * 1024 * 1024 + 1)
        if len(payload) > 4 * 1024 * 1024:
            raise SystemExit(f"{url} 响应超过 4 MiB。")
        return json.loads(payload)


def same_origin(left: str, right: str) -> bool:
    first, second = urlsplit(left), urlsplit(right)
    return (first.scheme, first.hostname, first.port) == (second.scheme, second.hostname, second.port)


def require_text(value: object, label: str, maximum: int = 1000) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not text or len(text) > maximum:
        raise SystemExit(f"{label} 必须是 1–{maximum} 字的非空文本。")
    return text


def warn(message: str) -> None:
    VALIDATION_WARNINGS.append(message)


def check_optional_text(value: object, label: str, maximum: int = 1000) -> None:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        warn(f"{label} 已提供但不是有效的非空文本，平台将忽略该增强项。")


def validate_schema(schema: dict, label: str, *, require_object_root: bool) -> None:
    if require_object_root and schema.get("type") != "object":
        raise SystemExit(f"{label}.type 必须为 object。")
    properties = schema.get("properties")
    if properties is not None and not isinstance(properties, dict):
        raise SystemExit(f"{label}.properties 必须是对象。")
    required = schema.get("required")
    if required is not None and (not isinstance(required, list) or len(required) != len(set(required))):
        raise SystemExit(f"{label}.required 必须是无重复字段名的数组。")
    unknown_required = [key for key in (required or []) if key not in (properties or {})]
    if unknown_required:
        raise SystemExit(f"{label}.required 引用了未定义字段：{', '.join(map(str, unknown_required))}")

    def walk_property(field_schema: object, field_label: str) -> None:
        if not isinstance(field_schema, dict):
            raise SystemExit(f"{field_label} 必须是 JSON Schema 对象。")
        if "description" not in field_schema:
            warn(f"{field_label} 建议补充 description。")
        else:
            check_optional_text(field_schema["description"], f"{field_label}.description", 500)
        nested = field_schema.get("properties")
        if nested is not None:
            if not isinstance(nested, dict):
                raise SystemExit(f"{field_label}.properties 必须是对象。")
            for nested_name, nested_schema in nested.items():
                walk_property(nested_schema, f"{field_label}.properties.{nested_name}")
        items = field_schema.get("items")
        if isinstance(items, dict) and isinstance(items.get("properties"), dict):
            for nested_name, nested_schema in items["properties"].items():
                walk_property(nested_schema, f"{field_label}.items.properties.{nested_name}")

    for field_name, field_schema in (properties or {}).items():
        walk_property(field_schema, f"{label}.properties.{field_name}")


EXPORT_RESULT_FIELDS = {
    "snapshotId", "snapshotAt", "columns", "rows", "rowCount", "nextCursor",
}
FORBIDDEN_ACTION_RESULT_FIELD_FRAGMENTS = {
    "backuppath", "serverpath", "filesystempath", "localpath", "databasepath", "dbpath",
    "databaseurl", "connectionstring", "contentref",
}


def reject_server_path_fields(schema: object, label: str) -> None:
    if not isinstance(schema, dict):
        return
    for field_name, child in (schema.get("properties") or {}).items():
        normalized = str(field_name).replace("_", "").casefold()
        if any(fragment in normalized for fragment in FORBIDDEN_ACTION_RESULT_FIELD_FRAGMENTS):
            raise SystemExit(f"{label} 不得声明服务器路径、数据库连接或备份字段。")
        reject_server_path_fields(child, label)
    reject_server_path_fields(schema.get("items"), label)


def validate_export_schema(action: dict, label: str) -> None:
    input_schema = action["inputSchema"]
    input_properties = input_schema.get("properties") or {}
    if input_schema.get("type") != "object" or input_schema.get("additionalProperties") is not False:
        raise SystemExit(f"{label}.inputSchema 必须是封闭对象。")
    limit_schema = input_properties.get("limit")
    if not isinstance(limit_schema, dict) or limit_schema.get("type") != "integer":
        raise SystemExit(f"{label}.inputSchema 必须声明整数 limit。")
    if not isinstance(limit_schema.get("maximum"), int) or limit_schema["maximum"] > 1000:
        raise SystemExit(f"{label}.inputSchema.limit.maximum 必须是不超过 1000 的整数。")
    for key in ("snapshotId", "nextCursor"):
        schema = input_properties.get(key)
        if not isinstance(schema, dict) or schema.get("type") != "string":
            raise SystemExit(f"{label}.inputSchema 必须声明字符串 {key}。")

    result_schema = action["resultSchema"]
    properties = result_schema.get("properties")
    required = set(result_schema.get("required") or [])
    if (
        result_schema.get("type") != "object"
        or result_schema.get("additionalProperties") is not False
        or not isinstance(properties, dict)
        or set(properties) != EXPORT_RESULT_FIELDS
        or required != EXPORT_RESULT_FIELDS
    ):
        raise SystemExit(f"{label}.resultSchema 必须使用封闭的标准分页数据集字段。")
    expected_types = {
        "snapshotId": "string", "snapshotAt": "string", "columns": "array",
        "rows": "array", "rowCount": "integer",
    }
    for key, expected in expected_types.items():
        if not isinstance(properties.get(key), dict) or properties[key].get("type") != expected:
            raise SystemExit(f"{label}.resultSchema.properties.{key}.type 必须为 {expected}。")
    if properties["snapshotAt"].get("format") != "date-time":
        raise SystemExit(f"{label}.resultSchema.properties.snapshotAt 必须声明 date-time 格式。")
    columns_items = properties["columns"].get("items")
    if not isinstance(columns_items, dict) or (
        columns_items.get("type") != "object"
        or columns_items.get("additionalProperties") is not False
        or set(columns_items.get("required") or []) != {"key", "label", "type"}
        or set((columns_items.get("properties") or {})) != {"key", "label", "type"}
    ):
        raise SystemExit(f"{label}.resultSchema.properties.columns 必须声明封闭的 key/label/type 列定义。")
    for key in ("key", "label"):
        if (columns_items["properties"].get(key) or {}).get("type") != "string":
            raise SystemExit(f"{label}.resultSchema.columns.{key} 必须为 string。")
    column_type = columns_items["properties"].get("type") or {}
    if column_type.get("type") != "string" or not set(column_type.get("enum") or []):
        raise SystemExit(f"{label}.resultSchema.columns.type 必须声明非空字符串枚举。")
    rows_items = properties["rows"].get("items")
    if not isinstance(rows_items, dict) or rows_items.get("type") != "object":
        raise SystemExit(f"{label}.resultSchema.properties.rows.items 必须为 object。")
    next_cursor_schema = properties.get("nextCursor", {})
    next_cursor_type = next_cursor_schema.get("type")
    any_of_types = {
        item.get("type") for item in next_cursor_schema.get("anyOf", []) if isinstance(item, dict)
    }
    if not (
        next_cursor_type == ["string", "null"]
        or next_cursor_type == ["null", "string"]
        or any_of_types == {"string", "null"}
    ):
        raise SystemExit(f"{label}.resultSchema.properties.nextCursor 必须允许 string 或 null。")

def validate_action_contract(action: dict, label: str) -> None:
    description = require_text(action.get("description"), f"{label}.description")
    if description.lower() in {"todo", "tbd", "placeholder", "execute action"} or description in {"执行操作", "处理数据"}:
        warn(f"{label}.description 看起来像占位文案，建议改成具体业务用途。")

    ai_tool = action.get("aiTool")
    if ai_tool is not None and not isinstance(ai_tool, dict):
        warn(f"{label}.aiTool 不是对象，平台将忽略该增强项。")
        ai_tool = None

    if isinstance(ai_tool, dict):
        if "whenToUse" in ai_tool:
            check_optional_text(ai_tool["whenToUse"], f"{label}.aiTool.whenToUse")
        elif action.get("aiEnabled"):
            warn(f"{label}.aiTool.whenToUse 是推荐增强项。")
        for field in ("whenNotToUse", "sideEffects"):
            if field in ai_tool:
                check_optional_text(ai_tool[field], f"{label}.aiTool.{field}")
            else:
                warn(f"{label}.aiTool.{field} 是推荐增强项。")
        if "confirmationPrompt" in ai_tool:
            check_optional_text(ai_tool["confirmationPrompt"], f"{label}.aiTool.confirmationPrompt", 500)
        preconditions = ai_tool.get("preconditions")
        if preconditions is None:
            warn(f"{label}.aiTool.preconditions 是推荐增强项。")
        elif not isinstance(preconditions, list) or len(preconditions) > 20:
            warn(f"{label}.aiTool.preconditions 格式不正确，平台将忽略该增强项。")
        else:
            for index, item in enumerate(preconditions):
                check_optional_text(item, f"{label}.aiTool.preconditions[{index}]", 500)
        examples = ai_tool.get("examples")
        if examples is None:
            warn(f"{label}.aiTool.examples 是推荐增强项。")
        elif not isinstance(examples, list) or len(examples) > 5:
            warn(f"{label}.aiTool.examples 格式不正确，平台将忽略该增强项。")
        else:
            for index, example in enumerate(examples):
                if not isinstance(example, dict) or not isinstance(example.get("params"), dict):
                    warn(f"{label}.aiTool.examples[{index}] 格式不正确，平台将忽略该示例。")
                    continue
                check_optional_text(
                    example.get("userRequest"), f"{label}.aiTool.examples[{index}].userRequest", 500
                )
        if action.get("requiresConfirmation") and "confirmationPrompt" not in ai_tool:
            warn(f"{label} 需要确认，建议补充 aiTool.confirmationPrompt。")
    elif action.get("aiEnabled"):
        warn(f"{label}.aiTool 未提供；平台将使用 description 生成基础工具，不阻断登记。")

    input_schema = action["inputSchema"]
    validate_schema(input_schema, f"{label}.inputSchema", require_object_root=True)
    result_schema = action["resultSchema"]
    validate_schema(result_schema, f"{label}.resultSchema", require_object_root=False)
    reject_server_path_fields(result_schema, f"{label}.resultSchema")

    operation = action.get("operation")
    if operation == "export":
        validate_export_schema(action, label)
    if action.get("aiEnabled") and operation in {"create", "update", "delete", "approve"}:
        properties = input_schema.get("properties")
        required = input_schema.get("required")
        if not isinstance(properties, dict) or not properties:
            raise SystemExit(f"{label}.inputSchema 必须声明真实业务字段，不能让 AI 猜参数。")
        if not isinstance(required, list) or not required:
            raise SystemExit(f"{label}.inputSchema.required 必须声明目标或必填业务字段。")
        if input_schema.get("additionalProperties") is not False:
            raise SystemExit(f"{label}.inputSchema.additionalProperties 必须为 false。")
    if action.get("aiEnabled") and operation in {"delete", "approve"} and not action.get("requiresConfirmation"):
        raise SystemExit(f"{label} 的删除或审批操作必须 requiresConfirmation=true。")


def main() -> int:
    parser = argparse.ArgumentParser(description="验证 Alphabet 模块系统 v2 接入协议")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--token-env", default="ZHUOJIAN_MANIFEST_ACCESS_TOKEN", help="Manifest 凭证环境变量名")
    parser.add_argument("--app-env-file", type=Path, help="Runtime 管理的应用凭证文件；默认按域名推断")
    parser.add_argument("--expect-min-modules", type=int, default=1, help="至少应发现多少个子模块")
    parser.add_argument("--expect-module-key", action="append", default=[], help="必须存在的 moduleKey，可重复")
    args = parser.parse_args()
    if args.expect_min_modules < 1:
        parser.error("--expect-min-modules 必须大于等于 1")
    base = args.base_url.rstrip("/") + "/"
    token = os.environ.get(args.token_env, "")
    if not token:
        hostname = urlsplit(base).hostname or ""
        inferred_slug = hostname.split(".", 1)[0]
        env_file = args.app_env_file or Path("/etc/zhuojian/apps") / f"{inferred_slug}.env"
        token = load_app_credentials(env_file)["manifest_access_token"]

    health = get_json(urljoin(base, "health"), token)
    if health.get("status") != "ok":
        raise SystemExit("/health 必须明确返回 status=ok。")
    manifest_url = urljoin(base, "api/integration/manifest")
    manifest = get_json(manifest_url, token)
    required_manifest = (
        "protocol", "version", "contractRevision", "enterprise", "applicationSlug",
        "eventsUrl", "eventDeliveriesUrl", "auth", "modules",
    )
    missing = [key for key in required_manifest if key not in manifest]
    if missing:
        raise SystemExit("清单缺少字段：" + "、".join(missing))
    if manifest.get("protocol") != "zhuojian-subsystem" or manifest.get("version") != 2:
        raise SystemExit("清单必须使用 zhuojian-subsystem version 2。")
    if manifest.get("contractRevision") != "2.5":
        raise SystemExit("冷启动验收要求 contractRevision=2.5。")
    for module in manifest.get("modules") or []:
        if not module.get("accessRoles"):
            raise SystemExit(f"子模块 {module.get('moduleKey')} 缺少 accessRoles 权限组合建议。")
    enterprise = manifest.get("enterprise")
    if not isinstance(enterprise, dict) or not enterprise.get("key") or not enterprise.get("name"):
        raise SystemExit("清单 enterprise 必须包含稳定 key 和 name。")
    auth = manifest.get("auth")
    if (
        not isinstance(auth, dict)
        or auth.get("ssoPath") != "/api/integration/sso"
        or auth.get("mode") != "authorization_code"
        or "algorithm" in auth
    ):
        raise SystemExit("清单 auth 必须声明固定 ssoPath 和 authorization_code，且不得声明 algorithm。")

    events_url = urljoin(manifest_url, str(manifest["eventsUrl"]))
    if not same_origin(base, events_url):
        raise SystemExit("eventsUrl 必须与系统入口同源。")
    delivery_url = urljoin(manifest_url, str(manifest["eventDeliveriesUrl"]))
    if not same_origin(base, delivery_url) or urlsplit(delivery_url).path != "/api/integration/event-deliveries":
        raise SystemExit("eventDeliveriesUrl 必须是同源固定端点 /api/integration/event-deliveries。")
    events = get_json(f"{events_url}?{urlencode({'after': 0, 'limit': 1})}", token)
    if not all(key in events for key in ("items", "nextAfter", "hasMore")):
        raise SystemExit("事件接口缺少 items/nextAfter/hasMore。")
    event_items = events.get("items")
    if not isinstance(event_items, list):
        raise SystemExit("事件接口 items 必须是数组。")
    if event_items:
        sequence = event_items[0].get("sequence") if isinstance(event_items[0], dict) else None
        if not isinstance(sequence, int) or sequence < 1_000_000_000_000:
            raise SystemExit("事件 sequence 必须使用跨数据库重建不回退的全局单调序列，不能从 1 重新开始。")

    modules = manifest.get("modules")
    if not isinstance(modules, list) or not modules:
        raise SystemExit("清单 modules 必须是非空列表。")
    if len(modules) < args.expect_min_modules:
        raise SystemExit(
            f"清单只有 {len(modules)} 个子模块，验收要求至少 {args.expect_min_modules} 个；"
            "不能把新增子模块发布成另一个一级应用来绕过。"
        )
    module_keys: set[str] = set()
    action_keys: set[str] = set()
    page_keys: set[str] = set()
    department_keys: set[str] = set()
    for index, module in enumerate(modules):
        label = f"modules[{index}]"
        if not isinstance(module, dict) or not all(module.get(key) for key in ("moduleKey", "name", "route")):
            raise SystemExit(f"{label} 必须包含 moduleKey/name/route。")
        route = str(module["route"])
        parsed_route = urlsplit(route)
        if not route.startswith("/") or route.startswith("//") or parsed_route.scheme or parsed_route.netloc:
            raise SystemExit(f"{label}.route 必须是站内相对路径。")
        module_key = str(module["moduleKey"])
        if not STABLE_KEY_RE.fullmatch(module_key) or module_key in module_keys:
            raise SystemExit(f"子模块 moduleKey 格式无效或重复：{module_key}")
        module_keys.add(module_key)
        departments = module.get("departments")
        if not isinstance(departments, list) or not departments:
            raise SystemExit(f"{label}.departments 必须是非空列表。")
        owners = 0
        local_departments: set[str] = set()
        for department_index, department in enumerate(departments):
            if not isinstance(department, dict) or not all(department.get(key) for key in ("key", "name", "role")):
                raise SystemExit(f"{label}.departments[{department_index}] 必须包含 key/name/role。")
            key = str(department["key"])
            if not STABLE_KEY_RE.fullmatch(key) or key in local_departments:
                raise SystemExit(f"{label} 的部门 key 格式无效或重复：{key}")
            local_departments.add(key)
            department_keys.add(key)
            owners += int(department["role"] == "owner")
        if owners != 1:
            raise SystemExit(f"{label} 必须恰好有一个 owner 部门。")
        actions = module.get("actions")
        if not isinstance(actions, list):
            raise SystemExit(f"{label}.actions 必须是列表。")
        for action_index, action in enumerate(actions):
            action_label = f"{label}.actions[{action_index}]"
            required = (
                "actionKey", "name", "description", "operation", "aiEnabled", "requiresConfirmation",
                "inputSchema", "resultSchema",
            )
            if not isinstance(action, dict) or any(key not in action for key in required):
                raise SystemExit(f"{action_label} 缺少 v2 操作字段。")
            key = str(action["actionKey"])
            if not STABLE_KEY_RE.fullmatch(key) or key in action_keys:
                raise SystemExit(f"操作 actionKey 格式无效或重复：{key}")
            action_keys.add(key)
            if action["operation"] not in {"query", "create", "update", "delete", "export", "approve"}:
                raise SystemExit(f"{action_label}.operation 不受支持。")
            if not isinstance(action["aiEnabled"], bool) or not isinstance(action["requiresConfirmation"], bool):
                raise SystemExit(f"{action_label} 的 AI/确认标记必须是布尔值。")
            if not isinstance(action["inputSchema"], dict) or not isinstance(action["resultSchema"], dict):
                raise SystemExit(f"{action_label} 的输入输出 Schema 必须是对象。")
            validate_action_contract(action, action_label)
        pages = module.get("pages")
        if not isinstance(pages, list) or not pages:
            raise SystemExit(f"{label}.pages 必须是非空列表。")
        module_action_keys = {str(item["actionKey"]) for item in actions}
        module_page_keys: set[str] = set()
        for page_index, page in enumerate(pages):
            page_label = f"{label}.pages[{page_index}]"
            required_page = ("pageKey", "name", "routePattern", "actionKeys", "contextSchema")
            if not isinstance(page, dict) or any(key not in page for key in required_page):
                raise SystemExit(f"{page_label} 缺少页面目录字段。")
            page_key = str(page["pageKey"])
            if not STABLE_KEY_RE.fullmatch(page_key) or page_key in page_keys:
                raise SystemExit(f"pageKey 格式无效或重复：{page_key}")
            page_keys.add(page_key)
            module_page_keys.add(page_key)
            route_pattern = str(page["routePattern"])
            parsed_page_route = urlsplit(route_pattern)
            if not route_pattern.startswith("/") or route_pattern.startswith("//") or parsed_page_route.scheme or parsed_page_route.netloc:
                raise SystemExit(f"{page_label}.routePattern 必须是站内相对路径。")
            if not isinstance(page["actionKeys"], list) or any(str(key) not in module_action_keys for key in page["actionKeys"]):
                raise SystemExit(f"{page_label}.actionKeys 引用了本子模块不存在的操作。")
            if page.get("queryActionKey") and page["queryActionKey"] not in page["actionKeys"]:
                raise SystemExit(f"{page_label}.queryActionKey 必须出现在 actionKeys 中。")
            if not isinstance(page["contextSchema"], dict):
                raise SystemExit(f"{page_label}.contextSchema 必须是对象。")
        for department_index, department in enumerate(departments):
            department_action_keys = department.get("actionKeys")
            department_page_keys = department.get("pageKeys")
            if department_action_keys is not None and (
                not isinstance(department_action_keys, list)
                or any(str(key) not in module_action_keys for key in department_action_keys)
            ):
                raise SystemExit(f"{label}.departments[{department_index}].actionKeys 引用了本子模块不存在的操作。")
            if department_page_keys is not None and (
                not isinstance(department_page_keys, list)
                or any(str(key) not in module_page_keys for key in department_page_keys)
            ):
                raise SystemExit(f"{label}.departments[{department_index}].pageKeys 引用了本子模块不存在的页面。")

    missing_expected = sorted(set(args.expect_module_key) - module_keys)
    if missing_expected:
        raise SystemExit("清单缺少预期子模块：" + "、".join(missing_expected))

    for message in VALIDATION_WARNINGS:
        print(f"WARNING: {message}")
    print(
        f"接入验证通过：健康状态 {health.get('status', 'ok')}，企业 {enterprise['name']}，"
        f"系统 {manifest['applicationSlug']}，子模块 {len(modules)} 个，"
        f"参与部门 {len(department_keys)} 个，页面 {len(page_keys)} 个，操作 {len(action_keys)} 个。"
    )
    print("Token 未输出；需要执行 SSO、页面感知 Action 和事件投递时继续运行 e2e_acceptance.py。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
