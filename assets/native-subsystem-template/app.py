from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import threading
import time
from contextlib import contextmanager, suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlsplit
from uuid import UUID, uuid4

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows-only development fallback
    fcntl = None

import jwt
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware

from storage import (
    InvalidStorageKey,
    StorageAuthorizationError,
    StorageError,
    StorageObjectNotFound,
    StorageUnavailableError,
    storage_for_backend,
    storage_from_env,
)

ROOT = Path(__file__).resolve().parent
MANIFEST = json.loads((ROOT / "subsystem.json").read_text(encoding="utf-8"))
APP_SLUG = MANIFEST["applicationSlug"]
MODULES = {item["moduleKey"]: item for item in MANIFEST["modules"]}
ACTIONS = {
    action["actionKey"]: {**action, "moduleKey": module["moduleKey"]}
    for module in MANIFEST["modules"] for action in module["actions"]
}
PAGES = {
    page["pageKey"]: {**page, "moduleKey": module["moduleKey"]}
    for module in MANIFEST["modules"] for page in module["pages"]
}
DB_PATH = os.getenv("DATABASE_PATH", str(ROOT / "subsystem.db"))
INTEGRATION_SECRET = os.getenv("ZHUOJIAN_INTEGRATION_SECRET", "")
SESSION_SECRET = os.getenv("SESSION_SECRET", "")
EXPECTED_ORGANIZATION_ID = os.getenv("ZHUOJIAN_ORGANIZATION_ID", "")


def canonical_https_origin(value: str, label: str) -> str:
    if not value or value != value.strip() or len(value) > 2048:
        raise RuntimeError(f"{label} must be one HTTPS origin")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise RuntimeError(f"{label} must be one HTTPS origin") from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError(f"{label} must be one HTTPS origin")
    hostname = parsed.hostname.lower()
    if ":" in hostname:
        hostname = f"[{hostname}]"
    authority = hostname if port in {None, 443} else f"{hostname}:{port}"
    return f"https://{authority}"


PUBLIC_ORIGIN = canonical_https_origin(
    os.getenv("ZHUOJIAN_PUBLIC_ORIGIN", ""),
    "ZHUOJIAN_PUBLIC_ORIGIN",
)
SAAS_ORIGINS = [
    canonical_https_origin(item.strip(), "ZHUOJIAN_SAAS_ORIGINS")
    for item in os.getenv(
        "ZHUOJIAN_SAAS_ORIGINS", "https://ai-platform.staging.zhuojianai.com"
    ).split(",")
    if item.strip()
]
try:
    FILE_STORAGE_MAX_UPLOAD_BYTES = int(os.getenv("FILE_STORAGE_MAX_UPLOAD_BYTES", str(512 * 1024 * 1024)))
except ValueError as exc:
    raise RuntimeError("FILE_STORAGE_MAX_UPLOAD_BYTES must be an integer") from exc
if FILE_STORAGE_MAX_UPLOAD_BYTES <= 0:
    raise RuntimeError("FILE_STORAGE_MAX_UPLOAD_BYTES must be greater than zero")
try:
    FILE_STORAGE_UPLOAD_LOCK_TIMEOUT_SECONDS = float(
        os.getenv("FILE_STORAGE_UPLOAD_LOCK_TIMEOUT_SECONDS", "30")
    )
except ValueError as exc:
    raise RuntimeError("FILE_STORAGE_UPLOAD_LOCK_TIMEOUT_SECONDS must be a number") from exc
if FILE_STORAGE_UPLOAD_LOCK_TIMEOUT_SECONDS <= 0:
    raise RuntimeError("FILE_STORAGE_UPLOAD_LOCK_TIMEOUT_SECONDS must be greater than zero")
try:
    FILE_STORAGE_RECOVERY_GRACE_SECONDS = int(
        os.getenv("FILE_STORAGE_RECOVERY_GRACE_SECONDS", "1800")
    )
    FILE_STORAGE_RECOVERY_IO_TIMEOUT_SECONDS = float(
        os.getenv("FILE_STORAGE_RECOVERY_IO_TIMEOUT_SECONDS", "5")
    )
except ValueError as exc:
    raise RuntimeError("file storage recovery settings must be numeric") from exc
if FILE_STORAGE_RECOVERY_GRACE_SECONDS < 1800:
    raise RuntimeError("FILE_STORAGE_RECOVERY_GRACE_SECONDS must be at least 1800")
if FILE_STORAGE_RECOVERY_IO_TIMEOUT_SECONDS <= 0:
    raise RuntimeError("FILE_STORAGE_RECOVERY_IO_TIMEOUT_SECONDS must be greater than zero")

FILE_STORAGE: dict[str, Any] = {}
PRIMARY_STORAGE_BACKEND: str | None = None
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
STABLE_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
CONFIRMATION_MAX_AGE_SECONDS = 5 * 60
ACTION_LEASE_SECONDS = 30
DELETION_RECOVERY_INTERVAL_SECONDS = 5
DELETION_RETRY_SECONDS = 5
STORAGE_RECOVERY_BATCH_SIZE = 10
STORAGE_RECOVERY_LOCK_YIELD_SECONDS = 0.1
JWT_MAX_LIFETIME_SECONDS = {
    "zhuojian-sso": 120,
    "zhuojian-action": 60,
    "zhuojian-event": 60,
}
LOGGER = logging.getLogger(__name__)
_UPLOAD_THREAD_LOCK = threading.Lock()
DELETION_RECOVERY_TASK: asyncio.Task[None] | None = None

if len(INTEGRATION_SECRET) < 32 or len(SESSION_SECRET) < 32 or not EXPECTED_ORGANIZATION_ID:
    raise RuntimeError(
        "ZHUOJIAN_INTEGRATION_SECRET and SESSION_SECRET must each contain at least 32 characters, "
        "and ZHUOJIAN_ORGANIZATION_ID is required"
    )

app = FastAPI(title=MANIFEST["applicationName"], docs_url=None, redoc_url=None)
app.add_middleware(
    SessionMiddleware, secret_key=SESSION_SECRET, https_only=True,
    same_site="lax", max_age=8 * 60 * 60,
)


@contextmanager
def db():
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def storage_adapter(backend: str | None = None):
    """Resolve the primary or recorded migration backend without fallback."""

    global PRIMARY_STORAGE_BACKEND
    if PRIMARY_STORAGE_BACKEND is None:
        primary = storage_from_env()
        PRIMARY_STORAGE_BACKEND = primary.backend
        FILE_STORAGE[primary.backend] = primary
    selected = {
        None: PRIMARY_STORAGE_BACKEND,
        "local-managed": "local",
        "oss": "oss-gateway",
        "oss_gateway": "oss-gateway",
    }.get(backend, backend)
    if selected not in FILE_STORAGE:
        FILE_STORAGE[selected] = storage_for_backend(str(selected))
    return FILE_STORAGE[selected]


def upload_spool_dir() -> Path:
    target = Path(os.getenv("FILE_STORAGE_SPOOL_DIR") or "/data/files/.tmp")
    target.mkdir(parents=True, exist_ok=True)
    return target


def upload_lock_path() -> Path:
    configured = os.getenv("FILE_STORAGE_UPLOAD_LOCK_FILE", "").strip()
    if configured:
        return Path(configured)
    # Standalone development still gets process-wide serialization. Production
    # Runtime always injects one stable, host-shared inode under /run/zhuojian.
    target = upload_spool_dir() / ".upload.lock"
    try:
        target.touch(mode=0o600, exist_ok=True)
    except OSError as exc:
        raise HTTPException(503, "File upload coordination is unavailable") from exc
    return target


def acquire_upload_lock() -> int | None:
    """Serialize disk-consuming uploads across every Runtime-managed container."""

    if fcntl is None:
        if not _UPLOAD_THREAD_LOCK.acquire(timeout=FILE_STORAGE_UPLOAD_LOCK_TIMEOUT_SECONDS):
            raise HTTPException(503, "Another file upload is still using shared storage")
        return None
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(upload_lock_path(), flags)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("upload lock is not a regular file")
        deadline = time.monotonic() + FILE_STORAGE_UPLOAD_LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return descriptor
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise HTTPException(503, "Another file upload is still using shared storage")
                time.sleep(0.1)
    except HTTPException:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise HTTPException(503, "File upload coordination is unavailable") from exc


def release_upload_lock(descriptor: int | None) -> None:
    if fcntl is None:
        _UPLOAD_THREAD_LOCK.release()
        return
    if descriptor is None:  # pragma: no cover - defensive production guard
        return
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def require_upload_capacity(incoming_bytes: int = 0, *, copies: int = 1) -> None:
    """Enforce the Runtime disk gate for local files and OSS buffering."""

    state_path = os.getenv("FILE_STORAGE_STATE_FILE", "").strip()
    minimum_free_bytes = 5 * 1024**3
    stop_upload_used_percent = 90
    if state_path:
        try:
            raw = Path(state_path).read_bytes()
            if len(raw) > 64 * 1024:
                raise ValueError("state file is too large")
            state = json.loads(raw)
            if not isinstance(state, dict):
                raise ValueError("state file must contain an object")
            thresholds = state.get("thresholds") or {}
            if not isinstance(thresholds, dict):
                raise ValueError("state file thresholds must contain an object")
            minimum_free_gib = thresholds.get("minimumFreeGiB", 5)
            stop_upload_used_percent = thresholds.get("stopUploadUsedPercent", 90)
            if (
                isinstance(minimum_free_gib, bool)
                or not isinstance(minimum_free_gib, int)
                or minimum_free_gib < 1
                or isinstance(stop_upload_used_percent, bool)
                or not isinstance(stop_upload_used_percent, int)
                or not 1 <= stop_upload_used_percent < 100
            ):
                raise ValueError("state file contains invalid storage thresholds")
            minimum_free_bytes = minimum_free_gib * 1024**3
            if state.get("uploadsAllowed") is not True:
                raise HTTPException(507, "File storage space is insufficient; contact an administrator")
        except HTTPException:
            raise
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise HTTPException(503, "File storage capacity status is unavailable") from exc
    try:
        usage = shutil.disk_usage(upload_spool_dir())
    except OSError as exc:
        raise HTTPException(503, "File storage capacity status is unavailable") from exc
    if incoming_bytes < 0 or copies < 1:
        raise ValueError("capacity checks require non-negative bytes and at least one copy")
    if (
        usage.total <= 0
        or usage.used < 0
        or usage.free < 0
        or usage.used * 100 >= stop_upload_used_percent * usage.total
        or usage.free - (incoming_bytes * copies) < minimum_free_bytes
    ):
        raise HTTPException(507, "File storage space is insufficient; contact an administrator")


def init_db() -> None:
    with db() as connection:
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS records (
          id TEXT PRIMARY KEY, module_key TEXT NOT NULL, data TEXT NOT NULL,
          department_id TEXT, created_by TEXT,
          status TEXT NOT NULL DEFAULT 'draft', version INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS request_results (
          request_id TEXT PRIMARY KEY, action_key TEXT NOT NULL,
          request_hash TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'completed',
          result TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT '',
          lease_owner TEXT, lease_expires_at TEXT
        );
        CREATE TABLE IF NOT EXISTS outbox (
          sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
          event_type TEXT NOT NULL, module_key TEXT NOT NULL, entity_type TEXT NOT NULL,
          entity_id TEXT NOT NULL, occurred_at TEXT NOT NULL, payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS consumed_tickets (jti TEXT PRIMARY KEY, expires_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS consumed_confirmations (
          confirmation_id TEXT PRIMARY KEY, consumed_at TEXT NOT NULL,
          request_id TEXT, action_key TEXT
        );
        CREATE TABLE IF NOT EXISTS page_confirmations (
          confirmation_id TEXT PRIMARY KEY, request_id TEXT NOT NULL,
          action_key TEXT NOT NULL, request_hash TEXT NOT NULL,
          actor TEXT NOT NULL, params_hash TEXT NOT NULL,
          confirmed_at TEXT NOT NULL, consumed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS event_deliveries (
          delivery_id TEXT PRIMARY KEY, event_id TEXT NOT NULL UNIQUE, event_type TEXT NOT NULL,
          source_application_slug TEXT NOT NULL DEFAULT '', target_module_key TEXT NOT NULL DEFAULT '',
          request_hash TEXT NOT NULL DEFAULT '', result TEXT NOT NULL, received_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS stored_files (
          storage_key TEXT PRIMARY KEY, file_id TEXT NOT NULL UNIQUE,
          module_key TEXT NOT NULL, original_name TEXT NOT NULL,
          mime_type TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL,
          storage_backend TEXT NOT NULL, created_by TEXT NOT NULL,
          business_type TEXT, business_id TEXT,
          deletion_state TEXT NOT NULL DEFAULT 'active', version INTEGER NOT NULL DEFAULT 1,
          deletion_owner TEXT, recovery_after TEXT,
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_stored_files_module_created
          ON stored_files(module_key, created_at DESC);
        """)
        request_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(request_results)")
        }
        request_additions = {
            "request_hash": "TEXT NOT NULL DEFAULT ''",
            "state": "TEXT NOT NULL DEFAULT 'completed'",
            "updated_at": "TEXT NOT NULL DEFAULT ''",
            "lease_owner": "TEXT",
            "lease_expires_at": "TEXT",
        }
        for column, declaration in request_additions.items():
            if column not in request_columns:
                connection.execute(
                    f"ALTER TABLE request_results ADD COLUMN {column} {declaration}"
                )
        connection.execute(
            "UPDATE request_results SET state='completed' WHERE state='' OR state IS NULL"
        )
        connection.execute(
            "UPDATE request_results SET updated_at=created_at WHERE updated_at='' OR updated_at IS NULL"
        )
        event_delivery_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(event_deliveries)")
        }
        event_delivery_additions = {
            "source_application_slug": "TEXT NOT NULL DEFAULT ''",
            "target_module_key": "TEXT NOT NULL DEFAULT ''",
            "request_hash": "TEXT NOT NULL DEFAULT ''",
        }
        for column, declaration in event_delivery_additions.items():
            if column not in event_delivery_columns:
                connection.execute(
                    f"ALTER TABLE event_deliveries ADD COLUMN {column} {declaration}"
                )
        confirmation_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(consumed_confirmations)")
        }
        for column in ("request_id", "action_key"):
            if column not in confirmation_columns:
                connection.execute(
                    f"ALTER TABLE consumed_confirmations ADD COLUMN {column} TEXT"
                )
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(stored_files)")
        }
        additions = {
            "file_id": "TEXT",
            "business_type": "TEXT",
            "business_id": "TEXT",
            "deletion_state": "TEXT NOT NULL DEFAULT 'active'",
            "version": "INTEGER NOT NULL DEFAULT 1",
            "deletion_owner": "TEXT",
            "recovery_after": "TEXT",
        }
        for column, declaration in additions.items():
            if column not in columns:
                connection.execute(
                    f"ALTER TABLE stored_files ADD COLUMN {column} {declaration}"
                )
        connection.execute(
            "UPDATE stored_files SET file_id=lower(hex(randomblob(16))) WHERE file_id IS NULL OR file_id=''"
        )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_stored_files_file_id ON stored_files(file_id)"
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_stored_files_recovery
            ON stored_files(deletion_state,recovery_after,created_at)
            """
        )
        record_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(records)").fetchall()
        }
        missing_scope_columns = {"department_id", "created_by"} - record_columns
        if missing_scope_columns:
            existing_records = int(
                connection.execute("SELECT COUNT(*) FROM records").fetchone()[0]
            )
            if existing_records:
                raise RuntimeError(
                    "Existing records need an explicit department_id/created_by backfill before "
                    "enabling platform role data scopes"
                )
            if "department_id" in missing_scope_columns:
                connection.execute("ALTER TABLE records ADD COLUMN department_id TEXT")
            if "created_by" in missing_scope_columns:
                connection.execute("ALTER TABLE records ADD COLUMN created_by TEXT")


class FileDeleteLeaseLost(RuntimeError):
    """A different worker owns or already completed this delete."""


def lease_expiry(value: Any) -> datetime:
    try:
        return datetime.fromisoformat(str(value)).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return datetime.fromtimestamp(0, timezone.utc)


def recovery_storage_adapter(backend: str):
    if backend not in {"oss", "oss_gateway", "oss-gateway"}:
        return storage_adapter(backend)
    environment = dict(os.environ)
    environment["FILE_STORAGE_GATEWAY_TIMEOUT_SECONDS"] = str(
        FILE_STORAGE_RECOVERY_IO_TIMEOUT_SECONDS
    )
    return storage_for_backend(backend, environment)


def mark_file_delete_retryable(request_id: str, lease_owner: str) -> None:
    retry_at = datetime.now(timezone.utc) + timedelta(seconds=DELETION_RETRY_SECONDS)
    with db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            UPDATE request_results
            SET state='retryable',updated_at=?,lease_expires_at=?
            WHERE request_id=? AND state='in_progress' AND lease_owner=?
            """,
            (datetime.now(timezone.utc).isoformat(), retry_at.isoformat(), request_id, lease_owner),
        )


def defer_upload_recovery(file_id: str, ready_at: datetime) -> None:
    with db() as connection:
        connection.execute(
            """
            UPDATE stored_files SET recovery_after=?
            WHERE file_id=? AND deletion_state='uploading'
            """,
            (ready_at.astimezone(timezone.utc).isoformat(), file_id),
        )


def finalize_file_delete(
    *,
    file_id: str,
    expected_version: int,
    request_id: str,
    lease_owner: str,
) -> dict[str, Any]:
    result = {
        "fileId": file_id,
        "deleted": True,
        "version": expected_version + 1,
    }
    with db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        request_row = connection.execute(
            "SELECT state,lease_owner FROM request_results WHERE request_id=?",
            (request_id,),
        ).fetchone()
        if request_row is not None and request_row["state"] == "completed":
            return result
        if (
            request_row is None
            or request_row["state"] != "in_progress"
            or request_row["lease_owner"] != lease_owner
        ):
            raise FileDeleteLeaseLost("file delete request lease was superseded")
        finalized_file = connection.execute(
            """
            UPDATE stored_files
            SET deletion_state='deleted',version=version+1,deletion_owner=NULL
            WHERE file_id=? AND version=? AND deletion_state='pending' AND deletion_owner=?
            """,
            (file_id, expected_version, lease_owner),
        )
        if finalized_file.rowcount != 1:
            raise FileDeleteLeaseLost("file delete metadata lease was superseded")
        finalized_request = connection.execute(
            """
            UPDATE request_results
            SET state='completed',result=?,updated_at=?,lease_owner=NULL,lease_expires_at=NULL
            WHERE request_id=? AND state='in_progress' AND lease_owner=?
            """,
            (
                json.dumps(result, ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(),
                request_id,
                lease_owner,
            ),
        )
        if finalized_request.rowcount != 1:
            raise FileDeleteLeaseLost("file delete request lease was superseded")
    return result


def claim_pending_file_delete(file_id: str) -> dict[str, Any] | None:
    now = datetime.now(timezone.utc)
    new_owner = uuid4().hex
    new_expiry = (now + timedelta(seconds=ACTION_LEASE_SECONDS)).isoformat()
    with db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT sf.file_id,sf.storage_key,sf.storage_backend,sf.version,
                   sf.deletion_owner,rr.request_id,rr.action_key,rr.request_hash,
                   rr.state AS request_state,rr.lease_expires_at
            FROM stored_files AS sf
            LEFT JOIN request_results AS rr ON rr.lease_owner=sf.deletion_owner
            WHERE sf.file_id=? AND sf.deletion_state='pending'
            LIMIT 1
            """,
            (file_id,),
        ).fetchone()
        if row is None:
            return None
        old_owner = row["deletion_owner"]
        request_id = row["request_id"]
        if (
            request_id
            and row["request_state"] in {"in_progress", "retryable"}
            and lease_expiry(row["lease_expires_at"]) <= now
        ):
            claimed_request = connection.execute(
                """
                UPDATE request_results
                SET state='in_progress',updated_at=?,lease_owner=?,lease_expires_at=?
                WHERE request_id=? AND state=? AND COALESCE(lease_owner,'')=?
                """,
                (
                    now.isoformat(),
                    new_owner,
                    new_expiry,
                    request_id,
                    row["request_state"],
                    old_owner or "",
                ),
            )
            if claimed_request.rowcount != 1:
                return None
        elif request_id:
            return None
        else:
            request_id = f"recovery-{uuid4().hex}"
            request_hash = hashlib.sha256(
                f"file-delete-recovery:{file_id}:{row['storage_key']}".encode()
            ).hexdigest()
            connection.execute(
                """
                INSERT INTO request_results(
                  request_id,action_key,request_hash,state,result,created_at,updated_at,
                  lease_owner,lease_expires_at
                ) VALUES(?,'internal.file.delete',?,'in_progress','{}',?,?,?,?)
                """,
                (
                    request_id,
                    request_hash,
                    now.isoformat(),
                    now.isoformat(),
                    new_owner,
                    new_expiry,
                ),
            )
        if old_owner is None:
            claimed_file = connection.execute(
                """
                UPDATE stored_files SET deletion_owner=?
                WHERE file_id=? AND deletion_state='pending' AND deletion_owner IS NULL
                """,
                (new_owner, file_id),
            )
        else:
            claimed_file = connection.execute(
                """
                UPDATE stored_files SET deletion_owner=?
                WHERE file_id=? AND deletion_state='pending' AND deletion_owner=?
                """,
                (new_owner, file_id, old_owner),
            )
        if claimed_file.rowcount != 1:
            raise FileDeleteLeaseLost("pending file delete was claimed concurrently")
        return {
            "file_id": file_id,
            "storage_key": row["storage_key"],
            "storage_backend": row["storage_backend"],
            "version": int(row["version"]),
            "request_id": str(request_id),
            "lease_owner": new_owner,
        }


def reconcile_uploading_files(limit: int = STORAGE_RECOVERY_BATCH_SIZE) -> int:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= STORAGE_RECOVERY_BATCH_SIZE:
        raise ValueError("upload recovery limit is outside the bounded batch size")
    local_adapter = storage_adapter("local")
    cleaner = getattr(local_adapter, "cleanup_stale_uploads", None)
    if callable(cleaner):
        cleaner(FILE_STORAGE_RECOVERY_GRACE_SECONDS)
    now = datetime.now(timezone.utc)
    with db() as connection:
        rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT file_id,storage_key,storage_backend,size,sha256,created_at
                FROM stored_files
                WHERE deletion_state='uploading'
                  AND (recovery_after IS NULL OR recovery_after<=?)
                ORDER BY COALESCE(recovery_after,created_at),created_at,file_id
                LIMIT ?
                """,
                (now.isoformat(), limit),
            )
        ]
    for row in rows:
        grace_ready_at = lease_expiry(row["created_at"]) + timedelta(
            seconds=FILE_STORAGE_RECOVERY_GRACE_SECONDS
        )
        old_enough = (
            now - lease_expiry(row["created_at"])
        ).total_seconds() >= FILE_STORAGE_RECOVERY_GRACE_SECONDS
        try:
            adapter = recovery_storage_adapter(row["storage_backend"])
            metadata = adapter.stat(row["storage_key"])
        except StorageObjectNotFound:
            if not old_enough:
                defer_upload_recovery(row["file_id"], grace_ready_at)
                continue
            with db() as connection:
                connection.execute(
                    "DELETE FROM stored_files WHERE file_id=? AND deletion_state='uploading'",
                    (row["file_id"],),
                )
            continue
        except StorageError:
            defer_upload_recovery(
                row["file_id"], datetime.now(timezone.utc) + timedelta(seconds=DELETION_RETRY_SECONDS)
            )
            continue
        matches = (
            metadata.storage_key == row["storage_key"]
            and metadata.backend == adapter.backend
            and int(metadata.size) == int(row["size"])
            and bool(metadata.sha256)
            and hmac.compare_digest(str(metadata.sha256), str(row["sha256"]))
        )
        if matches:
            with db() as connection:
                connection.execute(
                    """
                    UPDATE stored_files SET deletion_state='active',recovery_after=NULL
                    WHERE file_id=? AND deletion_state='uploading'
                    """,
                    (row["file_id"],),
                )
            continue
        if not old_enough:
            defer_upload_recovery(row["file_id"], grace_ready_at)
            continue
        try:
            adapter.delete(row["storage_key"])
        except StorageError:
            defer_upload_recovery(
                row["file_id"], datetime.now(timezone.utc) + timedelta(seconds=DELETION_RETRY_SECONDS)
            )
            continue
        with db() as connection:
            connection.execute(
                "DELETE FROM stored_files WHERE file_id=? AND deletion_state='uploading'",
                (row["file_id"],),
            )
    return len(rows)


async def recover_uploads_once() -> None:
    # Never hold the machine-wide upload lock across a whole remote-OSS batch:
    # one black-holed HEAD is bounded to the recovery I/O timeout, then normal
    # uploads get a scheduling window before the next recovery item.
    for index in range(STORAGE_RECOVERY_BATCH_SIZE):
        upload_lock = await run_in_threadpool(acquire_upload_lock)
        try:
            processed = await run_in_threadpool(reconcile_uploading_files, 1)
        finally:
            release_upload_lock(upload_lock)
        if processed == 0:
            break
        if index + 1 < STORAGE_RECOVERY_BATCH_SIZE:
            await asyncio.sleep(STORAGE_RECOVERY_LOCK_YIELD_SECONDS)


async def recover_pending_deletions_once() -> None:
    now = datetime.now(timezone.utc).isoformat()
    with db() as connection:
        file_ids = [
            row["file_id"]
            for row in connection.execute(
                """
                SELECT sf.file_id
                FROM stored_files AS sf
                LEFT JOIN request_results AS rr ON rr.lease_owner=sf.deletion_owner
                WHERE sf.deletion_state='pending'
                  AND (
                    sf.deletion_owner IS NULL
                    OR rr.request_id IS NULL
                    OR (
                      rr.state IN ('in_progress','retryable')
                      AND COALESCE(rr.lease_expires_at,'')<=?
                    )
                  )
                ORDER BY COALESCE(rr.lease_expires_at,sf.created_at),sf.created_at,sf.file_id
                LIMIT ?
                """,
                (now, STORAGE_RECOVERY_BATCH_SIZE),
            )
        ]
    for file_id in file_ids:
        try:
            claimed = claim_pending_file_delete(file_id)
        except FileDeleteLeaseLost:
            continue
        if claimed is None:
            continue
        try:
            await run_in_threadpool(
                recovery_storage_adapter(claimed["storage_backend"]).delete,
                claimed["storage_key"],
            )
        except StorageError:
            mark_file_delete_retryable(claimed["request_id"], claimed["lease_owner"])
            continue
        try:
            finalize_file_delete(
                file_id=claimed["file_id"],
                expected_version=claimed["version"],
                request_id=claimed["request_id"],
                lease_owner=claimed["lease_owner"],
            )
        except FileDeleteLeaseLost:
            continue


async def storage_recovery_loop() -> None:
    while True:
        try:
            await recover_uploads_once()
            await recover_pending_deletions_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("background file storage recovery failed")
        await asyncio.sleep(DELETION_RECOVERY_INTERVAL_SECONDS)


@app.on_event("startup")
def startup() -> None:
    storage_adapter()
    upload_spool_dir()
    init_db()


@app.on_event("startup")
async def start_storage_recovery() -> None:
    global DELETION_RECOVERY_TASK
    # Recovery starts immediately in the background; gateway/OSS outages must
    # never hold application startup or the deployment health check hostage.
    DELETION_RECOVERY_TASK = asyncio.create_task(storage_recovery_loop())


@app.on_event("shutdown")
async def stop_storage_recovery() -> None:
    global DELETION_RECOVERY_TASK
    if DELETION_RECOVERY_TASK is None:
        return
    DELETION_RECOVERY_TASK.cancel()
    with suppress(asyncio.CancelledError):
        await DELETION_RECOVERY_TASK
    DELETION_RECOVERY_TASK = None


@app.middleware("http")
async def security_headers(request: Request, call_next):
    if (
        request.method in {"POST", "PUT", "PATCH", "DELETE"}
        and request.url.path.startswith("/api/ui/")
    ):
        supplied_origin = request.headers.get("origin", "")
        try:
            supplied_origin = canonical_https_origin(supplied_origin, "Origin")
        except RuntimeError:
            supplied_origin = ""
        if not hmac.compare_digest(supplied_origin, PUBLIC_ORIGIN):
            return JSONResponse(
                status_code=403,
                content={"detail": "Same-origin UI request required"},
            )
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; frame-ancestors " + " ".join(SAAS_ORIGINS)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin"
    return response


def bearer(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Bearer token required")
    return authorization[7:]


def require_static_token(authorization: str | None) -> None:
    if not hmac.compare_digest(bearer(authorization), INTEGRATION_SECRET):
        raise HTTPException(401, "Invalid integration token")


def decode_jwt(token: str, expected_type: str) -> dict[str, Any]:
    try:
        claims = jwt.decode(
            token,
            INTEGRATION_SECRET,
            algorithms=["HS256"],
            audience=APP_SLUG,
            options={"require": ["exp", "iat"]},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(401, "Invalid integration JWT") from exc
    if claims.get("iss") != "zhuojian-saas" or claims.get("typ") != expected_type:
        raise HTTPException(401, "Invalid integration JWT type")
    issued_at, expires_at = claims.get("iat"), claims.get("exp")
    maximum_lifetime = JWT_MAX_LIFETIME_SECONDS.get(expected_type)
    if (
        maximum_lifetime is None
        or isinstance(issued_at, bool)
        or isinstance(expires_at, bool)
        or not isinstance(issued_at, (int, float))
        or not isinstance(expires_at, (int, float))
        or expires_at <= issued_at
        or expires_at - issued_at > maximum_lifetime
    ):
        raise HTTPException(401, "Integration JWT lifetime violates the contract")
    if str(claims.get("organizationId") or "") != EXPECTED_ORGANIZATION_ID:
        raise HTTPException(403, "Enterprise organization mismatch")
    return claims


def canonical_hash(params: dict[str, Any]) -> str:
    encoded = json.dumps(params, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def require_request_id(value: Any) -> str:
    if not isinstance(value, str) or not REQUEST_ID_PATTERN.fullmatch(value):
        raise HTTPException(
            422,
            "requestId must be 8-128 characters using letters, digits, '.', '_', ':' or '-'",
        )
    return value


def require_expected_version(operation: str, value: Any) -> int | None:
    mutating_existing = {"update", "delete", "approve"}
    if value is None and operation not in mutating_existing:
        return None
    if isinstance(value, bool):
        raise HTTPException(422, "expectedVersion must be a positive integer or null")
    if isinstance(value, str):
        if not value.isascii() or not value.isdecimal() or value.startswith("0"):
            raise HTTPException(422, "expectedVersion must be a positive integer")
        value = int(value)
    if not isinstance(value, int) or value < 1:
        message = (
            "expectedVersion is required for update, approve and delete"
            if operation in mutating_existing
            else "expectedVersion must be a positive integer or null"
        )
        raise HTTPException(422, message)
    return value


def require_event_delivery_payload(
    body: Any,
) -> tuple[str, str, dict[str, Any]]:
    if not isinstance(body, dict) or set(body) != {
        "deliveryId", "sourceApplicationSlug", "event"
    }:
        raise HTTPException(422, "Event delivery body must exactly match the v2 schema")
    delivery_id = body.get("deliveryId")
    source_slug = body.get("sourceApplicationSlug")
    event = body.get("event")
    if not isinstance(delivery_id, str) or not 8 <= len(delivery_id) <= 160:
        raise HTTPException(422, "deliveryId must contain 8-160 characters")
    if (
        not isinstance(source_slug, str)
        or len(source_slug) > 160
        or not STABLE_KEY_PATTERN.fullmatch(source_slug)
    ):
        raise HTTPException(422, "sourceApplicationSlug is invalid")
    required = {
        "eventId", "eventType", "enterpriseKey", "moduleKey",
        "entityType", "entityId", "occurredAt", "payload",
    }
    if not isinstance(event, dict) or not required.issubset(event):
        raise HTTPException(422, "event is missing required v2 fields")
    event_id = event.get("eventId")
    event_type = event.get("eventType")
    if not isinstance(event_id, str) or not 8 <= len(event_id) <= 160:
        raise HTTPException(422, "eventId must contain 8-160 characters")
    if (
        not isinstance(event_type, str)
        or len(event_type) > 160
        or not STABLE_KEY_PATTERN.fullmatch(event_type)
    ):
        raise HTTPException(422, "eventType is invalid")
    for key in ("enterpriseKey", "moduleKey", "entityType"):
        value = event.get(key)
        if (
            not isinstance(value, str)
            or len(value) > 160
            or not STABLE_KEY_PATTERN.fullmatch(value)
        ):
            raise HTTPException(422, f"event.{key} is invalid")
    entity_id = event.get("entityId")
    if not isinstance(entity_id, str) or not entity_id or len(entity_id) > 500:
        raise HTTPException(422, "event.entityId is invalid")
    if not isinstance(event.get("payload"), dict):
        raise HTTPException(422, "event.payload must be a JSON object")
    occurred_at = event.get("occurredAt")
    if not isinstance(occurred_at, str) or not occurred_at:
        raise HTTPException(422, "event.occurredAt must be an RFC3339 date-time")
    try:
        parsed_time = datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(422, "event.occurredAt must be an RFC3339 date-time") from exc
    if parsed_time.tzinfo is None or parsed_time.utcoffset() is None:
        raise HTTPException(422, "event.occurredAt must include a timezone")
    return delivery_id, source_slug, event


def require_action_payload(
    body: Any,
    action_key: str,
    action: dict,
    *,
    allow_confirmation: bool = False,
) -> tuple[str, str, str, dict[str, Any], int | None, dict[str, Any] | None]:
    if not isinstance(body, dict):
        raise HTTPException(422, "Action body must be a JSON object")
    allowed = {"requestId", "moduleKey", "pageKey", "operation", "expectedVersion", "params"}
    if allow_confirmation:
        allowed.add("confirmation")
    if set(body) - allowed:
        raise HTTPException(422, "Action body contains unsupported fields")
    module_key = body.get("moduleKey")
    page_key = body.get("pageKey")
    operation = body.get("operation")
    params = body.get("params")
    if not isinstance(module_key, str) or not isinstance(page_key, str):
        raise HTTPException(422, "moduleKey and pageKey are required")
    if operation != action.get("operation"):
        raise HTTPException(403, "Action operation mismatch")
    if not isinstance(params, dict):
        raise HTTPException(422, "params must be a JSON object")
    request_id = require_request_id(body.get("requestId"))
    expected_version = require_expected_version(str(operation), body.get("expectedVersion"))
    confirmation = body.get("confirmation")
    if confirmation is not None and not isinstance(confirmation, dict):
        raise HTTPException(422, "confirmation must be a JSON object")
    return request_id, module_key, page_key, params, expected_version, confirmation


def action_request_hash(
    action_key: str,
    module_key: str,
    page_key: str,
    actor: str,
    params: dict[str, Any],
    expected_version: int | None,
    *,
    subject: str = "business-record",
) -> str:
    return canonical_hash({
        "applicationSlug": APP_SLUG,
        "subject": subject,
        "actionKey": action_key,
        "moduleKey": module_key,
        "pageKey": page_key,
        "actor": actor,
        "params": params,
        "expectedVersion": expected_version,
    })


def parse_confirmation_time(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise HTTPException(403, "Valid user confirmation required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(403, "Valid user confirmation required") from exc
    if parsed.tzinfo is None:
        raise HTTPException(403, "Valid user confirmation required")
    parsed = parsed.astimezone(timezone.utc)
    now = datetime.now(timezone.utc)
    if parsed > now + timedelta(seconds=30):
        raise HTTPException(403, "Valid user confirmation required")
    if (now - parsed).total_seconds() > CONFIRMATION_MAX_AGE_SECONDS:
        raise HTTPException(403, "User confirmation has expired")
    return parsed


def validate_confirmation_declaration(
    declaration: Any,
    *,
    actor: str,
    request_id: str,
    params: dict[str, Any],
) -> tuple[str, str]:
    if not isinstance(declaration, dict):
        raise HTTPException(403, "Valid user confirmation required")
    confirmation_id = declaration.get("confirmationId")
    if not isinstance(confirmation_id, str):
        raise HTTPException(403, "Valid user confirmation required")
    try:
        parsed_id = UUID(confirmation_id)
    except (ValueError, AttributeError) as exc:
        raise HTTPException(403, "Valid user confirmation required") from exc
    if str(parsed_id) != confirmation_id.lower():
        raise HTTPException(403, "Valid user confirmation required")
    params_hash = declaration.get("paramsHash")
    expected_hash = canonical_hash(params)
    if (
        declaration.get("confirmed") is not True
        or declaration.get("confirmedBy") != actor
        or declaration.get("requestId") != request_id
        or not isinstance(params_hash, str)
        or not SHA256_PATTERN.fullmatch(params_hash)
        or not hmac.compare_digest(params_hash, expected_hash)
    ):
        raise HTTPException(403, "Valid user confirmation required")
    parse_confirmation_time(declaration.get("confirmedAt"))
    return confirmation_id, params_hash


def consume_confirmation(
    connection: sqlite3.Connection,
    declaration: Any,
    *,
    actor: str,
    request_id: str,
    action_key: str,
    request_hash: str,
    params: dict[str, Any],
    require_page_issued: bool,
) -> None:
    confirmation_id, params_hash = validate_confirmation_declaration(
        declaration,
        actor=actor,
        request_id=request_id,
        params=params,
    )
    if require_page_issued:
        issued = connection.execute(
            """
            SELECT request_id,action_key,request_hash,actor,params_hash,confirmed_at,consumed_at
            FROM page_confirmations WHERE confirmation_id=?
            """,
            (confirmation_id,),
        ).fetchone()
        if (
            issued is None
            or issued["request_id"] != request_id
            or issued["action_key"] != action_key
            or issued["request_hash"] != request_hash
            or issued["actor"] != actor
            or issued["params_hash"] != params_hash
            or issued["confirmed_at"] != declaration.get("confirmedAt")
            or issued["consumed_at"] is not None
        ):
            raise HTTPException(409, "Page confirmation is invalid or has already been consumed")
        # Page confirmations are issued by this server.  Expiration is based on
        # the immutable database timestamp, never on a timestamp echoed by the
        # browser.
        parse_confirmation_time(issued["confirmed_at"])
    if connection.execute(
        "SELECT 1 FROM consumed_confirmations WHERE confirmation_id=?",
        (confirmation_id,),
    ).fetchone():
        raise HTTPException(409, "Confirmation has already been consumed")
    consumed_at = datetime.now(timezone.utc).isoformat()
    connection.execute(
        """
        INSERT INTO consumed_confirmations(confirmation_id,consumed_at,request_id,action_key)
        VALUES(?,?,?,?)
        """,
        (confirmation_id, consumed_at, request_id, action_key),
    )
    if require_page_issued:
        connection.execute(
            "UPDATE page_confirmations SET consumed_at=? WHERE confirmation_id=?",
            (consumed_at, confirmation_id),
        )


def checked_request_result(
    connection: sqlite3.Connection,
    request_id: str,
    action_key: str,
    request_hash: str,
) -> sqlite3.Row | None:
    stored = connection.execute(
        """
        SELECT action_key,request_hash,state,result,updated_at,lease_owner,lease_expires_at
        FROM request_results WHERE request_id=?
        """,
        (request_id,),
    ).fetchone()
    if stored is None:
        return None
    if stored["action_key"] != action_key or not stored["request_hash"]:
        raise HTTPException(409, "requestId is already bound to a different request")
    if not hmac.compare_digest(stored["request_hash"], request_hash):
        raise HTTPException(409, "requestId is already bound to a different request")
    return stored


def execute_idempotent_action(
    *,
    action_key: str,
    action: dict,
    request_id: str,
    request_hash: str,
    actor: str,
    params: dict[str, Any],
    confirmation: dict[str, Any] | None,
    require_page_confirmation: bool,
    perform: Callable[[sqlite3.Connection], dict[str, Any]],
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    with db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        stored = checked_request_result(connection, request_id, action_key, request_hash)
        if stored is not None:
            if stored["state"] != "completed":
                raise HTTPException(409, "Action request is still in progress")
            return json.loads(stored["result"])
        connection.execute(
            """
            INSERT INTO request_results(
              request_id,action_key,request_hash,state,result,created_at,updated_at
            ) VALUES(?,?,?,'in_progress','{}',?,?)
            """,
            (request_id, action_key, request_hash, now, now),
        )
        if action.get("requiresConfirmation"):
            consume_confirmation(
                connection,
                confirmation,
                actor=actor,
                request_id=request_id,
                action_key=action_key,
                request_hash=request_hash,
                params=params,
                require_page_issued=require_page_confirmation,
            )
        result = perform(connection)
        connection.execute(
            """
            UPDATE request_results
            SET state='completed',result=?,updated_at=? WHERE request_id=?
            """,
            (json.dumps(result, ensure_ascii=False), datetime.now(timezone.utc).isoformat(), request_id),
        )
        return result


def page_allows(module_key: str, page_key: str, action_key: str) -> bool:
    page = PAGES.get(page_key)
    return bool(page and page["moduleKey"] == module_key and action_key in page.get("actionKeys", []))


def session_allows(session: dict, module_key: str, page_key: str, action_key: str) -> bool:
    if session.get("moduleKey") != module_key or action_key not in (session.get("actionKeys") or []):
        return False
    page = (session.get("pageAccess") or {}).get(page_key)
    return bool(
        isinstance(page, dict)
        and action_key in (page.get("actionKeys") or [])
        and page_allows(module_key, page_key, action_key)
    )


def require_file_action(
    request: Request,
    module_key: str,
    page_key: str,
    action_key: str,
    allowed_operations: set[str],
) -> dict:
    session = request.session
    if not session.get("sub"):
        raise HTTPException(401, "Open this module from ZhuoJian SaaS")
    action = ACTIONS.get(action_key)
    if (
        action is None
        or action["moduleKey"] != module_key
        or action.get("operation") not in allowed_operations
        or not session_allows(session, module_key, page_key, action_key)
    ):
        raise HTTPException(403, "File action context mismatch")
    permissions = set((session.get("pageAccess") or {}).get(page_key, {}).get("permissions") or [])
    if "view" not in permissions or required_permission(action["operation"]) not in permissions:
        raise HTTPException(403, "File action permission denied")
    return action


def safe_upload_filename(filename: str) -> str:
    candidate = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
    candidate = "".join(character for character in candidate if ord(character) >= 32 and ord(character) != 127)
    if candidate in {"", ".", ".."}:
        raise HTTPException(422, "A valid filename is required")
    encoded = candidate.encode("utf-8")
    if len(encoded) > 240:
        while len(candidate.encode("utf-8")) > 240:
            candidate = candidate[:-1]
    return candidate


def storage_http_error(exc: StorageError) -> HTTPException:
    if isinstance(exc, (StorageObjectNotFound, InvalidStorageKey)):
        return HTTPException(404 if isinstance(exc, StorageObjectNotFound) else 422, str(exc))
    if isinstance(exc, StorageAuthorizationError):
        return HTTPException(503, "File storage authorization is not ready")
    if isinstance(exc, StorageUnavailableError):
        return HTTPException(503, "File storage is temporarily unavailable")
    return HTTPException(503, "File storage is not ready")


def stream_storage_object(stream):
    try:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            yield chunk
    finally:
        stream.close()


def required_permission(operation: str) -> str:
    return {
        "query": "ai_query", "create": "ai_create", "update": "ai_update",
        "delete": "ai_delete", "approve": "ai_approve", "export": "export",
    }[operation]


def normalized_data_scope(actor: dict) -> tuple[bool, bool, bool, set[str]]:
    role_ids = actor.get("roleIds")
    scope = actor.get("effectiveDataScope")
    if not isinstance(role_ids, list) or not isinstance(scope, dict):
        raise HTTPException(403, "Platform role data scope is required")
    department_ids = scope.get("department_ids")
    if not isinstance(department_ids, (list, tuple)):
        raise HTTPException(403, "Platform department data scope is invalid")
    return (
        scope.get("unrestricted") is True,
        scope.get("include_self") is True,
        scope.get("own_only") is True,
        {str(value) for value in department_ids if value},
    )


def scoped_records_clause(actor: dict) -> tuple[str, list[str]]:
    unrestricted, include_self, own_only, department_ids = normalized_data_scope(actor)
    if unrestricted:
        return "", []
    clauses: list[str] = []
    values: list[str] = []
    if department_ids:
        clauses.append(f"department_id IN ({','.join('?' for _ in department_ids)})")
        values.extend(sorted(department_ids))
    if include_self or own_only:
        clauses.append("created_by=?")
        values.append(str(actor.get("sub") or ""))
    if not clauses:
        raise HTTPException(403, "Platform role grants no business data scope")
    return " AND (" + " OR ".join(clauses) + ")", values


def require_record_scope(actor: dict, department_id: str | None, created_by: str | None) -> None:
    unrestricted, include_self, own_only, department_ids = normalized_data_scope(actor)
    if unrestricted or (department_id and department_id in department_ids):
        return
    if (include_self or own_only) and created_by == str(actor.get("sub") or ""):
        return
    raise HTTPException(403, "Business record is outside the platform role data scope")


def emit_event(connection: sqlite3.Connection, module_key: str, event_type: str, entity_id: str, payload: dict) -> None:
    # The SaaS cursor survives container/database replacement.  A local
    # AUTOINCREMENT that restarts at 1 can therefore hide new events behind an
    # older cursor.  Epoch-microseconds stay below JavaScript's safe-integer
    # ceiling and remain monotonic inside this outbox transaction.
    previous = connection.execute("SELECT COALESCE(MAX(sequence), 0) FROM outbox").fetchone()[0]
    sequence = max(time.time_ns() // 1_000, int(previous) + 1)
    connection.execute(
        "INSERT INTO outbox(sequence,event_id,event_type,module_key,entity_type,entity_id,occurred_at,payload) VALUES(?,?,?,?,?,?,?,?)",
        (sequence, uuid4().hex, event_type, module_key, module_key, entity_id, datetime.now(timezone.utc).isoformat(), json.dumps(payload, ensure_ascii=False)),
    )


def execute_business_action(
    action: dict,
    params: dict,
    expected_version: int | None,
    actor: dict,
    connection: sqlite3.Connection | None = None,
) -> dict:
    if connection is None:
        with db() as owned_connection:
            return execute_business_action(
                action,
                params,
                expected_version,
                actor,
                owned_connection,
            )
    operation = action["operation"]
    module_key = action["moduleKey"]
    now = datetime.now(timezone.utc).isoformat()
    if operation == "query":
        scope_sql, scope_values = scoped_records_clause(actor)
        rows = connection.execute(
            "SELECT id,data,status,version,created_at,updated_at FROM records "
            f"WHERE module_key=?{scope_sql} ORDER BY updated_at DESC LIMIT 200",
            [module_key, *scope_values],
        ).fetchall()
        return {"items": [{**json.loads(row["data"]), "id": row["id"], "status": row["status"], "version": row["version"]} for row in rows]}
    if operation == "create":
        raw_data = params.get("data")
        if not isinstance(raw_data, dict):
            raise HTTPException(422, "Create params.data must be an object")
        record_id = str(params.get("id") or uuid4().hex)
        data = dict(raw_data)
        department_id = str(data.get("departmentId") or actor.get("departmentId") or "")
        if not department_id:
            raise HTTPException(422, "A business departmentId is required")
        data["departmentId"] = department_id
        created_by = str(actor.get("sub") or "")
        require_record_scope(actor, department_id, created_by)
        status = str(data.pop("status", "draft"))
        connection.execute(
            "INSERT INTO records(id,module_key,data,department_id,created_by,status,version,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,1,?,?)",
            (
                record_id,
                module_key,
                json.dumps(data, ensure_ascii=False),
                department_id,
                created_by,
                status,
                now,
                now,
            ),
        )
        emit_event(connection, module_key, f"{module_key}.created.v1", record_id, {"version": 1})
        return {"id": record_id, "version": 1, "status": status}
    record_id = str(params.get("id") or "")
    row = connection.execute("SELECT * FROM records WHERE id=? AND module_key=?", (record_id, module_key)).fetchone()
    if row is None:
        raise HTTPException(404, "Business record not found")
    require_record_scope(actor, row["department_id"], row["created_by"])
    if operation in {"update", "delete", "approve"} and (
        expected_version is None or row["version"] != expected_version
    ):
        raise HTTPException(409, "Business record version conflict")
    if operation == "update":
        changes = params.get("changes")
        if not isinstance(changes, dict):
            raise HTTPException(422, "Update params.changes must be an object")
        data = json.loads(row["data"])
        data.update(changes)
        department_id = str(data.get("departmentId") or row["department_id"] or "")
        require_record_scope(actor, department_id, row["created_by"])
        data["departmentId"] = department_id
        version = row["version"] + 1
        status = str(data.pop("status", row["status"]))
        connection.execute(
            "UPDATE records SET data=?,department_id=?,status=?,version=?,updated_at=? WHERE id=?",
            (
                json.dumps(data, ensure_ascii=False),
                department_id,
                status,
                version,
                now,
                record_id,
            ),
        )
        emit_event(connection, module_key, f"{module_key}.updated.v1", record_id, {"version": version, "status": status})
        return {"id": record_id, "version": version, "status": status}
    if operation == "approve":
        version = row["version"] + 1
        connection.execute(
            "UPDATE records SET status='approved',version=?,updated_at=? WHERE id=?",
            (version, now, record_id),
        )
        emit_event(
            connection, module_key, f"{module_key}.approved.v1", record_id,
            {"version": version, "status": "approved"},
        )
        return {"id": record_id, "version": version, "status": "approved"}
    if operation == "delete":
        connection.execute("DELETE FROM records WHERE id=?", (record_id,))
        emit_event(connection, module_key, f"{module_key}.deleted.v1", record_id, {"version": row["version"]})
        return {"id": record_id, "deleted": True}
    if operation == "export":
        return {"id": record_id, "record": {**json.loads(row["data"]), "status": row["status"], "version": row["version"]}}
    raise HTTPException(422, "Unsupported business operation")


@app.get("/health")
def health():
    return {
        "status": "ok",
        "applicationSlug": APP_SLUG,
        "contractRevision": MANIFEST.get("contractRevision"),
        "fileStorage": storage_adapter().backend,
    }


@app.get("/api/integration/manifest")
def manifest(authorization: str | None = Header(default=None)):
    require_static_token(authorization)
    return MANIFEST


@app.get("/api/integration/events")
def events(after: int = 0, limit: int = 100, authorization: str | None = Header(default=None)):
    require_static_token(authorization)
    limit = max(1, min(limit, 100))
    with db() as connection:
        rows = connection.execute("SELECT * FROM outbox WHERE sequence>? ORDER BY sequence LIMIT ?", (after, limit + 1)).fetchall()
    items = [{
        "sequence": row["sequence"], "eventId": row["event_id"], "eventType": row["event_type"],
        "enterpriseKey": MANIFEST["enterprise"]["key"], "moduleKey": row["module_key"], "departmentKeys": [],
        "entityType": row["entity_type"], "entityId": row["entity_id"], "action": row["event_type"].split(".")[-2],
        "occurredAt": row["occurred_at"], "payload": json.loads(row["payload"]),
    } for row in rows[:limit]]
    return {"items": items, "nextAfter": items[-1]["sequence"] if items else after, "hasMore": len(rows) > limit}


@app.post("/api/integration/actions/{action_key}")
async def invoke_action(action_key: str, request: Request, authorization: str | None = Header(default=None)):
    action = ACTIONS.get(action_key)
    # Platform filtering is not an authorization boundary.  The module must
    # independently enforce the Manifest AI switch even if a stale or forged
    # catalog produced an otherwise valid short-lived Action JWT.
    if action is None or action.get("aiEnabled") is not True:
        raise HTTPException(404, "Action not found")
    claims = decode_jwt(bearer(authorization), "zhuojian-action")
    body = await request.json()
    request_id, module_key, page_key, params, expected_version, _ = require_action_payload(
        body,
        action_key,
        action,
    )
    actor = claims.get("sub")
    if not isinstance(actor, str) or not actor:
        raise HTTPException(403, "Action actor is required")
    if (
        action.get("moduleKey") != module_key
        or claims.get("moduleKey") != module_key
        or claims.get("pageKey") != page_key
        or claims.get("actionKey") != action_key
    ):
        raise HTTPException(403, "Action context mismatch")
    if claims.get("operation") != action["operation"]:
        raise HTTPException(403, "Action operation mismatch")
    if (
        not isinstance(claims.get("requestId"), str)
        or claims.get("requestId") != request_id
        or not page_allows(module_key, page_key, action_key)
    ):
        raise HTTPException(403, "Action is not allowed on this page")
    permissions = set(claims.get("permissions") or [])
    if "view" not in permissions or required_permission(action["operation"]) not in permissions:
        raise HTTPException(403, "Action permission denied")
    request_hash = action_request_hash(
        action_key,
        module_key,
        page_key,
        actor,
        params,
        expected_version,
    )
    return execute_idempotent_action(
        action_key=action_key,
        action=action,
        request_id=request_id,
        request_hash=request_hash,
        actor=actor,
        params=params,
        confirmation=claims if action.get("requiresConfirmation") else None,
        require_page_confirmation=False,
        perform=lambda connection: execute_business_action(
            action,
            params,
            expected_version,
            claims,
            connection,
        ),
    )


@app.post("/api/integration/event-deliveries")
async def receive_event(request: Request, authorization: str | None = Header(default=None)):
    claims = decode_jwt(bearer(authorization), "zhuojian-event")
    body = await request.json()
    delivery_id, source_slug, event = require_event_delivery_payload(body)
    target_module_key = claims.get("targetModuleKey")
    target_module = MODULES.get(target_module_key) if isinstance(target_module_key, str) else None
    subscribed = (
        target_module.get("events", {}).get("subscribes", [])
        if isinstance(target_module, dict)
        else []
    )
    if (
        claims.get("deliveryId") != delivery_id
        or claims.get("eventId") != event["eventId"]
        or claims.get("eventType") != event["eventType"]
        or (claims.get("sourceApplicationSlug") is not None
            and claims.get("sourceApplicationSlug") != source_slug)
        or target_module is None
    ):
        raise HTTPException(403, "Event delivery context mismatch")
    if event["enterpriseKey"] != MANIFEST["enterprise"]["key"]:
        raise HTTPException(403, "Cross-enterprise event delivery is forbidden")
    if event["eventType"] not in subscribed:
        raise HTTPException(403, "Target module does not subscribe to this event type")
    request_hash = canonical_hash({
        "deliveryId": delivery_id,
        "sourceApplicationSlug": source_slug,
        "event": event,
        "targetModuleKey": target_module_key,
    })
    with db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            """
            SELECT delivery_id,event_id,request_hash,result
            FROM event_deliveries WHERE delivery_id=? OR event_id=?
            """,
            (delivery_id, event["eventId"]),
        ).fetchone()
        if existing:
            if (
                existing["delivery_id"] != delivery_id
                or existing["event_id"] != event["eventId"]
                or not existing["request_hash"]
                or not hmac.compare_digest(existing["request_hash"], request_hash)
            ):
                raise HTTPException(409, "deliveryId or eventId is bound to different event content")
            return {"status": "duplicate", **json.loads(existing["result"])}
        result = {
            "eventId": event["eventId"],
            "accepted": True,
            "processed": False,
            "targetModuleKey": target_module_key,
        }
        connection.execute(
            """
            INSERT INTO event_deliveries(
              delivery_id,event_id,event_type,source_application_slug,
              target_module_key,request_hash,result,received_at
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                delivery_id,
                event["eventId"],
                event["eventType"],
                source_slug,
                target_module_key,
                request_hash,
                json.dumps(result, ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
    return {"status": "accepted", **result}


@app.get("/api/ui/bootstrap")
def ui_bootstrap(request: Request, moduleKey: str, pageKey: str):
    session = request.session
    page = PAGES.get(pageKey)
    module = MODULES.get(moduleKey)
    page_access = (session.get("pageAccess") or {}).get(pageKey)
    if not session.get("sub"):
        raise HTTPException(401, "Open this module from ZhuoJian SaaS")
    if (
        module is None
        or page is None
        or page.get("moduleKey") != moduleKey
        or session.get("moduleKey") != moduleKey
        or not isinstance(page_access, dict)
        or pageKey not in (session.get("pageKeys") or [])
    ):
        raise HTTPException(403, "Page context mismatch")
    return {
        "applicationName": MANIFEST["applicationName"],
        "moduleName": module["name"],
        "pageName": page["name"],
    }


@app.post("/api/ui/confirmations", status_code=201)
async def issue_page_confirmation(request: Request):
    body = await request.json()
    if not isinstance(body, dict) or body.get("confirmed") is not True:
        raise HTTPException(409, "Explicit page confirmation required")
    payload = {key: value for key, value in body.items() if key != "confirmed"}
    action_key = payload.get("actionKey")
    action = ACTIONS.get(action_key) if isinstance(action_key, str) else None
    if action is None:
        raise HTTPException(404, "Action not found")
    if set(body) - {
        "requestId", "moduleKey", "pageKey", "actionKey", "operation",
        "expectedVersion", "params", "confirmed", "subject",
    }:
        raise HTTPException(422, "Confirmation body contains unsupported fields")
    subject = payload.pop("subject", "business-record")
    if subject not in {"business-record", "stored-file"}:
        raise HTTPException(422, "Confirmation subject is invalid")
    payload.pop("actionKey", None)
    request_id, module_key, page_key, params, expected_version, _ = require_action_payload(
        payload,
        action_key,
        action,
    )
    session = request.session
    actor = session.get("sub")
    if not isinstance(actor, str) or not actor:
        raise HTTPException(401, "Open this module from ZhuoJian SaaS")
    if action["moduleKey"] != module_key or not session_allows(
        session,
        module_key,
        page_key,
        action_key,
    ):
        raise HTTPException(403, "Page action context mismatch")
    permissions = set((session.get("pageAccess") or {}).get(page_key, {}).get("permissions") or [])
    if (
        not action.get("requiresConfirmation")
        or "view" not in permissions
        or required_permission(action["operation"]) not in permissions
    ):
        raise HTTPException(403, "Page confirmation is not allowed")
    if subject == "stored-file" and (
        action.get("operation") != "delete" or set(params) != {"fileId"}
    ):
        raise HTTPException(422, "Stored-file confirmation parameters are invalid")
    request_hash = action_request_hash(
        action_key,
        module_key,
        page_key,
        actor,
        params,
        expected_version,
        subject=subject,
    )
    confirmation_id = str(uuid4())
    confirmed_at = datetime.now(timezone.utc).isoformat()
    params_hash = canonical_hash(params)
    with db() as connection:
        connection.execute(
            """
            INSERT INTO page_confirmations(
              confirmation_id,request_id,action_key,request_hash,actor,
              params_hash,confirmed_at,consumed_at
            ) VALUES(?,?,?,?,?,?,?,NULL)
            """,
            (
                confirmation_id,
                request_id,
                action_key,
                request_hash,
                actor,
                params_hash,
                confirmed_at,
            ),
        )
    return {
        "confirmed": True,
        "confirmationId": confirmation_id,
        "confirmedBy": actor,
        "confirmedAt": confirmed_at,
        "paramsHash": params_hash,
        "requestId": request_id,
    }


@app.post("/api/ui/actions/{action_key}")
async def invoke_page_action(action_key: str, request: Request):
    action = ACTIONS.get(action_key)
    session = request.session
    if action is None or not session.get("sub"):
        raise HTTPException(401, "Open this module from ZhuoJian SaaS")
    body = await request.json()
    request_id, module_key, page_key, params, expected_version, confirmation = require_action_payload(
        body,
        action_key,
        action,
        allow_confirmation=True,
    )
    if action["moduleKey"] != module_key or not session_allows(
        session,
        module_key,
        page_key,
        action_key,
    ):
        raise HTTPException(403, "Page action context mismatch")
    permissions = set((session.get("pageAccess") or {}).get(page_key, {}).get("permissions") or [])
    if "view" not in permissions or required_permission(action["operation"]) not in permissions:
        raise HTTPException(403, "Page action permission denied")
    if not action.get("requiresConfirmation") and confirmation is not None:
        raise HTTPException(422, "confirmation is only valid for high-risk actions")
    actor = str(session["sub"])
    request_hash = action_request_hash(
        action_key,
        module_key,
        page_key,
        actor,
        params,
        expected_version,
    )
    return execute_idempotent_action(
        action_key=action_key,
        action=action,
        request_id=request_id,
        request_hash=request_hash,
        actor=actor,
        params=params,
        confirmation=confirmation,
        require_page_confirmation=bool(action.get("requiresConfirmation")),
        perform=lambda connection: execute_business_action(
            action,
            params,
            expected_version,
            session,
            connection,
        ),
    )


@app.get("/api/ui/files")
def list_files(request: Request, moduleKey: str, pageKey: str, actionKey: str):
    require_file_action(request, moduleKey, pageKey, actionKey, {"query", "export"})
    with db() as connection:
        rows = connection.execute(
            """
            SELECT file_id,original_name,mime_type,size,sha256,storage_backend,version,created_at
            FROM stored_files
            WHERE module_key=? AND deletion_state='active'
            ORDER BY created_at DESC LIMIT 200
            """,
            (moduleKey,),
        ).fetchall()
    return {"items": [{
        "fileId": row["file_id"],
        "filename": row["original_name"],
        "mimeType": row["mime_type"],
        "size": row["size"],
        "sha256": row["sha256"],
        "storageBackend": row["storage_backend"],
        "version": row["version"],
        "createdAt": row["created_at"],
    } for row in rows]}


@app.post("/api/ui/files", status_code=201)
async def upload_file(
    request: Request,
    moduleKey: str,
    pageKey: str,
    actionKey: str,
    filename: str,
    businessType: str | None = None,
    businessId: str | None = None,
):
    require_file_action(request, moduleKey, pageKey, actionKey, {"create", "update"})
    filename = safe_upload_filename(filename)
    announced_size = request.headers.get("content-length")
    if announced_size is None:
        raise HTTPException(411, "Content-Length is required for file uploads")
    if not re.fullmatch(r"[0-9]+", announced_size):
        raise HTTPException(400, "Invalid Content-Length")
    announced_bytes = int(announced_size)
    if announced_bytes > FILE_STORAGE_MAX_UPLOAD_BYTES:
        raise HTTPException(413, "File exceeds this module's upload limit")

    upload_lock = await run_in_threadpool(acquire_upload_lock)
    metadata = None
    try:
        # The host-wide lock stays held through spooling, backend commit and
        # metadata commit, so the two-copy peak cannot race another subsystem.
        require_upload_capacity(announced_bytes, copies=2)
        key_time = datetime.now(timezone.utc)
        file_id = uuid4().hex
        storage_key = f"{moduleKey}/{key_time:%Y/%m}/{file_id}"
        content_type = request.headers.get("content-type") or "application/octet-stream"
        with tempfile.SpooledTemporaryFile(
            max_size=8 * 1024 * 1024,
            mode="w+b",
            dir=upload_spool_dir(),
        ) as spool:
            received = 0
            payload_sha256 = hashlib.sha256()
            async for chunk in request.stream():
                received += len(chunk)
                if received > announced_bytes:
                    raise HTTPException(400, "File body exceeds Content-Length")
                require_upload_capacity(len(chunk))
                spool.write(chunk)
                payload_sha256.update(chunk)
            if received != announced_bytes:
                raise HTTPException(400, "File body does not match Content-Length")
            # Local storage briefly creates a destination-side temporary copy;
            # re-check immediately before that second write while still locked.
            require_upload_capacity(received)
            expected_digest = payload_sha256.hexdigest()
            adapter = storage_adapter()
            # Recovery grace starts when the backend commit can begin, not
            # when a potentially very slow request body first arrived.
            upload_recorded_at = datetime.now(timezone.utc)
            try:
                with db() as connection:
                    connection.execute(
                        """
                        INSERT INTO stored_files(
                          storage_key,file_id,module_key,original_name,mime_type,size,sha256,
                          storage_backend,created_by,business_type,business_id,deletion_state,created_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            storage_key,
                            file_id,
                            moduleKey,
                            filename,
                            content_type,
                            received,
                            expected_digest,
                            adapter.backend,
                            str(request.session["sub"]),
                            businessType,
                            businessId,
                            "uploading",
                            upload_recorded_at.isoformat(),
                        ),
                    )
            except sqlite3.Error as exc:
                raise HTTPException(500, "File upload could not be recorded") from exc
            spool.seek(0)
            try:
                metadata = await run_in_threadpool(
                    lambda: adapter.put(storage_key, spool, content_type=content_type)
                )
            except StorageError as exc:
                # The backend may have committed before a response was lost.
                # Keep the uploading row so the recovery worker can stat it and
                # either finalize the exact object or remove the orphan safely.
                raise storage_http_error(exc) from exc

        if (
            metadata.storage_key != storage_key
            or metadata.backend != adapter.backend
            or int(metadata.size) != received
            or not metadata.sha256
            or not hmac.compare_digest(str(metadata.sha256), expected_digest)
        ):
            mismatch_removed = False
            try:
                await run_in_threadpool(adapter.delete, storage_key)
                mismatch_removed = True
            except StorageError:
                pass
            if mismatch_removed:
                with db() as connection:
                    connection.execute(
                        "DELETE FROM stored_files WHERE file_id=? AND deletion_state='uploading'",
                        (file_id,),
                    )
            raise HTTPException(502, "File storage returned mismatched metadata")

        try:
            with db() as connection:
                finalized = connection.execute(
                    """
                    UPDATE stored_files SET deletion_state='active'
                    WHERE file_id=? AND deletion_state='uploading'
                    """,
                    (file_id,),
                )
                if finalized.rowcount != 1:
                    raise sqlite3.IntegrityError("upload metadata state changed unexpectedly")
        except sqlite3.Error as exc:
            # Leave the uploading row/object pair for deterministic recovery.
            raise HTTPException(500, "File upload is awaiting metadata recovery") from exc

        return {
            "fileId": file_id,
            "storageBackend": metadata.backend,
            "filename": filename,
            "mimeType": content_type,
            "size": metadata.size,
            "sha256": metadata.sha256,
        }
    finally:
        release_upload_lock(upload_lock)


@app.get("/api/ui/files/{file_id}")
async def download_file(
    file_id: str,
    request: Request,
    moduleKey: str,
    pageKey: str,
    actionKey: str,
):
    require_file_action(request, moduleKey, pageKey, actionKey, {"query", "export"})
    with db() as connection:
        row = connection.execute(
            """
            SELECT storage_key,module_key,original_name,mime_type,size,sha256,storage_backend
            FROM stored_files
            WHERE file_id=? AND module_key=? AND deletion_state='active'
            """,
            (file_id, moduleKey),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "File metadata not found")
    try:
        stream = await run_in_threadpool(
            storage_adapter(row["storage_backend"]).open,
            row["storage_key"],
        )
    except StorageError as exc:
        raise storage_http_error(exc) from exc
    encoded_filename = quote(row["original_name"], safe="")
    return StreamingResponse(
        stream_storage_object(stream),
        media_type=row["mime_type"],
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}",
            "Content-Length": str(row["size"]),
            "X-Content-SHA256": row["sha256"],
            "Cache-Control": "private, no-store",
        },
    )


@app.delete("/api/ui/files/{file_id}", status_code=204)
async def delete_file(
    file_id: str,
    request: Request,
    moduleKey: str,
    pageKey: str,
    actionKey: str,
):
    action = require_file_action(request, moduleKey, pageKey, actionKey, {"delete"})
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(422, "Delete action body must be JSON") from exc
    request_id, body_module, body_page, params, expected_version, confirmation = require_action_payload(
        body,
        actionKey,
        action,
        allow_confirmation=True,
    )
    if body_module != moduleKey or body_page != pageKey or params != {"fileId": file_id}:
        raise HTTPException(403, "File delete action context mismatch")
    actor = str(request.session["sub"])
    request_hash = action_request_hash(
        actionKey,
        moduleKey,
        pageKey,
        actor,
        params,
        expected_version,
        subject="stored-file",
    )
    now = datetime.now(timezone.utc)
    lease_owner = uuid4().hex
    lease_expires_at = (now + timedelta(seconds=ACTION_LEASE_SECONDS)).isoformat()
    row: dict[str, Any]
    with db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        stored = checked_request_result(connection, request_id, actionKey, request_hash)
        if stored is not None and stored["state"] == "completed":
            return Response(status_code=204)
        if stored is not None and stored["state"] == "in_progress":
            current_lease_expiry = lease_expiry(stored["lease_expires_at"])
            if current_lease_expiry > now:
                raise HTTPException(409, "File delete action is still in progress")
        if stored is not None and stored["state"] not in {"in_progress", "retryable"}:
            raise HTTPException(409, "File delete action has an invalid state")
        database_row = connection.execute(
            """
                SELECT storage_key,storage_backend,deletion_state,version,deletion_owner
            FROM stored_files WHERE file_id=? AND module_key=?
            """,
            (file_id, moduleKey),
        ).fetchone()
        if database_row is None:
            raise HTTPException(404, "File metadata not found")
        if stored is None:
            if database_row["deletion_state"] != "active":
                raise HTTPException(409, "File deletion is already in progress")
            if database_row["version"] != expected_version:
                raise HTTPException(409, "File metadata version conflict")
            connection.execute(
                """
                INSERT INTO request_results(
                  request_id,action_key,request_hash,state,result,created_at,updated_at,
                  lease_owner,lease_expires_at
                ) VALUES(?,?,?,'in_progress','{}',?,?,?,?)
                """,
                (
                    request_id,
                    actionKey,
                    request_hash,
                    now.isoformat(),
                    now.isoformat(),
                    lease_owner,
                    lease_expires_at,
                ),
            )
            consume_confirmation(
                connection,
                confirmation,
                actor=actor,
                request_id=request_id,
                action_key=actionKey,
                request_hash=request_hash,
                params=params,
                require_page_issued=True,
            )
        else:
            if database_row["deletion_state"] == "pending" and (
                (database_row["deletion_owner"] or "") != (stored["lease_owner"] or "")
            ):
                raise HTTPException(409, "File delete lease is owned by another worker")
            if database_row["deletion_state"] not in {"active", "pending"}:
                raise HTTPException(409, "File metadata is not deletable")
            claimed = connection.execute(
                """
                UPDATE request_results
                SET state='in_progress',updated_at=?,lease_owner=?,lease_expires_at=?
                WHERE request_id=? AND state=? AND COALESCE(lease_owner,'')=?
                """,
                (
                    now.isoformat(),
                    lease_owner,
                    lease_expires_at,
                    request_id,
                    stored["state"],
                    stored["lease_owner"] or "",
                ),
            )
            if claimed.rowcount != 1:
                raise HTTPException(409, "File delete lease could not be acquired")
        if database_row["deletion_state"] == "active":
            marked = connection.execute(
                """
                UPDATE stored_files SET deletion_state='pending',deletion_owner=?
                WHERE file_id=? AND version=? AND deletion_state='active'
                  AND deletion_owner IS NULL
                """,
                (lease_owner, file_id, expected_version),
            )
        elif database_row["deletion_owner"] is None:
            marked = connection.execute(
                """
                UPDATE stored_files SET deletion_owner=?
                WHERE file_id=? AND version=? AND deletion_state='pending'
                  AND deletion_owner IS NULL
                """,
                (lease_owner, file_id, expected_version),
            )
        else:
            marked = connection.execute(
                """
                UPDATE stored_files SET deletion_owner=?
                WHERE file_id=? AND version=? AND deletion_state='pending'
                  AND deletion_owner=?
                """,
                (lease_owner, file_id, expected_version, database_row["deletion_owner"]),
            )
        if marked.rowcount != 1:
            raise HTTPException(409, "File delete lease no longer owns the file")
        row = dict(database_row)
    try:
        await run_in_threadpool(
            storage_adapter(row["storage_backend"]).delete,
            row["storage_key"],
        )
    except StorageError as exc:
        mark_file_delete_retryable(request_id, lease_owner)
        raise storage_http_error(exc) from exc
    try:
        finalize_file_delete(
            file_id=file_id,
            expected_version=expected_version,
            request_id=request_id,
            lease_owner=lease_owner,
        )
    except FileDeleteLeaseLost as exc:
        # A concurrent retry may already have completed the same idempotent
        # request; preserve the established replay behavior.
        with db() as connection:
            current = checked_request_result(connection, request_id, actionKey, request_hash)
        if current is None or current["state"] != "completed":
            raise HTTPException(409, "File delete lease was superseded") from exc
    return Response(status_code=204)


@app.get("/api/integration/sso")
def sso(request: Request, ticket: str, redirect: str = "/"):
    if not redirect.startswith("/") or redirect.startswith("//") or urlsplit(redirect).scheme:
        raise HTTPException(400, "redirect must be a site-relative path")
    claims = decode_jwt(ticket, "zhuojian-sso")
    module_key, jti = str(claims.get("moduleKey") or ""), str(claims.get("jti") or "")
    if module_key not in MODULES or not jti:
        raise HTTPException(403, "Invalid module session")
    page_keys, action_keys, page_access = claims.get("pageKeys"), claims.get("actionKeys"), claims.get("pageAccess")
    if (
        not isinstance(page_keys, list) or not page_keys
        or any(not isinstance(key, str) for key in page_keys)
        or not isinstance(action_keys, list) or any(not isinstance(key, str) for key in action_keys)
        or not isinstance(page_access, dict)
        or not isinstance(claims.get("roleIds"), list)
        or not isinstance(claims.get("effectiveDataScope"), dict)
    ):
        raise HTTPException(403, "SSO page/action scope is required")
    if set(page_keys) != set(page_access):
        raise HTTPException(403, "SSO page scope mismatch")
    for page_key in page_keys:
        page = PAGES.get(str(page_key))
        access = page_access.get(page_key)
        if not page or page["moduleKey"] != module_key or not isinstance(access, dict):
            raise HTTPException(403, "SSO page is outside the module")
        if any(key not in page.get("actionKeys", []) or key not in action_keys for key in access.get("actionKeys") or []):
            raise HTTPException(403, "SSO action is outside the page scope")
    allowed_routes = {str(PAGES[key]["routePattern"]) for key in page_keys}
    if redirect not in allowed_routes:
        raise HTTPException(403, "SSO redirect is not an authorized page")
    with db() as connection:
        if connection.execute("SELECT 1 FROM consumed_tickets WHERE jti=?", (jti,)).fetchone():
            raise HTTPException(409, "SSO ticket was already consumed")
        connection.execute("INSERT INTO consumed_tickets VALUES(?,?)", (jti, int(claims["exp"])))
    request.session.update({
        "sub": claims["sub"], "organizationId": claims["organizationId"],
        "departmentId": claims.get("departmentId"),
        "departmentIds": claims.get("departmentIds") or [],
        "roleIds": claims.get("roleIds") or [],
        "effectiveDataScope": claims.get("effectiveDataScope") or {},
        "moduleKey": module_key, "permissions": claims.get("permissions") or [],
        "pageKeys": page_keys, "actionKeys": action_keys, "pageAccess": page_access,
    })
    return RedirectResponse(redirect, status_code=302)


@app.get("/")
@app.get("/{path:path}")
def frontend(request: Request, path: str = ""):
    if path.startswith("api/"):
        raise HTTPException(404)
    if not request.session.get("sub"):
        return JSONResponse({"detail": "Open this module from ZhuoJian SaaS"}, status_code=401)
    page_key = next((
        key for key in request.session.get("pageKeys") or []
        if PAGES.get(key, {}).get("routePattern") == request.url.path
    ), None)
    if page_key is None:
        return JSONResponse({"detail": "Page permission denied"}, status_code=403)
    return FileResponse(ROOT / "static" / "index.html")
