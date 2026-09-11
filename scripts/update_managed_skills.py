#!/usr/bin/env python3
"""Install and update handoff Skills from the shared enterprise catalog."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

from update_skill import validate_changelog


REPOSITORY = "ZhuoJian-AI/zhuojian-enterprise-skills"
CATALOG_URL = (
    "https://github.com/ZhuoJian-AI/zhuojian-enterprise-skills/"
    "releases/latest/download/catalog.json"
)
RELEASE_PATH_PREFIX = "/ZhuoJian-AI/zhuojian-enterprise-skills/releases/download/"
CORE_SKILL_NAME = "aifabei-subsystem-builder"
SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_CATALOG_BYTES = 512 * 1024
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_EXPANDED_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_FILES = 20_000


class ManagedUpdateError(RuntimeError):
    """A managed update failed without invalidating the installed Skill."""


@dataclass(frozen=True)
class SkillRelease:
    name: str
    version: str
    archive_url: str
    archive_sha256: str
    kind: str
    enterprise_keys: tuple[str, ...]
    runtime_ids: tuple[str, ...]
    hosts: tuple[str, ...]


@dataclass(frozen=True)
class ManagedUpdateResult:
    name: str
    status: str
    current_version: str | None
    available_version: str
    skill_dir: Path


def default_skills_dir() -> Path:
    configured = os.environ.get("CODEX_HOME")
    codex_home = Path(configured).expanduser() if configured else Path.home() / ".codex"
    return codex_home / "skills"


def parse_stable_semver(value: object, label: str) -> tuple[int, int, int]:
    if not isinstance(value, str):
        raise ManagedUpdateError(f"{label} 必须是稳定 SemVer")
    match = SEMVER_RE.fullmatch(value)
    if not match:
        raise ManagedUpdateError(f"{label} 必须是稳定 SemVer，不能包含预发布标记")
    return tuple(int(part) for part in match.groups())


def validate_skill_name(value: object, label: str = "Skill 名称") -> str:
    if not isinstance(value, str) or len(value) > 63 or not SKILL_NAME_RE.fullmatch(value):
        raise ManagedUpdateError(f"{label} 无效")
    return value


def validate_download_url(
    url: str,
    *,
    allow_test_url: bool,
    allow_catalog_url: bool = False,
) -> None:
    parsed = urlparse(url)
    if allow_test_url and parsed.scheme in {"file", "http", "https"}:
        return
    if allow_catalog_url and url == CATALOG_URL:
        return
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or not parsed.path.startswith(RELEASE_PATH_PREFIX)
    ):
        raise ManagedUpdateError("更新地址必须是企业 Skills 总仓库的 GitHub Release HTTPS 地址")


def read_url(
    url: str,
    *,
    maximum_bytes: int,
    timeout: float,
    allow_test_url: bool,
    allow_catalog_url: bool = False,
) -> bytes:
    validate_download_url(
        url,
        allow_test_url=allow_test_url,
        allow_catalog_url=allow_catalog_url,
    )
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/octet-stream", "User-Agent": "ZhuoJian-Skill-Manager/1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            declared = response.headers.get("Content-Length")
            if declared and int(declared) > maximum_bytes:
                raise ManagedUpdateError("更新文件超过允许大小")
            payload = response.read(maximum_bytes + 1)
    except ManagedUpdateError:
        raise
    except Exception as exc:  # noqa: BLE001 - surfaced as a bounded update failure
        raise ManagedUpdateError(f"无法下载稳定版：{exc}") from exc
    if len(payload) > maximum_bytes:
        raise ManagedUpdateError("更新文件超过允许大小")
    return payload


def read_json_bytes(payload: bytes, label: str) -> dict:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManagedUpdateError(f"{label} 不是有效 UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ManagedUpdateError(f"{label} 顶层必须是对象")
    return value


def string_tuple(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ManagedUpdateError(f"{label} 必须是不重复的非空字符串数组")
    if len(value) != len(set(value)):
        raise ManagedUpdateError(f"{label} 不能重复")
    return tuple(value)


def validate_catalog(payload: dict, *, allow_test_url: bool) -> dict[str, SkillRelease]:
    if (
        payload.get("schemaVersion") != 1
        or payload.get("channel") != "stable"
        or payload.get("repository") != REPOSITORY
    ):
        raise ManagedUpdateError("企业 Skill 目录身份或发布通道不正确")
    raw_skills = payload.get("skills")
    if not isinstance(raw_skills, dict) or not raw_skills:
        raise ManagedUpdateError("企业 Skill 目录没有可用 Skill")
    releases: dict[str, SkillRelease] = {}
    for raw_name, raw_release in raw_skills.items():
        name = validate_skill_name(raw_name)
        if not isinstance(raw_release, dict):
            raise ManagedUpdateError(f"{name} 的发布记录无效")
        if raw_release.get("skillName") != name:
            raise ManagedUpdateError(f"{name} 的发布身份不匹配")
        version = raw_release.get("skillVersion")
        parse_stable_semver(version, f"{name} skillVersion")
        archive_url = raw_release.get("archiveUrl")
        digest = raw_release.get("archiveSha256")
        kind = raw_release.get("kind")
        if not isinstance(archive_url, str):
            raise ManagedUpdateError(f"{name} 缺少 archiveUrl")
        validate_download_url(archive_url, allow_test_url=allow_test_url)
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ManagedUpdateError(f"{name} 缺少有效的 SHA-256")
        if kind not in {"core", "handoff"}:
            raise ManagedUpdateError(f"{name} 的 kind 无效")
        releases[name] = SkillRelease(
            name=name,
            version=str(version),
            archive_url=archive_url,
            archive_sha256=digest,
            kind=kind,
            enterprise_keys=string_tuple(raw_release.get("enterpriseKeys", []), f"{name} enterpriseKeys"),
            runtime_ids=string_tuple(raw_release.get("runtimeIds", []), f"{name} runtimeIds"),
            hosts=string_tuple(raw_release.get("hosts", []), f"{name} hosts"),
        )
    core = releases.get(CORE_SKILL_NAME)
    if core is None or core.kind != "core":
        raise ManagedUpdateError("企业 Skill 目录缺少总 Skill")
    return releases


def load_catalog(
    *,
    catalog_url: str = CATALOG_URL,
    timeout: float = 12,
    allow_test_url: bool = False,
) -> dict[str, SkillRelease]:
    payload = read_url(
        catalog_url,
        maximum_bytes=MAX_CATALOG_BYTES,
        timeout=timeout,
        allow_test_url=allow_test_url,
        allow_catalog_url=True,
    )
    return validate_catalog(read_json_bytes(payload, "企业 Skill 目录"), allow_test_url=allow_test_url)


def validate_version_metadata(payload: dict, expected_name: str, expected_version: str | None = None) -> str:
    if (
        payload.get("schemaVersion") != 1
        or payload.get("skillName") != expected_name
        or payload.get("managedBy") != REPOSITORY
    ):
        raise ManagedUpdateError("Skill 版本文件身份不匹配")
    version = payload.get("skillVersion")
    parse_stable_semver(version, "skillVersion")
    if expected_version is not None and version != expected_version:
        raise ManagedUpdateError("压缩包中的 skillVersion 与企业目录不一致")
    return str(version)


def read_local_version(skill_dir: Path, skill_name: str, *, bootstrap: bool) -> str | None:
    if not skill_dir.exists():
        if bootstrap:
            return None
        raise ManagedUpdateError(f"Skill 目录不存在：{skill_dir}")
    if not skill_dir.is_dir():
        raise ManagedUpdateError(f"Skill 路径不是目录：{skill_dir}")
    if (skill_dir / ".git").exists():
        raise ManagedUpdateError("检测到 Git 开发工作树；自动更新器未改动该目录")
    try:
        entry_text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    except OSError as exc:
        raise ManagedUpdateError("目标目录不是完整的 Codex Skill") from exc
    expected = re.compile(rf"(?m)^name:\s*[\"']?{re.escape(skill_name)}[\"']?\s*$")
    if expected.search(entry_text) is None:
        raise ManagedUpdateError("目标目录中的 Skill 名称不匹配，已拒绝覆盖")
    version_path = skill_dir / "skill-version.json"
    if not version_path.is_file():
        if bootstrap:
            return None
        raise ManagedUpdateError("当前 Skill 没有版本文件，需要由企业目录接管")
    try:
        metadata = read_json_bytes(version_path.read_bytes(), "本地 Skill 版本文件")
    except OSError as exc:
        raise ManagedUpdateError("无法读取本地 Skill 版本文件") from exc
    return validate_version_metadata(metadata, skill_name)


def safe_extract(archive_path: Path, destination: Path, skill_name: str) -> Path:
    expanded = 0
    try:
        archive = zipfile.ZipFile(archive_path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ManagedUpdateError("稳定版压缩包损坏") from exc
    with archive:
        entries = archive.infolist()
        if len(entries) > MAX_ARCHIVE_FILES:
            raise ManagedUpdateError("稳定版压缩包文件数量异常")
        for info in entries:
            normalized = info.filename.replace("\\", "/")
            member = PurePosixPath(normalized)
            if (
                member.is_absolute()
                or not member.parts
                or any(part in {"", ".", ".."} or ":" in part for part in member.parts)
                or member.parts[0] != skill_name
            ):
                raise ManagedUpdateError("稳定版压缩包包含不安全路径")
            unix_mode = (info.external_attr >> 16) & 0xFFFF
            file_type = stat.S_IFMT(unix_mode)
            if stat.S_ISLNK(unix_mode) or file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
                raise ManagedUpdateError("稳定版压缩包包含不允许的链接或特殊文件")
            expanded += info.file_size
            if expanded > MAX_EXPANDED_BYTES:
                raise ManagedUpdateError("稳定版解压后大小异常")
            if info.compress_size and info.file_size / info.compress_size > 200:
                raise ManagedUpdateError("稳定版压缩比异常")
            target = destination.joinpath(*member.parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            permissions = unix_mode & 0o777
            if permissions:
                target.chmod(permissions)
    return destination / skill_name


def verify_candidate(candidate: Path, release: SkillRelease) -> None:
    required = (
        candidate / "SKILL.md",
        candidate / "CHANGELOG.md",
        candidate / "skill-version.json",
    )
    if not all(path.is_file() for path in required) or (candidate / ".git").exists():
        raise ManagedUpdateError("稳定版压缩包缺少必要的 Skill 文件")
    entry_text = (candidate / "SKILL.md").read_text(encoding="utf-8")
    expected = re.compile(rf"(?m)^name:\s*[\"']?{re.escape(release.name)}[\"']?\s*$")
    if expected.search(entry_text) is None:
        raise ManagedUpdateError("稳定版压缩包中的 Skill 名称不匹配")
    metadata = read_json_bytes(
        (candidate / "skill-version.json").read_bytes(),
        "压缩包 Skill 版本文件",
    )
    validate_version_metadata(metadata, release.name, release.version)
    try:
        changelog = (candidate / "CHANGELOG.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ManagedUpdateError("无法读取压缩包中的 CHANGELOG.md") from exc
    try:
        validate_changelog(changelog, release.version)
    except Exception as exc:  # update_skill.UpdateError remains an implementation detail here
        raise ManagedUpdateError(str(exc)) from exc


def replace_skill_directory(skill_dir: Path, candidate: Path) -> None:
    parent = skill_dir.parent
    backup = parent / f".{skill_dir.name}.backup-{uuid.uuid4().hex}"
    had_existing = skill_dir.exists()
    if had_existing:
        os.replace(skill_dir, backup)
    try:
        os.replace(candidate, skill_dir)
    except Exception as exc:  # noqa: BLE001 - rollback must cover filesystem errors
        if had_existing and backup.exists() and not skill_dir.exists():
            os.replace(backup, skill_dir)
        raise ManagedUpdateError("安装新版失败，旧版已恢复") from exc
    if had_existing and backup.exists():
        shutil.rmtree(backup, ignore_errors=True)


def update_release(
    release: SkillRelease,
    skills_dir: Path,
    *,
    timeout: float = 12,
    allow_test_url: bool = False,
) -> ManagedUpdateResult:
    skills_dir = skills_dir.expanduser().resolve()
    skill_dir = skills_dir / release.name
    current_version = read_local_version(skill_dir, release.name, bootstrap=True)
    available_tuple = parse_stable_semver(release.version, f"{release.name} skillVersion")
    if current_version is not None:
        current_tuple = parse_stable_semver(current_version, "本地 skillVersion")
        if available_tuple <= current_tuple:
            return ManagedUpdateResult(release.name, "current", current_version, release.version, skill_dir)
        if available_tuple[0] != current_tuple[0]:
            return ManagedUpdateResult(
                release.name,
                "major_available",
                current_version,
                release.version,
                skill_dir,
            )

    skills_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f".{release.name}-update-", dir=skills_dir))
    try:
        archive_payload = read_url(
            release.archive_url,
            maximum_bytes=MAX_ARCHIVE_BYTES,
            timeout=timeout,
            allow_test_url=allow_test_url,
        )
        actual_digest = hashlib.sha256(archive_payload).hexdigest()
        if actual_digest != release.archive_sha256:
            raise ManagedUpdateError("稳定版压缩包 SHA-256 校验失败")
        archive_path = work / "release.zip"
        archive_path.write_bytes(archive_payload)
        candidate = safe_extract(archive_path, work / "expanded", release.name)
        verify_candidate(candidate, release)
        replace_skill_directory(skill_dir, candidate)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return ManagedUpdateResult(
        release.name,
        "installed" if current_version is None else "updated",
        current_version,
        release.version,
        skill_dir,
    )


def release_matches(
    release: SkillRelease,
    *,
    enterprise_key: str | None,
    runtime_id: str | None,
    host: str | None,
) -> bool:
    if release.kind != "handoff":
        return False
    if runtime_id and runtime_id in release.runtime_ids:
        return True
    if host and host in release.hosts:
        return True
    has_server_selector = bool(release.runtime_ids or release.hosts)
    return bool(
        enterprise_key
        and enterprise_key in release.enterprise_keys
        and not has_server_selector
    )


def select_installed(releases: dict[str, SkillRelease], skills_dir: Path) -> list[SkillRelease]:
    return [
        release
        for release in releases.values()
        if release.kind == "handoff" and (skills_dir / release.name).is_dir()
    ]


def select_resolved(
    releases: dict[str, SkillRelease],
    *,
    enterprise_key: str | None,
    runtime_id: str | None,
    host: str | None,
) -> list[SkillRelease]:
    return [
        release
        for release in releases.values()
        if release_matches(
            release,
            enterprise_key=enterprise_key,
            runtime_id=runtime_id,
            host=host,
        )
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="同步企业总仓库中的专属交接 Skills")
    parser.add_argument("--catalog-url", default=CATALOG_URL)
    parser.add_argument("--skills-dir", type=Path, default=default_skills_dir())
    parser.add_argument("--timeout", type=float, default=12)
    parser.add_argument("--allow-test-url", action="store_true", help=argparse.SUPPRESS)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("installed", help="更新本机已经安装的企业交接 Skills")
    resolve = subparsers.add_parser("resolve", help="按 ECS 企业与 Runtime 安装交接 Skills")
    resolve.add_argument("--enterprise-key")
    resolve.add_argument("--runtime-id")
    resolve.add_argument("--host")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.timeout <= 0:
        print("MANAGED_SKILLS_WARNING 更新参数无效")
        return 1
    try:
        releases = load_catalog(
            catalog_url=args.catalog_url,
            timeout=args.timeout,
            allow_test_url=args.allow_test_url,
        )
        if args.command == "installed":
            selected = select_installed(releases, args.skills_dir)
        else:
            if not any((args.enterprise_key, args.runtime_id, args.host)):
                raise ManagedUpdateError("resolve 至少需要 enterprise-key、runtime-id 或 host")
            selected = select_resolved(
                releases,
                enterprise_key=args.enterprise_key,
                runtime_id=args.runtime_id,
                host=args.host,
            )
        if not selected:
            print("MANAGED_SKILLS_CURRENT 没有匹配的交接 Skill")
            return 0
        failed = False
        for release in sorted(selected, key=lambda item: item.name):
            try:
                result = update_release(
                    release,
                    args.skills_dir,
                    timeout=args.timeout,
                    allow_test_url=args.allow_test_url,
                )
            except ManagedUpdateError as exc:
                failed = True
                print(f"MANAGED_SKILLS_WARNING {release.name}：{exc}")
                continue
            if result.status == "current":
                print(f"MANAGED_SKILLS_CURRENT {result.name} {result.current_version}")
            elif result.status == "major_available":
                print(
                    "MANAGED_SKILLS_MAJOR_AVAILABLE "
                    f"{result.name} {result.current_version} -> {result.available_version}"
                )
            elif result.status == "installed":
                print(
                    "MANAGED_SKILLS_UPDATED installed "
                    f"{result.name} {result.available_version} {result.skill_dir}"
                )
            else:
                print(
                    "MANAGED_SKILLS_UPDATED "
                    f"{result.name} {result.current_version} -> {result.available_version} "
                    f"{result.skill_dir}"
                )
        return 1 if failed else 0
    except ManagedUpdateError as exc:
        print(f"MANAGED_SKILLS_WARNING {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
