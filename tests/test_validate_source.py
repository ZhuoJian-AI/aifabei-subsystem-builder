from __future__ import annotations

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
if (window.parent !== window) document.documentElement.setAttribute("data-zhuojian-embedded", "true")
window.parent.postMessage(message, "https://saas.example.com")
"""


def run_validator(project: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "validate_source.py"),
            "--path",
            str(project),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def write_valid_project(tmp_path: Path) -> Path:
    project = tmp_path / "business-system"
    project.mkdir()
    (project / "app.py").write_text(VALID_SOURCE, encoding="utf-8")
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
