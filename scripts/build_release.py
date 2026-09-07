#!/usr/bin/env python3
"""Build immutable GitHub Release assets for the public Skill."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from contract_versions import load_skill_metadata
from update_skill import UpdateError, validate_changelog


REPOSITORY = "ZhuoJian-AI/aifabei-subsystem-builder"
ARCHIVE_NAME = "aifabei-subsystem-builder.zip"


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode:
        raise SystemExit(result.stderr.strip() or result.stdout.strip() or "Git 命令失败")
    return result.stdout.strip()


def tracked_files(root: Path) -> list[tuple[Path, int]]:
    raw = subprocess.run(
        ["git", "ls-files", "--stage", "-z"],
        cwd=root,
        capture_output=True,
        check=True,
    ).stdout
    files: list[tuple[Path, int]] = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        metadata, encoded_path = record.split(b"\t", 1)
        mode = int(metadata.split(b" ", 1)[0], 8)
        path = Path(encoded_path.decode("utf-8"))
        if (root / path).is_file():
            files.append((path, mode))
    return sorted(files, key=lambda item: item[0].as_posix())


def main() -> int:
    parser = argparse.ArgumentParser(description="构建 Skill GitHub Release 资产")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--source-dir", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()

    root = args.source_dir.expanduser().resolve()
    output = args.output_dir.expanduser().resolve()
    if Path(git(root, "rev-parse", "--show-toplevel")).resolve() != root:
        raise SystemExit("--source-dir 必须是 Skill Git 仓库根目录")
    if git(root, "status", "--porcelain"):
        raise SystemExit("发布构建要求干净工作树")
    metadata = load_skill_metadata(root)
    version = metadata["skillVersion"]
    tag = f"v{version}"
    source_commit = git(root, "rev-parse", "HEAD")
    files = tracked_files(root)
    required = {
        Path("SKILL.md"),
        Path("CHANGELOG.md"),
        Path("skill-version.json"),
        Path("scripts/update_skill.py"),
    }
    if not required.issubset({path for path, _ in files}):
        raise SystemExit("发布内容缺少必要文件")
    try:
        validate_changelog((root / "CHANGELOG.md").read_text(encoding="utf-8"), version)
    except (OSError, UnicodeDecodeError, UpdateError) as exc:
        raise SystemExit(f"发布更新记录无效：{exc}") from exc

    output.mkdir(parents=True, exist_ok=True)
    archive_path = output / ARCHIVE_NAME
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for relative, git_mode in files:
            info = zipfile.ZipInfo(
                f"aifabei-subsystem-builder/{relative.as_posix()}",
                date_time=(1980, 1, 1, 0, 0, 0),
            )
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            permissions = 0o755 if git_mode & 0o111 else 0o644
            info.external_attr = ((0o100000 | permissions) << 16)
            archive.writestr(info, (root / relative).read_bytes())

    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    release_manifest = {
        "schemaVersion": 1,
        "skillName": metadata["skillName"],
        "skillVersion": version,
        "channel": "stable",
        "archiveUrl": (
            f"https://github.com/{REPOSITORY}/releases/download/{tag}/{ARCHIVE_NAME}"
        ),
        "archiveSha256": digest,
        "sourceCommit": source_commit,
        "releasedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "defaultContractRevision": metadata["defaultContractRevision"],
        "supportedContractRevisions": metadata["supportedContractRevisions"],
    }
    (output / "update-manifest.json").write_text(
        json.dumps(release_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    shutil.copy2(root / "scripts" / "update_skill.py", output / "update_skill.py")
    print(json.dumps({
        "tag": tag,
        "sourceCommit": source_commit,
        "archive": str(archive_path),
        "sha256": digest,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
