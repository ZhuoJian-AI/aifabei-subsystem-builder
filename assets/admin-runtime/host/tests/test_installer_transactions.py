from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


HOST_ROOT = Path(__file__).resolve().parents[1]


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


def test_signal_after_runtime_patch_restores_the_original_profile_atomically(tmp_path: Path):
    installer = (HOST_ROOT / "install.sh").read_text(encoding="utf-8")
    functions = "\n".join(
        _function(installer, name)
        for name in (
            "restore_runtime_profile",
            "rollback_nginx_guard",
            "remove_transaction_backups",
            "nginx_install_on_exit",
            "nginx_install_on_signal",
        )
    )
    functions = functions.replace(
        "/etc/zhuojian/.runtime.rollback.XXXXXX",
        '"$TEST_ROOT/.runtime.rollback.XXXXXX"',
    )
    script = tmp_path / "runtime-signal.sh"
    script.write_text(
        "#!/bin/sh\nset -eu\n"
        'TEST_ROOT="$1"\nRUNTIME_PROFILE="$1/runtime.json"\n'
        'RUNTIME_BACKUP="$1/runtime.backup"\n'
        'printf old-profile >"$RUNTIME_PROFILE"\n'
        'cp "$RUNTIME_PROFILE" "$RUNTIME_BACKUP"\n'
        "RUNTIME_MODE=600\nRUNTIME_PATCH_STARTED=1\n"
        "DENY_TOUCHED=0\nDENY_HAD_PREVIOUS=0\nDENY_BACKUP=\n"
        "MOVED_STOCK_DEFAULT=0\nNGINX_TRANSACTION_COMMITTED=0\n"
        'DENY_TARGET="$1/deny"\nDISABLED_STOCK_LINK="$1/disabled"\nSTOCK_LINK="$1/stock"\n'
        "nginx() { return 0; }\nsystemctl() { return 0; }\n"
        "install() { while [ \"$#\" -gt 0 ]; do case \"$1\" in -o|-g|-m) shift 2 ;; *) break ;; esac; done; cp \"$1\" \"$2\"; }\n"
        + functions
        + "\ntrap nginx_install_on_exit 0\n"
        + "trap 'nginx_install_on_signal 143' 15\n"
        + '( sleep 0.1; kill -TERM "$$" ) &\n'
        + '"$2" -c \'printf new-profile >"$1"; sleep 0.4\' worker "$RUNTIME_PROFILE"\n'
        + "NGINX_TRANSACTION_COMMITTED=1\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [_shell(), str(script), str(tmp_path), _shell()],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 143, result.stdout + result.stderr
    assert (tmp_path / "runtime.json").read_text(encoding="utf-8") == "old-profile"
    assert not list(tmp_path.glob(".runtime.rollback.*"))
