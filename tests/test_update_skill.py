from __future__ import annotations

import hashlib
import json
import os
import zipfile
from pathlib import Path

import pytest

import update_skill


def version_payload(version: str) -> dict:
    return {
        "schemaVersion": 1,
        "skillName": update_skill.SKILL_NAME,
        "skillVersion": version,
        "defaultContractRevision": "2.5",
        "supportedContractRevisions": ["2.4", "2.5"],
    }


def write_skill(path: Path, version: str | None, marker: str) -> None:
    (path / "scripts").mkdir(parents=True)
    (path / "SKILL.md").write_text(
        f"---\nname: {update_skill.SKILL_NAME}\ndescription: test\n---\n{marker}\n",
        encoding="utf-8",
    )
    (path / "scripts" / "update_skill.py").write_text("# updater\n", encoding="utf-8")
    if version is not None:
        (path / "skill-version.json").write_text(
            json.dumps(version_payload(version)),
            encoding="utf-8",
        )


def build_archive(
    tmp_path: Path,
    version: str,
    marker: str,
    *,
    traversal: bool = False,
    include_changelog: bool = True,
    changelog_version: str | None = None,
) -> Path:
    archive_path = tmp_path / f"release-{version}.zip"
    prefix = update_skill.SKILL_NAME
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            f"{prefix}/SKILL.md",
            f"---\nname: {prefix}\ndescription: test\n---\n{marker}\n",
        )
        archive.writestr(f"{prefix}/skill-version.json", json.dumps(version_payload(version)))
        archive.writestr(f"{prefix}/scripts/update_skill.py", "# updater\n")
        archive.writestr(f"{prefix}/scripts/server_access_memory.py", "# access memory\n")
        archive.writestr(
            f"{prefix}/references/server-access-memory.md",
            "# server access memory\n",
        )
        if include_changelog:
            release_version = changelog_version or version
            archive.writestr(
                f"{prefix}/CHANGELOG.md",
                f"# 更新记录\n\n## {release_version} - 2026-09-07\n\n- test release\n",
            )
        if traversal:
            archive.writestr(f"{prefix}/../outside.txt", "unsafe")
    return archive_path


def write_remote_manifest(tmp_path: Path, version: str, archive_path: Path, digest: str | None = None) -> Path:
    manifest_path = tmp_path / f"manifest-{version}.json"
    manifest_path.write_text(json.dumps({
        "schemaVersion": 1,
        "skillName": update_skill.SKILL_NAME,
        "skillVersion": version,
        "channel": "stable",
        "archiveUrl": archive_path.as_uri(),
        "archiveSha256": digest or hashlib.sha256(archive_path.read_bytes()).hexdigest(),
    }), encoding="utf-8")
    return manifest_path


def test_same_version_does_not_download_or_replace(tmp_path: Path) -> None:
    skill_dir = tmp_path / update_skill.SKILL_NAME
    write_skill(skill_dir, "1.0.0", "old")
    missing_archive = tmp_path / "not-needed.zip"
    manifest = write_remote_manifest(tmp_path, "1.0.0", missing_archive, "0" * 64)

    result = update_skill.run_update(
        skill_dir,
        manifest_url=manifest.as_uri(),
        allow_test_url=True,
    )

    assert result.status == "current"
    assert "old" in (skill_dir / "SKILL.md").read_text(encoding="utf-8")


def test_latest_url_is_allowed_only_for_the_stable_manifest() -> None:
    update_skill.validate_download_url(
        update_skill.LATEST_MANIFEST_URL,
        allow_test_url=False,
        allow_latest_manifest=True,
    )
    with pytest.raises(update_skill.UpdateError, match="GitHub Release"):
        update_skill.validate_download_url(
            update_skill.LATEST_MANIFEST_URL,
            allow_test_url=False,
        )


def test_same_major_stable_release_is_installed(tmp_path: Path) -> None:
    skill_dir = tmp_path / update_skill.SKILL_NAME
    write_skill(skill_dir, "1.0.0", "old")
    archive = build_archive(tmp_path, "1.0.1", "new")
    manifest = write_remote_manifest(tmp_path, "1.0.1", archive)

    result = update_skill.run_update(
        skill_dir,
        manifest_url=manifest.as_uri(),
        allow_test_url=True,
    )

    assert result.status == "updated"
    assert result.available_version == "1.0.1"
    assert "new" in (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    assert "## 1.0.1" in (skill_dir / "CHANGELOG.md").read_text(encoding="utf-8")


def test_release_without_changelog_is_rejected(tmp_path: Path) -> None:
    skill_dir = tmp_path / update_skill.SKILL_NAME
    write_skill(skill_dir, "1.0.0", "old")
    archive = build_archive(tmp_path, "1.0.1", "new", include_changelog=False)
    manifest = write_remote_manifest(tmp_path, "1.0.1", archive)

    with pytest.raises(update_skill.UpdateError, match="缺少必要"):
        update_skill.run_update(
            skill_dir,
            manifest_url=manifest.as_uri(),
            allow_test_url=True,
        )

    assert "old" in (skill_dir / "SKILL.md").read_text(encoding="utf-8")


def test_release_with_mismatched_changelog_is_rejected(tmp_path: Path) -> None:
    skill_dir = tmp_path / update_skill.SKILL_NAME
    write_skill(skill_dir, "1.0.0", "old")
    archive = build_archive(tmp_path, "1.0.1", "new", changelog_version="1.0.2")
    manifest = write_remote_manifest(tmp_path, "1.0.1", archive)

    with pytest.raises(update_skill.UpdateError, match="缺少 1.0.1"):
        update_skill.run_update(
            skill_dir,
            manifest_url=manifest.as_uri(),
            allow_test_url=True,
        )

    assert "old" in (skill_dir / "SKILL.md").read_text(encoding="utf-8")


def test_cross_major_release_only_reports_availability(tmp_path: Path) -> None:
    skill_dir = tmp_path / update_skill.SKILL_NAME
    write_skill(skill_dir, "1.9.9", "old")
    missing_archive = tmp_path / "not-needed.zip"
    manifest = write_remote_manifest(tmp_path, "2.0.0", missing_archive, "0" * 64)

    result = update_skill.run_update(
        skill_dir,
        manifest_url=manifest.as_uri(),
        allow_test_url=True,
    )

    assert result.status == "major_available"
    assert "old" in (skill_dir / "SKILL.md").read_text(encoding="utf-8")


def test_offline_update_keeps_local_skill_and_returns_success(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    skill_dir = tmp_path / update_skill.SKILL_NAME
    write_skill(skill_dir, "1.0.0", "old")

    exit_code = update_skill.main([
        "--skill-dir", str(skill_dir),
        "--manifest-url", (tmp_path / "offline.json").as_uri(),
        "--allow-test-url",
    ])

    assert exit_code == 0
    assert "SKILL_UPDATE_WARNING" in capsys.readouterr().out
    assert "old" in (skill_dir / "SKILL.md").read_text(encoding="utf-8")


def test_checksum_mismatch_is_rejected_without_replacement(tmp_path: Path) -> None:
    skill_dir = tmp_path / update_skill.SKILL_NAME
    write_skill(skill_dir, "1.0.0", "old")
    archive = build_archive(tmp_path, "1.0.1", "new")
    manifest = write_remote_manifest(tmp_path, "1.0.1", archive, "0" * 64)

    with pytest.raises(update_skill.UpdateError, match="SHA-256"):
        update_skill.run_update(
            skill_dir,
            manifest_url=manifest.as_uri(),
            allow_test_url=True,
        )

    assert "old" in (skill_dir / "SKILL.md").read_text(encoding="utf-8")


def test_path_traversal_archive_is_rejected(tmp_path: Path) -> None:
    skill_dir = tmp_path / update_skill.SKILL_NAME
    write_skill(skill_dir, "1.0.0", "old")
    archive = build_archive(tmp_path, "1.0.1", "new", traversal=True)
    manifest = write_remote_manifest(tmp_path, "1.0.1", archive)

    with pytest.raises(update_skill.UpdateError, match="不安全路径"):
        update_skill.run_update(
            skill_dir,
            manifest_url=manifest.as_uri(),
            allow_test_url=True,
        )

    assert not (tmp_path / "outside.txt").exists()
    assert "old" in (skill_dir / "SKILL.md").read_text(encoding="utf-8")


def test_interrupted_replacement_rolls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    skill_dir = tmp_path / update_skill.SKILL_NAME
    write_skill(skill_dir, "1.0.0", "old")
    archive = build_archive(tmp_path, "1.0.1", "new")
    manifest = write_remote_manifest(tmp_path, "1.0.1", archive)
    real_replace = os.replace
    calls = 0

    def fail_second_replace(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated interruption")
        real_replace(source, target)

    monkeypatch.setattr(update_skill.os, "replace", fail_second_replace)

    with pytest.raises(update_skill.UpdateError, match="旧版已恢复"):
        update_skill.run_update(
            skill_dir,
            manifest_url=manifest.as_uri(),
            allow_test_url=True,
        )

    assert skill_dir.is_dir()
    assert "old" in (skill_dir / "SKILL.md").read_text(encoding="utf-8")


def test_bootstrap_installs_over_unversioned_legacy_copy(tmp_path: Path) -> None:
    skill_dir = tmp_path / update_skill.SKILL_NAME
    write_skill(skill_dir, None, "legacy")
    archive = build_archive(tmp_path, "1.0.0", "release")
    manifest = write_remote_manifest(tmp_path, "1.0.0", archive)

    result = update_skill.run_update(
        skill_dir,
        manifest_url=manifest.as_uri(),
        bootstrap=True,
        allow_test_url=True,
    )

    assert result.status == "installed"
    assert json.loads((skill_dir / "skill-version.json").read_text(encoding="utf-8"))["skillVersion"] == "1.0.0"
    assert "release" in (skill_dir / "SKILL.md").read_text(encoding="utf-8")
