import argparse
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import sys
import tarfile
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


runtime_admin = load_module(
    "runtime_admin_under_test",
    ROOT / "assets" / "admin-runtime" / "host" / "runtime_admin.py",
)
provision_runtime = load_module(
    "provision_runtime_under_test", ROOT / "scripts" / "provision_runtime.py"
)


class RuntimeHostTests(unittest.TestCase):
    def paths(self, root: Path):
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

    def profile(self, paths, *, default_mode="local-managed"):
        profile = {
            "schemaVersion": 2,
            "enterpriseKey": "aifabei",
            "organizationId": "00000000-0000-4000-8000-000000000001",
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
            "fileStorage": {
                "provider": "local-disk",
                "mode": default_mode,
                "defaultMode": default_mode,
                "root": str(paths.data),
                "warningUsedPercent": 80,
                "stopUploadUsedPercent": 90,
                "minimumFreeGiB": 5,
                "verified": True,
            },
        }
        if default_mode == "oss-gateway":
            profile["fileStorage"]["provider"] = "aliyun-oss"
            profile["objectStorage"] = {
                "provider": "aliyun-oss",
                "mode": "gateway-api-v1",
                "bucket": "alphabet-company-files",
                "region": "oss-cn-hongkong",
                "rootPrefix": "apps",
                "gatewayBaseUrl": runtime_admin.STORAGE_GATEWAY_URL,
                "credentialRef": str(runtime_admin.STORAGE_CREDENTIAL),
                "verified": True,
            }
        return profile

    def create_roots(self, paths):
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

    def test_alphabet_is_canonical_and_legacy_runtime_identity_is_accepted(self):
        self.assertEqual(runtime_admin.canonical_enterprise_key("alphabet"), "alphabet")
        self.assertEqual(runtime_admin.canonical_enterprise_key("aifabei"), "alphabet")
        with self.assertRaises(runtime_admin.AdminError):
            runtime_admin.canonical_enterprise_key("alphabat")

    def test_new_app_env_uses_alphabet_on_a_legacy_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self.paths(Path(directory))
            self.create_roots(paths)
            profile = self.profile(paths)

            env_file = runtime_admin.ensure_env("new-app", profile, paths)

            self.assertIn(
                "ZHUOJIAN_ENTERPRISE_KEY=alphabet\n",
                env_file.read_text(encoding="utf-8"),
            )

    def test_slug_and_management_host_validation_remain_strict(self):
        self.assertEqual(runtime_admin.validate_slug("sample-review"), "sample-review")
        for invalid in ("../sample", "Sample", "a_b", "a.b", "-bad", "bad-", ""):
            with self.subTest(invalid=invalid), self.assertRaises(runtime_admin.AdminError):
                runtime_admin.validate_slug(invalid)
        self.assertEqual(runtime_admin.validate_management_host("203.0.113.10"), "203.0.113.10")
        for invalid in ("https://ecs.example.com", "host;id", "host name", "0.0.0.0"):
            with self.subTest(invalid=invalid), self.assertRaises(runtime_admin.AdminError):
                runtime_admin.validate_management_host(invalid)

    def test_archive_extraction_still_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "unsafe.tar.gz"
            content = b"escape"
            with tarfile.open(archive, "w:gz") as bundle:
                member = tarfile.TarInfo("data/../../outside")
                member.size = len(content)
                bundle.addfile(member, io.BytesIO(content))
            with self.assertRaises(runtime_admin.AdminError):
                runtime_admin.safe_extract_data(archive, Path(directory) / "extract")

    def test_disk_gate_still_enforces_percent_and_free_space(self):
        profile = {
            "fileStorage": {
                "warningUsedPercent": 80,
                "stopUploadUsedPercent": 90,
                "minimumFreeGiB": 5,
            }
        }
        with mock.patch.object(runtime_admin, "load_runtime", return_value=profile):
            with mock.patch.object(
                runtime_admin.shutil,
                "disk_usage",
                return_value=runtime_admin.shutil._ntuple_diskusage(
                    100 * runtime_admin.GIB,
                    91 * runtime_admin.GIB,
                    9 * runtime_admin.GIB,
                ),
            ):
                self.assertFalse(runtime_admin.disk_state()["uploadsAllowed"])
            with mock.patch.object(
                runtime_admin.shutil,
                "disk_usage",
                return_value=runtime_admin.shutil._ntuple_diskusage(
                    100 * runtime_admin.GIB,
                    70 * runtime_admin.GIB,
                    4 * runtime_admin.GIB,
                ),
            ):
                self.assertFalse(runtime_admin.disk_state()["uploadsAllowed"])

    def test_pre_oss_release_stays_local_after_runtime_default_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self.paths(Path(directory))
            self.create_roots(paths)
            profile = self.profile(paths, default_mode="oss-gateway")
            slug = "legacy-app"
            names = runtime_admin.expected_names(slug, profile, paths)
            target = runtime_admin.release_path(slug, paths)
            target.parent.mkdir()
            target.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "managedBy": runtime_admin.MANAGED_BY,
                        **names,
                        "port": 18000,
                        "current": None,
                        "history": [],
                        "status": "provisioned",
                    }
                ),
                encoding="utf-8",
            )

            release = runtime_admin.load_release(slug, profile, paths)

            self.assertEqual(release["storageMode"], "local-managed")
            self.assertNotIn("storageEnvFile", release)

    def test_new_oss_release_gets_fixed_storage_identity_without_token(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self.paths(Path(directory))
            self.create_roots(paths)
            profile = self.profile(paths, default_mode="oss-gateway")
            storage_env = runtime_admin.storage_env_path("new-app", paths)
            with mock.patch.object(runtime_admin, "container_exists", return_value=False), mock.patch.object(
                runtime_admin, "allocate_port", return_value=18001
            ), mock.patch.object(
                runtime_admin, "ensure_storage_identity", return_value=storage_env
            ) as ensure:
                release = runtime_admin.provision_release("new-app", profile, paths)

            ensure.assert_called_once_with("new-app", profile, paths)
            self.assertEqual(release["storageMode"], "oss-gateway")
            self.assertEqual(release["storageEnvFile"], str(storage_env))
            serialized = json.dumps(release)
            self.assertNotIn("TOKEN=", serialized)
            self.assertNotIn("AccessKey", serialized)

    def test_oss_container_joins_private_network_and_uses_second_env_file(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self.paths(Path(directory))
            self.create_roots(paths)
            paths.state.write_text("{}", encoding="utf-8")
            profile = self.profile(paths, default_mode="oss-gateway")
            storage_env = runtime_admin.storage_env_path("new-app", paths)
            release = {
                "applicationSlug": "new-app",
                "storageMode": "oss-gateway",
                "storageEnvFile": str(storage_env),
                "envFile": str(paths.apps_env / "new-app.env"),
                "dataDir": str(paths.data / "new-app"),
                "port": 18001,
            }
            captured = []

            def fake_run(argv, *, check=True):
                captured.append(argv)
                return mock.Mock(returncode=0, stdout="", stderr="")

            with mock.patch.object(runtime_admin, "container_exists", return_value=False), mock.patch.object(
                runtime_admin, "ensure_storage_identity", return_value=storage_env
            ), mock.patch.object(runtime_admin, "run", side_effect=fake_run):
                runtime_admin.start_container(
                    "zhuojian-aifabei-new-app",
                    "zhuojian/aifabei/new-app:" + "a" * 40,
                    release,
                    "new-app",
                    profile,
                    paths,
                )

            command = captured[-1]
            self.assertEqual(command.count("--env-file"), 2)
            self.assertIn(str(storage_env), command)
            self.assertIn("--network", command)
            self.assertIn(runtime_admin.storage_network_name("new-app"), command)

    def test_gateway_failure_never_forwards_cli_output(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self.paths(Path(directory))
            profile = self.profile(paths, default_mode="oss-gateway")
            failed = mock.Mock(returncode=7, stdout="sensitive-output", stderr="secret-value")
            with mock.patch.object(runtime_admin, "storage_foundation_ready"), mock.patch.object(
                runtime_admin.subprocess, "run", return_value=failed
            ):
                with self.assertRaises(runtime_admin.AdminError) as caught:
                    runtime_admin.ensure_storage_identity("new-app", profile, paths)
            self.assertNotIn("sensitive-output", str(caught.exception))
            self.assertNotIn("secret-value", str(caught.exception))

    def test_configure_gateway_preserves_runtime_credential_and_old_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self.paths(Path(directory))
            self.create_roots(paths)
            credential = b"publisher-credential-must-not-change\n"
            paths.credential.write_bytes(credential)
            os.chmod(paths.credential, stat.S_IRUSR | stat.S_IWUSR)
            paths.runtime.write_text(json.dumps(self.profile(paths)), encoding="utf-8")
            os.chmod(paths.runtime, stat.S_IRUSR | stat.S_IWUSR)
            old_release = paths.deployments / "old-app" / "release.json"
            old_release.parent.mkdir()
            old_release.write_text('{"storageMode":"local-managed"}', encoding="utf-8")
            old_release_before = old_release.read_bytes()
            args = argparse.Namespace(
                bucket="alphabet-company-files",
                region="oss-cn-hongkong",
                gateway_url=runtime_admin.STORAGE_GATEWAY_URL,
                credential_ref=str(runtime_admin.STORAGE_CREDENTIAL),
                verified=True,
            )
            with mock.patch.object(runtime_admin, "locked", return_value=contextlib.nullcontext()), mock.patch.object(
                runtime_admin, "storage_foundation_ready"
            ), mock.patch.object(
                runtime_admin, "verify_storage_foundation"
            ), mock.patch.object(
                runtime_admin,
                "credential_metadata",
                return_value={"exists": True, "ownerUid": 0, "mode": "0600", "secure": True},
            ):
                result = runtime_admin.configure_oss_gateway(args, paths)
                second = runtime_admin.configure_oss_gateway(args, paths)

            configured = json.loads(paths.runtime.read_text(encoding="utf-8"))
            self.assertTrue(result["credentialPreserved"])
            self.assertFalse(second["changed"])
            self.assertEqual(paths.credential.read_bytes(), credential)
            self.assertEqual(old_release.read_bytes(), old_release_before)
            self.assertEqual(configured["fileStorage"]["defaultMode"], "oss-gateway")
            self.assertEqual(configured["objectStorage"]["rootPrefix"], "apps")

    def test_configure_gateway_probe_failure_does_not_replace_runtime_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self.paths(Path(directory))
            self.create_roots(paths)
            original = (json.dumps(self.profile(paths), indent=2) + "\n").encode()
            paths.runtime.write_bytes(original)
            credential = b"publisher-credential-must-not-change\n"
            paths.credential.write_bytes(credential)
            args = argparse.Namespace(
                bucket="alphabet-company-files",
                region="oss-cn-hongkong",
                gateway_url=runtime_admin.STORAGE_GATEWAY_URL,
                credential_ref=str(runtime_admin.STORAGE_CREDENTIAL),
                verified=True,
            )
            failed = mock.Mock(
                returncode=23,
                stdout="probe-output-must-not-leak",
                stderr="probe-secret-must-not-leak",
            )

            with mock.patch.object(
                runtime_admin, "locked", return_value=contextlib.nullcontext()
            ), mock.patch.object(runtime_admin, "storage_foundation_ready"), mock.patch.object(
                runtime_admin,
                "credential_metadata",
                return_value={"exists": True, "ownerUid": 0, "mode": "0600", "secure": True},
            ), mock.patch.object(runtime_admin.subprocess, "run", return_value=failed):
                with self.assertRaises(runtime_admin.AdminError) as caught:
                    runtime_admin.configure_oss_gateway(args, paths)

            self.assertIn("acceptance probe failed (exit 23)", str(caught.exception))
            self.assertNotIn("probe-output-must-not-leak", str(caught.exception))
            self.assertNotIn("probe-secret-must-not-leak", str(caught.exception))
            self.assertEqual(paths.runtime.read_bytes(), original)
            self.assertEqual(paths.credential.read_bytes(), credential)
            self.assertFalse((paths.backups / "runtime-profile").exists())

    def test_configure_gateway_refuses_scope_change_with_existing_oss_release(self):
        requests = (
            ("alphabet-company-files-next", "oss-cn-hongkong"),
            ("alphabet-company-files", "oss-cn-shanghai"),
        )
        for bucket, region in requests:
            with self.subTest(bucket=bucket, region=region), tempfile.TemporaryDirectory() as directory:
                paths = self.paths(Path(directory))
                self.create_roots(paths)
                profile = self.profile(paths, default_mode="oss-gateway")
                profile["capabilities"] = {"fileStorage": True, "objectStorage": True}
                original = (json.dumps(profile, indent=2) + "\n").encode()
                paths.runtime.write_bytes(original)
                release = paths.deployments / "oss-app" / "release.json"
                release.parent.mkdir()
                release.write_text(
                    json.dumps(
                        {
                            "managedBy": runtime_admin.MANAGED_BY,
                            "storageMode": runtime_admin.OSS_STORAGE_MODE,
                        }
                    ),
                    encoding="utf-8",
                )
                args = argparse.Namespace(
                    bucket=bucket,
                    region=region,
                    gateway_url=runtime_admin.STORAGE_GATEWAY_URL,
                    credential_ref=str(runtime_admin.STORAGE_CREDENTIAL),
                    verified=True,
                )

                with mock.patch.object(
                    runtime_admin, "locked", return_value=contextlib.nullcontext()
                ), mock.patch.object(
                    runtime_admin,
                    "credential_metadata",
                    return_value={"exists": True, "ownerUid": 0, "mode": "0600", "secure": True},
                ), mock.patch.object(
                    runtime_admin, "storage_foundation_ready"
                ) as foundation, mock.patch.object(
                    runtime_admin, "verify_storage_foundation"
                ) as probe:
                    with self.assertRaises(runtime_admin.AdminError) as caught:
                        runtime_admin.configure_oss_gateway(args, paths)

                self.assertIn("managed OSS releases exist: oss-app", str(caught.exception))
                self.assertEqual(paths.runtime.read_bytes(), original)
                foundation.assert_not_called()
                probe.assert_not_called()

    def test_provisioner_accepts_only_the_managed_internal_http_gateway(self):
        self.assertEqual(
            provision_runtime._storage_gateway("http://zhuojian-storage-gateway:8080"),
            "http://zhuojian-storage-gateway:8080",
        )
        with self.assertRaises(argparse.ArgumentTypeError):
            provision_runtime._storage_gateway("http://another-container:8080")

    def test_initial_provision_refuses_oss_before_a_real_gateway_probe(self):
        argv = [
            "provision_runtime.py",
            "--organization-id",
            "00000000-0000-4000-8000-000000000001",
            "--runtime-key",
            "aifabei-hk-01",
            "--domain-suffix",
            "aifabei.example.com",
            "--public-address",
            "203.0.113.10",
            "--management-access-verified",
            "--storage-mode",
            "oss",
            "--storage-bucket",
            "alphabet-company-files",
            "--storage-region",
            "oss-cn-hongkong",
            "--storage-verified",
        ]
        error_output = io.StringIO()
        with mock.patch.object(sys, "argv", argv), mock.patch.object(
            provision_runtime, "call_json"
        ) as call_json, contextlib.redirect_stderr(error_output), self.assertRaises(
            SystemExit
        ):
            provision_runtime.main()

        self.assertIn("不接受 --storage-mode oss", error_output.getvalue())
        call_json.assert_not_called()

    def test_provisioner_rejects_sub_gib_minimum_that_host_runtime_cannot_accept(self):
        argv = [
            "provision_runtime.py",
            "--organization-id",
            "00000000-0000-4000-8000-000000000001",
            "--runtime-key",
            "aifabei-hk-01",
            "--domain-suffix",
            "aifabei.example.com",
            "--public-address",
            "203.0.113.10",
            "--management-access-verified",
            "--storage-minimum-free-gib",
            "0.5",
        ]
        error_output = io.StringIO()
        with mock.patch.object(sys, "argv", argv), mock.patch.object(
            provision_runtime, "call_json"
        ) as call_json, contextlib.redirect_stderr(error_output), self.assertRaises(
            SystemExit
        ):
            provision_runtime.main()

        self.assertIn("invalid int value", error_output.getvalue())
        call_json.assert_not_called()

    def test_provisioner_rejects_fractional_minimum_instead_of_truncating_it(self):
        argv = [
            "provision_runtime.py",
            "--organization-id",
            "00000000-0000-4000-8000-000000000001",
            "--runtime-key",
            "aifabei-hk-01",
            "--domain-suffix",
            "aifabei.example.com",
            "--public-address",
            "203.0.113.10",
            "--management-access-verified",
            "--storage-minimum-free-gib",
            "5.9",
        ]
        error_output = io.StringIO()
        with mock.patch.object(sys, "argv", argv), mock.patch.object(
            provision_runtime, "call_json"
        ) as call_json, contextlib.redirect_stderr(error_output), self.assertRaises(
            SystemExit
        ):
            provision_runtime.main()

        self.assertIn("invalid int value", error_output.getvalue())
        call_json.assert_not_called()

    def test_initial_provision_stays_local_until_gateway_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile_out = root / "runtime.json"
            credential_out = root / "runtime.key"
            response = {
                "credential": "one-time-publisher-value",
                "runtime": {"id": "runtime-id", "runtime_key": "aifabei-hk-01"},
                "runtime_profile": {
                    "schemaVersion": 2,
                    "enterpriseKey": "aifabei",
                    "organizationId": "00000000-0000-4000-8000-000000000001",
                    "deployment": {},
                    "domains": {"suffix": "aifabei.example.com"},
                },
            }
            argv = [
                "provision_runtime.py",
                "--organization-id",
                "00000000-0000-4000-8000-000000000001",
                "--runtime-key",
                "aifabei-hk-01",
                "--domain-suffix",
                "aifabei.example.com",
                "--public-address",
                "203.0.113.10",
                "--management-access-verified",
                "--storage-verified",
                "--local-storage-root",
                str(root),
                "--profile-out",
                str(profile_out),
                "--credential-out",
                str(credential_out),
            ]
            output = io.StringIO()
            with mock.patch.object(sys, "argv", argv), mock.patch.dict(
                os.environ, {"ZHUOJIAN_ADMIN_TOKEN": "admin-session"}, clear=False
            ), mock.patch.object(
                provision_runtime, "LOCAL_STORAGE_ROOT", root
            ), mock.patch.object(
                provision_runtime, "call_json", return_value=response
            ), mock.patch.object(
                provision_runtime.shutil,
                "disk_usage",
                return_value=mock.Mock(free=10 * 1024**3),
            ), contextlib.redirect_stdout(output):
                self.assertEqual(provision_runtime.main(), 0)

            profile = json.loads(profile_out.read_text(encoding="utf-8"))
            self.assertEqual(profile["fileStorage"]["defaultMode"], "local-managed")
            self.assertFalse(profile["capabilities"]["objectStorage"])
            self.assertNotIn("objectStorage", profile)
            self.assertNotIn("one-time-publisher-value", output.getvalue())

    def test_provisioner_preflights_private_outputs_before_api_call(self):
        argv = [
            "provision_runtime.py",
            "--organization-id",
            "00000000-0000-4000-8000-000000000001",
            "--runtime-key",
            "aifabei-hk-01",
            "--domain-suffix",
            "aifabei.example.com",
            "--public-address",
            "203.0.113.10",
            "--management-access-verified",
        ]
        with mock.patch.object(sys, "argv", argv), mock.patch.dict(
            os.environ, {"ZHUOJIAN_ADMIN_TOKEN": "admin-session"}, clear=False
        ), mock.patch.object(
            provision_runtime,
            "_preflight_output",
            side_effect=PermissionError("unsafe parent"),
        ), mock.patch.object(provision_runtime, "call_json") as call_json:
            with self.assertRaises(SystemExit) as caught:
                provision_runtime.main()
        self.assertIn("输出位置预检失败", str(caught.exception))
        call_json.assert_not_called()

    def test_provisioner_rejects_same_profile_and_credential_path_before_api(self):
        with tempfile.TemporaryDirectory() as directory:
            target = str(Path(directory) / "runtime-output")
            argv = [
                "provision_runtime.py",
                "--organization-id",
                "00000000-0000-4000-8000-000000000001",
                "--runtime-key",
                "aifabei-hk-01",
                "--domain-suffix",
                "aifabei.example.com",
                "--public-address",
                "203.0.113.10",
                "--management-access-verified",
                "--profile-out",
                target,
                "--credential-out",
                target,
            ]
            with mock.patch.object(sys, "argv", argv), mock.patch.object(
                provision_runtime, "call_json"
            ) as call_json, self.assertRaises(SystemExit):
                provision_runtime.main()
            call_json.assert_not_called()

    def test_local_storage_probe_is_reversible(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provision_runtime.verify_local_storage(root, 0.000001)
            self.assertEqual(list(root.iterdir()), [])

    def test_local_storage_probe_cleans_up_after_read_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original_read_text = Path.read_text

            def fail_probe_read(path, *args, **kwargs):
                if path.name == "probe.txt":
                    raise OSError("simulated read failure")
                return original_read_text(path, *args, **kwargs)

            with mock.patch.object(Path, "read_text", fail_probe_read):
                with self.assertRaises(OSError):
                    provision_runtime.verify_local_storage(root, 0.000001)
            self.assertEqual(list(root.iterdir()), [])

    def test_provisioner_rejects_storage_root_different_from_host_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            argv = [
                "provision_runtime.py",
                "--organization-id",
                "00000000-0000-4000-8000-000000000001",
                "--runtime-key",
                "aifabei-hk-01",
                "--domain-suffix",
                "aifabei.example.com",
                "--public-address",
                "203.0.113.10",
                "--management-access-verified",
                "--local-storage-root",
                directory,
            ]
            with mock.patch.object(sys, "argv", argv), mock.patch.object(
                provision_runtime, "call_json"
            ) as call_json, self.assertRaises(SystemExit):
                provision_runtime.main()
            call_json.assert_not_called()

    @unittest.skipIf(os.name == "nt", "POSIX ownership and mode semantics")
    def test_secure_write_rejects_weak_existing_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / "weak"
            parent.mkdir(mode=0o755)
            os.chmod(parent, 0o755)
            with self.assertRaises(PermissionError):
                provision_runtime.secure_write(parent / "secret", "value\n")


if __name__ == "__main__":
    unittest.main()
