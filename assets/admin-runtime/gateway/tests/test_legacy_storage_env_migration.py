from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import stat
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "migrate_legacy_storage_envs.py"
SPEC = importlib.util.spec_from_file_location("legacy_storage_env_migration", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
migration = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = migration
SPEC.loader.exec_module(migration)


def private_dir(path: Path) -> None:
    path.mkdir(parents=True)
    if os.name == "posix":
        os.chmod(path, 0o700)


def test_migrates_managed_legacy_secret_and_release_atomically(tmp_path: Path) -> None:
    legacy = tmp_path / "apps"
    target = tmp_path / "storage-apps"
    deployments = tmp_path / "deployments"
    private_dir(legacy)
    private_dir(target)
    deployments.mkdir()
    app = deployments / "orders"
    app.mkdir()
    secret = legacy / "orders.storage.env"
    secret.write_text("FILE_STORAGE_TOKEN=never-print-this\n", encoding="utf-8")
    os.chmod(secret, 0o600)
    release_path = app / "release.json"
    release_path.write_text(
        json.dumps(
            {
                "managedBy": migration.MANAGED_BY,
                "applicationSlug": "orders",
                "storageMode": "oss-gateway",
                "storageEnvFile": str(secret),
            }
        ),
        encoding="utf-8",
    )
    os.chmod(release_path, 0o640)

    preview = migration.migrate(legacy, target, deployments, dry_run=True)
    assert preview["planned"] == 1
    assert secret.exists()

    result = migration.migrate(legacy, target, deployments)
    current = target / "orders.storage.env"
    assert result["migrated"] == 1
    assert not secret.exists()
    assert current.read_text(encoding="utf-8") == "FILE_STORAGE_TOKEN=never-print-this\n"
    release = json.loads(release_path.read_text(encoding="utf-8"))
    assert release["storageEnvFile"] == str(current)

    repeated = migration.migrate(legacy, target, deployments)
    assert repeated["migrated"] == 0
    assert repeated["alreadyCurrent"] == 1


def test_refuses_unexpected_managed_storage_env_path(tmp_path: Path) -> None:
    legacy = tmp_path / "apps"
    target = tmp_path / "storage-apps"
    deployments = tmp_path / "deployments"
    private_dir(legacy)
    private_dir(target)
    deployments.mkdir()
    app = deployments / "orders"
    app.mkdir()
    release = app / "release.json"
    release.write_text(
        json.dumps(
            {
                "managedBy": migration.MANAGED_BY,
                "applicationSlug": "orders",
                "storageMode": "oss-gateway",
                "storageEnvFile": str(tmp_path / "unknown.env"),
            }
        ),
        encoding="utf-8",
    )
    os.chmod(release, stat.S_IRUSR | stat.S_IWUSR)

    with pytest.raises(migration.MigrationError, match="unexpected storage env path"):
        migration.migrate(legacy, target, deployments, dry_run=True)
