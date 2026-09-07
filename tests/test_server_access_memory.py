from __future__ import annotations

import json
import subprocess
from pathlib import Path

import server_access_memory
import ssh_via_http_proxy


def _fake_keygen(command: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
    assert command[0] == "ssh-keygen"
    private_key = Path(command[command.index("-f") + 1])
    private_key.write_text("PRIVATE TEST KEY\n", encoding="utf-8")
    private_key.with_suffix(".pub").write_text(
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestKey aifabei-test\n",
        encoding="utf-8",
    )
    return subprocess.CompletedProcess(command, 0)


def test_access_id_is_stable_and_separates_hosts() -> None:
    assert server_access_memory.access_id("203.0.113.10", "root") == server_access_memory.access_id(
        "203.0.113.10", "root"
    )
    assert server_access_memory.access_id("203.0.113.10", "root") != server_access_memory.access_id(
        "203.0.113.11", "root"
    )


def test_prepare_generates_dedicated_key_without_password(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(server_access_memory.subprocess, "run", _fake_keygen)

    private_key, public_key = server_access_memory.ensure_keypair(
        tmp_path, "203.0.113.10", "root"
    )

    assert private_key.is_file()
    assert public_key.startswith("ssh-ed25519 ")
    assert "password" not in private_key.read_text(encoding="utf-8").lower()


def test_verify_writes_profile_only_after_key_login_succeeds(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[0] == "ssh-keygen":
            return _fake_keygen(command, **kwargs)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(server_access_memory.subprocess, "run", fake_run)

    profile = server_access_memory.verify_and_remember(
        tmp_path,
        "203.0.113.10",
        "root",
        443,
        proxy_url="http://127.0.0.1:7897",
        requires_vpn=True,
    )
    loaded = server_access_memory.load_profile(tmp_path, "203.0.113.10", "root")
    _private_key, _public_key, profile_path = server_access_memory.access_paths(
        tmp_path, "203.0.113.10", "root"
    )
    stored = profile_path.read_text(encoding="utf-8")

    assert loaded == profile
    assert "password" not in stored.lower()
    assert profile["requiresVpn"] is True
    assert profile["port"] == 443
    assert calls[-1][-1] == "true"
    assert "PasswordAuthentication=no" in calls[-1]
    assert "BatchMode=yes" in calls[-1]


def test_failed_key_verification_does_not_create_profile(
    tmp_path: Path, monkeypatch
) -> None:
    def fake_run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        if command[0] == "ssh-keygen":
            return _fake_keygen(command, **kwargs)
        return subprocess.CompletedProcess(command, 255)

    monkeypatch.setattr(server_access_memory.subprocess, "run", fake_run)

    try:
        server_access_memory.verify_and_remember(
            tmp_path,
            "203.0.113.10",
            "root",
            22,
            proxy_url=None,
            requires_vpn=False,
        )
    except server_access_memory.AccessMemoryError as exc:
        assert "未写入长期访问档案" in str(exc)
    else:
        raise AssertionError("verification failure must be rejected")

    _private_key, _public_key, profile_path = server_access_memory.access_paths(
        tmp_path, "203.0.113.10", "root"
    )
    assert not profile_path.exists()


def test_missing_profile_has_stable_noninteractive_status(
    tmp_path: Path, capsys
) -> None:
    exit_code = server_access_memory.main([
        "--store-dir",
        str(tmp_path),
        "resolve",
        "--host",
        "203.0.113.10",
    ])

    assert exit_code == 2
    assert capsys.readouterr().out.startswith("SERVER_ACCESS_MISSING")


def test_proxy_wrapper_switches_to_key_only_authentication(tmp_path: Path) -> None:
    identity_file = tmp_path / "identity"
    identity_file.write_text("test", encoding="utf-8")

    command = ssh_via_http_proxy.build_ssh_command(
        proxy_url="http://127.0.0.1:7897",
        host="203.0.113.10",
        port=443,
        user="root",
        connect_timeout=12,
        identity_file=identity_file,
        batch_mode=True,
        remote_command=["true"],
    )

    assert "PasswordAuthentication=no" in command
    assert "KbdInteractiveAuthentication=no" in command
    assert "PubkeyAuthentication=yes" in command
    assert "IdentitiesOnly=yes" in command
    assert f"IdentityFile={identity_file}" in command
    assert "BatchMode=yes" in command
    assert command[-1] == "true"


def test_proxy_wrapper_keeps_first_login_on_password_only() -> None:
    command = ssh_via_http_proxy.build_ssh_command(
        proxy_url="http://127.0.0.1:7897",
        host="203.0.113.10",
        port=443,
        user="root",
        connect_timeout=12,
    )

    assert "PasswordAuthentication=yes" in command
    assert "KbdInteractiveAuthentication=yes" in command
    assert "PubkeyAuthentication=no" in command
    assert not any(item.startswith("IdentityFile=") for item in command)


def test_safe_profile_output_omits_proxy_and_secret_fields() -> None:
    profile = {
        "schemaVersion": 1,
        "accessId": "abc",
        "host": "203.0.113.10",
        "user": "root",
        "port": 22,
        "identityFile": "/tmp/key",
        "route": "direct-or-transparent-vpn",
        "requiresVpn": False,
        "proxyUrl": None,
        "verifiedAt": "2026-09-07T00:00:00Z",
    }

    visible = json.dumps(server_access_memory._safe_profile(profile))

    assert "proxyUrl" not in visible
    assert "password" not in visible.lower()
