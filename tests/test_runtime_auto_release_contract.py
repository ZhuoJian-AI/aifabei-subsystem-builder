from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_runtime_release_does_not_require_per_version_admin_review() -> None:
    skill = _read("SKILL.md")
    deployment = _read("references/direct-ecs-deployment.md")
    contract = _read("references/platform-contract.md")

    assert "不等待管理员逐版本审核" in skill
    assert "新系统先只自动授权“系统研发者”" in skill
    assert "只有返回 `healthy` 才表示候选已验证并自动成为生效版本" in deployment
    assert "新系统返回 `pending_review` 属于正常结果" not in deployment
    assert "候选版本" in contract and "生效版本" in contract
    assert "latest-wins" in contract


def test_managed_developer_inheritance_and_admin_stops_are_explicit() -> None:
    contract = _read("references/platform-contract.md")
    aggregation = _read("references/native-aggregation.md")

    assert "zj-runtime-developer" in contract
    assert "不能改名、删除、停用或修改权限" in contract
    assert "既有业务角色只在该应用现有授权上限内继承" in contract
    assert "事件声明只更新目录，不能自动创建投递路由" in contract
    assert "管理员明确移除的资源、停用的系统或 Action 不得被后续同步恢复" in aggregation


def test_runtime_publisher_accepts_only_healthy_registration() -> None:
    publisher = _read("scripts/publish_subsystem.py")

    assert 'if result.get("status") != "healthy":' in publisher
    assert 'if result.get("status") not in {"healthy", "pending_review"}:' not in publisher
