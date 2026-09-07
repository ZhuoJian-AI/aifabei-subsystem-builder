from __future__ import annotations

import json
from io import BytesIO
from unittest import mock
from urllib.error import HTTPError

import pytest

from scripts import publish_subsystem


def runtime_release(commit: str = "a" * 40) -> dict:
    return {
        "managedBy": "zhuojian-runtime-admin/v1",
        "applicationSlug": "orders",
        "status": "awaiting_platform_registration",
        "current": {
            "commit": commit,
            "image": f"zhuojian/aifabei/orders:{commit}",
        },
    }


def test_http_error_body_is_never_relayed_to_logs(monkeypatch):
    canary = "zjac_canary-secret-that-must-never-appear"
    error = HTTPError(
        "https://platform.example/register",
        400,
        "bad request",
        {},
        BytesIO(f'{{"echo":"{canary}"}}'.encode()),
    )
    opener = mock.Mock()
    opener.open.side_effect = error
    monkeypatch.setattr(publish_subsystem, "build_opener", lambda *_args: opener)

    with pytest.raises(SystemExit) as raised:
        publish_subsystem.call_json(
            "https://platform.example/register",
            "runtime-secret",
            "POST",
            {"credentials": {"action_signing_secret": canary}},
        )

    assert "HTTP 400" in str(raised.value)
    assert canary not in str(raised.value)


def test_runtime_release_must_match_the_managed_application_and_image(tmp_path):
    path = tmp_path / "release.json"
    path.write_text(json.dumps(runtime_release()), encoding="utf-8")
    path.chmod(0o640)

    loaded = publish_subsystem.load_runtime_release(path, "orders")
    assert loaded["current"]["commit"] == "a" * 40

    damaged = runtime_release()
    damaged["current"]["image"] = "zhuojian/orders:latest"
    path.write_text(json.dumps(damaged), encoding="utf-8")
    path.chmod(0o640)
    with pytest.raises(SystemExit, match="可核对的 commit SHA"):
        publish_subsystem.load_runtime_release(path, "orders")


def test_platform_verification_is_persisted_into_the_runtime_release(tmp_path):
    path = tmp_path / "release.json"
    release = runtime_release()
    path.write_text(json.dumps(release), encoding="utf-8")
    path.chmod(0o640)

    publish_subsystem.update_runtime_release_status(
        path,
        release,
        status="healthy",
        platform_release={
            "id": "release-1",
            "requested_commit": "a" * 40,
            "last_success_commit": "a" * 40,
        },
        contract_revision="2.5",
        manifest_digest="b" * 64,
    )

    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["status"] == "healthy"
    assert stored["current"]["contractRevision"] == "2.5"
    assert stored["current"]["manifestDigest"] == "b" * 64
    assert stored["platformRelease"] == {
        "id": "release-1",
        "status": "healthy",
        "requestedCommit": "a" * 40,
        "lastSuccessCommit": "a" * 40,
    }


def test_publisher_requires_the_exact_running_managed_container():
    release = {
        **runtime_release(),
        "containerName": "zhuojian-aifabei-orders",
    }
    inspected = mock.Mock(
        returncode=0,
        stdout=(
            "true|zhuojian/aifabei/orders:" + "a" * 40
            + "|zhuojian-runtime-admin/v1|orders|aifabei|" + "a" * 40 + "\n"
        ),
    )
    with mock.patch.object(publish_subsystem.subprocess, "run", return_value=inspected):
        publish_subsystem.verify_running_container(release, "aifabei", "orders")

    inspected.stdout = inspected.stdout.replace("|orders|aifabei|", "|other|aifabei|")
    with mock.patch.object(
        publish_subsystem.subprocess, "run", return_value=inspected
    ), pytest.raises(SystemExit, match="不一致"):
        publish_subsystem.verify_running_container(release, "aifabei", "orders")


def test_publisher_rechecks_release_before_persisting_platform_result(tmp_path):
    path = tmp_path / "release.json"
    release = {
        **runtime_release(),
        "containerName": "zhuojian-aifabei-orders",
    }
    path.write_text(json.dumps(release), encoding="utf-8")
    path.chmod(0o640)

    with mock.patch.object(publish_subsystem, "verify_running_container") as verify:
        latest = publish_subsystem.reload_verified_runtime_release(
            path,
            "orders",
            "aifabei",
            release["current"],
        )
    assert latest["current"] == release["current"]
    verify.assert_called_once_with(latest, "aifabei", "orders")

    changed = {**release, "releaseSwitch": {"phase": "switching"}}
    path.write_text(json.dumps(changed), encoding="utf-8")
    path.chmod(0o640)
    with pytest.raises(SystemExit, match="状态发生变化"):
        publish_subsystem.reload_verified_runtime_release(
            path,
            "orders",
            "aifabei",
            release["current"],
        )


def test_real_publisher_entrypoint_takes_the_shared_runtime_lock(monkeypatch):
    events: list[str] = []

    class FakeLock:
        def __enter__(self):
            events.append("lock")

        def __exit__(self, *_args):
            events.append("unlock")

    monkeypatch.setattr(publish_subsystem, "runtime_locked", lambda: FakeLock())
    monkeypatch.setattr(publish_subsystem.sys, "argv", ["publish_subsystem.py"])

    @publish_subsystem.with_runtime_lock
    def operation():
        events.append("publish")
        return 7

    assert operation() == 7
    assert events == ["lock", "publish", "unlock"]


def v25_environment() -> dict[str, str]:
    return {
        "ZHUOJIAN_MANIFEST_ACCESS_TOKEN": "zjmf_" + "a" * 40,
        "ZHUOJIAN_SSO_EXCHANGE_TOKEN": "zjss_" + "b" * 40,
        "ZHUOJIAN_ACTION_SIGNING_SECRET": "zjac_" + "c" * 40,
        "ZHUOJIAN_EVENT_SIGNING_SECRET": "zjev_" + "d" * 40,
    }


def test_publisher_selects_v25_registration_shape():
    values = v25_environment()

    token, revision = publish_subsystem.select_manifest_credential(values)
    payload = publish_subsystem.registration_auth_payload(values, "2.5")

    assert token == values["ZHUOJIAN_MANIFEST_ACCESS_TOKEN"]
    assert revision == "2.5"
    assert set(payload) == {"credentials"}
    assert "integration_secret" not in payload


def test_publisher_preserves_v24_registration_shape():
    values = {"ZHUOJIAN_INTEGRATION_SECRET": "legacy-" + "x" * 40}

    token, revision = publish_subsystem.select_manifest_credential(values)
    payload = publish_subsystem.registration_auth_payload(values, "2.4")

    assert token == values["ZHUOJIAN_INTEGRATION_SECRET"]
    assert revision == "2.4"
    assert payload == {"integration_secret": values["ZHUOJIAN_INTEGRATION_SECRET"]}


def test_publisher_refuses_implicit_contract_migration():
    with pytest.raises(SystemExit, match="不得自动迁移"):
        publish_subsystem.registration_auth_payload(v25_environment(), "2.4")


def test_publisher_rejects_ambiguous_mixed_credentials():
    values = {
        **v25_environment(),
        "ZHUOJIAN_INTEGRATION_SECRET": "legacy-" + "x" * 40,
    }
    with pytest.raises(SystemExit, match="同时包含 2.4 和 2.5"):
        publish_subsystem.select_manifest_credential(values)
