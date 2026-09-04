from __future__ import annotations

import hashlib
import os
import re
import secrets
import sqlite3
import stat
import time
from dataclasses import dataclass
from pathlib import Path


_SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class RegistryError(RuntimeError):
    pass


class InvalidApplicationSlug(RegistryError):
    pass


class UnknownApplication(RegistryError):
    pass


class InvalidCredential(RegistryError):
    pass


class ApplicationUnavailable(RegistryError):
    pass


@dataclass(frozen=True)
class ApplicationScope:
    slug: str


@dataclass(frozen=True)
class ProvisionResult:
    slug: str
    env_path: Path
    status: str
    reused: bool


class CredentialRegistry:
    """SQLite registry containing app state and SHA-256 token hashes only."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._prepare_database()

    def resolve(self, token: str) -> ApplicationScope:
        if not token or len(token) > 512 or any(char.isspace() for char in token):
            raise InvalidCredential("invalid bearer credential")
        now = int(time.time())
        token_hash = _token_hash(token)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT a.slug, a.state, c.expires_at, c.revoked_at
                FROM credentials AS c
                JOIN applications AS a ON a.slug = c.slug
                WHERE c.token_hash = ?
                """,
                (token_hash,),
            ).fetchone()
        if row is None:
            raise InvalidCredential("invalid bearer credential")
        if row["revoked_at"] is not None or (
            row["expires_at"] is not None and row["expires_at"] <= now
        ):
            raise InvalidCredential("expired bearer credential")
        if row["state"] != "active":
            raise ApplicationUnavailable("application storage is not active")
        return ApplicationScope(slug=row["slug"])

    def ensure_app(self, slug: str, apps_env_dir: Path, gateway_url: str) -> ProvisionResult:
        slug = validate_slug(slug)
        env_path = _env_path(apps_env_dir, slug)
        now = int(time.time())
        previous: tuple[bytes, int] | None = None

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            application = connection.execute(
                "SELECT state FROM applications WHERE slug = ?", (slug,)
            ).fetchone()
            if application is not None and application["state"] != "active":
                raise ApplicationUnavailable(
                    f"application storage is {application['state']}; administrator action is required"
                )
            # Read the env only after the database write lock is held.  This
            # makes two concurrent first deployments converge on one token.
            previous = _read_existing_file(env_path)
            existing_token = _read_token_from_env(previous[0]) if previous else None
            reusable = False
            if existing_token:
                credential = connection.execute(
                    """
                    SELECT 1 FROM credentials
                    WHERE token_hash = ? AND slug = ? AND revoked_at IS NULL
                      AND (expires_at IS NULL OR expires_at > ?)
                    """,
                    (_token_hash(existing_token), slug, now),
                ).fetchone()
                reusable = credential is not None

            token = existing_token if reusable else _new_token()
            if application is None:
                connection.execute(
                    "INSERT INTO applications(slug, state, created_at, updated_at) VALUES (?, 'active', ?, ?)",
                    (slug, now, now),
                )
            else:
                connection.execute(
                    "UPDATE applications SET updated_at = ? WHERE slug = ?",
                    (now, slug),
                )

            if not reusable:
                connection.execute(
                    "UPDATE credentials SET revoked_at = ? WHERE slug = ? AND revoked_at IS NULL",
                    (now, slug),
                )
                connection.execute(
                    """
                    INSERT INTO credentials(token_hash, slug, created_at, expires_at, revoked_at)
                    VALUES (?, ?, ?, NULL, NULL)
                    """,
                    (_token_hash(token), slug, now),
                )

            _atomic_write_env(env_path, _render_env(token, gateway_url))
            connection.commit()
        except Exception:
            connection.rollback()
            _restore_file(env_path, previous)
            raise
        finally:
            connection.close()

        return ProvisionResult(
            slug=slug,
            env_path=env_path,
            status="active",
            reused=reusable,
        )

    def rotate_app(
        self,
        slug: str,
        apps_env_dir: Path,
        gateway_url: str,
        *,
        grace_seconds: int = 300,
    ) -> ProvisionResult:
        slug = validate_slug(slug)
        if grace_seconds < 0 or grace_seconds > 86400:
            raise RegistryError("grace_seconds must be between 0 and 86400")
        env_path = _env_path(apps_env_dir, slug)
        previous = _read_existing_file(env_path)
        token = _new_token()
        now = int(time.time())
        grace_deadline = now + grace_seconds

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            application = connection.execute(
                "SELECT 1 FROM applications WHERE slug = ?", (slug,)
            ).fetchone()
            if application is None:
                raise UnknownApplication(f"unknown application: {slug}")
            if grace_seconds:
                connection.execute(
                    """
                    UPDATE credentials
                    SET expires_at = CASE
                        WHEN expires_at IS NULL OR expires_at > ? THEN ?
                        ELSE expires_at
                    END
                    WHERE slug = ? AND revoked_at IS NULL
                    """,
                    (grace_deadline, grace_deadline, slug),
                )
            else:
                connection.execute(
                    "UPDATE credentials SET revoked_at = ? WHERE slug = ? AND revoked_at IS NULL",
                    (now, slug),
                )
            connection.execute(
                """
                INSERT INTO credentials(token_hash, slug, created_at, expires_at, revoked_at)
                VALUES (?, ?, ?, NULL, NULL)
                """,
                (_token_hash(token), slug, now),
            )
            connection.execute(
                "UPDATE applications SET state = 'active', updated_at = ? WHERE slug = ?",
                (now, slug),
            )
            _atomic_write_env(env_path, _render_env(token, gateway_url))
            connection.commit()
        except Exception:
            connection.rollback()
            _restore_file(env_path, previous)
            raise
        finally:
            connection.close()

        return ProvisionResult(slug=slug, env_path=env_path, status="active", reused=False)

    def suspend_app(self, slug: str) -> None:
        slug = validate_slug(slug)
        now = int(time.time())
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE applications SET state = 'suspended', updated_at = ? WHERE slug = ?",
                (now, slug),
            )
            if cursor.rowcount != 1:
                raise UnknownApplication(f"unknown application: {slug}")

    def revoke_app(self, slug: str, apps_env_dir: Path) -> None:
        slug = validate_slug(slug)
        env_path = _env_path(apps_env_dir, slug)
        previous = _read_existing_file(env_path)
        now = int(time.time())
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE applications SET state = 'revoked', updated_at = ? WHERE slug = ?",
                (now, slug),
            )
            if cursor.rowcount != 1:
                raise UnknownApplication(f"unknown application: {slug}")
            connection.execute(
                "UPDATE credentials SET revoked_at = ? WHERE slug = ? AND revoked_at IS NULL",
                (now, slug),
            )
            if env_path.exists():
                if env_path.is_symlink() or not env_path.is_file():
                    raise RegistryError("application env path is not a regular file")
                env_path.unlink()
                _fsync_directory(env_path.parent)
            connection.commit()
        except Exception:
            connection.rollback()
            _restore_file(env_path, previous)
            raise
        finally:
            connection.close()

    def healthcheck(self) -> None:
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()

    def _prepare_database(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            os.chmod(self.db_path.parent, 0o700)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS applications (
                    slug TEXT PRIMARY KEY,
                    state TEXT NOT NULL CHECK(state IN ('active', 'suspended', 'revoked')),
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS credentials (
                    token_hash TEXT PRIMARY KEY,
                    slug TEXT NOT NULL REFERENCES applications(slug) ON DELETE CASCADE,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER,
                    revoked_at INTEGER
                );

                CREATE INDEX IF NOT EXISTS credentials_slug_idx ON credentials(slug);
                """
            )
        if os.name == "posix":
            os.chmod(self.db_path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection


def validate_slug(slug: str) -> str:
    value = slug.strip()
    if not _SLUG.fullmatch(value):
        raise InvalidApplicationSlug(
            "application slug must be 1-63 lowercase letters, numbers, or interior hyphens"
        )
    return value


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_token() -> str:
    return secrets.token_urlsafe(48)


def _env_path(apps_env_dir: Path, slug: str) -> Path:
    return apps_env_dir / f"{slug}.storage.env"


def _render_env(token: str, gateway_url: str) -> bytes:
    url = gateway_url.strip().rstrip("/")
    if not url.startswith(("http://", "https://")) or any(char in url for char in "\r\n"):
        raise RegistryError("gateway_url is invalid")
    if any(char in token for char in "\r\n\x00"):
        raise RegistryError("generated credential is invalid")
    # The first three names are the v1 contract. The final two are temporary
    # aliases for already-generated subsystems and can be removed after migration.
    return (
        "FILE_STORAGE_DRIVER=oss-gateway\n"
        f"FILE_STORAGE_GATEWAY_URL={url}\n"
        f"FILE_STORAGE_TOKEN={token}\n"
        f"STORAGE_GATEWAY_URL={url}\n"
        f"STORAGE_PROJECT_TOKEN={token}\n"
    ).encode("utf-8")


def _read_token_from_env(content: bytes) -> str | None:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return None
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        if "=" not in raw_line:
            continue
        name, value = raw_line.split("=", 1)
        values[name.strip()] = value.strip()
    return values.get("FILE_STORAGE_TOKEN") or values.get("STORAGE_PROJECT_TOKEN")


def _read_existing_file(path: Path) -> tuple[bytes, int] | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise RegistryError("application env path is not a regular file")
    file_stat = path.stat()
    if os.name == "posix" and stat.S_IMODE(file_stat.st_mode) & 0o077:
        raise RegistryError("application env file permissions are too broad")
    return path.read_bytes(), stat.S_IMODE(file_stat.st_mode)


def _atomic_write_env(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        os.chmod(path.parent, 0o700)
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise RegistryError("application env path is not a regular file")
    temporary = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def _restore_file(path: Path, previous: tuple[bytes, int] | None) -> None:
    try:
        if previous is None:
            if path.exists() and path.is_file() and not path.is_symlink():
                path.unlink()
                _fsync_directory(path.parent)
            return
        _atomic_write_env(path, previous[0])
        os.chmod(path, previous[1])
    except OSError:
        # The next ensure-app call repairs an interrupted cross-file transaction.
        pass


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
