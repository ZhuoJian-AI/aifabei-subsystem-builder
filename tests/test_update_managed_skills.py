from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import update_managed_skills


def version_payload(name: str, version: str) -> dict:
    return {
        "schemaVersion": 1,
        "skillName": name,
        "skillVersion": version,
        "managedBy": update_managed_skills.REPOSITORY,
    }


def build_skill_archive(tmp_path: Path, name: str, version: str, marker: str) -> Path:
    archive_path = tmp_path / f"{name}-{version}.zip"
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            f"{name}/SKILL.md",
            f"---\nname: {name}\ndescription: test\n---\n{marker}\n",
        )
        archive.writestr(
            f"{name}/skill-version.json",
            json.dumps(version_payload(name, version)),
        )
        archive.writestr(
            f"{name}/CHANGELOG.md",
            f"# 更新记录\n\n## {version} - 2026-09-11\n\n- test release\n",
        )
    return archive_path


def release_entry(
    name: str,
    version: str,
    archive: Path,
    *,
    kind: str,
    enterprises: list[str] | None = None,
    runtimes: list[str] | None = None,
    hosts: list[str] | None = None,
) -> dict:
    return {
        "skillName": name,
        "skillVersion": version,
        "archiveUrl": archive.as_uri(),
        "archiveSha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "kind": kind,
        "enterpriseKeys": enterprises or [],
        "runtimeIds": runtimes or [],
        "hosts": hosts or [],
    }


def write_catalog(tmp_path: Path, skills: dict[str, dict]) -> Path:
    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        json.dumps({
            "schemaVersion": 1,
            "channel": "stable",
            "repository": update_managed_skills.REPOSITORY,
            "skills": skills,
        }),
        encoding="utf-8",
    )
    return catalog


def base_catalog(tmp_path: Path, handoff: dict) -> Path:
    core_archive = build_skill_archive(
        tmp_path,
        update_managed_skills.CORE_SKILL_NAME,
        "1.1.0",
        "core",
    )
    return write_catalog(tmp_path, {
        update_managed_skills.CORE_SKILL_NAME: release_entry(
            update_managed_skills.CORE_SKILL_NAME,
            "1.1.0",
            core_archive,
            kind="core",
        ),
        handoff["skillName"]: handoff,
    })


def test_resolve_installs_host_handoff_skill(tmp_path: Path, capsys) -> None:
    name = "alphabet-daoxun-data-bridge"
    archive = build_skill_archive(tmp_path, name, "1.0.0", "installed")
    catalog = base_catalog(tmp_path, release_entry(
        name,
        "1.0.0",
        archive,
        kind="handoff",
        enterprises=["alphabet"],
        hosts=["8.218.208.205"],
    ))
    skills_dir = tmp_path / "skills"

    exit_code = update_managed_skills.main([
        "--catalog-url", catalog.as_uri(),
        "--skills-dir", str(skills_dir),
        "--allow-test-url",
        "resolve",
        "--enterprise-key", "alphabet",
        "--host", "8.218.208.205",
    ])

    assert exit_code == 0
    assert "MANAGED_SKILLS_UPDATED installed" in capsys.readouterr().out
    assert "installed" in (skills_dir / name / "SKILL.md").read_text(encoding="utf-8")


def test_server_specific_skill_is_not_selected_by_company_alone(tmp_path: Path, capsys) -> None:
    name = "alphabet-daoxun-data-bridge"
    archive = build_skill_archive(tmp_path, name, "1.0.0", "not installed")
    catalog = base_catalog(tmp_path, release_entry(
        name,
        "1.0.0",
        archive,
        kind="handoff",
        enterprises=["alphabet"],
        hosts=["8.218.208.205"],
    ))
    skills_dir = tmp_path / "skills"

    exit_code = update_managed_skills.main([
        "--catalog-url", catalog.as_uri(),
        "--skills-dir", str(skills_dir),
        "--allow-test-url",
        "resolve",
        "--enterprise-key", "alphabet",
    ])

    assert exit_code == 0
    assert "没有匹配" in capsys.readouterr().out
    assert not (skills_dir / name).exists()


def test_runtime_id_takes_a_stable_route(tmp_path: Path) -> None:
    name = "alphabet-runtime-handover"
    archive = build_skill_archive(tmp_path, name, "1.0.0", "runtime")
    catalog = base_catalog(tmp_path, release_entry(
        name,
        "1.0.0",
        archive,
        kind="handoff",
        enterprises=["alphabet"],
        runtimes=["alphabet-hk-01"],
        hosts=["8.12.3.1"],
    ))
    skills_dir = tmp_path / "skills"

    exit_code = update_managed_skills.main([
        "--catalog-url", catalog.as_uri(),
        "--skills-dir", str(skills_dir),
        "--allow-test-url",
        "resolve",
        "--runtime-id", "alphabet-hk-01",
        "--host", "203.0.113.7",
    ])

    assert exit_code == 0
    assert (skills_dir / name / "SKILL.md").is_file()


def test_installed_updates_unversioned_legacy_copy(tmp_path: Path) -> None:
    name = "alphabet-daoxun-data-bridge"
    archive = build_skill_archive(tmp_path, name, "1.0.0", "managed")
    catalog = base_catalog(tmp_path, release_entry(
        name,
        "1.0.0",
        archive,
        kind="handoff",
        hosts=["8.218.208.205"],
    ))
    skill_dir = tmp_path / "skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: legacy\n---\nlegacy\n",
        encoding="utf-8",
    )

    exit_code = update_managed_skills.main([
        "--catalog-url", catalog.as_uri(),
        "--skills-dir", str(tmp_path / "skills"),
        "--allow-test-url",
        "installed",
    ])

    assert exit_code == 0
    assert "managed" in (skill_dir / "SKILL.md").read_text(encoding="utf-8")


def test_checksum_failure_keeps_legacy_copy(tmp_path: Path) -> None:
    name = "alphabet-daoxun-data-bridge"
    archive = build_skill_archive(tmp_path, name, "1.0.0", "managed")
    entry = release_entry(name, "1.0.0", archive, kind="handoff", hosts=["8.218.208.205"])
    entry["archiveSha256"] = "0" * 64
    catalog = base_catalog(tmp_path, entry)
    skill_dir = tmp_path / "skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: legacy\n---\nlegacy\n",
        encoding="utf-8",
    )

    exit_code = update_managed_skills.main([
        "--catalog-url", catalog.as_uri(),
        "--skills-dir", str(tmp_path / "skills"),
        "--allow-test-url",
        "installed",
    ])

    assert exit_code == 1
    assert "legacy" in (skill_dir / "SKILL.md").read_text(encoding="utf-8")


def test_cross_company_host_does_not_install_wrong_skill(tmp_path: Path) -> None:
    alphabet_name = "alphabet-runtime-handover"
    zhipu_name = "zhipu-runtime-handover"
    alphabet_archive = build_skill_archive(tmp_path, alphabet_name, "1.0.0", "alphabet")
    zhipu_archive = build_skill_archive(tmp_path, zhipu_name, "1.0.0", "zhipu")
    core_archive = build_skill_archive(
        tmp_path,
        update_managed_skills.CORE_SKILL_NAME,
        "1.1.0",
        "core",
    )
    catalog = write_catalog(tmp_path, {
        update_managed_skills.CORE_SKILL_NAME: release_entry(
            update_managed_skills.CORE_SKILL_NAME,
            "1.1.0",
            core_archive,
            kind="core",
        ),
        alphabet_name: release_entry(
            alphabet_name,
            "1.0.0",
            alphabet_archive,
            kind="handoff",
            enterprises=["alphabet"],
            hosts=["8.12.3.1"],
        ),
        zhipu_name: release_entry(
            zhipu_name,
            "1.0.0",
            zhipu_archive,
            kind="handoff",
            enterprises=["zhipu"],
            hosts=["8.20.30.40"],
        ),
    })
    skills_dir = tmp_path / "skills"

    exit_code = update_managed_skills.main([
        "--catalog-url", catalog.as_uri(),
        "--skills-dir", str(skills_dir),
        "--allow-test-url",
        "resolve",
        "--enterprise-key", "alphabet",
        "--host", "8.12.3.1",
    ])

    assert exit_code == 0
    assert (skills_dir / alphabet_name).exists()
    assert not (skills_dir / zhipu_name).exists()
