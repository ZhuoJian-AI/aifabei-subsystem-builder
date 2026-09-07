#!/usr/bin/env python3
"""Keep Skill release versions separate from subsystem contract revisions."""

from __future__ import annotations

import json
import re
from pathlib import Path


SKILL_NAME = "aifabei-subsystem-builder"
SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def load_skill_metadata(skill_root: Path | None = None) -> dict:
    root = skill_root or Path(__file__).resolve().parents[1]
    path = root / "skill-version.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取 Skill 版本文件：{path}") from exc
    if not isinstance(payload, dict) or payload.get("schemaVersion") != 1:
        raise ValueError("skill-version.json 的 schemaVersion 必须为 1")
    if payload.get("skillName") != SKILL_NAME:
        raise ValueError(f"skill-version.json 的 skillName 必须为 {SKILL_NAME}")
    version = payload.get("skillVersion")
    if not isinstance(version, str) or not SEMVER_RE.fullmatch(version):
        raise ValueError("skill-version.json 的 skillVersion 必须是稳定 SemVer")
    supported = payload.get("supportedContractRevisions")
    if (
        not isinstance(supported, list)
        or not supported
        or len(supported) != len(set(supported))
        or not all(isinstance(item, str) and re.fullmatch(r"2\.\d+", item) for item in supported)
    ):
        raise ValueError("supportedContractRevisions 必须是无重复的 2.x 字符串数组")
    if payload.get("defaultContractRevision") not in supported:
        raise ValueError("defaultContractRevision 必须包含在 supportedContractRevisions 中")
    return payload


def supported_contract_revisions(skill_root: Path | None = None) -> tuple[str, ...]:
    return tuple(load_skill_metadata(skill_root)["supportedContractRevisions"])


def require_supported_contract_revision(
    revision: object,
    *,
    skill_root: Path | None = None,
) -> str:
    supported = supported_contract_revisions(skill_root)
    value = revision.strip() if isinstance(revision, str) else ""
    if value not in supported:
        raise ValueError(
            "不支持的 contractRevision："
            f"{value or '缺少'}；当前 Skill 仅支持 {', '.join(supported)}"
        )
    return value


def detect_project_contract_revision(
    project_root: Path,
    *,
    explicit_revision: str | None = None,
    skill_root: Path | None = None,
) -> str:
    """Read an existing project's revision without changing it."""

    manifest_path = project_root / "subsystem.json"
    manifest_revision: object | None = None
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"无法读取 {manifest_path}：{exc}") from exc
        if not isinstance(manifest, dict):
            raise ValueError("subsystem.json 顶层必须是对象")
        if manifest.get("protocol") != "zhuojian-subsystem" or manifest.get("version") != 2:
            raise ValueError("subsystem.json 必须使用 zhuojian-subsystem version 2")
        manifest_revision = manifest.get("contractRevision")

    if manifest_revision is None and explicit_revision is None:
        raise ValueError(
            "无法判断现有系统的接入契约：请保留 subsystem.json，"
            "或显式传入 --contract-revision；校验器不会猜测或自动升级版本"
        )

    manifest_value = (
        require_supported_contract_revision(manifest_revision, skill_root=skill_root)
        if manifest_revision is not None
        else None
    )
    explicit_value = (
        require_supported_contract_revision(explicit_revision, skill_root=skill_root)
        if explicit_revision is not None
        else None
    )
    if manifest_value and explicit_value and manifest_value != explicit_value:
        raise ValueError(
            f"--contract-revision={explicit_value} 与 subsystem.json={manifest_value} 不一致；"
            "普通维护不得只改版本号"
        )
    return manifest_value or explicit_value or ""
