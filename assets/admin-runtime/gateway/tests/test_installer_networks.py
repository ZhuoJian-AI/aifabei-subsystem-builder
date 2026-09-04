from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


GATEWAY_ROOT = Path(__file__).resolve().parents[1]


def _network_functions() -> str:
    installer = (GATEWAY_ROOT / "install.sh").read_text(encoding="utf-8")
    match = re.search(
        r"(?ms)^list_application_networks\(\) \{.*?^\}\n\n"
        r"^validate_application_network\(\) \{.*?^\}\n\n"
        r"^preflight_application_networks\(\) \{.*?^\}\n\n"
        r"^reattach_application_networks\(\) \{.*?^\}\n",
        installer,
    )
    assert match is not None, "installer network helpers must remain directly testable"
    return match.group(0)


def _run_shell(source: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    shell = shutil.which("sh")
    if shell is None and os.name == "nt":
        git = shutil.which("git")
        if git:
            git_root = Path(git).resolve().parent
            shell = next(
                (
                    str(candidate)
                    for candidate in (git_root / "sh.exe", git_root.parent / "bin" / "sh.exe")
                    if candidate.is_file()
                ),
                None,
            )
    if shell is None:
        pytest.skip("a POSIX sh is required for installer behavior tests")
    script = tmp_path / "network-test.sh"
    script.write_text("#!/bin/sh\nset -eu\n" + source, encoding="utf-8")
    return subprocess.run(
        [shell, str(script)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_network_enumeration_failure_is_not_treated_as_an_empty_success(tmp_path: Path):
    result = _run_shell(
        """
docker() { return 73; }
die() { printf '%s\\n' "$1" >&2; exit 2; }
APPLICATION_NETWORK_ROLE=application-storage
"""
        + _network_functions()
        + "\npreflight_application_networks\n",
        tmp_path,
    )

    assert result.returncode == 2
    assert "failed to enumerate managed application storage networks" in result.stderr


def test_reattach_propagates_network_enumeration_failure(tmp_path: Path):
    result = _run_shell(
        """
docker() { return 73; }
APPLICATION_NETWORK_ROLE=application-storage
"""
        + _network_functions()
        + "\nreattach_application_networks\n",
        tmp_path,
    )

    assert result.returncode != 0


def test_failed_upgrade_recreates_previous_image_with_previous_compose_snapshot():
    installer = (GATEWAY_ROOT / "install.sh").read_text(encoding="utf-8")

    assert 'install -o root -g root -m 0600 "$INSTALL_DIR/compose.yaml" "$PREVIOUS_COMPOSE_FILE"' in installer
    baseline = re.search(
        r"(?ms)^restore_compose_baseline\(\) \{.*?^\}",
        installer,
    )
    assert baseline is not None
    assert 'mv -f -- "$restored_compose_tmp" "$INSTALL_DIR/compose.yaml"' in baseline.group(0)
    restore = re.search(
        r"(?ms)^restore_previous\(\) \{.*?^\}",
        installer,
    )
    assert restore is not None
    assert "restore_compose_baseline || return 1" in restore.group(0)
    assert "previous_compose up -d --no-deps --force-recreate" in restore.group(0)
    assert "current_image_id=" not in restore.group(0)
    assert "compose up -d --no-deps --force-recreate" not in restore.group(0).replace(
        "previous_compose up -d --no-deps --force-recreate", ""
    )


def test_any_early_exit_restores_the_persistent_compose_baseline():
    installer = (GATEWAY_ROOT / "install.sh").read_text(encoding="utf-8")

    exit_handler = re.search(
        r"(?ms)^rollback_install_on_exit\(\) \{.*?^\}",
        installer,
    )
    signal_handler = re.search(
        r"(?ms)^rollback_install_on_signal\(\) \{.*?^\}",
        installer,
    )
    assert exit_handler is not None
    assert signal_handler is not None
    assert 'if [ "$INSTALL_COMPLETED" -ne 1 ]' in exit_handler.group(0)
    assert "rollback_interrupted_install" in exit_handler.group(0)
    assert "rollback_interrupted_install" in signal_handler.group(0)
    assert "trap rollback_install_on_exit 0" in installer
    assert "trap 'rollback_install_on_signal 130' 2" in installer
    assert "trap 'rollback_install_on_signal 143' 15" in installer

    build_position = installer.index('compose build "$COMPOSE_SERVICE"')
    completion_position = installer.rindex("INSTALL_COMPLETED=1")
    assert build_position < completion_position


def test_interrupt_after_container_switch_restores_the_previous_runtime():
    installer = (GATEWAY_ROOT / "install.sh").read_text(encoding="utf-8")
    rollback = re.search(
        r"(?ms)^rollback_interrupted_install\(\) \{.*?^\}",
        installer,
    )
    assert rollback is not None
    assert 'if [ "$CONTAINER_SWITCH_STARTED" -eq 1 ]' in rollback.group(0)
    assert "restore_previous" in rollback.group(0)
    switch_position = installer.index("CONTAINER_SWITCH_STARTED=1")
    compose_up_position = installer.index('compose up -d --no-deps "$COMPOSE_SERVICE"')
    migration_position = installer.index("MIGRATION_STARTED=1")
    assert switch_position < compose_up_position < migration_position
    assert "gateway_current_healthy" in rollback.group(0)


def test_interrupt_after_build_restores_the_previous_stable_image_tag():
    installer = (GATEWAY_ROOT / "install.sh").read_text(encoding="utf-8")
    build_started = installer.index("IMAGE_BUILD_STARTED=1")
    build = installer.index('compose build "$COMPOSE_SERVICE"')
    switch = installer.index("CONTAINER_SWITCH_STARTED=1")
    completed = installer.rindex("INSTALL_COMPLETED=1")

    assert build_started < build < switch < completed
    restore_tag = re.search(
        r"(?ms)^restore_previous_image_tag\(\) \{.*?^\}", installer
    )
    assert restore_tag is not None
    assert 'docker image tag "$previous_tag_image_id" "$IMAGE_NAME"' in restore_tag.group(0)
    rollback = re.search(
        r"(?ms)^rollback_interrupted_install\(\) \{.*?^\}", installer
    )
    assert rollback is not None
    assert "restore_previous_image_tag || true" in rollback.group(0)


def test_migration_boundary_never_rolls_back_to_an_incompatible_old_mount():
    installer = (GATEWAY_ROOT / "install.sh").read_text(encoding="utf-8")
    rollback = re.search(
        r"(?ms)^rollback_interrupted_install\(\) \{.*?^\}", installer
    )
    assert rollback is not None
    body = rollback.group(0)
    migration_branch = body[
        body.index('if [ "$MIGRATION_STARTED" -eq 1 ]'):
        body.index('if [ "$CONTAINER_SWITCH_STARTED" -eq 1 ]')
    ]
    assert "return 0" in migration_branch
    assert "restore_previous" not in migration_branch
    assert "restore_compose_baseline" not in migration_branch
    assert "explicit forward-only transaction boundary" in migration_branch
