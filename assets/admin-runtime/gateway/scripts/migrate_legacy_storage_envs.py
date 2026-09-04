#!/usr/bin/env python3
"""Move legacy per-app storage env files without exposing their contents."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any


MANAGED_BY = "zhuojian-runtime-admin/v1"
SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class MigrationError(RuntimeError):
    pass


def _assert_directory(path: Path, *, private: bool = False) -> None:
    if path.is_symlink() or not path.is_dir():
        raise MigrationError(f"unsafe directory: {path}")
    if os.name == "posix":
        metadata = path.stat()
        if metadata.st_uid != os.geteuid():
            raise MigrationError(f"directory has an unexpected owner: {path}")
        mode = stat.S_IMODE(metadata.st_mode)
        if (private and mode != 0o700) or (not private and mode & 0o022):
            raise MigrationError(f"directory has unsafe permissions: {path}")


def _assert_file(path: Path, *, secret: bool = False) -> int:
    if path.is_symlink() or not path.is_file():
        raise MigrationError(f"unsafe file: {path}")
    metadata = path.stat()
    mode = stat.S_IMODE(metadata.st_mode)
    if os.name == "posix":
        if metadata.st_uid != os.geteuid():
            raise MigrationError(f"file has an unexpected owner: {path}")
        if (secret and mode != 0o600) or (not secret and mode & 0o022):
            raise MigrationError(f"file has unsafe permissions: {path}")
    return mode


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_replace(path: Path, content: bytes, mode: int) -> None:
    temporary = path.parent / f".{path.name}.storage-env-migration-{os.getpid()}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _release_bytes(release: dict[str, Any]) -> bytes:
    return (json.dumps(release, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def migrate(
    legacy_dir: Path,
    target_dir: Path,
    deployments_root: Path,
    *,
    dry_run: bool = False,
) -> dict[str, int | bool]:
    _assert_directory(legacy_dir, private=True)
    _assert_directory(target_dir, private=True)
    _assert_directory(deployments_root)
    plans: list[dict[str, Any]] = []
    already_current = 0

    for deployment in sorted(deployments_root.iterdir()):
        if deployment.is_symlink():
            raise MigrationError(f"symlink deployment directory is not allowed: {deployment}")
        if not deployment.is_dir():
            continue
        release_path = deployment / "release.json"
        if not release_path.exists() and not release_path.is_symlink():
            continue
        release_mode = _assert_file(release_path)
        original = release_path.read_bytes()
        try:
            release = json.loads(original)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MigrationError(f"invalid release record: {release_path}") from exc
        if not isinstance(release, dict) or release.get("managedBy") != MANAGED_BY:
            continue
        slug = release.get("applicationSlug")
        if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug) or slug != deployment.name:
            raise MigrationError(f"managed release has an invalid application slug: {release_path}")
        if release.get("storageMode") != "oss-gateway":
            continue

        legacy = legacy_dir / f"{slug}.storage.env"
        target = target_dir / f"{slug}.storage.env"
        recorded = release.get("storageEnvFile")
        if recorded == str(target):
            _assert_file(target, secret=True)
            if legacy.exists() or legacy.is_symlink():
                raise MigrationError(f"both legacy and current storage env files exist for {slug}")
            already_current += 1
            continue
        if recorded != str(legacy):
            raise MigrationError(f"managed OSS release has an unexpected storage env path: {slug}")

        legacy_exists = legacy.exists() or legacy.is_symlink()
        target_exists = target.exists() or target.is_symlink()
        if legacy_exists and target_exists:
            raise MigrationError(f"both legacy and target storage env files exist for {slug}")
        if not legacy_exists and not target_exists:
            raise MigrationError(f"managed OSS release has no storage env file: {slug}")
        if legacy_exists:
            _assert_file(legacy, secret=True)
        else:
            # Recover an interruption after the secret was moved but before the
            # release record was atomically replaced.
            _assert_file(target, secret=True)
        updated = dict(release)
        updated["storageEnvFile"] = str(target)
        plans.append(
            {
                "slug": slug,
                "legacy": legacy,
                "target": target,
                "release_path": release_path,
                "release_mode": release_mode,
                "original": original,
                "updated": _release_bytes(updated),
                "move_required": legacy_exists,
            }
        )

    if dry_run:
        return {"ok": True, "planned": len(plans), "alreadyCurrent": already_current}

    completed: list[dict[str, Any]] = []
    try:
        for plan in plans:
            moved = False
            try:
                if plan["move_required"]:
                    os.replace(plan["legacy"], plan["target"])
                    _fsync_directory(legacy_dir)
                    _fsync_directory(target_dir)
                    moved = True
                _atomic_replace(
                    plan["release_path"], plan["updated"], plan["release_mode"]
                )
            except BaseException:
                _atomic_replace(
                    plan["release_path"], plan["original"], plan["release_mode"]
                )
                if moved and plan["target"].exists() and not plan["legacy"].exists():
                    os.replace(plan["target"], plan["legacy"])
                    _fsync_directory(legacy_dir)
                    _fsync_directory(target_dir)
                raise
            completed.append(plan)
    except BaseException:
        for plan in reversed(completed):
            _atomic_replace(plan["release_path"], plan["original"], plan["release_mode"])
            if plan["target"].exists() and not plan["legacy"].exists():
                os.replace(plan["target"], plan["legacy"])
                _fsync_directory(legacy_dir)
                _fsync_directory(target_dir)
        raise

    return {"ok": True, "migrated": len(plans), "alreadyCurrent": already_current}


def main() -> int:
    parser = argparse.ArgumentParser(description="migrate managed storage env files")
    parser.add_argument("--legacy-dir", type=Path, default=Path("/etc/zhuojian/apps"))
    parser.add_argument(
        "--target-dir", type=Path, default=Path("/etc/zhuojian/storage-apps")
    )
    parser.add_argument(
        "--deployments-root", type=Path, default=Path("/srv/zhuojian/deployments")
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if os.name == "posix" and os.geteuid() != 0:
        parser.error("migration must run as root")
    try:
        result = migrate(
            args.legacy_dir,
            args.target_dir,
            args.deployments_root,
            dry_run=args.dry_run,
        )
    except (OSError, MigrationError) as exc:
        print(f"storage env migration failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
