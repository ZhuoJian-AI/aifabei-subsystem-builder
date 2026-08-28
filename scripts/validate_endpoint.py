#!/usr/bin/env python3
"""Validate a deployed module contract without placing a token on the command line."""

from __future__ import annotations

import argparse
import json
import os
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def get_json(url: str, token: str) -> dict:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urlopen(Request(url, headers=headers), timeout=15) as response:
        return json.load(response)


def main() -> int:
    parser = argparse.ArgumentParser(description="验证已部署模块系统的清单、协作部门、操作和事件游标")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--token-env", default="ZHUOJIAN_SUBSYSTEM_TOKEN", help="保存 Token 的环境变量名")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    token = os.environ.get(args.token_env, "")
    manifest = get_json(f"{base}/api/integration/manifest", token)
    events = get_json(f"{base}/api/integration/events?{urlencode({'after': 0, 'limit': 1})}", token)
    required_manifest = ("protocol", "version", "enterprise", "applicationSlug", "modules")
    missing = [key for key in required_manifest if key not in manifest]
    if missing:
        raise SystemExit("清单缺少字段：" + "、".join(missing))
    if not isinstance(manifest.get("version"), int) or manifest["version"] < 2:
        raise SystemExit("清单 version 必须为模块聚合协议版本 2 或更高。")
    enterprise = manifest.get("enterprise")
    if not isinstance(enterprise, dict) or not enterprise.get("key") or not enterprise.get("name"):
        raise SystemExit("清单 enterprise 必须包含稳定 key 和 name。")
    modules = manifest.get("modules")
    if not isinstance(modules, list) or not modules:
        raise SystemExit("清单 modules 必须是非空列表。")
    module_keys: set[str] = set()
    action_keys: set[str] = set()
    for index, module in enumerate(modules):
        label = f"modules[{index}]"
        if not isinstance(module, dict) or not all(module.get(key) for key in ("key", "name", "route")):
            raise SystemExit(f"{label} 必须包含 key/name/route。")
        if module["key"] in module_keys:
            raise SystemExit(f"模块 key 重复：{module['key']}")
        module_keys.add(module["key"])
        if "department" in module:
            raise SystemExit(f"{label} 不应使用单值 department，请迁移为 departments 列表。")
        departments = module.get("departments")
        if not isinstance(departments, list) or not departments:
            raise SystemExit(f"{label}.departments 必须是非空列表，不能使用单值 department。")
        department_keys: set[str] = set()
        for department_index, department in enumerate(departments):
            if not isinstance(department, dict) or not all(department.get(key) for key in ("key", "name", "role")):
                raise SystemExit(f"{label}.departments[{department_index}] 必须包含 key/name/role。")
            if department["key"] in department_keys:
                raise SystemExit(f"{label} 的部门 key 重复：{department['key']}")
            department_keys.add(department["key"])
        actions = module.get("actions")
        if not isinstance(actions, list):
            raise SystemExit(f"{label}.actions 必须是列表；没有 AI 可调用操作时使用空列表。")
        for action_index, action in enumerate(actions):
            action_label = f"{label}.actions[{action_index}]"
            if not isinstance(action, dict) or not all(
                action.get(key) for key in ("key", "name", "method", "path", "permission")
            ):
                raise SystemExit(f"{action_label} 必须包含 key/name/method/path/permission。")
            if action["key"] in action_keys:
                raise SystemExit(f"操作 key 重复：{action['key']}")
            action_keys.add(action["key"])
            if action["method"].upper() != "POST" or not action["path"].startswith("/api/integration/actions/"):
                raise SystemExit(f"{action_label} 必须使用 POST /api/integration/actions/{{actionKey}}。")
            if not isinstance(action.get("requiresConfirmation"), bool):
                raise SystemExit(f"{action_label}.requiresConfirmation 必须是布尔值。")
    if not all(key in events for key in ("items", "nextAfter", "hasMore")):
        raise SystemExit("事件接口缺少 items/nextAfter/hasMore。")
    department_keys = {
        department["key"]
        for module in modules
        for department in module["departments"]
    }
    action_count = sum(len(module["actions"]) for module in modules)
    print(
        f"接入验证通过：企业 {enterprise['name']}，应用 {manifest['applicationSlug']}，"
        f"模块 {len(modules)} 个，参与部门 {len(department_keys)} 个，操作 {action_count} 个。"
    )
    print("Token 未输出。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
