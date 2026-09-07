from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VALID_SOURCE = """
pageKeys actionKeys pageAccess roleIds effectiveDataScope
ZHUOJIAN_MANIFEST_ACCESS_TOKEN
ZHUOJIAN_SSO_EXCHANGE_TOKEN
ZHUOJIAN_ACTION_SIGNING_SECRET
ZHUOJIAN_EVENT_SIGNING_SECRET
/api/v1/subsystem-sso/exchange
launch_nonce
zhuojian:ready
zhuojian:context
zhuojian:refresh
zhuojian:refresh-result
module_key page_key request_id event.origin event.source deferred
if (window.parent !== window) document.documentElement.setAttribute("data-zhuojian-embedded", "true")
window.parent.postMessage(message, "https://saas.example.com")
"""
VALID_SOURCE_V24 = """
pageKeys actionKeys pageAccess roleIds effectiveDataScope
ZHUOJIAN_INTEGRATION_SECRET
zhuojian:context
window.parent.postMessage(message, "https://saas.example.com")
"""


def run_validator(project: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "validate_source.py"),
            "--path",
            str(project),
            *extra,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def write_valid_project(tmp_path: Path, contract_revision: str = "2.5") -> Path:
    project = tmp_path / "business-system"
    project.mkdir()
    (project / "app.py").write_text(
        VALID_SOURCE if contract_revision == "2.5" else VALID_SOURCE_V24,
        encoding="utf-8",
    )
    (project / "subsystem.json").write_text(
        json.dumps({
            "protocol": "zhuojian-subsystem",
            "version": 2,
            "contractRevision": contract_revision,
            "modules": [{
                "moduleKey": "orders",
                "pages": [{
                    "pageKey": "orders.list",
                    "queryActionKey": "orders.query",
                    "actionKeys": ["orders.query"],
                    **({
                        "aiSemantics": {
                            "purpose": "查询订单",
                            "primaryEntities": ["order"],
                            "fieldSemantics": [],
                            "supportedIntents": ["查询订单"],
                            "relatedPages": [],
                            "businessTerms": [],
                            "defaultQueryActionKey": "orders.query",
                        },
                    } if contract_revision == "2.5" else {}),
                }],
                "actions": [{
                    "actionKey": "orders.query",
                    "name": "查询订单",
                    "operation": "query",
                    "aiEnabled": True,
                    "description": "按当前角色的数据范围查询订单列表。",
                    "inputSchema": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "limit": {"type": "integer", "maximum": 500},
                        },
                    },
                    "resultSchema": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"items": {"type": "array"}},
                    },
                }],
            }],
        }),
        encoding="utf-8",
    )
    return project


@pytest.mark.parametrize(
    ("filename", "forbidden_source", "expected_message"),
    [
        (".env", "DEEPSEEK_API_KEY=must-not-live-here", "模型供应商凭证"),
        ("ai.py", "from openai import AsyncOpenAI", "模型供应商 SDK"),
        ("requirements.txt", "anthropic>=0.34", "模型供应商 SDK"),
        ("package.json", '{"dependencies":{"openai":"^4.0.0"}}', "模型供应商 SDK"),
        (
            "client.ts",
            'fetch("https://api.deepseek.com/chat/completions")',
            "禁止直连模型供应商 URL",
        ),
        (
            "model.yaml",
            "base_url: https://ws-example.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
            "禁止直连模型供应商 URL",
        ),
        (
            "legacy.py",
            'secret = os.getenv("ZHUOJIAN_INTEGRATION_SECRET")',
            "旧接入密钥",
        ),
        (
            "bridge.js",
            'window.parent.postMessage({type: "zhuojian:context"}, "*")',
            "postMessage targetOrigin",
        ),
    ],
)
def test_validator_rejects_forbidden_runtime_integration(
    tmp_path: Path,
    filename: str,
    forbidden_source: str,
    expected_message: str,
):
    project = write_valid_project(tmp_path)
    (project / filename).write_text(forbidden_source, encoding="utf-8")

    result = run_validator(project)

    assert result.returncode == 1
    assert expected_message in result.stdout


@pytest.mark.parametrize(
    "credential",
    [
        "ZHUOJIAN_MANIFEST_ACCESS_TOKEN",
        "ZHUOJIAN_SSO_EXCHANGE_TOKEN",
        "ZHUOJIAN_ACTION_SIGNING_SECRET",
        "ZHUOJIAN_EVENT_SIGNING_SECRET",
    ],
)
def test_validator_rejects_each_missing_v25_credential(
    tmp_path: Path,
    credential: str,
):
    project = write_valid_project(tmp_path)
    source = (project / "app.py").read_text(encoding="utf-8")
    (project / "app.py").write_text(source.replace(credential, ""), encoding="utf-8")

    result = run_validator(project)

    assert result.returncode == 1
    assert "未实现 v2.5 分用途凭证" in result.stdout
    assert credential in result.stdout


def test_validator_rejects_missing_native_embedded_mode(tmp_path: Path):
    project = write_valid_project(tmp_path)
    source = (project / "app.py").read_text(encoding="utf-8")
    (project / "app.py").write_text(
        source.replace("data-zhuojian-embedded", "missing-embedded-marker"),
        encoding="utf-8",
    )

    result = run_validator(project)

    assert result.returncode == 1
    assert "未实现 iframe 原生嵌入模式" in result.stdout


def test_validator_rejects_missing_bridge_ready(tmp_path: Path):
    project = write_valid_project(tmp_path)
    source = (project / "app.py").read_text(encoding="utf-8")
    (project / "app.py").write_text(
        source.replace("zhuojian:ready", "missing-bridge-ready"),
        encoding="utf-8",
    )

    result = run_validator(project)

    assert result.returncode == 1
    assert "zhuojian:ready Bridge 就绪消息" in result.stdout


def test_validator_rejects_context_without_launch_binding(tmp_path: Path):
    project = write_valid_project(tmp_path)
    source = (project / "app.py").read_text(encoding="utf-8")
    (project / "app.py").write_text(
        source.replace("launch_nonce\nzhuojian:ready\nzhuojian:context", "zhuojian:ready\nzhuojian:context"),
        encoding="utf-8",
    )
    (project / "sso.py").write_text("launch_nonce", encoding="utf-8")

    result = run_validator(project)

    assert result.returncode == 1
    assert "zhuojian:context 未携带本次启动的 launch_nonce" in result.stdout


@pytest.mark.parametrize(
    ("marker", "expected_message"),
    [
        ("zhuojian:refresh\n", "zhuojian:refresh 当前模块静默刷新处理"),
        ("zhuojian:refresh-result\n", "zhuojian:refresh-result 静默刷新结果"),
        ("event.origin", "静默刷新未同时校验来源"),
        ("deferred", "存在未保存编辑时返回 deferred"),
    ],
)
def test_validator_rejects_incomplete_silent_refresh_contract(
    tmp_path: Path,
    marker: str,
    expected_message: str,
):
    project = write_valid_project(tmp_path)
    source = (project / "app.py").read_text(encoding="utf-8")
    (project / "app.py").write_text(source.replace(marker, ""), encoding="utf-8")

    result = run_validator(project)

    assert result.returncode == 1
    assert expected_message in result.stdout


def test_validator_rejects_page_reload_in_refresh_handler(tmp_path: Path):
    project = write_valid_project(tmp_path)
    with (project / "app.py").open("a", encoding="utf-8") as source:
        source.write("\nzhuojian:refresh\nwindow.location.reload()\n")

    result = run_validator(project)

    assert result.returncode == 1
    assert "禁止调用 location.reload" in result.stdout


def test_validator_rejects_nested_iframe_without_same_origin_ancestor(tmp_path: Path):
    project = write_valid_project(tmp_path)
    (project / "index.html").write_text(
        '<iframe src="/detail"></iframe>', encoding="utf-8"
    )
    with (project / "app.py").open("a", encoding="utf-8") as source:
        source.write(
            '\nContent-Security-Policy: frame-ancestors https://saas.example.com\n'
        )

    result = run_validator(project)

    assert result.returncode == 1
    assert "frame-ancestors 未包含 'self'" in result.stdout


def test_validator_accepts_nested_iframe_with_same_origin_ancestor(tmp_path: Path):
    project = write_valid_project(tmp_path)
    (project / "index.html").write_text(
        '<iframe src="/detail"></iframe>', encoding="utf-8"
    )
    with (project / "app.py").open("a", encoding="utf-8") as source:
        source.write(
            "\nContent-Security-Policy: frame-ancestors 'self' https://saas.example.com\n"
        )

    result = run_validator(project)

    assert result.returncode == 0, result.stdout + result.stderr


def test_validator_ignores_documentation_and_test_fixtures(tmp_path: Path):
    project = write_valid_project(tmp_path)
    docs = project / "docs"
    tests = project / "tests"
    docs.mkdir()
    tests.mkdir()
    forbidden_example = """
from openai import OpenAI
OPENAI_API_KEY = "fixture-only"
MODEL_URL = "https://api.openai.com/v1"
window.parent.postMessage(message, "*")
ZHUOJIAN_INTEGRATION_SECRET
"""
    (docs / "migration_example.py").write_text(forbidden_example, encoding="utf-8")
    (tests / "test_legacy_fixture.py").write_text(forbidden_example, encoding="utf-8")

    result = run_validator(project)

    assert result.returncode == 0, result.stdout + result.stderr


def test_v24_maintenance_does_not_require_v25_credentials_or_bridge(tmp_path: Path):
    project = write_valid_project(tmp_path, "2.4")
    before = (project / "subsystem.json").read_bytes()

    result = run_validator(project)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "contractRevision=2.4" in result.stdout
    assert (project / "subsystem.json").read_bytes() == before


def test_v25_requires_page_semantics(tmp_path: Path):
    project = write_valid_project(tmp_path)
    manifest = json.loads((project / "subsystem.json").read_text(encoding="utf-8"))
    del manifest["modules"][0]["pages"][0]["aiSemantics"]
    (project / "subsystem.json").write_text(json.dumps(manifest), encoding="utf-8")

    result = run_validator(project)

    assert result.returncode == 1
    assert "缺少 aiSemantics" in result.stdout


def test_v25_rejects_unknown_related_page_and_unbounded_query(tmp_path: Path):
    project = write_valid_project(tmp_path)
    manifest = json.loads((project / "subsystem.json").read_text(encoding="utf-8"))
    page = manifest["modules"][0]["pages"][0]
    page["aiSemantics"]["relatedPages"] = [{
        "moduleKey": "missing",
        "pageKey": "missing.main",
        "relationship": "不存在的页面",
    }]
    del manifest["modules"][0]["actions"][0]["inputSchema"]["properties"]["limit"]["maximum"]
    (project / "subsystem.json").write_text(json.dumps(manifest), encoding="utf-8")

    result = run_validator(project)

    assert result.returncode == 1
    assert "不存在的页面" in result.stdout
    assert "maximum 不超过 500" in result.stdout


def test_v25_rejects_duplicate_ai_action_meaning(tmp_path: Path):
    project = write_valid_project(tmp_path)
    manifest = json.loads((project / "subsystem.json").read_text(encoding="utf-8"))
    module = manifest["modules"][0]
    duplicate = dict(module["actions"][0])
    duplicate["actionKey"] = "sample.query_duplicate"
    module["actions"].append(duplicate)
    module["pages"][0]["actionKeys"].append(duplicate["actionKey"])
    (project / "subsystem.json").write_text(json.dumps(manifest), encoding="utf-8")

    result = run_validator(project)

    assert result.returncode == 1
    assert "操作语义重复" in result.stdout


def test_unknown_contract_revision_is_rejected(tmp_path: Path):
    project = write_valid_project(tmp_path)
    manifest = json.loads((project / "subsystem.json").read_text(encoding="utf-8"))
    manifest["contractRevision"] = "2.6"
    (project / "subsystem.json").write_text(json.dumps(manifest), encoding="utf-8")

    result = run_validator(project)

    assert result.returncode == 1
    assert "不支持的 contractRevision" in result.stdout


def test_explicit_revision_cannot_override_subsystem_json(tmp_path: Path):
    project = write_valid_project(tmp_path, "2.4")

    result = run_validator(project, "--contract-revision", "2.5")

    assert result.returncode == 1
    assert "普通维护不得只改版本号" in result.stdout


def test_missing_manifest_requires_explicit_revision(tmp_path: Path):
    project = tmp_path / "manifestless"
    project.mkdir()
    (project / "app.py").write_text(VALID_SOURCE_V24, encoding="utf-8")

    result = run_validator(project)
    explicit = run_validator(project, "--contract-revision", "2.4")

    assert result.returncode == 1
    assert "无法判断现有系统的接入契约" in result.stdout
    assert explicit.returncode == 0, explicit.stdout + explicit.stderr
