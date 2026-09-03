#!/usr/bin/env python3
"""Validate a deployed protocol-v2 subsystem without exposing its token."""

from __future__ import annotations

import argparse
import json
import os
import re
from urllib.error import HTTPError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise HTTPError(req.full_url, code, "重定向不被接入协议允许", headers, fp)


OPENER = build_opener(RejectRedirects)
STABLE_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def get_json(url: str, token: str) -> dict:
    headers = {"Accept": "application/json", "User-Agent": "Aifabei-Contract-Validator/2.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with OPENER.open(Request(url, headers=headers), timeout=15) as response:
        if not 200 <= response.status < 300:
            raise SystemExit(f"{url} 返回 HTTP {response.status}")
        return json.load(response)


def same_origin(left: str, right: str) -> bool:
    first, second = urlsplit(left), urlsplit(right)
    return (first.scheme, first.hostname, first.port) == (second.scheme, second.hostname, second.port)


def main() -> int:
    parser = argparse.ArgumentParser(description="验证 Alphabet 模块系统 v2 接入协议")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--token-env", default="ZHUOJIAN_INTEGRATION_SECRET", help="保存唯一接入密钥的环境变量名")
    parser.add_argument("--expect-min-modules", type=int, default=1, help="至少应发现多少个子模块")
    parser.add_argument("--expect-module-key", action="append", default=[], help="必须存在的 moduleKey，可重复")
    args = parser.parse_args()
    if args.expect_min_modules < 1:
        parser.error("--expect-min-modules 必须大于等于 1")
    base = args.base_url.rstrip("/") + "/"
    token = os.environ.get(args.token_env, "")

    health = get_json(urljoin(base, "health"), token)
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
    if manifest.get("contractRevision") != "2.4":
        raise SystemExit("冷启动验收要求 contractRevision=2.4。")
    for module in manifest.get("modules") or []:
        if not module.get("accessRoles"):
            raise SystemExit(f"子模块 {module.get('moduleKey')} 缺少 accessRoles 角色建议。")
    enterprise = manifest.get("enterprise")
    if not isinstance(enterprise, dict) or not enterprise.get("key") or not enterprise.get("name"):
        raise SystemExit("清单 enterprise 必须包含稳定 key 和 name。")
    auth = manifest.get("auth")
    if (
        not isinstance(auth, dict)
        or auth.get("ssoPath") != "/api/integration/sso"
        or auth.get("algorithm") != "HS256"
    ):
        raise SystemExit("清单 auth 必须声明固定 ssoPath 和 HS256。")

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
            if not isinstance(department.get("actionKeys"), list) or not isinstance(department.get("pageKeys"), list):
                raise SystemExit(f"{label}.departments[{department_index}] 必须声明 actionKeys/pageKeys。")
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
                "actionKey", "name", "operation", "aiEnabled", "requiresConfirmation",
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
            if any(str(key) not in module_action_keys for key in department["actionKeys"]):
                raise SystemExit(f"{label}.departments[{department_index}].actionKeys 引用了本子模块不存在的操作。")
            if any(str(key) not in module_page_keys for key in department["pageKeys"]):
                raise SystemExit(f"{label}.departments[{department_index}].pageKeys 引用了本子模块不存在的页面。")

    missing_expected = sorted(set(args.expect_module_key) - module_keys)
    if missing_expected:
        raise SystemExit("清单缺少预期子模块：" + "、".join(missing_expected))

    print(
        f"接入验证通过：健康状态 {health.get('status', 'ok')}，企业 {enterprise['name']}，"
        f"系统 {manifest['applicationSlug']}，子模块 {len(modules)} 个，"
        f"参与部门 {len(department_keys)} 个，页面 {len(page_keys)} 个，操作 {len(action_keys)} 个。"
    )
    print("Token 未输出；需要执行 SSO、页面感知 Action 和事件投递时继续运行 e2e_acceptance.py。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
