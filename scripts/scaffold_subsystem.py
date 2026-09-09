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
    parser.add_argument("--company-slug", default="alphabet", help="企业稳定英文标识，默认 alphabet")
    parser.add_argument("--company-name", default="Alphabet", help="企业显示名称，默认 Alphabet")
    parser.add_argument("--application-slug", required=True)
    parser.add_argument("--application-name", required=True)
    parser.add_argument("--module-key", required=True)
    parser.add_argument("--module-name", required=True)
    parser.add_argument("--department", action="append", default=[], help="可重复：key:显示名:role")
    parser.add_argument(
        "--platform-ai-capability",
        action="append",
        default=[],
        choices=(
            "vision.ocr", "vision.compare", "vision.classify",
            "speech.transcribe", "text.extract", "business.predict",
        ),
        help="可重复：业务确实需要时由 AI 选择平台受控专业 AI 能力",
    )
    args = parser.parse_args()

    try:
        company_slug = project_part(args.company_slug, "companySlug")
        application_slug = project_part(args.application_slug, "applicationSlug")
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))
    if company_slug == "aifabei":
        company_slug = "alphabet"
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
    operation_name = {
            "query": "查询", "create": "新增", "update": "修改", "delete": "删除",
            "approve": "审批", "export": "导出",
    }
    input_schemas = {
        "query": {"type": "object", "additionalProperties": False, "properties": {
            "filters": {"type": "object", "description": f"用于筛选{args.module_name}记录的业务条件"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 500, "description": "单次查询最多返回的记录数"},
        }},
        "create": {"type": "object", "additionalProperties": False, "required": ["data"], "properties": {
            "data": {"type": "object", "description": f"创建{args.module_name}记录所需的业务字段"},
        }},
        "update": {"type": "object", "additionalProperties": False, "required": ["id", "changes"], "properties": {
            "id": {"type": "string", "description": f"需要修改的{args.module_name}记录标识"},
            "changes": {"type": "object", "description": "本次需要修改的业务字段和值"},
        }},
        "delete": {"type": "object", "additionalProperties": False, "required": ["id"], "properties": {
            "id": {"type": "string", "description": f"需要删除的{args.module_name}记录标识"},
        }},
        "approve": {"type": "object", "additionalProperties": False, "required": ["id"], "properties": {
            "id": {"type": "string", "description": f"需要审批的{args.module_name}记录标识"},
            "comment": {"type": "string", "description": "审批意见，没有意见时可以省略"},
        }},
        "export": {"type": "object", "additionalProperties": False, "properties": {
            "filters": {"type": "object", "description": f"用于筛选{args.module_name}记录的业务条件"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 500, "description": "单页最大记录数"},
            "snapshotId": {"type": "string", "description": "首轮返回的快照标识，首次调用省略"},
            "nextCursor": {"type": "string", "description": "上一页返回的不透明游标，首次调用省略"},
        }},
    }
    example_params = {
        "query": {"filters": {"status": "待处理"}},
        "create": {"data": {"name": "示例记录"}},
        "update": {"id": "record-id", "changes": {"status": "已更新"}},
        "delete": {"id": "record-id"},
        "approve": {"id": "record-id", "comment": "同意"},
        "export": {"filters": {"status": "待处理"}, "limit": 200},
    }
    result_schemas = {
        "query": {"type": "object", "additionalProperties": False, "description": f"查询{args.module_name}后的结构化业务结果", "required": ["items"], "properties": {
            "items": {"type": "array", "description": f"当前用户权限范围内的{args.module_name}记录列表"},
        }},
        "create": {"type": "object", "additionalProperties": False, "description": f"新增{args.module_name}后的结构化业务结果", "required": ["id", "version"], "properties": {
            "id": {"type": "string", "description": "新建记录的稳定标识"},
            "version": {"type": "integer", "description": "新建记录的当前版本号"},
            "status": {"type": "string", "description": "新建记录的当前业务状态"},
        }},
        "update": {"type": "object", "additionalProperties": False, "description": f"修改{args.module_name}后的结构化业务结果", "required": ["id", "version"], "properties": {
            "id": {"type": "string", "description": "已修改记录的稳定标识"},
            "version": {"type": "integer", "description": "修改后的记录版本号"},
            "status": {"type": "string", "description": "修改后的业务状态"},
        }},
        "delete": {"type": "object", "additionalProperties": False, "description": f"删除{args.module_name}后的结构化业务结果", "required": ["id", "deleted"], "properties": {
            "id": {"type": "string", "description": "已删除记录的稳定标识"},
            "deleted": {"type": "boolean", "description": "是否已完成删除"},
        }},
        "approve": {"type": "object", "additionalProperties": False, "description": f"审批{args.module_name}后的结构化业务结果", "required": ["id", "version", "status"], "properties": {
            "id": {"type": "string", "description": "已审批记录的稳定标识"},
            "version": {"type": "integer", "description": "审批后的记录版本号"},
            "status": {"type": "string", "description": "审批后的业务状态"},
        }},
        "export": {"type": "object", "additionalProperties": False, "description": f"导出{args.module_name}的标准分页数据集", "required": ["snapshotId", "snapshotAt", "columns", "rows", "rowCount", "nextCursor"], "properties": {
            "snapshotId": {"type": "string", "description": "绑定本次权限范围和筛选条件的快照标识"},
            "snapshotAt": {"type": "string", "format": "date-time", "description": "快照生成时间"},
            "columns": {"type": "array", "description": "导出字段定义", "items": {"type": "object", "additionalProperties": False, "required": ["key", "label", "type"], "properties": {
                "key": {"type": "string", "description": "稳定字段键"},
                "label": {"type": "string", "description": "字段显示名"},
                "type": {"type": "string", "enum": ["string", "number", "boolean", "date", "datetime"], "description": "字段数据类型"},
            }}},
            "rows": {"type": "array", "description": "当前页权限过滤后的业务记录", "items": {
                "type": "object",
                "additionalProperties": {"type": ["string", "number", "boolean", "null"]},
            }},
            "rowCount": {"type": "integer", "minimum": 0, "description": "当前权限和筛选条件下的总记录数"},
            "nextCursor": {"type": ["string", "null"], "description": "下一页不透明游标；末页为空"},
        }},
    }
    action_rows = []
    for operation, confirm in actions:
        verb = operation_name[operation]
        writes_data = operation in {"create", "update", "delete", "approve"}
        ai_tool = {
            "whenToUse": f"用户明确要求{verb}{args.module_name}业务数据，并且目标和条件已经足够明确时使用。",
            "whenNotToUse": f"用户只是咨询规则、信息不足，或要求执行{args.module_name}以外的业务时不要使用。",
            "preconditions": [
                f"当前用户已获得{args.module_name}页面及 {module_key}.{operation} Action 权限。",
                "涉及具体记录时，已经通过最新查询或页面上下文确认目标记录。",
            ],
            "sideEffects": (
                f"会对{args.module_name}业务数据执行{verb}并留下审计记录。"
                if writes_data else f"不会修改{args.module_name}业务数据。"
            ),
            "examples": [{
                "userRequest": f"请帮我{verb}一条{args.module_name}记录",
                "params": example_params[operation],
            }],
        }
        if confirm:
            ai_tool["confirmationPrompt"] = f"即将{verb}{args.module_name}业务数据，是否确认继续？"
        action_rows.append({
            "actionKey": f"{module_key}.{operation}",
            "name": f"{verb}{args.module_name}",
            "description": f"{verb}{args.module_name}业务数据，并返回可供页面和平台 AI 继续处理的结构化结果。",
            "operation": operation,
            "aiEnabled": True,
            "requiresConfirmation": confirm,
            "aiTool": ai_tool,
            "inputSchema": input_schemas[operation],
            "resultSchema": result_schemas[operation],
        })
    specialist_specs = {
        "vision.ocr": (
            "图片文字识别", ["image"],
            {
                "text": {"type": "string", "description": "忠实识别的文字"},
                "sections": {
                    "type": "array", "description": "按版面分组的识别结果",
                    "items": {"type": "string"},
                },
            },
        ),
        "vision.compare": (
            "图片差异比较", ["image"],
            {
                "changes": {
                    "type": "array", "description": "有可见证据的差异",
                    "items": {"type": "string"},
                },
            },
        ),
        "vision.classify": (
            "图片业务分类", ["image"],
            {
                "category": {"type": "string", "description": "业务分类"},
                "reason": {"type": "string", "description": "可见证据说明"},
            },
        ),
        "speech.transcribe": (
            "业务语音转写", ["audio"],
            {"transcript": {"type": "string", "description": "可人工校正的转写文字"}},
        ),
        "text.extract": (
            "业务文字抽取", ["text"],
            {
                "items": {
                    "type": "array", "description": "按业务要求抽取的内容",
                    "items": {"type": "string"},
                },
            },
        ),
        "business.predict": (
            "业务辅助预测", ["json"],
            {
                "prediction": {"type": "string", "description": "辅助判断，不是确定结论"},
                "reasons": {
                    "type": "array", "description": "基于输入业务事实的理由",
                    "items": {"type": "string"},
                },
            },
        ),
    }
    for capability in dict.fromkeys(args.platform_ai_capability):
        name, input_kinds, properties = specialist_specs[capability]
        action_rows.append({
            "actionKey": f"{module_key}.{capability.replace('.', '_')}",
            "name": name,
            "description": (
                f"通过灼见 SaaS 对{args.module_name}输入执行{name}，"
                "只返回可人工校正的草稿，不直接写业务数据。"
            ),
            "operation": "query",
            "aiEnabled": True,
            "requiresConfirmation": False,
            "aiTool": {
                "whenToUse": f"用户明确要求{name}时使用。",
                "whenNotToUse": "不得把 AI 草稿当成已确认业务记录。",
                "preconditions": ["当前页面和该专业 AI Action 已获平台授权。"],
                "sideEffects": "只生成草稿，不修改业务记录。",
                "examples": [],
            },
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "recordId": {
                        "type": "string",
                        "description": "可选：当前要关联的业务记录标识",
                    },
                },
            },
            "resultSchema": {
                "type": "object",
                "additionalProperties": False,
                "properties": properties,
                "required": list(properties),
            },
            "platformAiCapability": {
                "type": capability,
                "inputKinds": input_kinds,
                "humanConfirmation": "required",
            },
        })
    page_key = f"{module_key}.list"
    department_rows = list(departments)
    permission_bundles = [
        ("basic", "基础查看", {"query"}),
    ]
    access_roles = [{
        "roleKey": f"{module_key}.{bundle_key}",
        "name": f"{args.module_name}{bundle_name}权限组合",
        "description": "最小冷启动权限组合；发布前应按真实业务增补必要组合。它只供管理员向 SaaS 已有平台角色授权，不创建子系统角色。",
        "pageKeys": [page_key],
        "actionKeys": [
            row["actionKey"] for row in action_rows
            if row["operation"] in operations
        ],
    } for bundle_key, bundle_name, operations in permission_bundles]
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
        "auth": {"ssoPath": "/api/integration/sso", "mode": "authorization_code"},
        "modules": [{
            "moduleKey": module_key,
            "name": args.module_name.strip(),
            "route": f"/{module_key.replace('_', '-')}",
            "departments": department_rows,
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
                "aiSemantics": {
                    "purpose": f"查看、筛选并处理{args.module_name}业务记录。",
                    "primaryEntities": [module_key],
                    "fieldSemantics": [],
                    "supportedIntents": [
                        f"说明{args.module_name}页面用途",
                        f"查询符合条件的{args.module_name}记录",
                        f"生成{args.module_name}业务数据文件",
                    ],
                    "relatedPages": [],
                    "businessTerms": [],
                    "defaultQueryActionKey": f"{module_key}.query",
                },
            }],
            "actions": action_rows,
            "events": {"publishes": [], "subscribes": []},
        }],
    }
    (output / "subsystem.json").write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    replacements = {
        "__COMPANY_SLUG__": company_slug,
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
