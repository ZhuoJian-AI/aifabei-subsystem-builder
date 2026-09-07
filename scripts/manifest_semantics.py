"""Semantic and tool-contract checks shared by source validation and tests."""

from __future__ import annotations

import re
from typing import Any


STABLE_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
AI_SEMANTICS_FIELDS = {
    "purpose",
    "primaryEntities",
    "fieldSemantics",
    "supportedIntents",
    "relatedPages",
    "businessTerms",
    "defaultQueryActionKey",
}
EXPORT_RESULT_FIELDS = {
    "snapshotId",
    "snapshotAt",
    "columns",
    "rows",
    "rowCount",
    "nextCursor",
}


def _closed_object_schema(value: object) -> bool:
    return (
        isinstance(value, dict)
        and value.get("type") == "object"
        and value.get("additionalProperties") is False
        and isinstance(value.get("properties"), dict)
        and bool(value["properties"])
    )


def validate_manifest_semantics(manifest: object, *, require_semantics: bool) -> list[str]:
    if not isinstance(manifest, dict):
        return ["subsystem.json 根节点必须是对象"]
    modules = manifest.get("modules")
    if not isinstance(modules, list) or not modules:
        return ["v2.5 subsystem.json 必须声明非空 modules"] if require_semantics else []

    failures: list[str] = []
    page_index: set[tuple[str, str]] = set()
    for module in modules:
        if not isinstance(module, dict):
            continue
        module_key = str(module.get("moduleKey") or "")
        for page in module.get("pages") or []:
            if isinstance(page, dict):
                page_index.add((module_key, str(page.get("pageKey") or "")))

    for module in modules:
        if not isinstance(module, dict):
            failures.append("modules 只能包含对象")
            continue
        module_key = str(module.get("moduleKey") or "")
        actions = {
            str(action.get("actionKey") or ""): action
            for action in module.get("actions") or []
            if isinstance(action, dict)
        }
        if require_semantics:
            action_meanings: dict[tuple[str, str], str] = {}
            for action_key, action in actions.items():
                if not action.get("aiEnabled"):
                    continue
                operation = str(action.get("operation") or "")
                meaning = re.sub(
                    r"\s+",
                    " ",
                    str(action.get("description") or action.get("name") or "").strip().casefold(),
                )
                if not meaning:
                    failures.append(f"Action {action_key} 缺少可区分的 name 或 description")
                    continue
                fingerprint = (operation, meaning)
                if fingerprint in action_meanings:
                    failures.append(
                        f"Action {action_key} 与 {action_meanings[fingerprint]} 的操作语义重复，"
                        "请合并或明确区分"
                    )
                else:
                    action_meanings[fingerprint] = action_key
        pages = [page for page in module.get("pages") or [] if isinstance(page, dict)]
        for page in pages:
            page_key = str(page.get("pageKey") or "")
            label = f"页面 {module_key}/{page_key}"
            semantics = page.get("aiSemantics")
            if semantics is None and require_semantics:
                failures.append(f"{label} 缺少 aiSemantics")
                continue
            if semantics is None:
                continue
            if not isinstance(semantics, dict):
                failures.append(f"{label} aiSemantics 必须是对象")
                continue
            unknown = sorted(set(semantics) - AI_SEMANTICS_FIELDS)
            if unknown:
                failures.append(f"{label} aiSemantics 含未支持字段：{', '.join(unknown)}")
            purpose = semantics.get("purpose")
            if not isinstance(purpose, str) or not purpose.strip():
                failures.append(f"{label} aiSemantics.purpose 不能为空")
            entities = semantics.get("primaryEntities")
            if (
                not isinstance(entities, list)
                or not entities
                or any(not isinstance(item, str) or not STABLE_KEY_RE.fullmatch(item) for item in entities)
                or len(set(entities)) != len(entities)
            ):
                failures.append(f"{label} aiSemantics.primaryEntities 必须是非空且唯一的稳定标识")
            supported = semantics.get("supportedIntents")
            if (
                not isinstance(supported, list)
                or not supported
                or any(not isinstance(item, str) or not item.strip() for item in supported)
            ):
                failures.append(f"{label} aiSemantics.supportedIntents 必须列出可回答的问题")
            for collection, key_name, meaning_name in (
                (semantics.get("fieldSemantics", []), "field", "meaning"),
                (semantics.get("businessTerms", []), "term", "meaning"),
            ):
                if not isinstance(collection, list) or any(
                    not isinstance(item, dict)
                    or set(item) != {key_name, meaning_name}
                    or not str(item.get(key_name) or "").strip()
                    or not str(item.get(meaning_name) or "").strip()
                    for item in collection
                ):
                    failures.append(f"{label} 的 {key_name} 语义声明格式无效")
            related = semantics.get("relatedPages", [])
            if not isinstance(related, list):
                failures.append(f"{label} aiSemantics.relatedPages 必须是列表")
            else:
                seen_related: set[tuple[str, str]] = set()
                for item in related:
                    if not isinstance(item, dict) or set(item) != {"moduleKey", "pageKey", "relationship"}:
                        failures.append(f"{label} 包含格式无效的关联页面")
                        continue
                    target = (str(item.get("moduleKey") or ""), str(item.get("pageKey") or ""))
                    if target == (module_key, page_key) or target in seen_related or target not in page_index:
                        failures.append(f"{label} 关联了自身、重复或不存在的页面 {target[0]}/{target[1]}")
                    if not str(item.get("relationship") or "").strip():
                        failures.append(f"{label} 的关联页面缺少 relationship")
                    seen_related.add(target)
            default_query = str(semantics.get("defaultQueryActionKey") or "")
            page_action_keys = page.get("actionKeys") or []
            if not default_query:
                failures.append(f"{label} 缺少 aiSemantics.defaultQueryActionKey")
            elif (
                default_query not in page_action_keys
                or default_query not in actions
                or actions[default_query].get("operation") != "query"
            ):
                failures.append(f"{label} defaultQueryActionKey 未指向本页 query Action")

        for action_key, action in actions.items():
            if not action.get("aiEnabled"):
                continue
            # v2.4 remains a maintained compatibility contract.  The closed
            # schemas, bounded queries and dataset export shape are new v2.5
            # requirements and must not silently turn a Skill update into a
            # contract migration for an existing v2.4 subsystem.
            if not require_semantics:
                continue
            input_schema = action.get("inputSchema")
            result_schema = action.get("resultSchema")
            if not _closed_object_schema(input_schema):
                failures.append(f"Action {action_key} inputSchema 必须是非空封闭对象 Schema")
            if not _closed_object_schema(result_schema):
                failures.append(f"Action {action_key} resultSchema 必须是非空封闭对象 Schema")
            operation = action.get("operation")
            if operation in {"query", "export"} and isinstance(input_schema, dict):
                limit = (input_schema.get("properties") or {}).get("limit")
                if (
                    not isinstance(limit, dict)
                    or limit.get("type") != "integer"
                    or not isinstance(limit.get("maximum"), int)
                    or limit["maximum"] > 500
                ):
                    failures.append(f"Action {action_key} 必须提供 maximum 不超过 500 的 limit")
            if operation == "export" and isinstance(result_schema, dict):
                properties = result_schema.get("properties") or {}
                required = set(result_schema.get("required") or [])
                if set(properties) != EXPORT_RESULT_FIELDS or required != EXPORT_RESULT_FIELDS:
                    failures.append(f"Action {action_key} 必须返回标准分页数据集字段")
    return failures
