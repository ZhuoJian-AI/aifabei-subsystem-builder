from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


GATEWAY_ROOT = Path(__file__).resolve().parents[1]


def _shell() -> str:
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
        pytest.skip("a POSIX sh is required for installer transaction tests")
    return shell


def _function(source: str, name: str) -> str:
    match = re.search(rf"(?ms)^{re.escape(name)}\(\) \{{.*?^\}}\n", source)
    assert match is not None, f"missing shell function {name}"
    return match.group(0)


def test_signal_after_successful_build_restores_the_old_stable_tag(tmp_path: Path):
    installer = (GATEWAY_ROOT / "install.sh").read_text(encoding="utf-8")
    functions = "\n".join(
        _function(installer, name)
        for name in (
            "restore_previous_image_tag",
            "rollback_interrupted_install",
            "rollback_install_on_exit",
            "rollback_install_on_signal",
        )
    )
    script = tmp_path / "image-tag-signal.sh"
    script.write_text(
        "#!/bin/sh\nset -eu\n"
        'TAG_STATE="$1/tag"\n'
        "INSTALL_COMPLETED=0\nIMAGE_BUILD_STARTED=1\n"
        "CONTAINER_SWITCH_STARTED=0\nCONTAINER_ROLLBACK_DONE=0\nMIGRATION_STARTED=0\n"
        "previous_tag_image_id=old-image-id\nIMAGE_NAME=zhuojian/storage-gateway:local\n"
        "ROLLBACK_CONFIG_DIR=\nPREVIOUS_COMPOSE_FILE=\n"
        'docker() {\n'
        '  if [ "$1 $2" = "image inspect" ]; then return 0; fi\n'
        '  if [ "$1 $2" = "image tag" ]; then printf %s "$3" >"$TAG_STATE"; return 0; fi\n'
        "  return 1\n}\n"
        "restore_previous() { return 1; }\nrestore_compose_baseline() { return 0; }\n"
        "discard_rollback_config() { return 0; }\n"
        "gateway_current_healthy() { return 1; }\npython3() { return 1; }\n"
        + functions
        + "\ntrap rollback_install_on_exit 0\n"
        + "trap 'rollback_install_on_signal 143' 15\n"
        + 'printf new-image-id >"$TAG_STATE"\n'
        + '( sleep 0.1; kill -TERM "$$" ) &\n'
        + "sleep 0.4\nINSTALL_COMPLETED=1\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [_shell(), str(script), str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 143, result.stdout + result.stderr
    assert (tmp_path / "tag").read_text(encoding="utf-8") == "old-image-id"
