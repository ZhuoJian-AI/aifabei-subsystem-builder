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
_ROTATION_OPERATION = re.compile(r"^[a-z0-9][a-z0-9._:-]{7,127}$")
_ROTATION_ENV_KEY = "ZHUOJIAN_STORAGE_ROTATION_OPERATION_ID"


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


@dataclass(frozen=True)
class RotationResult:
    slug: str
    env_path: Path
    operation_id: str
    phase: str
    reused: bool
    grace_until: int | None = None


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
        snapshot_loaded = False
        env_write_attempted = False

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
            snapshot_loaded = True
            existing_token = _read_token_from_env(previous[0]) if previous else None
            existing_rotation = (
                _read_rotation_operation_from_env(previous[0]) if previous else None
            )
            if existing_rotation:
                rotation = connection.execute(
                    """
                    SELECT slug,new_token_hash,state FROM storage_rotations
                    WHERE operation_id=?
                    """,
                    (existing_rotation,),
                ).fetchone()
                if (
                    rotation is None
                    or rotation["slug"] != slug
                    or not existing_token
                    or rotation["new_token_hash"] != _token_hash(existing_token)
                ):
                    raise RegistryError(
                        "application env contains an interrupted storage rotation; "
                        "resume it with its original operation id"
                    )
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

            env_write_attempted = True
            _atomic_write_env(
                env_path,
                _render_env(
                    token,
                    gateway_url,
                    rotation_operation_id=existing_rotation,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            # A validation failure while reading an existing path must be
            # strictly non-destructive.  Restore only after a complete,
            # trusted snapshot was loaded and this transaction attempted to
            # replace the file.
            if snapshot_loaded and env_write_attempted:
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
        """Backward-compatible one-shot rotation built on the durable two-phase API."""

        operation_id = secrets.token_hex(16)
        prepared = self.prepare_rotation(
            slug,
            apps_env_dir,
            gateway_url,
            operation_id=operation_id,
            grace_seconds=grace_seconds,
        )
        self.commit_rotation(slug, apps_env_dir, operation_id=operation_id)
        return ProvisionResult(
            slug=prepared.slug,
            env_path=prepared.env_path,
            status="active",
            reused=False,
        )

    def prepare_rotation(
        self,
        slug: str,
        apps_env_dir: Path,
        gateway_url: str,
        *,
        operation_id: str,
        grace_seconds: int = 300,
    ) -> RotationResult:
        """Install a new token without expiring the token used by the old container.

        The operation id is also written into the root-only env file.  If the
        process is killed after the atomic env replacement but before SQLite
        commits, a retry with that exact id can safely adopt that exact token.
        """

        slug = validate_slug(slug)
        operation_id = validate_rotation_operation_id(operation_id)
        if (
            isinstance(grace_seconds, bool)
            or not isinstance(grace_seconds, int)
            or grace_seconds < 0
            or grace_seconds > 86400
        ):
            raise RegistryError("grace_seconds must be between 0 and 86400")
        env_path = _env_path(apps_env_dir, slug)
        previous: tuple[bytes, int] | None = None
        snapshot_loaded = False
        env_write_attempted = False
        now = int(time.time())

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            application = connection.execute(
                "SELECT state FROM applications WHERE slug = ?", (slug,)
            ).fetchone()
            if application is None:
                raise UnknownApplication(f"unknown application: {slug}")
            if application["state"] != "active":
                raise ApplicationUnavailable(
                    f"application storage is {application['state']}; administrator must explicitly resume or recreate it"
                )

            previous = _read_existing_file(env_path)
            snapshot_loaded = True
            if previous is None:
                raise InvalidCredential("application has no installed credential")
            installed_token = _read_token_from_env(previous[0])
            if not installed_token:
                raise InvalidCredential("application has no installed credential")
            installed_hash = _token_hash(installed_token)
            env_operation_id = _read_rotation_operation_from_env(previous[0])

            operation = connection.execute(
                """
                SELECT slug,new_token_hash,state,grace_seconds,grace_until
                FROM storage_rotations WHERE operation_id=?
                """,
                (operation_id,),
            ).fetchone()
            if operation is not None:
                if operation["slug"] != slug or operation["grace_seconds"] != grace_seconds:
                    raise RegistryError("rotation operation id was already used with different parameters")
                if operation["new_token_hash"] != installed_hash:
                    raise InvalidCredential("prepared rotation env does not match its registered token")
                credential = connection.execute(
                    """
                    SELECT expires_at,revoked_at FROM credentials
                    WHERE token_hash=? AND slug=?
                    """,
                    (installed_hash, slug),
                ).fetchone()
                if (
                    credential is None
                    or credential["revoked_at"] is not None
                    or (
                        credential["expires_at"] is not None
                        and credential["expires_at"] <= now
                    )
                ):
                    raise InvalidCredential("prepared rotation credential is no longer valid")
                if env_operation_id not in {None, operation_id}:
                    raise RegistryError("application env belongs to a different rotation operation")
                connection.commit()
                return RotationResult(
                    slug=slug,
                    env_path=env_path,
                    operation_id=operation_id,
                    phase=operation["state"],
                    reused=True,
                    grace_until=operation["grace_until"],
                )

            pending = connection.execute(
                """
                SELECT operation_id FROM storage_rotations
                WHERE slug=? AND state='prepared'
                """,
                (slug,),
            ).fetchone()
            if pending is not None:
                raise RegistryError(
                    "another storage rotation is already prepared for this application"
                )

            interrupted_env = env_operation_id == operation_id
            if interrupted_env:
                # This is the one recoverable cross-file window: the env was
                # fsync'ed, but the transaction containing both the credential
                # hash and operation row did not commit.
                already_registered = connection.execute(
                    "SELECT 1 FROM credentials WHERE token_hash=?",
                    (installed_hash,),
                ).fetchone()
                if already_registered is not None:
                    raise RegistryError(
                        "interrupted rotation token is already registered without its operation"
                    )
                token = installed_token
            else:
                if env_operation_id:
                    previous_operation = connection.execute(
                        """
                        SELECT slug,new_token_hash,state FROM storage_rotations
                        WHERE operation_id=?
                        """,
                        (env_operation_id,),
                    ).fetchone()
                    if (
                        previous_operation is None
                        or previous_operation["slug"] != slug
                        or previous_operation["new_token_hash"] != installed_hash
                        or previous_operation["state"] != "committed"
                    ):
                        raise RegistryError(
                            "application env belongs to an unfinished rotation operation"
                        )
                current = connection.execute(
                    """
                    SELECT 1 FROM credentials
                    WHERE token_hash=? AND slug=? AND revoked_at IS NULL
                      AND (expires_at IS NULL OR expires_at > ?)
                    """,
                    (installed_hash, slug, now),
                ).fetchone()
                if current is None:
                    raise InvalidCredential("installed application credential is not active")
                token = _new_token()

            token_hash = _token_hash(token)
            connection.execute(
                """
                INSERT INTO credentials(token_hash,slug,created_at,expires_at,revoked_at)
                VALUES(?,?,?,NULL,NULL)
                """,
                (token_hash, slug, now),
            )
            connection.execute(
                """
                INSERT INTO storage_rotations(
                    operation_id,slug,new_token_hash,state,grace_seconds,
                    created_at,committed_at,grace_until
                ) VALUES(?,?,?,'prepared',?,?,NULL,NULL)
                """,
                (operation_id, slug, token_hash, grace_seconds, now),
            )
            connection.execute(
                "UPDATE applications SET updated_at=? WHERE slug=?", (now, slug)
            )
            env_write_attempted = True
            _atomic_write_env(
                env_path,
                _render_env(
                    token,
                    gateway_url,
                    rotation_operation_id=operation_id,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            if snapshot_loaded and env_write_attempted:
                _restore_file(env_path, previous)
            raise
        finally:
            connection.close()

        return RotationResult(
            slug=slug,
            env_path=env_path,
            operation_id=operation_id,
            phase="prepared",
            reused=interrupted_env,
        )

    def commit_rotation(
        self,
        slug: str,
        apps_env_dir: Path,
        *,
        operation_id: str,
    ) -> RotationResult:
        """Expire old credentials only after the replacement container is healthy."""

        slug = validate_slug(slug)
        operation_id = validate_rotation_operation_id(operation_id)
        env_path = _env_path(apps_env_dir, slug)
        now = int(time.time())
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            operation = connection.execute(
                """
                SELECT slug,new_token_hash,state,grace_seconds,grace_until
                FROM storage_rotations WHERE operation_id=?
                """,
                (operation_id,),
            ).fetchone()
            if operation is None:
                raise RegistryError("unknown storage rotation operation")
            if operation["slug"] != slug:
                raise RegistryError("storage rotation operation belongs to another application")
            application = connection.execute(
                "SELECT state FROM applications WHERE slug=?", (slug,)
            ).fetchone()
            if application is None:
                raise UnknownApplication(f"unknown application: {slug}")
            if application["state"] != "active":
                raise ApplicationUnavailable(
                    f"application storage is {application['state']}; rotation cannot be committed"
                )
            installed = _read_existing_file(env_path)
            installed_token = _read_token_from_env(installed[0]) if installed else None
            if not installed_token or _token_hash(installed_token) != operation["new_token_hash"]:
                raise InvalidCredential("prepared rotation env does not match its registered token")
            credential = connection.execute(
                """
                SELECT expires_at,revoked_at FROM credentials
                WHERE token_hash=? AND slug=?
                """,
                (operation["new_token_hash"], slug),
            ).fetchone()
            if (
                credential is None
                or credential["revoked_at"] is not None
                or credential["expires_at"] is not None
            ):
                raise InvalidCredential("prepared rotation credential is no longer current")

            if operation["state"] == "committed":
                connection.commit()
                return RotationResult(
                    slug=slug,
                    env_path=env_path,
                    operation_id=operation_id,
                    phase="committed",
                    reused=True,
                    grace_until=operation["grace_until"],
                )

            grace_seconds = operation["grace_seconds"]
            grace_until = now + grace_seconds
            if grace_seconds:
                connection.execute(
                    """
                    UPDATE credentials
                    SET expires_at=CASE
                        WHEN expires_at IS NULL OR expires_at > ? THEN ?
                        ELSE expires_at
                    END
                    WHERE slug=? AND token_hash<>? AND revoked_at IS NULL
                    """,
                    (grace_until, grace_until, slug, operation["new_token_hash"]),
                )
            else:
                connection.execute(
                    """
                    UPDATE credentials SET revoked_at=?
                    WHERE slug=? AND token_hash<>? AND revoked_at IS NULL
                    """,
                    (now, slug, operation["new_token_hash"]),
                )
            updated = connection.execute(
                """
                UPDATE storage_rotations
                SET state='committed',committed_at=?,grace_until=?
                WHERE operation_id=? AND state='prepared'
                """,
                (now, grace_until, operation_id),
            )
            if updated.rowcount != 1:
                raise RegistryError("storage rotation state changed concurrently")
            connection.execute(
                "UPDATE applications SET updated_at=? WHERE slug=?", (now, slug)
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

        return RotationResult(
            slug=slug,
            env_path=env_path,
            operation_id=operation_id,
            phase="committed",
            reused=False,
            grace_until=grace_until,
        )

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

    def resume_app(self, slug: str, apps_env_dir: Path) -> None:
        """Reactivate only a suspended app whose installed token is still valid."""

        slug = validate_slug(slug)
        env_path = _env_path(apps_env_dir, slug)
        now = int(time.time())
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            application = connection.execute(
                "SELECT state FROM applications WHERE slug = ?", (slug,)
            ).fetchone()
            if application is None:
                raise UnknownApplication(f"unknown application: {slug}")
            if application["state"] == "revoked":
                raise ApplicationUnavailable("revoked application storage cannot be resumed")
            if application["state"] == "active":
                connection.commit()
                return
            installed = _read_existing_file(env_path)
            token = _read_token_from_env(installed[0]) if installed else None
            if not token:
                raise InvalidCredential("suspended application has no installed credential")
            credential = connection.execute(
                """
                SELECT 1 FROM credentials
                WHERE token_hash = ? AND slug = ? AND revoked_at IS NULL
                  AND (expires_at IS NULL OR expires_at > ?)
                """,
                (_token_hash(token), slug, now),
            ).fetchone()
            if credential is None:
                raise InvalidCredential("suspended application credential is no longer valid")
            connection.execute(
                "UPDATE applications SET state = 'active', updated_at = ? WHERE slug = ? AND state = 'suspended'",
                (now, slug),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

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
        _ensure_private_directory(self.db_path.parent)
        if self.db_path.exists() or self.db_path.is_symlink():
            _assert_private_file(self.db_path, "credential registry")
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

                CREATE TABLE IF NOT EXISTS storage_rotations (
                    operation_id TEXT PRIMARY KEY,
                    slug TEXT NOT NULL REFERENCES applications(slug) ON DELETE CASCADE,
                    new_token_hash TEXT NOT NULL REFERENCES credentials(token_hash),
                    state TEXT NOT NULL CHECK(state IN ('prepared', 'committed')),
                    grace_seconds INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    committed_at INTEGER,
                    grace_until INTEGER
                );

                CREATE UNIQUE INDEX IF NOT EXISTS storage_rotations_one_prepared_per_app
                ON storage_rotations(slug) WHERE state='prepared';
                """
            )
        if os.name == "posix":
            os.chmod(self.db_path, 0o600)
            _assert_private_file(self.db_path, "credential registry")

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


def validate_rotation_operation_id(operation_id: str) -> str:
    if not isinstance(operation_id, str) or not _ROTATION_OPERATION.fullmatch(operation_id):
        raise RegistryError(
            "rotation operation id must be 8-128 lowercase letters, numbers, '.', ':', '_' or '-'"
        )
    return operation_id


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_token() -> str:
    return secrets.token_urlsafe(48)


def _env_path(apps_env_dir: Path, slug: str) -> Path:
    return apps_env_dir / f"{slug}.storage.env"


def _render_env(
    token: str,
    gateway_url: str,
    *,
    rotation_operation_id: str | None = None,
) -> bytes:
    url = gateway_url.strip().rstrip("/")
    if not url.startswith(("http://", "https://")) or any(char in url for char in "\r\n"):
        raise RegistryError("gateway_url is invalid")
    if any(char in token for char in "\r\n\x00"):
        raise RegistryError("generated credential is invalid")
    rotation_line = ""
    if rotation_operation_id is not None:
        rotation_line = (
            f"{_ROTATION_ENV_KEY}="
            f"{validate_rotation_operation_id(rotation_operation_id)}\n"
        )
    # The first three names are the v1 contract. The final two are temporary
    # aliases for already-generated subsystems and can be removed after migration.
    return (
        "FILE_STORAGE_DRIVER=oss-gateway\n"
        f"FILE_STORAGE_GATEWAY_URL={url}\n"
        f"FILE_STORAGE_TOKEN={token}\n"
        "FILE_STORAGE_GATEWAY_TIMEOUT_SECONDS=900\n"
        f"STORAGE_GATEWAY_URL={url}\n"
        f"STORAGE_PROJECT_TOKEN={token}\n"
        f"{rotation_line}"
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


def _read_rotation_operation_from_env(content: bytes) -> str | None:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RegistryError("application env is not valid UTF-8") from exc
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        if "=" not in raw_line:
            continue
        name, value = raw_line.split("=", 1)
        values[name.strip()] = value.strip()
    operation_id = values.get(_ROTATION_ENV_KEY)
    if operation_id is None:
        return None
    return validate_rotation_operation_id(operation_id)


def _read_existing_file(path: Path) -> tuple[bytes, int] | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise RegistryError("application env path is not a regular file")
    _assert_private_file(path, "application env")
    file_stat = path.stat()
    return path.read_bytes(), stat.S_IMODE(file_stat.st_mode)


def _atomic_write_env(path: Path, content: bytes) -> None:
    _ensure_private_directory(path.parent)
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


def _ensure_private_directory(path: Path) -> None:
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_dir():
            raise RegistryError(f"private directory is unsafe: {path}")
        if os.name == "posix":
            metadata = path.stat()
            if metadata.st_uid != os.geteuid():
                raise RegistryError(f"private directory has an unexpected owner: {path}")
            if stat.S_IMODE(metadata.st_mode) != 0o700:
                raise RegistryError(f"private directory must have mode 0700: {path}")
        return
    path.mkdir(parents=True, mode=0o700)
    if os.name == "posix":
        os.chmod(path, 0o700)


def _assert_private_file(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise RegistryError(f"{label} path is not a regular file")
    if os.name == "posix":
        metadata = path.stat()
        if metadata.st_uid != os.geteuid():
            raise RegistryError(f"{label} has an unexpected owner")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise RegistryError(f"{label} must have mode 0600")
