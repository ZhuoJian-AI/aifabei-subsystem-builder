#!/usr/bin/env python3
"""Install stable same-major releases of this public Codex Skill."""

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


SKILL_NAME = "aifabei-subsystem-builder"
LATEST_MANIFEST_URL = (
    "https://github.com/ZhuoJian-AI/aifabei-subsystem-builder/"
    "releases/latest/download/update-manifest.json"
)
RELEASE_PATH_PREFIX = "/ZhuoJian-AI/aifabei-subsystem-builder/releases/download/"
SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
MAX_MANIFEST_BYTES = 256 * 1024
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_EXPANDED_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_FILES = 20_000


class UpdateError(RuntimeError):
    """A recoverable update failure; the installed Skill must remain usable."""


@dataclass(frozen=True)
class UpdateResult:
    status: str
    current_version: str | None
    available_version: str


def parse_stable_semver(value: object, label: str) -> tuple[int, int, int]:
    if not isinstance(value, str):
        raise UpdateError(f"{label} 必须是稳定 SemVer")
    match = SEMVER_RE.fullmatch(value)
    if not match:
        raise UpdateError(f"{label} 必须是稳定 SemVer，不能包含预发布标记")
    return tuple(int(part) for part in match.groups())


def validate_download_url(url: str, *, allow_test_url: bool) -> None:
    parsed = urlparse(url)
    if allow_test_url and parsed.scheme in {"file", "http", "https"}:
        return
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or not parsed.path.startswith(RELEASE_PATH_PREFIX)
    ):
        raise UpdateError("更新地址必须是本 Skill 的 GitHub Release HTTPS 地址")


def read_url(
    url: str,
    *,
    maximum_bytes: int,
    timeout: float,
    allow_test_url: bool,
) -> bytes:
    validate_download_url(url, allow_test_url=allow_test_url)
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/octet-stream", "User-Agent": "Aifabei-Skill-Updater/1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            declared = response.headers.get("Content-Length")
            if declared and int(declared) > maximum_bytes:
                raise UpdateError("更新文件超过允许大小")
            payload = response.read(maximum_bytes + 1)
    except UpdateError:
        raise
    except Exception as exc:  # noqa: BLE001 - converted into a non-blocking warning by main()
        raise UpdateError(f"无法下载稳定版：{exc}") from exc
    if len(payload) > maximum_bytes:
        raise UpdateError("更新文件超过允许大小")
    return payload


def read_json_bytes(payload: bytes, label: str) -> dict:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateError(f"{label} 不是有效 UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise UpdateError(f"{label} 顶层必须是对象")
    return value


def validate_version_metadata(payload: dict, *, expected_version: str | None = None) -> str:
    if payload.get("schemaVersion") != 1 or payload.get("skillName") != SKILL_NAME:
        raise UpdateError("Skill 版本文件身份不匹配")
    version = payload.get("skillVersion")
    parse_stable_semver(version, "skillVersion")
    if expected_version is not None and version != expected_version:
        raise UpdateError("压缩包中的 skillVersion 与更新清单不一致")
    supported = payload.get("supportedContractRevisions")
    default_revision = payload.get("defaultContractRevision")
    if (
        not isinstance(supported, list)
        or not supported
        or len(supported) != len(set(supported))
        or default_revision not in supported
        or not all(isinstance(item, str) and re.fullmatch(r"2\.\d+", item) for item in supported)
    ):
        raise UpdateError("Skill 版本文件的接入契约范围无效")
    return str(version)


def read_local_version(skill_dir: Path, *, bootstrap: bool) -> str | None:
    if not skill_dir.exists():
        if bootstrap:
            return None
        raise UpdateError(f"Skill 目录不存在：{skill_dir}")
    if not skill_dir.is_dir():
        raise UpdateError(f"Skill 路径不是目录：{skill_dir}")
    if (skill_dir / ".git").exists():
        raise UpdateError("检测到 Git 开发工作树；请用 Git 更新，自动更新器未改动该目录")
    skill_entry = skill_dir / "SKILL.md"
    try:
        entry_text = skill_entry.read_text(encoding="utf-8")
    except OSError as exc:
        raise UpdateError("目标目录不是完整的 Codex Skill") from exc
    if not re.search(r"(?m)^name:\s*[\"']?aifabei-subsystem-builder[\"']?\s*$", entry_text):
        raise UpdateError("目标目录中的 Skill 名称不匹配，已拒绝覆盖")
    version_path = skill_dir / "skill-version.json"
    if not version_path.is_file():
        if bootstrap:
            return None
        raise UpdateError("当前 Skill 没有版本文件，请先执行一次引导安装")
    try:
        metadata = read_json_bytes(version_path.read_bytes(), "本地 Skill 版本文件")
    except OSError as exc:
        raise UpdateError("无法读取本地 Skill 版本文件") from exc
    return validate_version_metadata(metadata)


def validate_update_manifest(payload: dict, *, allow_test_url: bool) -> tuple[str, str, str]:
    if (
        payload.get("schemaVersion") != 1
        or payload.get("skillName") != SKILL_NAME
        or payload.get("channel") != "stable"
    ):
        raise UpdateError("更新清单身份或发布通道不正确")
    version = payload.get("skillVersion")
    parse_stable_semver(version, "更新清单 skillVersion")
    archive_url = payload.get("archiveUrl")
    digest = payload.get("archiveSha256")
    if not isinstance(archive_url, str):
        raise UpdateError("更新清单缺少 archiveUrl")
    validate_download_url(archive_url, allow_test_url=allow_test_url)
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise UpdateError("更新清单缺少有效的 SHA-256")
    return str(version), archive_url, digest


def safe_extract(archive_path: Path, destination: Path) -> Path:
    expanded = 0
    try:
        archive = zipfile.ZipFile(archive_path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise UpdateError("稳定版压缩包损坏") from exc
    with archive:
        entries = archive.infolist()
        if len(entries) > MAX_ARCHIVE_FILES:
            raise UpdateError("稳定版压缩包文件数量异常")
        for info in entries:
            normalized = info.filename.replace("\\", "/")
            member = PurePosixPath(normalized)
            if (
                member.is_absolute()
                or not member.parts
                or any(part in {"", ".", ".."} or ":" in part for part in member.parts)
                or member.parts[0] != SKILL_NAME
            ):
                raise UpdateError("稳定版压缩包包含不安全路径")
            unix_mode = (info.external_attr >> 16) & 0xFFFF
            file_type = stat.S_IFMT(unix_mode)
            if stat.S_ISLNK(unix_mode) or file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
                raise UpdateError("稳定版压缩包包含不允许的链接或特殊文件")
            expanded += info.file_size
            if expanded > MAX_EXPANDED_BYTES:
                raise UpdateError("稳定版解压后大小异常")
            if info.compress_size and info.file_size / info.compress_size > 200:
                raise UpdateError("稳定版压缩比异常")
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
    return destination / SKILL_NAME


def verify_candidate(candidate: Path, expected_version: str) -> None:
    required = (
        candidate / "SKILL.md",
        candidate / "skill-version.json",
        candidate / "scripts" / "update_skill.py",
    )
    if not all(path.is_file() for path in required) or (candidate / ".git").exists():
        raise UpdateError("稳定版压缩包缺少必要的 Skill 文件")
    entry_text = required[0].read_text(encoding="utf-8")
    if not re.search(r"(?m)^name:\s*[\"']?aifabei-subsystem-builder[\"']?\s*$", entry_text):
        raise UpdateError("稳定版压缩包中的 Skill 名称不匹配")
    metadata = read_json_bytes(required[1].read_bytes(), "压缩包 Skill 版本文件")
    validate_version_metadata(metadata, expected_version=expected_version)


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
        raise UpdateError("安装新版失败，旧版已恢复") from exc
    if had_existing and backup.exists():
        try:
            shutil.rmtree(backup)
        except OSError:
            # The new Skill is already atomically installed. A hidden backup is
            # harmless and preserves a manual recovery point.
            pass


def run_update(
    skill_dir: Path,
    *,
    manifest_url: str = LATEST_MANIFEST_URL,
    timeout: float = 12,
    bootstrap: bool = False,
    allow_test_url: bool = False,
) -> UpdateResult:
    skill_dir = skill_dir.expanduser().resolve()
    if skill_dir.name != SKILL_NAME:
        raise UpdateError(f"安装目录名必须为 {SKILL_NAME}")
    current_version = read_local_version(skill_dir, bootstrap=bootstrap)
    manifest_payload = read_url(
        manifest_url,
        maximum_bytes=MAX_MANIFEST_BYTES,
        timeout=timeout,
        allow_test_url=allow_test_url,
    )
    manifest = read_json_bytes(manifest_payload, "更新清单")
    available_version, archive_url, expected_digest = validate_update_manifest(
        manifest,
        allow_test_url=allow_test_url,
    )
    available_tuple = parse_stable_semver(available_version, "更新清单 skillVersion")
    if current_version is not None:
        current_tuple = parse_stable_semver(current_version, "本地 skillVersion")
        if available_tuple <= current_tuple:
            return UpdateResult("current", current_version, available_version)
        if available_tuple[0] != current_tuple[0]:
            return UpdateResult("major_available", current_version, available_version)

    skill_dir.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f".{SKILL_NAME}-update-", dir=skill_dir.parent))
    try:
        archive_payload = read_url(
            archive_url,
            maximum_bytes=MAX_ARCHIVE_BYTES,
            timeout=timeout,
            allow_test_url=allow_test_url,
        )
        actual_digest = hashlib.sha256(archive_payload).hexdigest()
        if actual_digest != expected_digest:
            raise UpdateError("稳定版压缩包 SHA-256 校验失败")
        archive_path = work / "release.zip"
        archive_path.write_bytes(archive_payload)
        candidate = safe_extract(archive_path, work / "expanded")
        verify_candidate(candidate, available_version)
        replace_skill_directory(skill_dir, candidate)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return UpdateResult("installed" if current_version is None else "updated", current_version, available_version)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="检查并安装 aifabei-subsystem-builder 稳定版")
    parser.add_argument("--manifest-url", default=LATEST_MANIFEST_URL)
    parser.add_argument("--timeout", type=float, default=12)
    parser.add_argument("--install-dir", type=Path, help="首次引导安装到指定 Codex Skill 目录")
    parser.add_argument("--skill-dir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--allow-test-url", action="store_true", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.timeout <= 0 or (args.install_dir and args.skill_dir):
        print("SKILL_UPDATE_WARNING 更新参数无效，继续使用本地版本")
        return 1 if args.install_dir else 0
    default_dir = Path(__file__).resolve().parents[1]
    skill_dir = args.install_dir or args.skill_dir or default_dir
    bootstrap = args.install_dir is not None
    try:
        result = run_update(
            skill_dir,
            manifest_url=args.manifest_url,
            timeout=args.timeout,
            bootstrap=bootstrap,
            allow_test_url=args.allow_test_url,
        )
    except UpdateError as exc:
        print(f"SKILL_UPDATE_WARNING {exc}；继续使用本地版本")
        return 1 if bootstrap and not skill_dir.exists() else 0
    if result.status == "current":
        print(f"SKILL_UPDATE_CURRENT {result.current_version}")
    elif result.status == "major_available":
        print(
            "SKILL_UPDATE_MAJOR_AVAILABLE "
            f"{result.current_version} -> {result.available_version}；请由管理员决定是否升级"
        )
    elif result.status == "installed":
        print(f"SKILL_UPDATED installed {result.available_version}")
    else:
        print(f"SKILL_UPDATED {result.current_version} -> {result.available_version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
