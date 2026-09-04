import argparse
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from unittest import mock

import pytest


HOST_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "zhuojian_runtime_admin_host_tests", HOST_ROOT / "runtime_admin.py"
)
assert SPEC is not None and SPEC.loader is not None
runtime_admin = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runtime_admin
SPEC.loader.exec_module(runtime_admin)


def paths_for(root: Path):
    return runtime_admin.Paths(
        runtime=root / "runtime.json",
        credential=root / "runtime-registration.key",
        apps_env=root / "apps",
        storage_apps=root / "storage-apps",
        repositories=root / "repositories",
        deployments=root / "deployments",
        data=root / "data",
        backups=root / "backups",
        nginx=root / "nginx",
        acme=root / "acme",
        state=root / "run" / "storage-state.json",
        upload_lock=root / "run" / "upload.lock",
        lock=root / "run" / "runtime.lock",
    )


def profile_for(paths, *, region="oss-cn-hongkong"):
    return {
        "schemaVersion": 2,
        "enterpriseKey": "aifabei",
        "organizationId": "00000000-0000-4000-8000-000000000001",
        "platform": {"baseUrl": "https://Portal.Example.com/"},
        "domains": {"suffix": "aifabei.example.com"},
        "deployment": {
            "repositoriesRoot": str(paths.repositories),
            "deploymentsRoot": str(paths.deployments),
            "dataRoot": str(paths.data),
            "backupsRoot": str(paths.backups),
            "nginxConfigRoot": str(paths.nginx),
            "registrationCredentialRef": str(paths.credential),
        },
        "resources": {"appPortRange": [18000, 18010]},
        "network": {"managementAccess": {"mode": "standard-ssh"}},
        "capabilities": {"fileStorage": True, "objectStorage": True},
        "fileStorage": {
            "provider": "aliyun-oss",
            "mode": runtime_admin.OSS_STORAGE_MODE,
            "defaultMode": runtime_admin.OSS_STORAGE_MODE,
            "root": str(paths.data),
            "verified": True,
        },
        "objectStorage": {
            "provider": "aliyun-oss",
            "mode": "gateway-api-v1",
            "bucket": "alphabet-company-files",
            "region": region,
            "rootPrefix": "apps",
            "gatewayBaseUrl": runtime_admin.STORAGE_GATEWAY_URL,
            "credentialRef": str(runtime_admin.STORAGE_CREDENTIAL),
            "verified": True,
        },
    }


def create_roots(paths):
    for path in (
        paths.apps_env,
        paths.storage_apps,
        paths.repositories,
        paths.deployments,
        paths.data,
        paths.backups,
        paths.nginx,
        paths.acme,
        paths.state.parent,
    ):
        path.mkdir(parents=True, exist_ok=True)
    paths.upload_lock.touch(exist_ok=True)
    paths.upload_lock.chmod(0o444)


def ok_result(stdout=""):
    return mock.Mock(returncode=0, stdout=stdout, stderr="")


def test_platform_origin_is_canonical_and_rejects_env_or_nginx_injection():
    assert runtime_admin.platform_origin(
        {"platform": {"baseUrl": "https://Portal.Example.com/"}}
    ) == "https://portal.example.com"
    for value in (
        "http://portal.example.com",
        "https://portal.example.com/path",
        "https://user@portal.example.com",
        "https://portal.example.com\nINJECTED=yes",
    ):
        with pytest.raises(runtime_admin.AdminError):
            runtime_admin.platform_origin({"platform": {"baseUrl": value}})


def test_nginx_streams_request_body_to_managed_disk_gates():
    with tempfile.TemporaryDirectory() as directory:
        paths = paths_for(Path(directory))
        profile = profile_for(paths)
        with mock.patch.object(Path, "is_file", return_value=True):
            rendered = runtime_admin.nginx_text("new-app", 18001, profile, paths)
    assert "client_max_body_size 512m;" in rendered
    assert "proxy_request_buffering off;" in rendered
    assert "client_body_timeout 30s;" in rendered
    assert "proxy_read_timeout 900s;" in rendered


def test_ensure_env_injects_runtime_saas_origin_and_upsert_is_fail_closed():
    with tempfile.TemporaryDirectory() as directory:
        paths = paths_for(Path(directory))
        paths.apps_env.mkdir()
        profile = profile_for(paths)
        target = runtime_admin.ensure_env("new-app", profile, paths)
        payload = target.read_bytes()
        assert b"ZHUOJIAN_SAAS_ORIGINS=https://portal.example.com\n" in payload
        assert b"ZHUOJIAN_PUBLIC_ORIGIN=https://new-app.aifabei.example.com\n" in payload
        if os.name != "nt":
            assert stat.S_IMODE(target.stat().st_mode) == 0o600

    updated = runtime_admin.upsert_env_value(
        b"SESSION_SECRET=do-not-print\n", "ZHUOJIAN_SAAS_ORIGINS", "https://portal.example.com"
    )
    assert updated.endswith(b"ZHUOJIAN_SAAS_ORIGINS=https://portal.example.com\n")
    with pytest.raises(runtime_admin.AdminError):
        runtime_admin.upsert_env_value(
            b"ZHUOJIAN_SAAS_ORIGINS=https://one.example.com\n"
            b"ZHUOJIAN_SAAS_ORIGINS=https://two.example.com\n",
            "ZHUOJIAN_SAAS_ORIGINS",
            "https://portal.example.com",
        )


def test_existing_release_refreshes_public_origin_before_next_deploy():
    with tempfile.TemporaryDirectory() as directory:
        paths = paths_for(Path(directory))
        create_roots(paths)
        profile = profile_for(paths)
        names = runtime_admin.expected_names("existing-app", profile, paths)
        release_file = paths.deployments / "existing-app" / "release.json"
        release_file.parent.mkdir()
        release_file.write_text(
            json.dumps({
                "schemaVersion": 1,
                "managedBy": runtime_admin.MANAGED_BY,
                **names,
                "port": 18001,
                "containerPort": 8000,
                "current": None,
                "history": [],
                "status": "provisioned",
                "storageMode": runtime_admin.LOCAL_STORAGE_MODE,
            }),
            encoding="utf-8",
        )
        env_file = Path(names["envFile"])
        runtime_admin.atomic_write(
            env_file,
            b"ZHUOJIAN_INTEGRATION_SECRET=keep-this-secret\n"
            b"SESSION_SECRET=keep-this-session\n",
            0o600,
        )

        with mock.patch.object(
            runtime_admin,
            "read_secure_application_env",
            return_value=env_file.read_bytes(),
        ):
            runtime_admin.provision_release("existing-app", profile, paths)

        updated = env_file.read_text(encoding="utf-8")
        assert "ZHUOJIAN_INTEGRATION_SECRET=keep-this-secret\n" in updated
        assert "SESSION_SECRET=keep-this-session\n" in updated
        assert "ZHUOJIAN_PUBLIC_ORIGIN=https://existing-app.aifabei.example.com\n" in updated
        assert "ZHUOJIAN_SAAS_ORIGINS=https://portal.example.com\n" in updated
        assert "FILE_STORAGE_UPLOAD_LOCK_FILE=/run/zhuojian/upload.lock\n" in updated


def test_host_installer_rolls_back_nginx_from_any_precommit_exit_or_signal():
    installer = (HOST_ROOT / "install.sh").read_text(encoding="utf-8")
    move_position = installer.index('mv -- "$STOCK_LINK" "$DISABLED_STOCK_LINK"')
    trap_position = installer.index("trap nginx_install_on_exit 0")
    guard_install_position = installer.index(
        'install -o root -g root -m 0644 "$DENY_SOURCE" "$DENY_TARGET"'
    )
    commit_position = installer.index("NGINX_TRANSACTION_COMMITTED=1")

    assert trap_position < move_position < guard_install_position < commit_position
    assert "rollback_nginx_guard" in installer[trap_position:commit_position]
    assert "trap 'nginx_install_on_signal 130' 2" in installer
    assert "trap 'nginx_install_on_signal 143' 15" in installer


def test_host_installer_rolls_back_runtime_profile_with_nginx_transaction():
    installer = (HOST_ROOT / "install.sh").read_text(encoding="utf-8")
    snapshot_position = installer.index('cp -- "$RUNTIME_PROFILE" "$RUNTIME_BACKUP"')
    move_position = installer.index('mv -- "$STOCK_LINK" "$DISABLED_STOCK_LINK"')
    patch_flag_position = installer.index("RUNTIME_PATCH_STARTED=1")
    patch_position = installer.index(
        '/usr/local/sbin/zhuojian-runtime patch-runtime --host "$PUBLIC_ADDRESS"'
    )
    commit_position = installer.index("NGINX_TRANSACTION_COMMITTED=1", patch_position)

    assert snapshot_position < move_position < patch_flag_position < patch_position < commit_position
    rollback = installer[
        installer.index("restore_runtime_profile() {"):
        installer.index("nginx_install_on_exit() {")
    ]
    assert 'mktemp /etc/zhuojian/.runtime.rollback.XXXXXX' in rollback
    assert 'mv -f -- "$runtime_rollback_tmp" "$RUNTIME_PROFILE"' in rollback
    assert "restore_runtime_profile || true" in rollback
    assert "remove_transaction_backups" in installer[patch_position:commit_position + 200]


def test_host_bootstrap_creates_one_shared_upload_lock_inode():
    tmpfiles = (HOST_ROOT / "zhuojian-tmpfiles.conf").read_text(encoding="utf-8")
    installer = (HOST_ROOT / "install.sh").read_text(encoding="utf-8")

    assert "f /run/zhuojian/upload.lock 0444 root root -" in tmpfiles
    assert 'stat -c \'%u:%g:%a\' -- /run/zhuojian/upload.lock' in installer


def test_equivalent_aliyun_regions_do_not_trigger_scope_scan_and_are_canonicalized():
    with tempfile.TemporaryDirectory() as directory:
        paths = paths_for(Path(directory))
        create_roots(paths)
        profile = profile_for(paths, region="oss-cn-hongkong")
        paths.runtime.write_text(json.dumps(profile), encoding="utf-8")
        paths.credential.write_text("not-read", encoding="utf-8")
        damaged = paths.deployments / "damaged" / "release.json"
        damaged.parent.mkdir()
        damaged.write_text("{not-json", encoding="utf-8")
        args = argparse.Namespace(
            bucket="alphabet-company-files",
            region="cn-hongkong",
            gateway_url=runtime_admin.STORAGE_GATEWAY_URL,
            credential_ref=str(runtime_admin.STORAGE_CREDENTIAL),
        )
        with mock.patch.object(
            runtime_admin, "locked", return_value=contextlib.nullcontext()
        ), mock.patch.object(
            runtime_admin,
            "credential_metadata",
            return_value={"secure": True},
        ), mock.patch.object(runtime_admin, "storage_foundation_ready"), mock.patch.object(
            runtime_admin, "verify_storage_foundation"
        ):
            result = runtime_admin.configure_oss_gateway(args, paths)
        assert result["ok"] is True
        configured = json.loads(paths.runtime.read_text(encoding="utf-8"))
        assert configured["objectStorage"]["region"] == "cn-hongkong"


def test_scope_change_fails_closed_on_damaged_release_before_probe():
    with tempfile.TemporaryDirectory() as directory:
        paths = paths_for(Path(directory))
        create_roots(paths)
        paths.runtime.write_text(json.dumps(profile_for(paths)), encoding="utf-8")
        damaged = paths.deployments / "damaged" / "release.json"
        damaged.parent.mkdir()
        damaged.write_text("{not-json", encoding="utf-8")
        args = argparse.Namespace(
            bucket="alphabet-company-files-next",
            region="cn-hongkong",
            gateway_url=runtime_admin.STORAGE_GATEWAY_URL,
            credential_ref=str(runtime_admin.STORAGE_CREDENTIAL),
        )
        with mock.patch.object(
            runtime_admin, "locked", return_value=contextlib.nullcontext()
        ), mock.patch.object(
            runtime_admin, "credential_metadata", return_value={"secure": True}
        ), mock.patch.object(runtime_admin, "storage_foundation_ready") as foundation:
            with pytest.raises(runtime_admin.AdminError, match="release record is invalid"):
                runtime_admin.configure_oss_gateway(args, paths)
        foundation.assert_not_called()


def test_storage_acceptance_requires_anonymous_and_scope_denials():
    checks = {
        "anonymousReadDenied": True,
        "put": True,
        "get": True,
        "delete": True,
        "twoApplicationIsolation": True,
        "temporaryCredentialsRevoked": True,
        "outsidePrefixDenied": True,
    }
    with mock.patch.object(
        runtime_admin.subprocess,
        "run",
        return_value=ok_result(json.dumps({"ok": True, "checks": checks})),
    ):
        runtime_admin.verify_storage_foundation("alphabet-company-files", "cn-hongkong")

    missing_anonymous = {**checks, "anonymousReadDenied": False}
    with mock.patch.object(
        runtime_admin.subprocess,
        "run",
        return_value=ok_result(json.dumps({"ok": True, "checks": missing_anonymous})),
    ):
        with pytest.raises(runtime_admin.AdminError, match="did not pass every required check"):
            runtime_admin.verify_storage_foundation("alphabet-company-files", "cn-hongkong")


def test_per_application_storage_network_is_labeled_and_gateway_only():
    with tempfile.TemporaryDirectory() as directory:
        profile = profile_for(paths_for(Path(directory)))
        name = runtime_admin.storage_network_name("new-app")
        labels = runtime_admin.storage_network_labels("new-app", profile)
        empty_network = {"Labels": labels, "Driver": "bridge", "Scope": "local", "Containers": {}}
        joined_network = {
            **empty_network,
            "Containers": {"gateway-id": {"Name": runtime_admin.STORAGE_GATEWAY_SERVICE}},
        }
        responses = iter(
            [
                mock.Mock(returncode=1, stdout="", stderr="not found"),
                ok_result(name + "\n"),
                ok_result(json.dumps(empty_network)),
                ok_result(),
                ok_result(json.dumps(joined_network)),
            ]
        )
        calls = []

        def fake_run(argv, *, check=True):
            calls.append(argv)
            return next(responses)

        with mock.patch.object(runtime_admin, "run", side_effect=fake_run):
            assert runtime_admin.ensure_storage_network("new-app", profile) == name
        create = calls[1]
        assert create[:4] == ["docker", "network", "create", "--driver"]
        assert f"com.zhuojian.application=new-app" in create
        assert calls[3] == [
            "docker",
            "network",
            "connect",
            "--alias",
            runtime_admin.STORAGE_GATEWAY_SERVICE,
            name,
            runtime_admin.STORAGE_GATEWAY_SERVICE,
        ]


def test_storage_network_refuses_unknown_member():
    with tempfile.TemporaryDirectory() as directory:
        profile = profile_for(paths_for(Path(directory)))
        network = {
            "Labels": runtime_admin.storage_network_labels("new-app", profile),
            "Driver": "bridge",
            "Scope": "local",
            "Containers": {"other": {"Name": "zhuojian-aifabei-other-app"}},
        }
        with mock.patch.object(runtime_admin, "run", return_value=ok_result(json.dumps(network))):
            with pytest.raises(runtime_admin.AdminError, match="unknown member"):
                runtime_admin.ensure_storage_network("new-app", profile)


@pytest.mark.parametrize(
    "identity",
    [
        f"{runtime_admin.MANAGED_BY}|new-app|aifabei|true",
        "unmanaged|new-app|aifabei|false",
        f"{runtime_admin.MANAGED_BY}|other-app|aifabei|false",
    ],
)
def test_storage_network_refuses_running_or_mislabeled_rollback_member(identity):
    with tempfile.TemporaryDirectory() as directory:
        profile = profile_for(paths_for(Path(directory)))
        name = runtime_admin.storage_network_name("new-app")
        rollback_name = "zhuojian-aifabei-new-app-rollback-012345abcdef"
        network = {
            "Labels": runtime_admin.storage_network_labels("new-app", profile),
            "Driver": "bridge",
            "Scope": "local",
            "Containers": {"rollback-id": {"Name": rollback_name}},
        }
        responses = iter([ok_result(json.dumps(network)), ok_result(identity)])
        with mock.patch.object(runtime_admin, "run", side_effect=lambda *args, **kwargs: next(responses)):
            with pytest.raises(runtime_admin.AdminError, match="rollback member"):
                runtime_admin.inspect_storage_network(name, "new-app", profile)


def test_storage_network_allows_exact_stopped_managed_rollback_member():
    with tempfile.TemporaryDirectory() as directory:
        profile = profile_for(paths_for(Path(directory)))
        name = runtime_admin.storage_network_name("new-app")
        rollback_name = "zhuojian-aifabei-new-app-rollback-storage-012345abcdef"
        network = {
            "Labels": runtime_admin.storage_network_labels("new-app", profile),
            "Driver": "bridge",
            "Scope": "local",
            "Containers": {"rollback-id": {"Name": rollback_name}},
        }
        identity = f"{runtime_admin.MANAGED_BY}|new-app|aifabei|false"
        responses = iter([ok_result(json.dumps(network)), ok_result(identity)])
        with mock.patch.object(runtime_admin, "run", side_effect=lambda *args, **kwargs: next(responses)):
            inspected = runtime_admin.inspect_storage_network(name, "new-app", profile)
        assert inspected == network


def test_oss_container_uses_only_its_application_storage_network():
    with tempfile.TemporaryDirectory() as directory:
        paths = paths_for(Path(directory))
        create_roots(paths)
        paths.state.write_text("{}", encoding="utf-8")
        profile = profile_for(paths)
        storage_env = runtime_admin.storage_env_path("new-app", paths)
        release = {
            "applicationSlug": "new-app",
            "storageMode": runtime_admin.OSS_STORAGE_MODE,
            "storageEnvFile": str(storage_env),
            "storageNetwork": runtime_admin.storage_network_name("new-app"),
            "envFile": str(paths.apps_env / "new-app.env"),
            "dataDir": str(paths.data / "new-app"),
            "port": 18001,
        }
        calls = []
        with mock.patch.object(runtime_admin, "container_exists", return_value=False), mock.patch.object(
            runtime_admin, "ensure_storage_identity", return_value=storage_env
        ), mock.patch.object(
            runtime_admin, "run", side_effect=lambda argv, check=True: calls.append(argv) or ok_result()
        ):
            runtime_admin.start_container(
                "zhuojian-aifabei-new-app",
                "zhuojian/aifabei/new-app:" + "a" * 40,
                release,
                "new-app",
                profile,
                paths,
            )
        command = calls[-1]
        network_index = command.index("--network")
        assert command[network_index + 1] == "zhuojian-storage-new-app"
        assert runtime_admin.STORAGE_NETWORK not in command


def test_nginx_profiles_and_upload_limit_are_exact():
    standard = (HOST_ROOT / "nginx-default-deny.conf").read_text(encoding="utf-8")
    multiplex = (HOST_ROOT / "nginx-default-deny-multiplex.conf").read_text(encoding="utf-8")
    installer = (HOST_ROOT / "install.sh").read_text(encoding="utf-8")
    assert "listen 443 ssl default_server;" in standard
    assert "127.0.0.1:8443" not in standard
    assert "listen 127.0.0.1:8443 ssl default_server;" in multiplex
    assert "listen 443 ssl default_server;" not in multiplex
    assert "EFFECTIVE_MANAGEMENT_MODE" in installer
    assert "nginx-default-deny-multiplex.conf" in installer
    assert runtime_admin.NGINX_CLIENT_MAX_BODY_SIZE == "512m"


def rotation_release(paths):
    commit = "a" * 40
    return {
        "applicationSlug": "new-app",
        "containerName": "zhuojian-aifabei-new-app",
        "hostname": "new-app.aifabei.example.com",
        "port": 18001,
        "storageMode": runtime_admin.OSS_STORAGE_MODE,
        "storageEnvFile": str(runtime_admin.storage_env_path("new-app", paths)),
        "storageNetwork": runtime_admin.storage_network_name("new-app"),
        "current": {
            "commit": commit,
            "image": f"zhuojian/aifabei/new-app:{commit}",
            "deployedAt": "earlier",
        },
        "history": [],
        "status": "healthy",
    }


def test_rotation_persists_prepare_before_switch_and_commits_after_health():
    with tempfile.TemporaryDirectory() as directory:
        paths = paths_for(Path(directory))
        profile = profile_for(paths)
        release = rotation_release(paths)
        args = argparse.Namespace(application_slug="new-app", grace_seconds=300, health_timeout=60)
        operation_id = "1" * 32
        canonical = release["containerName"]
        previous = f"{canonical}-rollback-storage-{operation_id[:12]}"
        containers = {canonical: {"running": True, "operation": None}}
        events = []

        def fake_run(argv, check=True):
            del check
            if argv[:4] == ["docker", "image", "inspect", release["current"]["image"]]:
                return ok_result()
            if argv[:2] == ["docker", "stop"]:
                events.append("stop")
                containers[argv[-1]]["running"] = False
            elif argv[:2] == ["docker", "rename"]:
                events.append("rename")
                containers[argv[3]] = containers.pop(argv[2])
            elif argv[:2] == ["docker", "rm"]:
                events.append("remove-old")
                containers.pop(argv[-1], None)
            return ok_result()

        def fake_start(name, image, *_args, storage_rotation_operation_id=None, **_kwargs):
            events.append("start-new")
            assert image == release["current"]["image"]
            containers[name] = {"running": True, "operation": storage_rotation_operation_id}

        def fake_atomic(_path, value, _mode):
            events.append(f"persist:{(value.get('storageRotation') or {}).get('phase', 'complete')}")

        with contextlib.ExitStack() as stack:
            for patcher in (
                mock.patch.object(runtime_admin, "locked", return_value=contextlib.nullcontext()),
                mock.patch.object(runtime_admin, "load_runtime", return_value=profile),
                mock.patch.object(runtime_admin, "load_release", return_value=release),
                mock.patch.object(runtime_admin.secrets, "token_hex", return_value=operation_id),
                mock.patch.object(runtime_admin, "container_exists", side_effect=lambda name: name in containers),
                mock.patch.object(runtime_admin, "container_running", side_effect=lambda name: containers[name]["running"]),
                mock.patch.object(runtime_admin, "container_storage_rotation_operation", side_effect=lambda name: containers[name]["operation"]),
                mock.patch.object(runtime_admin, "assert_managed_container"),
                mock.patch.object(runtime_admin, "run", side_effect=fake_run),
                mock.patch.object(runtime_admin, "start_container", side_effect=fake_start),
                mock.patch.object(runtime_admin, "atomic_json", side_effect=fake_atomic),
            ):
                stack.enter_context(patcher)
            stack.enter_context(
                mock.patch.object(
                    runtime_admin,
                    "prepare_storage_rotation",
                    side_effect=lambda *_args, **_kwargs: events.append("prepare")
                    or Path(release["storageEnvFile"]),
                )
            )
            stack.enter_context(
                mock.patch.object(
                    runtime_admin,
                    "commit_storage_rotation",
                    side_effect=lambda *_args, **_kwargs: events.append("commit") or 2_000_000_000,
                )
            )
            stack.enter_context(
                mock.patch.object(
                    runtime_admin,
                    "wait_for_health",
                    side_effect=lambda *_args, **_kwargs: events.append("health"),
                )
            )
            result = runtime_admin.cmd_rotate_app_storage(args, paths)
        assert result["status"] == "healthy"
        assert "storageRotation" not in release
        assert containers == {canonical: {"running": True, "operation": operation_id}}
        assert events.index("persist:preparing") < events.index("prepare")
        assert events.index("prepare") < events.index("stop") < events.index("start-new")
        assert events.index("start-new") < events.index("commit") < events.index("remove-old")


def test_rotation_prepare_failure_preserves_durable_marker_and_old_container():
    with tempfile.TemporaryDirectory() as directory:
        paths = paths_for(Path(directory))
        profile = profile_for(paths)
        release = rotation_release(paths)
        canonical = release["containerName"]
        persisted = []
        with contextlib.ExitStack() as stack:
            for patcher in (
                mock.patch.object(runtime_admin, "locked", return_value=contextlib.nullcontext()),
                mock.patch.object(runtime_admin, "load_runtime", return_value=profile),
                mock.patch.object(runtime_admin, "load_release", return_value=release),
                mock.patch.object(runtime_admin.secrets, "token_hex", return_value="2" * 32),
                mock.patch.object(runtime_admin, "container_exists", side_effect=lambda name: name == canonical),
                mock.patch.object(runtime_admin, "container_running", return_value=True),
                mock.patch.object(runtime_admin, "assert_managed_container"),
                mock.patch.object(runtime_admin, "wait_for_health"),
                mock.patch.object(runtime_admin, "run", return_value=ok_result()),
                mock.patch.object(runtime_admin, "atomic_json", side_effect=lambda _p, value, _m: persisted.append(json.loads(json.dumps(value)))),
                mock.patch.object(runtime_admin, "prepare_storage_rotation", side_effect=runtime_admin.AdminError("lost response")),
            ):
                stack.enter_context(patcher)
            with pytest.raises(runtime_admin.AdminError, match="lost response"):
                runtime_admin.cmd_rotate_app_storage(
                    argparse.Namespace(application_slug="new-app", grace_seconds=300, health_timeout=60),
                    paths,
                )
        assert persisted[0]["storageRotation"]["phase"] == "preparing"
        assert release["storageRotation"]["operationId"] == "2" * 32


def test_rotation_commit_response_loss_keeps_new_container_and_resumes_same_operation():
    with tempfile.TemporaryDirectory() as directory:
        paths = paths_for(Path(directory))
        profile = profile_for(paths)
        release = rotation_release(paths)
        operation_id = "3" * 32
        canonical = release["containerName"]
        previous = f"{canonical}-rollback-storage-{operation_id[:12]}"
        release["storageRotation"] = {
            "operationId": operation_id,
            "phase": "healthy",
            "graceSeconds": 300,
            "healthTimeout": 60,
            "commit": release["current"]["commit"],
            "image": release["current"]["image"],
            "rollbackContainer": previous,
            "requestedAt": "earlier",
        }
        containers = {
            canonical: {"running": True, "operation": operation_id},
            previous: {"running": False, "operation": None},
        }
        with contextlib.ExitStack() as stack:
            for patcher in (
                mock.patch.object(runtime_admin, "locked", return_value=contextlib.nullcontext()),
                mock.patch.object(runtime_admin, "load_runtime", return_value=profile),
                mock.patch.object(runtime_admin, "load_release", return_value=release),
                mock.patch.object(runtime_admin, "container_exists", side_effect=lambda name: name in containers),
                mock.patch.object(runtime_admin, "container_running", side_effect=lambda name: containers[name]["running"]),
                mock.patch.object(runtime_admin, "container_storage_rotation_operation", side_effect=lambda name: containers[name]["operation"]),
                mock.patch.object(runtime_admin, "assert_managed_container"),
                mock.patch.object(runtime_admin, "wait_for_health"),
                mock.patch.object(runtime_admin, "run", return_value=ok_result()),
                mock.patch.object(runtime_admin, "atomic_json"),
                mock.patch.object(runtime_admin, "prepare_storage_rotation", return_value=Path(release["storageEnvFile"])),
                mock.patch.object(runtime_admin, "commit_storage_rotation", side_effect=runtime_admin.AdminError("response lost")),
            ):
                stack.enter_context(patcher)
            restore = stack.enter_context(
                mock.patch.object(runtime_admin, "restore_prepared_rotation_container")
            )
            with pytest.raises(runtime_admin.AdminError, match="commit is pending"):
                runtime_admin.cmd_rotate_app_storage(
                    argparse.Namespace(application_slug="new-app", grace_seconds=300, health_timeout=60),
                    paths,
                )
        restore.assert_not_called()
        assert release["storageRotation"]["operationId"] == operation_id
        assert release["storageRotation"]["phase"] == "healthy"


@pytest.mark.parametrize("crash_point", ["after-stop", "after-rename", "after-start"])
def test_rotation_resumes_each_container_switch_crash_window(crash_point):
    with tempfile.TemporaryDirectory() as directory:
        paths = paths_for(Path(directory))
        profile = profile_for(paths)
        release = rotation_release(paths)
        operation_id = "4" * 32
        canonical = release["containerName"]
        previous = f"{canonical}-rollback-storage-{operation_id[:12]}"
        release["storageRotation"] = {
            "operationId": operation_id,
            "phase": "switching",
            "graceSeconds": 300,
            "healthTimeout": 60,
            "commit": release["current"]["commit"],
            "image": release["current"]["image"],
            "rollbackContainer": previous,
            "requestedAt": "earlier",
        }
        if crash_point == "after-stop":
            containers = {canonical: {"running": False, "operation": None}}
        elif crash_point == "after-rename":
            containers = {previous: {"running": False, "operation": None}}
        else:
            containers = {
                previous: {"running": False, "operation": None},
                canonical: {"running": True, "operation": operation_id},
            }

        def fake_run(argv, check=True):
            del check
            if argv[:2] == ["docker", "stop"]:
                containers[argv[-1]]["running"] = False
            elif argv[:2] == ["docker", "rename"]:
                containers[argv[3]] = containers.pop(argv[2])
            elif argv[:2] == ["docker", "start"]:
                containers[argv[-1]]["running"] = True
            elif argv[:2] == ["docker", "rm"]:
                containers.pop(argv[-1], None)
            return ok_result()

        def fake_start(name, _image, *_args, storage_rotation_operation_id=None, **_kwargs):
            containers[name] = {"running": True, "operation": storage_rotation_operation_id}

        with contextlib.ExitStack() as stack:
            for patcher in (
                mock.patch.object(runtime_admin, "locked", return_value=contextlib.nullcontext()),
                mock.patch.object(runtime_admin, "load_runtime", return_value=profile),
                mock.patch.object(runtime_admin, "load_release", return_value=release),
                mock.patch.object(runtime_admin, "container_exists", side_effect=lambda name: name in containers),
                mock.patch.object(runtime_admin, "container_running", side_effect=lambda name: containers[name]["running"]),
                mock.patch.object(runtime_admin, "container_storage_rotation_operation", side_effect=lambda name: containers[name]["operation"]),
                mock.patch.object(runtime_admin, "assert_managed_container"),
                mock.patch.object(runtime_admin, "wait_for_health"),
                mock.patch.object(runtime_admin, "run", side_effect=fake_run),
                mock.patch.object(runtime_admin, "start_container", side_effect=fake_start),
                mock.patch.object(runtime_admin, "atomic_json"),
                mock.patch.object(runtime_admin, "prepare_storage_rotation", return_value=Path(release["storageEnvFile"])),
                mock.patch.object(runtime_admin, "commit_storage_rotation", return_value=2_000_000_000),
            ):
                stack.enter_context(patcher)
            result = runtime_admin.cmd_rotate_app_storage(
                argparse.Namespace(application_slug="new-app", grace_seconds=999, health_timeout=1),
                paths,
            )
        assert result["status"] == "healthy"
        assert "storageRotation" not in release
        assert containers == {canonical: {"running": True, "operation": operation_id}}


def test_storage_env_transition_accepts_exactly_one_legacy_or_current_path():
    with tempfile.TemporaryDirectory() as directory:
        paths = paths_for(Path(directory))
        create_roots(paths)
        legacy = runtime_admin.legacy_storage_env_path("new-app", paths)
        current = runtime_admin.storage_env_path("new-app", paths)
        legacy.write_text("FILE_STORAGE_TOKEN=legacy\n", encoding="utf-8")
        legacy.chmod(0o600)
        with mock.patch.object(runtime_admin, "secure_file_metadata"):
            assert runtime_admin.installed_storage_env_path("new-app", paths) == legacy
            assert runtime_admin.installed_storage_env_path(
                "new-app", paths, expected=str(legacy)
            ) == legacy
            with pytest.raises(runtime_admin.AdminError, match="supported managed path"):
                runtime_admin.installed_storage_env_path(
                    "new-app", paths, expected=str(paths.data / "secret.env")
                )

            current.write_text("FILE_STORAGE_TOKEN=current\n", encoding="utf-8")
            current.chmod(0o600)
            with pytest.raises(runtime_admin.AdminError, match="both legacy and current"):
                runtime_admin.installed_storage_env_path("new-app", paths)

            legacy.unlink()
            assert runtime_admin.installed_storage_env_path("new-app", paths) == current


def test_load_release_and_ensure_identity_support_host_first_legacy_gateway():
    with tempfile.TemporaryDirectory() as directory:
        paths = paths_for(Path(directory))
        create_roots(paths)
        profile = profile_for(paths)
        slug = "legacy-app"
        legacy = runtime_admin.legacy_storage_env_path(slug, paths)
        current = runtime_admin.storage_env_path(slug, paths)
        legacy.write_text("FILE_STORAGE_TOKEN=legacy\n", encoding="utf-8")
        release = {
            "schemaVersion": 1,
            "managedBy": runtime_admin.MANAGED_BY,
            **runtime_admin.expected_names(slug, profile, paths),
            "port": 18001,
            "storageMode": runtime_admin.OSS_STORAGE_MODE,
            "storageEnvFile": str(legacy),
            "storageNetwork": runtime_admin.storage_network_name(slug),
            "current": None,
            "history": [],
            "status": "provisioned",
        }
        target = runtime_admin.release_path(slug, paths)
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps(release), encoding="utf-8")

        with mock.patch.object(runtime_admin, "secure_file_metadata"), mock.patch.object(
            runtime_admin, "storage_foundation_ready"
        ), mock.patch.object(runtime_admin, "ensure_storage_network"), mock.patch.object(
            runtime_admin.subprocess, "run", return_value=ok_result()
        ):
            assert runtime_admin.load_release(slug, profile, paths)["storageEnvFile"] == str(legacy)
            assert runtime_admin.ensure_storage_identity(slug, profile, paths) == legacy

            legacy.replace(current)
            release["storageEnvFile"] = str(current)
            target.write_text(json.dumps(release), encoding="utf-8")
            assert runtime_admin.load_release(slug, profile, paths)["storageEnvFile"] == str(current)
            assert runtime_admin.ensure_storage_identity(slug, profile, paths) == current

            release["storageEnvFile"] = str(paths.data / "arbitrary.env")
            target.write_text(json.dumps(release), encoding="utf-8")
            with pytest.raises(runtime_admin.AdminError, match="supported managed path"):
                runtime_admin.load_release(slug, profile, paths)
