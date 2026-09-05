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


def action(operation: str, *, schema: dict, confirmation: bool = False) -> dict:
    return {
        "description": "执行明确的业务操作并返回结果。",
        "operation": operation,
        "aiEnabled": True,
        "requiresConfirmation": confirmation,
        "inputSchema": schema,
        "resultSchema": {"type": "object"},
    }


class ActionContractValidationTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
