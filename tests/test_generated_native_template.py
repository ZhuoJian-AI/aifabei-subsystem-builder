from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run_checked(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def test_scaffold_defaults_to_the_canonical_alphabet_identity(tmp_path: Path):
    project = tmp_path / "default-identity"
    run_checked(
        [
            sys.executable,
            str(ROOT / "scripts" / "scaffold_subsystem.py"),
            "--output",
            str(project),
            "--application-slug",
            "identity-probe",
            "--application-name",
            "Identity probe",
            "--module-key",
            "identity.main",
            "--module-name",
            "Identity",
            "--department",
            "ops:Operations:owner",
        ],
        cwd=ROOT,
    )

    manifest = json.loads((project / "subsystem.json").read_text(encoding="utf-8"))
    page_html = (project / "static" / "index.html").read_text(encoding="utf-8")
    app = (project / "app.py").read_text(encoding="utf-8")
    assert manifest["enterprise"]["key"] == "alphabet"
    assert manifest["contractRevision"] == "2.5"
    manifest_page = manifest["modules"][0]["pages"][0]
    assert manifest_page["aiSemantics"]["defaultQueryActionKey"] == f"{manifest_page['pageKey'].rsplit('.', 1)[0]}.query"
    assert manifest_page["aiSemantics"]["primaryEntities"]
    assert all(
        action["inputSchema"].get("additionalProperties") is False
        and action["resultSchema"].get("additionalProperties") is False
        for action in manifest["modules"][0]["actions"]
    )
    assert "enterprise_key:'alphabet'" in page_html
    assert "data-zhuojian-embedded" in page_html
    assert page_html.index("data-zhuojian-embedded") < page_html.index("<style>")
    assert "100dvh" in page_html
    assert "viewport-fit=cover" in page_html
    assert "safe-area-inset-bottom" in page_html
    assert 'html[data-zhuojian-embedded="true"] .bar' in page_html
    assert 'html[data-zhuojian-embedded="true"] .module-nav' in page_html
    assert "display:none!important" in page_html
    assert ".table-scroll" in page_html
    assert "@media(max-width:768px)" in page_html
    assert "@media(prefers-reduced-motion:reduce)" in page_html
    assert "frame-ancestors 'self' " in app


def test_scaffolded_native_system_runs_its_security_and_recovery_suite(tmp_path: Path):
    project = tmp_path / "generated-native-system"
    run_checked(
        [
            sys.executable,
            str(ROOT / "scripts" / "scaffold_subsystem.py"),
            "--output",
            str(project),
            "--company-slug",
            "alphabet",
            "--company-name",
            "Alphabet",
            "--application-slug",
            "ci-contract-probe",
            "--application-name",
            "CI contract probe",
            "--module-key",
            "probe.main",
            "--module-name",
            "Probe",
            "--department",
            "ops:Operations:owner",
        ],
        cwd=ROOT,
    )
    run_checked([sys.executable, "-m", "pytest", "-q"], cwd=project)
    run_checked(
        [
            sys.executable,
            str(ROOT / "scripts" / "validate_source.py"),
            "--path",
            str(project),
            "--requires-file-storage",
            "--requires-object-storage",
        ],
        cwd=ROOT,
    )
