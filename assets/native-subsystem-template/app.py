from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import jwt
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

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
        """)
        record_columns = {
            str(row["name"]) for row in connection.execute("PRAGMA table_info(records)").fetchall()
        }
        missing_scope_columns = {"department_id", "created_by"} - record_columns
        if missing_scope_columns:
            existing_records = int(connection.execute("SELECT COUNT(*) FROM records").fetchone()[0])
            if existing_records:
                raise RuntimeError(
                    "Existing records need an explicit department_id/created_by backfill before enabling "
                    "platform role data scopes"
                )
            if "department_id" in missing_scope_columns:
                connection.execute("ALTER TABLE records ADD COLUMN department_id TEXT")
            if "created_by" in missing_scope_columns:
                connection.execute("ALTER TABLE records ADD COLUMN created_by TEXT")


@app.on_event("startup")
def startup() -> None:
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
    expected_version: str | int | None,
    actor: dict,
) -> dict:
    operation = action["operation"]
    module_key = action["moduleKey"]
    now = datetime.now(timezone.utc).isoformat()
    with db() as connection:
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
                (record_id, module_key, json.dumps(data, ensure_ascii=False), department_id, created_by, status, now, now),
            )
            emit_event(connection, module_key, f"{module_key}.created.v1", record_id, {"version": 1})
            return {"id": record_id, "version": 1, "status": status}
        record_id = str(params.get("id") or "")
        row = connection.execute("SELECT * FROM records WHERE id=? AND module_key=?", (record_id, module_key)).fetchone()
        if row is None:
            raise HTTPException(404, "Business record not found")
        require_record_scope(actor, row["department_id"], row["created_by"])
        if operation in {"update", "delete"} and (
            expected_version is None or str(row["version"]) != str(expected_version)
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
                (json.dumps(data, ensure_ascii=False), department_id, status, version, now, record_id),
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
    return {"status": "ok", "applicationSlug": APP_SLUG, "contractRevision": MANIFEST.get("contractRevision")}


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
    result = execute_business_action(action, params, body.get("expectedVersion"), claims)
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
    return execute_business_action(action, params, body.get("expectedVersion"), session)


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
