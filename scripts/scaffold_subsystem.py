#!/usr/bin/env python3
"""Create an isolated native subsystem starter for an ECS-local Git repository."""

from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path

KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
PROJECT_PART_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def stable(value: str, label: str) -> str:
    value = value.strip()
    if not KEY_RE.fullmatch(value):
        raise argparse.ArgumentTypeError(f"{label} 必须是小写稳定标识")
    return value


def project_part(value: str, label: str) -> str:
    value = value.strip()
    if not PROJECT_PART_RE.fullmatch(value):
        raise argparse.ArgumentTypeError(f"{label} 只允许小写字母、数字和单连字符")
    return value


def department(value: str) -> dict[str, str]:
    parts = value.split(":", 2)
    if len(parts) != 3 or parts[2] not in {"owner", "collaborator", "approver", "consumer"}:
        raise argparse.ArgumentTypeError("部门格式必须是 key:显示名:owner|collaborator|approver|consumer")
    return {"key": stable(parts[0], "部门 key"), "name": parts[1].strip(), "role": parts[2]}


def main() -> int:
    parser = argparse.ArgumentParser(description="建立灼见原生模块系统骨架")
    parser.add_argument("--output", required=True, help="新的项目目录，必须为空或不存在")
    parser.add_argument("--company-slug", default="aifabei", help="企业稳定英文标识，默认 aifabei")
    parser.add_argument("--company-name", default="Alphabet", help="企业显示名称，默认 Alphabet")
    parser.add_argument("--application-slug", required=True)
    parser.add_argument("--application-name", required=True)
    parser.add_argument("--module-key", required=True)
    parser.add_argument("--module-name", required=True)
    parser.add_argument("--department", action="append", default=[], help="可重复：key:显示名:role")
    args = parser.parse_args()

    try:
        company_slug = project_part(args.company_slug, "companySlug")
        application_slug = project_part(args.application_slug, "applicationSlug")
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))
    if application_slug == company_slug or application_slug.startswith(f"{company_slug}-"):
        parser.error("applicationSlug 不得重复 companySlug 前缀；本地项目名会自动添加企业前缀")
    project_name = f"{company_slug}-{application_slug}"
    module_key = stable(args.module_key, "moduleKey")
    departments = [department(item) for item in args.department]
    if not departments:
        parser.error("至少提供一个 --department，且必须恰好一个 owner")
    if sum(item["role"] == "owner" for item in departments) != 1:
        parser.error("参与部门必须恰好有一个 owner")

    output = Path(args.output).expanduser().resolve()
    if output.exists() and any(output.iterdir()):
        parser.error(f"输出目录不是空目录：{output}")
    template = Path(__file__).resolve().parents[1] / "assets" / "native-subsystem-template"
    output.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        template,
        output,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(
            "__pycache__",
            "*.pyc",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
        ),
    )

    actions = [
        ("query", False), ("create", False), ("update", False), ("delete", True),
        ("approve", True), ("export", False),
    ]
    action_rows = [{
        "actionKey": f"{module_key}.{operation}",
        "name": {
            "query": "查询", "create": "新增", "update": "修改", "delete": "删除",
            "approve": "审批", "export": "导出",
        }[operation] + args.module_name,
        "description": f"{operation} {args.module_name}业务数据",
        "operation": operation,
        "aiEnabled": True,
        "requiresConfirmation": confirm,
        "inputSchema": {"type": "object", "properties": {}},
        "resultSchema": {"type": "object"},
    } for operation, confirm in actions]
    page_key = f"{module_key}.list"
    role_operations = {
        "owner": {"query", "create", "update", "delete", "approve", "export"},
        "collaborator": {"query", "create", "update", "export"},
        "approver": {"query", "approve", "export"},
        "consumer": {"query", "export"},
    }
    access_roles = [{
        "roleKey": f"{module_key}.{item['key']}.{item['role']}",
        "name": f"{args.module_name}{item['name']}{'负责人' if item['role'] == 'owner' else '协作角色'}",
        "suggestedDepartmentKey": item["key"],
        "pageKeys": [page_key],
        "actionKeys": [
            row["actionKey"] for row in action_rows
            if row["operation"] in role_operations[item["role"]]
        ],
    } for item in departments]
    config = {
        "protocol": "zhuojian-subsystem",
        "version": 2,
        "contractRevision": "2.5",
        "enterprise": {"key": company_slug, "name": args.company_name.strip()},
        "applicationSlug": application_slug,
        "applicationName": args.application_name.strip(),
        "bridgeVersion": 1,
        "eventsUrl": "/api/integration/events",
        "eventDeliveriesUrl": "/api/integration/event-deliveries",
        "auth": {"ssoPath": "/api/integration/sso", "algorithm": "HS256"},
        "modules": [{
            "moduleKey": module_key,
            "name": args.module_name.strip(),
            "route": f"/{module_key.replace('_', '-')}",
            "departments": departments,
            "accessRoles": access_roles,
            "pages": [{
                "pageKey": page_key,
                "name": f"{args.module_name}列表",
                "routePattern": f"/{module_key.replace('_', '-')}",
                "queryActionKey": f"{module_key}.query",
                "actionKeys": [row["actionKey"] for row in action_rows],
                "contextSchema": {"type": "object", "properties": {
                    "filters": {"type": "object"}, "selection": {"type": "object"},
                    "entity_id": {"type": ["string", "null"]}, "data_version": {"type": ["integer", "string", "null"]},
                }},
            }],
            "actions": action_rows,
            "events": {"publishes": [], "subscribes": []},
        }],
    }
    (output / "subsystem.json").write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    replacements = {
        "__APPLICATION_NAME__": args.application_name.strip(),
        "__APPLICATION_SLUG__": application_slug,
        "__LOCAL_PROJECT_NAME__": project_name,
        "__MODULE_NAME__": args.module_name.strip(),
        "__MODULE_KEY__": module_key,
    }
    for relative in ("README.md", "AGENTS.md", "static/index.html"):
        path = output / relative
        content = path.read_text(encoding="utf-8")
        for old, new in replacements.items():
            content = content.replace(old, new)
        path.write_text(content, encoding="utf-8")
    print(json.dumps({
        "status": "created",
        "path": str(output),
        "companySlug": company_slug,
        "applicationSlug": application_slug,
        "localProjectName": project_name,
        "moduleKey": module_key,
    }, ensure_ascii=False))
    print("下一步：实现真实字段和流程，运行项目测试，再执行 validate_endpoint.py 与 e2e_acceptance.py。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
