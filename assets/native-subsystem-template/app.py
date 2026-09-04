from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit
from uuid import uuid4

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
SAAS_ORIGINS = [item.strip() for item in os.getenv(
    "ZHUOJIAN_SAAS_ORIGINS", "https://ai-platform.staging.zhuojianai.com"
).split(",") if item.strip()]
try:
    FILE_STORAGE_MAX_UPLOAD_BYTES = int(os.getenv("FILE_STORAGE_MAX_UPLOAD_BYTES", str(512 * 1024 * 1024)))
except ValueError as exc:
    raise RuntimeError("FILE_STORAGE_MAX_UPLOAD_BYTES must be an integer") from exc
if FILE_STORAGE_MAX_UPLOAD_BYTES <= 0:
    raise RuntimeError("FILE_STORAGE_MAX_UPLOAD_BYTES must be greater than zero")

FILE_STORAGE: dict[str, Any] = {}
PRIMARY_STORAGE_BACKEND: str | None = None

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


def require_upload_capacity(incoming_bytes: int = 0) -> None:
    """Enforce the Runtime disk gate for local files and OSS buffering."""

    state_path = os.getenv("FILE_STORAGE_STATE_FILE", "").strip()
    minimum_free_bytes = 5 * 1024**3
    if state_path:
        try:
            raw = Path(state_path).read_bytes()
            if len(raw) > 64 * 1024:
                raise ValueError("state file is too large")
            state = json.loads(raw)
            minimum_free_bytes = int(
                float((state.get("thresholds") or {}).get("minimumFreeGiB", 5))
                * 1024**3
            )
            if state.get("uploadsAllowed") is not True:
                raise HTTPException(507, "File storage space is insufficient; contact an administrator")
        except HTTPException:
            raise
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise HTTPException(503, "File storage capacity status is unavailable") from exc
    try:
        free_bytes = shutil.disk_usage(upload_spool_dir()).free
    except OSError as exc:
        raise HTTPException(503, "File storage capacity status is unavailable") from exc
    # During an upload the request spool and the destination/gateway spool can
    # briefly coexist, so reserve twice the announced payload plus the floor.
    if free_bytes - (incoming_bytes * 2) < minimum_free_bytes:
        raise HTTPException(507, "File storage space is insufficient; contact an administrator")


def init_db() -> None:
    with db() as connection:
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS records (
          id TEXT PRIMARY KEY, module_key TEXT NOT NULL, data TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'draft', version INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS request_results (
          request_id TEXT PRIMARY KEY, action_key TEXT NOT NULL, result TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS outbox (
          sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
          event_type TEXT NOT NULL, module_key TEXT NOT NULL, entity_type TEXT NOT NULL,
          entity_id TEXT NOT NULL, occurred_at TEXT NOT NULL, payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS consumed_tickets (jti TEXT PRIMARY KEY, expires_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS consumed_confirmations (confirmation_id TEXT PRIMARY KEY, consumed_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS event_deliveries (
          delivery_id TEXT PRIMARY KEY, event_id TEXT NOT NULL UNIQUE, event_type TEXT NOT NULL,
          result TEXT NOT NULL, received_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS stored_files (
          storage_key TEXT PRIMARY KEY, file_id TEXT NOT NULL UNIQUE,
          module_key TEXT NOT NULL, original_name TEXT NOT NULL,
          mime_type TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL,
          storage_backend TEXT NOT NULL, created_by TEXT NOT NULL,
          business_type TEXT, business_id TEXT,
          deletion_state TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_stored_files_module_created
          ON stored_files(module_key, created_at DESC);
        """)
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(stored_files)")
        }
        additions = {
            "file_id": "TEXT",
            "business_type": "TEXT",
            "business_id": "TEXT",
            "deletion_state": "TEXT NOT NULL DEFAULT 'active'",
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


@app.on_event("startup")
def startup() -> None:
    storage_adapter()
    upload_spool_dir()
    init_db()


@app.middleware("http")
async def security_headers(request: Request, call_next):
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
        claims = jwt.decode(token, INTEGRATION_SECRET, algorithms=["HS256"], audience=APP_SLUG)
    except jwt.PyJWTError as exc:
        raise HTTPException(401, "Invalid integration JWT") from exc
    if claims.get("iss") != "zhuojian-saas" or claims.get("typ") != expected_type:
        raise HTTPException(401, "Invalid integration JWT type")
    if str(claims.get("organizationId") or "") != EXPECTED_ORGANIZATION_ID:
        raise HTTPException(403, "Enterprise organization mismatch")
    return claims


def canonical_hash(params: dict[str, Any]) -> str:
    encoded = json.dumps(params, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


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


def execute_business_action(action: dict, params: dict, expected_version: str | int | None) -> dict:
    operation = action["operation"]
    module_key = action["moduleKey"]
    now = datetime.now(timezone.utc).isoformat()
    with db() as connection:
        if operation == "query":
            rows = connection.execute(
                "SELECT id,data,status,version,created_at,updated_at FROM records WHERE module_key=? ORDER BY updated_at DESC LIMIT 200",
                (module_key,),
            ).fetchall()
            return {"items": [{**json.loads(row["data"]), "id": row["id"], "status": row["status"], "version": row["version"]} for row in rows]}
        if operation == "create":
            record_id = str(params.get("id") or uuid4().hex)
            data = {key: value for key, value in params.items() if key not in {"id", "status", "version"}}
            connection.execute(
                "INSERT INTO records(id,module_key,data,status,version,created_at,updated_at) VALUES(?,?,?,?,1,?,?)",
                (record_id, module_key, json.dumps(data, ensure_ascii=False), str(params.get("status") or "draft"), now, now),
            )
            emit_event(connection, module_key, f"{module_key}.created.v1", record_id, {"version": 1})
            return {"id": record_id, "version": 1, "status": str(params.get("status") or "draft")}
        record_id = str(params.get("id") or "")
        row = connection.execute("SELECT * FROM records WHERE id=? AND module_key=?", (record_id, module_key)).fetchone()
        if row is None:
            raise HTTPException(404, "Business record not found")
        if expected_version is None or str(row["version"]) != str(expected_version):
            raise HTTPException(409, "Business record version conflict")
        if operation == "update":
            data = json.loads(row["data"])
            data.update({key: value for key, value in params.items() if key not in {"id", "status", "version"}})
            version = row["version"] + 1
            status = str(params.get("status") or row["status"])
            connection.execute("UPDATE records SET data=?,status=?,version=?,updated_at=? WHERE id=?", (json.dumps(data, ensure_ascii=False), status, version, now, record_id))
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
    if action is None:
        raise HTTPException(404, "Action not found")
    claims = decode_jwt(bearer(authorization), "zhuojian-action")
    body = await request.json()
    module_key, page_key = str(body.get("moduleKey") or ""), str(body.get("pageKey") or "")
    request_id = str(body.get("requestId") or "")
    params = body.get("params") if isinstance(body.get("params"), dict) else {}
    if claims.get("moduleKey") != module_key or claims.get("pageKey") != page_key or claims.get("actionKey") != action_key:
        raise HTTPException(403, "Action context mismatch")
    if claims.get("operation") != action["operation"] or body.get("operation") != action["operation"]:
        raise HTTPException(403, "Action operation mismatch")
    if claims.get("requestId") != request_id or not page_allows(module_key, page_key, action_key):
        raise HTTPException(403, "Action is not allowed on this page")
    permissions = set(claims.get("permissions") or [])
    if "view" not in permissions or required_permission(action["operation"]) not in permissions:
        raise HTTPException(403, "Action permission denied")
    if action.get("requiresConfirmation"):
        confirmation_id = str(claims.get("confirmationId") or "")
        if not claims.get("confirmed") or claims.get("confirmedBy") != claims.get("sub") or claims.get("paramsHash") != canonical_hash(params) or not confirmation_id:
            raise HTTPException(403, "Valid user confirmation required")
        with db() as connection:
            if connection.execute("SELECT 1 FROM consumed_confirmations WHERE confirmation_id=?", (confirmation_id,)).fetchone():
                stored = connection.execute("SELECT result FROM request_results WHERE request_id=?", (request_id,)).fetchone()
                if stored:
                    return json.loads(stored["result"])
                raise HTTPException(409, "Confirmation has already been consumed")
            connection.execute("INSERT INTO consumed_confirmations VALUES(?,?)", (confirmation_id, datetime.now(timezone.utc).isoformat()))
    with db() as connection:
        stored = connection.execute("SELECT action_key,result FROM request_results WHERE request_id=?", (request_id,)).fetchone()
        if stored:
            if stored["action_key"] != action_key:
                raise HTTPException(409, "requestId is bound to another action")
            return json.loads(stored["result"])
    result = execute_business_action(action, params, body.get("expectedVersion"))
    with db() as connection:
        connection.execute("INSERT INTO request_results VALUES(?,?,?,?)", (request_id, action_key, json.dumps(result, ensure_ascii=False), datetime.now(timezone.utc).isoformat()))
    return result


@app.post("/api/integration/event-deliveries")
async def receive_event(request: Request, authorization: str | None = Header(default=None)):
    claims = decode_jwt(bearer(authorization), "zhuojian-event")
    body = await request.json()
    delivery_id, event = str(body.get("deliveryId") or ""), body.get("event")
    if claims.get("deliveryId") != delivery_id or not isinstance(event, dict) or claims.get("eventId") != event.get("eventId"):
        raise HTTPException(403, "Event delivery context mismatch")
    with db() as connection:
        existing = connection.execute("SELECT result FROM event_deliveries WHERE delivery_id=? OR event_id=?", (delivery_id, event.get("eventId"))).fetchone()
        if existing:
            return {"status": "duplicate", **json.loads(existing["result"])}
        result = {"eventId": event["eventId"], "accepted": True}
        connection.execute("INSERT INTO event_deliveries VALUES(?,?,?,?,?)", (delivery_id, event["eventId"], event["eventType"], json.dumps(result), datetime.now(timezone.utc).isoformat()))
    return {"status": "accepted", **result}


@app.post("/api/ui/actions/{action_key}")
async def invoke_page_action(action_key: str, request: Request):
    action = ACTIONS.get(action_key)
    session = request.session
    if action is None or not session.get("sub"):
        raise HTTPException(401, "Open this module from ZhuoJian SaaS")
    body = await request.json()
    page_key = str(body.get("pageKey") or "")
    if not session_allows(session, action["moduleKey"], page_key, action_key):
        raise HTTPException(403, "Page action context mismatch")
    permissions = set((session.get("pageAccess") or {}).get(page_key, {}).get("permissions") or [])
    if "view" not in permissions or required_permission(action["operation"]) not in permissions:
        raise HTTPException(403, "Page action permission denied")
    if action.get("requiresConfirmation") and body.get("confirmed") is not True:
        raise HTTPException(409, "Explicit page confirmation required")
    params = body.get("params") if isinstance(body.get("params"), dict) else {}
    return execute_business_action(action, params, body.get("expectedVersion"))


@app.get("/api/ui/files")
def list_files(request: Request, moduleKey: str, pageKey: str, actionKey: str):
    require_file_action(request, moduleKey, pageKey, actionKey, {"query", "export"})
    with db() as connection:
        rows = connection.execute(
            """
            SELECT file_id,original_name,mime_type,size,sha256,storage_backend,created_at
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
    if announced_size:
        try:
            announced_bytes = int(announced_size)
            if announced_bytes > FILE_STORAGE_MAX_UPLOAD_BYTES:
                raise HTTPException(413, "File exceeds this module's upload limit")
            if announced_bytes < 0:
                raise HTTPException(400, "Invalid Content-Length")
        except ValueError as exc:
            raise HTTPException(400, "Invalid Content-Length") from exc
    else:
        announced_bytes = 0
    require_upload_capacity(announced_bytes)

    now = datetime.now(timezone.utc)
    file_id = uuid4().hex
    storage_key = f"{moduleKey}/{now:%Y/%m}/{file_id}"
    content_type = request.headers.get("content-type") or "application/octet-stream"
    with tempfile.SpooledTemporaryFile(
        max_size=8 * 1024 * 1024,
        mode="w+b",
        dir=upload_spool_dir(),
    ) as spool:
        received = 0
        async for chunk in request.stream():
            received += len(chunk)
            if received > FILE_STORAGE_MAX_UPLOAD_BYTES:
                raise HTTPException(413, "File exceeds this module's upload limit")
            require_upload_capacity()
            spool.write(chunk)
        spool.seek(0)
        try:
            metadata = await run_in_threadpool(
                lambda: storage_adapter().put(storage_key, spool, content_type=content_type)
            )
        except StorageError as exc:
            raise storage_http_error(exc) from exc

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
                    metadata.storage_key,
                    file_id,
                    moduleKey,
                    filename,
                    content_type,
                    metadata.size,
                    metadata.sha256 or "",
                    metadata.backend,
                    str(request.session["sub"]),
                    businessType,
                    businessId,
                    "active",
                    now.isoformat(),
                ),
            )
    except sqlite3.Error as exc:
        try:
            await run_in_threadpool(storage_adapter().delete, metadata.storage_key)
        except StorageError:
            pass
        raise HTTPException(500, "File metadata could not be saved") from exc

    return {
        "fileId": file_id,
        "storageBackend": metadata.backend,
        "filename": filename,
        "mimeType": content_type,
        "size": metadata.size,
        "sha256": metadata.sha256,
    }


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
    require_file_action(request, moduleKey, pageKey, actionKey, {"delete"})
    with db() as connection:
        row = connection.execute(
            """
            SELECT storage_key,storage_backend,deletion_state
            FROM stored_files WHERE file_id=? AND module_key=?
            """,
            (file_id, moduleKey),
        ).fetchone()
        if row is None or row["deletion_state"] == "deleted":
            return Response(status_code=204)
        connection.execute(
            "UPDATE stored_files SET deletion_state='pending' WHERE file_id=?",
            (file_id,),
        )
    try:
        await run_in_threadpool(
            storage_adapter(row["storage_backend"]).delete,
            row["storage_key"],
        )
    except StorageError as exc:
        raise storage_http_error(exc) from exc
    with db() as connection:
        connection.execute(
            "UPDATE stored_files SET deletion_state='deleted' WHERE file_id=?",
            (file_id,),
        )
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
