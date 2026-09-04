from __future__ import annotations

import importlib
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(
    (PROJECT_ROOT / "subsystem.json").is_file(),
    "action integration tests run in a project produced by scaffold_subsystem.py",
)
class ActionRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import jwt
            from fastapi.testclient import TestClient
        except ImportError as exc:  # pragma: no cover
            raise unittest.SkipTest("install requirements-dev.txt to run route tests") from exc

        cls.jwt = jwt
        cls.temporary = tempfile.TemporaryDirectory()
        temporary_root = Path(cls.temporary.name)
        cls.previous_environment = {
            key: os.environ.get(key)
            for key in (
                "ZHUOJIAN_INTEGRATION_SECRET",
                "SESSION_SECRET",
                "ZHUOJIAN_ORGANIZATION_ID",
                "ZHUOJIAN_PUBLIC_ORIGIN",
                "DATABASE_PATH",
                "FILE_STORAGE_DRIVER",
                "FILE_STORAGE_ROOT",
            )
        }
        cls.integration_secret = "integration-secret-used-only-by-action-tests"
        cls.organization_id = "test-organization"
        os.environ.update({
            "ZHUOJIAN_INTEGRATION_SECRET": cls.integration_secret,
            "SESSION_SECRET": "session-secret-used-only-by-action-tests-123",
            "ZHUOJIAN_ORGANIZATION_ID": cls.organization_id,
            "ZHUOJIAN_PUBLIC_ORIGIN": "https://testserver",
            "DATABASE_PATH": str(temporary_root / "subsystem.db"),
            "FILE_STORAGE_DRIVER": "local",
            "FILE_STORAGE_ROOT": str(temporary_root / "files"),
        })
        sys.path.insert(0, str(PROJECT_ROOT))
        sys.modules.pop("app", None)
        cls.application = importlib.import_module("app")
        cls.client_context = TestClient(
            cls.application.app,
            base_url="https://testserver",
            headers={"Origin": "https://testserver"},
        )
        cls.client = cls.client_context.__enter__()

        module = next(iter(cls.application.MODULES.values()))
        cls.module_key = module["moduleKey"]
        page = module["pages"][0]
        cls.page_key = page["pageKey"]
        cls.actions = {
            action["operation"]: cls.application.ACTIONS[action["actionKey"]]
            for action in module["actions"]
        }
        action_keys = [action["actionKey"] for action in module["actions"]]
        now = int(time.time())
        ticket = jwt.encode({
            "iss": "zhuojian-saas",
            "typ": "zhuojian-sso",
            "aud": cls.application.APP_SLUG,
            "sub": "page-action-test-user",
            "organizationId": cls.organization_id,
            "moduleKey": cls.module_key,
            "pageKeys": [cls.page_key],
            "actionKeys": action_keys,
            "pageAccess": {
                cls.page_key: {
                    "actionKeys": action_keys,
                    "permissions": [
                        "view", "ai_query", "ai_create", "ai_update",
                        "ai_delete", "ai_approve", "export",
                    ],
                }
            },
            "jti": uuid4().hex,
            "iat": now,
            "exp": now + 120,
        }, cls.integration_secret, algorithm="HS256")
        response = cls.client.get(
            "/api/integration/sso",
            params={"ticket": ticket, "redirect": page["routePattern"]},
            follow_redirects=False,
        )
        if response.status_code != 302:
            raise AssertionError(response.text)

    @classmethod
    def tearDownClass(cls):
        cls.client_context.__exit__(None, None, None)
        sys.modules.pop("app", None)
        if sys.path and sys.path[0] == str(PROJECT_ROOT):
            sys.path.pop(0)
        for key, value in cls.previous_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        cls.temporary.cleanup()

    def action_body(
        self,
        operation: str,
        request_id: str,
        params: dict,
        expected_version: int | None = None,
    ) -> dict:
        return {
            "requestId": request_id,
            "moduleKey": self.module_key,
            "pageKey": self.page_key,
            "operation": operation,
            "expectedVersion": expected_version,
            "params": params,
        }

    def action_token(
        self,
        operation: str,
        request_id: str,
        params: dict,
        *,
        confirmed_at: datetime | None = None,
        confirmation_id: str | None = None,
    ) -> str:
        action = self.actions[operation]
        now = int(time.time())
        claims = {
            "iss": "zhuojian-saas",
            "typ": "zhuojian-action",
            "aud": self.application.APP_SLUG,
            "sub": "integration-action-test-user",
            "organizationId": self.organization_id,
            "moduleKey": self.module_key,
            "pageKey": self.page_key,
            "actionKey": action["actionKey"],
            "operation": operation,
            "requestId": request_id,
            "permissions": ["view", self.application.required_permission(operation)],
            "iat": now,
            "exp": now + 60,
        }
        if action.get("requiresConfirmation"):
            claims.update({
                "confirmed": True,
                "confirmationId": confirmation_id or str(uuid4()),
                "confirmedBy": claims["sub"],
                "confirmedAt": (confirmed_at or datetime.now(timezone.utc)).isoformat(),
                "paramsHash": self.application.canonical_hash(params),
            })
        return self.jwt.encode(claims, self.integration_secret, algorithm="HS256")

    def event_body(self, *, event_type: str = "inventory.changed.v1") -> dict:
        return {
            "deliveryId": "delivery-" + uuid4().hex,
            "sourceApplicationSlug": "inventory-source",
            "event": {
                "eventId": "event-" + uuid4().hex,
                "eventType": event_type,
                "enterpriseKey": self.application.MANIFEST["enterprise"]["key"],
                "moduleKey": "inventory",
                "entityType": "stock_item",
                "entityId": "STOCK-001",
                "occurredAt": datetime.now(timezone.utc).isoformat(),
                "payload": {"available": 3},
            },
        }

    def event_token(self, body: dict, **overrides) -> str:
        now = int(time.time())
        event = body["event"]
        claims = {
            "iss": "zhuojian-saas",
            "typ": "zhuojian-event",
            "aud": self.application.APP_SLUG,
            "organizationId": self.organization_id,
            "deliveryId": body["deliveryId"],
            "eventId": event["eventId"],
            "eventType": event["eventType"],
            "targetModuleKey": self.module_key,
            "iat": now,
            "exp": now + 60,
            **overrides,
        }
        return self.jwt.encode(claims, self.integration_secret, algorithm="HS256")

    def post_event(self, body: dict, *, token: str | None = None):
        return self.client.post(
            "/api/integration/event-deliveries",
            headers={"Authorization": f"Bearer {token or self.event_token(body)}"},
            json=body,
        )

    def post_integration(
        self,
        operation: str,
        body: dict,
        *,
        token: str | None = None,
    ):
        request_id = body.get("requestId")
        params = body.get("params") if isinstance(body.get("params"), dict) else {}
        token = token or self.action_token(operation, request_id, params)
        return self.client.post(
            f"/api/integration/actions/{self.actions[operation]['actionKey']}",
            headers={"Authorization": f"Bearer {token}"},
            json=body,
        )

    def test_request_id_schema_and_payload_binding_are_enforced(self):
        bad_body = self.action_body("create", "bad/id", {"id": uuid4().hex})
        bad = self.post_integration("create", bad_body)
        self.assertEqual(bad.status_code, 422, bad.text)

        request_id = uuid4().hex
        record_id = uuid4().hex
        body = self.action_body("create", request_id, {"id": record_id, "name": "first"})
        created = self.post_integration("create", body)
        self.assertEqual(created.status_code, 200, created.text)

        changed = self.action_body("create", request_id, {"id": record_id, "name": "changed"})
        rebound = self.post_integration("create", changed)
        self.assertEqual(rebound.status_code, 409, rebound.text)

    def test_ai_disabled_action_is_enforced_by_the_module(self):
        action = self.actions["query"]
        previous = action.get("aiEnabled")
        action["aiEnabled"] = False
        try:
            body = self.action_body("query", uuid4().hex, {})
            response = self.post_integration("query", body)
        finally:
            action["aiEnabled"] = previous

        self.assertEqual(response.status_code, 404, response.text)

    def test_sibling_origin_cannot_use_the_ui_session_with_simple_content_type(self):
        body = self.action_body("delete", uuid4().hex, {"id": "victim"}, expected_version=1)
        response = self.client.post(
            "/api/ui/confirmations",
            headers={
                "Origin": "https://evil-sibling.example.com",
                "Content-Type": "text/plain",
            },
            content=json.dumps({
                **body,
                "actionKey": self.actions["delete"]["actionKey"],
                "confirmed": True,
            }),
        )

        self.assertEqual(response.status_code, 403, response.text)

    def test_event_delivery_is_schema_bound_subscribed_and_conflict_safe(self):
        body = self.event_body()
        module_events = self.application.MODULES[self.module_key].setdefault(
            "events", {"publishes": [], "subscribes": []}
        )
        previous_subscriptions = list(module_events.get("subscribes") or [])
        module_events["subscribes"] = [body["event"]["eventType"]]
        token = self.event_token(body)
        try:
            accepted = self.post_event(body, token=token)
            replay = self.post_event(body, token=token)
            changed = {
                **body,
                "event": {**body["event"], "payload": {"available": 999}},
            }
            conflict = self.post_event(changed, token=token)
        finally:
            module_events["subscribes"] = previous_subscriptions

        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(accepted.json()["status"], "accepted")
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.json()["status"], "duplicate")
        self.assertEqual(conflict.status_code, 409, conflict.text)

    def test_event_delivery_rejects_invalid_scope_schema_and_unsubscribed_type(self):
        body = self.event_body()
        module_events = self.application.MODULES[self.module_key].setdefault(
            "events", {"publishes": [], "subscribes": []}
        )
        previous_subscriptions = list(module_events.get("subscribes") or [])
        try:
            module_events["subscribes"] = []
            unsubscribed = self.post_event(body)

            module_events["subscribes"] = [body["event"]["eventType"]]
            invalid_source = self.post_event({**body, "sourceApplicationSlug": "Bad Source"})
            missing_entity = {
                **body,
                "event": {
                    key: value for key, value in body["event"].items() if key != "entityId"
                },
            }
            missing = self.post_event(missing_entity, token=self.event_token(body))
            cross_enterprise = {
                **body,
                "event": {**body["event"], "enterpriseKey": "other-company"},
            }
            cross = self.post_event(cross_enterprise, token=self.event_token(body))
            wrong_claim = self.post_event(
                body,
                token=self.event_token(body, eventType="different.event.v1"),
            )
        finally:
            module_events["subscribes"] = previous_subscriptions

        self.assertEqual(unsubscribed.status_code, 403, unsubscribed.text)
        self.assertEqual(invalid_source.status_code, 422, invalid_source.text)
        self.assertEqual(missing.status_code, 422, missing.text)
        self.assertEqual(cross.status_code, 403, cross.text)
        self.assertEqual(wrong_claim.status_code, 403, wrong_claim.text)

    def test_jwt_requires_exp_and_enforces_contract_lifetime(self):
        now = int(time.time())
        for token_type, maximum_lifetime in (
            ("zhuojian-sso", 120),
            ("zhuojian-action", 60),
            ("zhuojian-event", 60),
        ):
            base_claims = {
                "iss": "zhuojian-saas",
                "typ": token_type,
                "aud": self.application.APP_SLUG,
                "organizationId": self.organization_id,
                "iat": now,
            }
            missing_exp = self.jwt.encode(
                base_claims,
                self.integration_secret,
                algorithm="HS256",
            )
            with self.assertRaises(Exception) as missing_context:
                self.application.decode_jwt(missing_exp, token_type)
            self.assertEqual(missing_context.exception.status_code, 401)

            excessive = self.jwt.encode(
                {**base_claims, "exp": now + maximum_lifetime + 1},
                self.integration_secret,
                algorithm="HS256",
            )
            with self.assertRaises(Exception) as excessive_context:
                self.application.decode_jwt(excessive, token_type)
            self.assertEqual(excessive_context.exception.status_code, 401)

            valid = self.jwt.encode(
                {**base_claims, "exp": now + maximum_lifetime},
                self.integration_secret,
                algorithm="HS256",
            )
            self.assertEqual(
                self.application.decode_jwt(valid, token_type)["typ"],
                token_type,
            )

    def test_concurrent_same_request_executes_business_change_once(self):
        request_id = uuid4().hex
        record_id = uuid4().hex
        params = {"id": record_id, "name": "concurrent"}
        body = self.action_body("create", request_id, params)
        token = self.action_token("create", request_id, params)

        def submit():
            return self.post_integration("create", body, token=token)

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _index: submit(), range(2)))

        self.assertEqual([response.status_code for response in responses], [200, 200])
        self.assertEqual(responses[0].json(), responses[1].json())
        with closing(sqlite3.connect(os.environ["DATABASE_PATH"])) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM records WHERE id=?", (record_id,)).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM outbox WHERE entity_id=?", (record_id,)).fetchone()[0],
                1,
            )
            request_row = connection.execute(
                "SELECT state,request_hash FROM request_results WHERE request_id=?",
                (request_id,),
            ).fetchone()
            self.assertEqual(request_row[0], "completed")
            self.assertRegex(request_row[1], r"^[0-9a-f]{64}$")

    def test_business_change_and_idempotency_result_rollback_together(self):
        request_id = uuid4().hex
        record_id = uuid4().hex
        action = self.actions["create"]
        params = {"id": record_id, "name": "crash-test"}
        request_hash = self.application.action_request_hash(
            action["actionKey"],
            self.module_key,
            self.page_key,
            "integration-action-test-user",
            params,
            None,
        )

        def crash_after_business_change(connection):
            self.application.execute_business_action(action, params, None, connection)
            raise RuntimeError("simulated crash before request result commit")

        with self.assertRaisesRegex(RuntimeError, "simulated crash"):
            self.application.execute_idempotent_action(
                action_key=action["actionKey"],
                action=action,
                request_id=request_id,
                request_hash=request_hash,
                actor="integration-action-test-user",
                params=params,
                confirmation=None,
                require_page_confirmation=False,
                perform=crash_after_business_change,
            )

        with closing(sqlite3.connect(os.environ["DATABASE_PATH"])) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM records WHERE id=?", (record_id,)).fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM request_results WHERE request_id=?", (request_id,)).fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM outbox WHERE entity_id=?", (record_id,)).fetchone()[0], 0)

        result = self.application.execute_idempotent_action(
            action_key=action["actionKey"],
            action=action,
            request_id=request_id,
            request_hash=request_hash,
            actor="integration-action-test-user",
            params=params,
            confirmation=None,
            require_page_confirmation=False,
            perform=lambda connection: self.application.execute_business_action(
                action, params, None, connection
            ),
        )
        self.assertEqual(result["id"], record_id)

    def test_high_risk_integration_confirmation_expires_and_is_idempotent(self):
        record_id = uuid4().hex
        create_id = uuid4().hex
        created = self.post_integration(
            "create",
            self.action_body("create", create_id, {"id": record_id}),
        )
        self.assertEqual(created.status_code, 200, created.text)

        params = {"id": record_id}
        expired_id = uuid4().hex
        expired_body = self.action_body("delete", expired_id, params, expected_version=1)
        expired_token = self.action_token(
            "delete",
            expired_id,
            params,
            confirmed_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        )
        expired = self.post_integration("delete", expired_body, token=expired_token)
        self.assertEqual(expired.status_code, 403, expired.text)

        request_id = uuid4().hex
        body = self.action_body("delete", request_id, params, expected_version=1)
        token = self.action_token("delete", request_id, params)
        deleted = self.post_integration("delete", body, token=token)
        repeated = self.post_integration("delete", body, token=token)
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(repeated.status_code, 200, repeated.text)
        self.assertEqual(deleted.json(), repeated.json())
        with closing(sqlite3.connect(os.environ["DATABASE_PATH"])) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM records WHERE id=?", (record_id,)).fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM consumed_confirmations WHERE request_id=?", (request_id,)).fetchone()[0], 1)

    def test_page_high_risk_action_requires_issued_matching_confirmation(self):
        record_id = uuid4().hex
        created = self.post_integration(
            "create",
            self.action_body("create", uuid4().hex, {"id": record_id}),
        )
        self.assertEqual(created.status_code, 200, created.text)

        request_id = uuid4().hex
        params = {"id": record_id}
        body = self.action_body("delete", request_id, params, expected_version=1)
        unconfirmed = self.client.post(
            f"/api/ui/actions/{self.actions['delete']['actionKey']}",
            json=body,
        )
        self.assertEqual(unconfirmed.status_code, 403, unconfirmed.text)

        issued = self.client.post(
            "/api/ui/confirmations",
            json={
                **body,
                "actionKey": self.actions["delete"]["actionKey"],
                "confirmed": True,
            },
        )
        self.assertEqual(issued.status_code, 201, issued.text)
        confirmed_body = {**body, "confirmation": issued.json()}
        deleted = self.client.post(
            f"/api/ui/actions/{self.actions['delete']['actionKey']}",
            json=confirmed_body,
        )
        repeated = self.client.post(
            f"/api/ui/actions/{self.actions['delete']['actionKey']}",
            json=confirmed_body,
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(repeated.status_code, 200, repeated.text)
        self.assertEqual(deleted.json(), repeated.json())

    def test_page_confirmation_cannot_refresh_database_issued_time(self):
        record_id = uuid4().hex
        created = self.post_integration(
            "create",
            self.action_body("create", uuid4().hex, {"id": record_id}),
        )
        self.assertEqual(created.status_code, 200, created.text)

        request_id = uuid4().hex
        params = {"id": record_id}
        body = self.action_body("delete", request_id, params, expected_version=1)
        issued = self.client.post(
            "/api/ui/confirmations",
            json={
                **body,
                "actionKey": self.actions["delete"]["actionKey"],
                "confirmed": True,
            },
        )
        self.assertEqual(issued.status_code, 201, issued.text)
        confirmation = issued.json()
        stale_database_time = (
            datetime.now(timezone.utc)
            - timedelta(seconds=self.application.CONFIRMATION_MAX_AGE_SECONDS + 1)
        ).isoformat()
        with closing(sqlite3.connect(os.environ["DATABASE_PATH"])) as connection:
            connection.execute(
                "UPDATE page_confirmations SET confirmed_at=? WHERE confirmation_id=?",
                (stale_database_time, confirmation["confirmationId"]),
            )
            connection.commit()

        forged_fresh_time = {
            **confirmation,
            "confirmedAt": datetime.now(timezone.utc).isoformat(),
        }
        rejected = self.client.post(
            f"/api/ui/actions/{self.actions['delete']['actionKey']}",
            json={**body, "confirmation": forged_fresh_time},
        )
        self.assertIn(rejected.status_code, {403, 409}, rejected.text)
        with closing(sqlite3.connect(os.environ["DATABASE_PATH"])) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM records WHERE id=?",
                    (record_id,),
                ).fetchone()[0],
                1,
            )


if __name__ == "__main__":
    unittest.main()
