import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("validate_endpoint", ROOT / "scripts" / "validate_endpoint.py")
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def action(operation: str, *, schema: dict, confirmation: bool = False, result_schema: dict | None = None) -> dict:
    return {
        "description": "执行明确的业务操作并返回结果。",
        "operation": operation,
        "aiEnabled": True,
        "requiresConfirmation": confirmation,
        "inputSchema": schema,
        "resultSchema": result_schema or {"type": "object"},
    }


class ActionContractValidationTests(unittest.TestCase):
    @staticmethod
    def export_input() -> dict:
        return {
            "type": "object", "additionalProperties": False,
            "properties": {
                "limit": {"type": "integer", "maximum": 500, "description": "单页数量"},
                "snapshotId": {"type": "string", "description": "快照标识"},
                "nextCursor": {"type": "string", "description": "下一页游标"},
            },
        }

    @staticmethod
    def export_result() -> dict:
        properties = {
            "snapshotId": {"type": "string", "description": "快照标识"},
            "snapshotAt": {"type": "string", "format": "date-time", "description": "快照时间"},
            "columns": {"type": "array", "description": "列", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["key", "label", "type"],
                "properties": {
                    "key": {"type": "string", "description": "字段键"},
                    "label": {"type": "string", "description": "显示名"},
                    "type": {"type": "string", "enum": ["string", "number"], "description": "类型"},
                },
            }},
            "rows": {"type": "array", "description": "行", "items": {
                "type": "object",
                "additionalProperties": {"type": ["string", "number", "boolean", "null"]},
            }},
            "rowCount": {"type": "integer", "description": "总数"},
            "nextCursor": {"type": ["string", "null"], "description": "游标"},
        }
        return {
            "type": "object", "additionalProperties": False,
            "required": list(properties), "properties": properties,
        }

    def test_ai_mutation_rejects_empty_schema(self) -> None:
        with self.assertRaisesRegex(SystemExit, "真实业务字段"):
            MODULE.validate_action_contract(
                action("update", schema={"type": "object", "properties": {}}), "action"
            )

    def test_ai_mutation_rejects_open_schema(self) -> None:
        with self.assertRaisesRegex(SystemExit, "additionalProperties"):
            MODULE.validate_action_contract(
                action(
                    "update",
                    schema={
                        "type": "object",
                        "properties": {"id": {"type": "integer", "description": "记录 ID"}},
                        "required": ["id"],
                    },
                ),
                "action",
            )

    def test_delete_and_approve_require_confirmation(self) -> None:
        schema = {
            "type": "object",
            "properties": {"id": {"type": "integer", "description": "记录 ID"}},
            "required": ["id"],
            "additionalProperties": False,
        }
        for operation in ("delete", "approve"):
            with self.subTest(operation=operation), self.assertRaisesRegex(SystemExit, "必须 requiresConfirmation"):
                MODULE.validate_action_contract(action(operation, schema=schema), "action")

    def test_closed_targeted_update_passes(self) -> None:
        MODULE.validate_action_contract(
            action(
                "update",
                schema={
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer", "description": "记录 ID"},
                        "changes": {"type": "object", "description": "允许修改的字段"},
                    },
                    "required": ["id", "changes"],
                    "additionalProperties": False,
                },
            ),
            "action",
        )

    def test_export_requires_standard_dataset(self) -> None:
        with self.assertRaisesRegex(SystemExit, "标准分页数据集"):
            MODULE.validate_action_contract(
                action("export", schema=self.export_input(), result_schema={"type": "object"}),
                "action",
            )

    def test_export_rejects_nested_server_path(self) -> None:
        result = self.export_result()
        result["properties"]["rows"]["items"] = {
            "type": "object",
            "properties": {"backupPath": {"type": "string", "description": "备份位置"}},
        }
        with self.assertRaisesRegex(SystemExit, "服务器路径"):
            MODULE.validate_action_contract(
                action("export", schema=self.export_input(), result_schema=result), "action",
            )

    def test_export_rejects_unbounded_or_nested_row_values(self) -> None:
        result = self.export_result()
        result["properties"]["rows"]["items"] = {"type": "object"}
        with self.assertRaisesRegex(SystemExit, "标量字段白名单"):
            MODULE.validate_action_contract(
                action("export", schema=self.export_input(), result_schema=result), "action",
            )
        result["properties"]["rows"]["items"] = {
            "type": "object",
            "additionalProperties": {"type": "object"},
        }
        with self.assertRaisesRegex(SystemExit, "只能声明"):
            MODULE.validate_action_contract(
                action("export", schema=self.export_input(), result_schema=result), "action",
            )

    def test_query_rejects_nested_database_path(self) -> None:
        result = {
            "type": "object",
            "properties": {
                "metadata": {
                    "type": "object",
                    "properties": {"dbPath": {"type": "string"}},
                },
            },
        }
        with self.assertRaisesRegex(SystemExit, "服务器路径"):
            MODULE.validate_action_contract(
                action(
                    "query",
                    schema={"type": "object", "properties": {}},
                    result_schema=result,
                ),
                "action",
            )

    def test_standard_export_dataset_passes(self) -> None:
        MODULE.validate_action_contract(
            action("export", schema=self.export_input(), result_schema=self.export_result()), "action",
        )

    def test_v24_maintenance_keeps_legacy_export_shape(self) -> None:
        MODULE.validate_action_contract(
            action(
                "export",
                schema={"type": "object", "properties": {}},
                result_schema={
                    "type": "object",
                    "properties": {"serverPath": {"type": "string"}},
                },
            ),
            "action",
            "2.4",
        )


if __name__ == "__main__":
    unittest.main()
