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
from unittest import mock
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
                "ZHUOJIAN_MANIFEST_ACCESS_TOKEN",
                "ZHUOJIAN_SSO_EXCHANGE_TOKEN",
                "ZHUOJIAN_ACTION_SIGNING_SECRET",
                "ZHUOJIAN_EVENT_SIGNING_SECRET",
                "SESSION_SECRET",
                "ZHUOJIAN_ORGANIZATION_ID",
                "ZHUOJIAN_PUBLIC_ORIGIN",
                "ZHUOJIAN_SAAS_ORIGIN",
                "DATABASE_PATH",
                "FILE_STORAGE_DRIVER",
                "FILE_STORAGE_ROOT",
            )
        }
        cls.manifest_token = "zjmf_manifest-token-used-only-by-action-tests-123456789"
        cls.sso_token = "zjss_exchange-token-used-only-by-action-tests-123456789"
        cls.action_secret = "zjac_action-secret-used-only-by-action-tests-123456789"
        cls.event_secret = "zjev_event-secret-used-only-by-action-tests-123456789"
        cls.organization_id = "11111111-1111-4111-8111-111111111111"
        cls.department_id = "ops"
        cls.role_ids = ["ops-owner"]
        cls.effective_data_scope = {
            "unrestricted": False,
            "include_self": False,
            "own_only": False,
            "department_ids": [cls.department_id],
        }
        os.environ.update({
            "ZHUOJIAN_MANIFEST_ACCESS_TOKEN": cls.manifest_token,
            "ZHUOJIAN_SSO_EXCHANGE_TOKEN": cls.sso_token,
            "ZHUOJIAN_ACTION_SIGNING_SECRET": cls.action_secret,
            "ZHUOJIAN_EVENT_SIGNING_SECRET": cls.event_secret,
            "SESSION_SECRET": "session-secret-used-only-by-action-tests-123",
            "ZHUOJIAN_ORGANIZATION_ID": cls.organization_id,
            "ZHUOJIAN_PUBLIC_ORIGIN": "https://testserver",
            "ZHUOJIAN_SAAS_ORIGIN": "https://saas.test.example.com",
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
        now = datetime.now(timezone.utc)
        launch_nonce = "launch_nonce_used_by_action_tests"
        code = "zjsc_" + "a" * 48
        claims = {
            "iss": "zhuojian-saas",
            "typ": "zhuojian-sso-code",
            "aud": cls.application.APP_SLUG,
            "sub": "page-action-test-user",
            "organizationId": cls.organization_id,
            "departmentId": cls.department_id,
            "departmentIds": [cls.department_id],
            "roleIds": cls.role_ids,
            "effectiveDataScope": cls.effective_data_scope,
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
                    "dataScopes": {
                        permission: cls.effective_data_scope
                        for permission in [
                            "view", "ai_query", "ai_create", "ai_update",
                            "ai_delete", "ai_approve", "export",
                        ]
                    },
                    "actionDataScopes": {
                        action_key: cls.effective_data_scope for action_key in action_keys
                    },
                }
            },
            "jti": uuid4().hex,
            "launchNonce": launch_nonce,
            "sessionBindingHash": "a" * 64,
            "authEpoch": 0,
            "permissions": [
                "view", "ai_query", "ai_create", "ai_update",
                "ai_delete", "ai_approve", "export",
            ],
            "iat": now.isoformat(),
            "exp": (now + timedelta(seconds=120)).isoformat(),
        }
        exchange_response = mock.Mock(
            status_code=200,
            content=b"{}",
            headers={"content-type": "application/json"},
        )
        cls.sso_claims = claims
        cls.sso_exchange_payload = {
            "application_id": "22222222-2222-4222-8222-222222222222",
            "application_slug": cls.application.APP_SLUG,
            "organization_id": cls.organization_id,
            "module_key": cls.module_key,
            "redirect": page["routePattern"],
            "launch_nonce": launch_nonce,
            "claims": claims,
        }
        exchange_response.json.return_value = cls.sso_exchange_payload
        with mock.patch.object(cls.application.httpx, "post", return_value=exchange_response):
            response = cls.client.get(
                "/api/integration/sso",
                params={
                    "code": code,
                    "redirect": page["routePattern"],
                    "launch_nonce": launch_nonce,
                },
                headers={
                    "Referer": cls.application.SAAS_ORIGIN + "/terminal",
                    "Sec-Fetch-Dest": "iframe",
                },
                follow_redirects=False,
            )
        if response.status_code != 302:
            raise AssertionError(response.text)
        if response.headers.get("location") != page["routePattern"]:
            raise AssertionError("SSO code was not removed from the redirect URL")
        cls.sso_set_cookie = response.headers.get("set-cookie", "")
        cls.live_session_patcher = mock.patch.object(
            cls.application,
            "validate_live_session",
            side_effect=lambda session, module_key, page_key, action_key=None: (
                cls.application.action_scoped_actor(session, page_key, action_key)
                if action_key
                else {
                    **session,
                    "effectiveDataScope": session["pageAccess"][page_key]["dataScopes"]["view"],
                }
            ),
        )
        cls.live_session_patcher.start()

    @classmethod
    def tearDownClass(cls):
        cls.live_session_patcher.stop()
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
            "departmentId": self.department_id,
            "departmentIds": [self.department_id],
            "roleIds": self.role_ids,
            "effectiveDataScope": self.effective_data_scope,
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
        return self.jwt.encode(claims, self.action_secret, algorithm="HS256")

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
        return self.jwt.encode(claims, self.event_secret, algorithm="HS256")

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

    def test_manifest_and_event_polling_accept_only_the_manifest_token(self):
        for path in ("/api/integration/manifest", "/api/integration/events"):
            accepted = self.client.get(
                path,
                headers={"Authorization": f"Bearer {self.manifest_token}"},
            )
            rejected = self.client.get(
                path,
                headers={"Authorization": f"Bearer {self.action_secret}"},
            )
            self.assertEqual(accepted.status_code, 200, accepted.text)
            self.assertEqual(rejected.status_code, 401, rejected.text)

    def test_sso_exchange_uses_fixed_url_and_dedicated_bearer(self):
        payload = json.loads(json.dumps(self.sso_exchange_payload))
        nonce = "different_launch_nonce_for_binding_test"
        payload["launch_nonce"] = "attacker_nonce_that_must_not_be_accepted"
        response_from_saas = mock.Mock(
            status_code=200,
            content=b"{}",
            headers={"content-type": "application/json"},
        )
        response_from_saas.json.return_value = payload

        with mock.patch.object(
            self.application.httpx, "post", return_value=response_from_saas
        ) as post:
            response = self.client.get(
                "/api/integration/sso",
                params={
                    "code": "zjsc_" + "c" * 48,
                    "redirect": payload["redirect"],
                    "launch_nonce": nonce,
                },
                headers={
                    "Referer": self.application.SAAS_ORIGIN + "/terminal",
                    "Sec-Fetch-Dest": "iframe",
                },
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(post.call_args.args[0], self.application.SSO_EXCHANGE_URL)
        self.assertEqual(
            post.call_args.kwargs["headers"]["Authorization"],
            f"Bearer {self.sso_token}",
        )
        self.assertEqual(post.call_args.kwargs["json"]["launch_nonce"], nonce)
        self.assertFalse(post.call_args.kwargs["follow_redirects"])
        self.assertFalse(post.call_args.kwargs["trust_env"])

    def test_sso_uses_small_partitioned_server_side_cookie_and_requires_saas_navigation(self):
        self.assertIn("SameSite=None", self.sso_set_cookie)
        self.assertIn("Partitioned", self.sso_set_cookie)
        self.assertIn("HttpOnly", self.sso_set_cookie)
        self.assertLess(len(self.sso_set_cookie), 1024)
        with closing(sqlite3.connect(os.environ["DATABASE_PATH"])) as connection:
            stored = connection.execute("SELECT data FROM browser_sessions").fetchone()
        self.assertIsNotNone(stored)
        self.assertIn("pageAccess", json.loads(stored[0]))

        response = self.client.get(
            "/api/integration/sso",
            params={
                "code": "zjsc_" + "n" * 48,
                "redirect": self.sso_exchange_payload["redirect"],
                "launch_nonce": self.sso_exchange_payload["launch_nonce"],
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 403, response.text)

    def test_sso_exchange_rejects_claims_over_120_seconds(self):
        payload = json.loads(json.dumps(self.sso_exchange_payload))
        now = datetime.now(timezone.utc)
        payload["claims"]["iat"] = now.isoformat()
        payload["claims"]["exp"] = (now + timedelta(seconds=121)).isoformat()
        response_from_saas = mock.Mock(
            status_code=200,
            content=b"{}",
            headers={"content-type": "application/json"},
        )
        response_from_saas.json.return_value = payload
        with mock.patch.object(
            self.application.httpx, "post", return_value=response_from_saas
        ):
            response = self.client.get(
                "/api/integration/sso",
                params={
                    "code": "zjsc_" + "d" * 48,
                    "redirect": payload["redirect"],
                    "launch_nonce": payload["launch_nonce"],
                },
                headers={
                    "Referer": self.application.SAAS_ORIGIN + "/terminal",
                    "Sec-Fetch-Dest": "iframe",
                },
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 401, response.text)

    def test_request_id_schema_and_payload_binding_are_enforced(self):
        bad_body = self.action_body(
            "create", "bad/id", {"id": uuid4().hex, "data": {}}
        )
        bad = self.post_integration("create", bad_body)
        self.assertEqual(bad.status_code, 422, bad.text)

        request_id = uuid4().hex
        record_id = uuid4().hex
        body = self.action_body(
            "create", request_id, {"id": record_id, "data": {"name": "first"}}
        )
        created = self.post_integration("create", body)
        self.assertEqual(created.status_code, 200, created.text)

        changed = self.action_body(
            "create", request_id, {"id": record_id, "data": {"name": "changed"}}
        )
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

    def test_role_scope_and_dynamic_action_parameter_shapes_are_enforced(self):
        denied = self.post_integration(
            "create",
            self.action_body(
                "create",
                uuid4().hex,
                {"data": {"name": "outside", "departmentId": "finance"}},
            ),
        )
        self.assertEqual(denied.status_code, 403, denied.text)

        record_id = uuid4().hex
        created = self.post_integration(
            "create",
            self.action_body(
                "create",
                uuid4().hex,
                {"id": record_id, "data": {"name": "inside"}},
            ),
        )
        self.assertEqual(created.status_code, 200, created.text)

        changed = self.post_integration(
            "update",
            self.action_body(
                "update",
                uuid4().hex,
                {"id": record_id, "changes": {"name": "updated"}},
                expected_version=1,
            ),
        )
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual(changed.json()["version"], 2)

        malformed = self.post_integration(
            "update",
            self.action_body(
                "update",
                uuid4().hex,
                {"id": record_id, "name": "flat-shape-is-not-v2.4"},
                expected_version=2,
            ),
        )
        self.assertEqual(malformed.status_code, 422, malformed.text)

        with closing(sqlite3.connect(os.environ["DATABASE_PATH"])) as connection:
            row = connection.execute(
                "SELECT data,department_id,created_by,version FROM records WHERE id=?",
                (record_id,),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(json.loads(row[0])["name"], "updated")
        self.assertEqual(row[1], self.department_id)
        self.assertEqual(row[2], "integration-action-test-user")
        self.assertEqual(row[3], 2)

    def test_page_action_uses_its_own_scope_not_the_broader_view_scope(self):
        session = json.loads(json.dumps(self.sso_claims))
        session["effectiveDataScope"] = {
            "unrestricted": True,
            "include_self": False,
            "own_only": False,
            "department_ids": [],
        }
        delete_key = self.actions["delete"]["actionKey"]
        session["pageAccess"][self.page_key]["actionDataScopes"][delete_key] = {
            "unrestricted": False,
            "include_self": True,
            "own_only": True,
            "department_ids": [],
        }

        actor = self.application.action_scoped_actor(session, self.page_key, delete_key)
        with self.assertRaises(self.application.HTTPException) as raised:
            self.application.require_record_scope(actor, "finance", "another-user")
        self.assertEqual(raised.exception.status_code, 403)

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
        for token_type, maximum_lifetime, signing_secret in (
            ("zhuojian-action", 60, self.action_secret),
            ("zhuojian-event", 60, self.event_secret),
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
                signing_secret,
                algorithm="HS256",
            )
            with self.assertRaises(Exception) as missing_context:
                self.application.decode_jwt(missing_exp, token_type)
            self.assertEqual(missing_context.exception.status_code, 401)

            excessive = self.jwt.encode(
                {**base_claims, "exp": now + maximum_lifetime + 1},
                signing_secret,
                algorithm="HS256",
            )
            with self.assertRaises(Exception) as excessive_context:
                self.application.decode_jwt(excessive, token_type)
            self.assertEqual(excessive_context.exception.status_code, 401)

            valid = self.jwt.encode(
                {**base_claims, "exp": now + maximum_lifetime},
                signing_secret,
                algorithm="HS256",
            )
            self.assertEqual(
                self.application.decode_jwt(valid, token_type)["typ"],
                token_type,
            )

    def test_concurrent_same_request_executes_business_change_once(self):
        request_id = uuid4().hex
        record_id = uuid4().hex
        params = {"id": record_id, "data": {"name": "concurrent"}}
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
        params = {"id": record_id, "data": {"name": "crash-test"}}
        actor_claims = {
            "sub": "integration-action-test-user",
            "departmentId": self.department_id,
            "roleIds": self.role_ids,
            "effectiveDataScope": self.effective_data_scope,
        }
        request_hash = self.application.action_request_hash(
            action["actionKey"],
            self.module_key,
            self.page_key,
            "integration-action-test-user",
            params,
            None,
        )

        def crash_after_business_change(connection):
            self.application.execute_business_action(
                action, params, None, actor_claims, connection
            )
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
                action, params, None, actor_claims, connection
            ),
        )
        self.assertEqual(result["id"], record_id)

    def test_high_risk_integration_confirmation_expires_and_is_idempotent(self):
        record_id = uuid4().hex
        create_id = uuid4().hex
        created = self.post_integration(
            "create",
            self.action_body("create", create_id, {"id": record_id, "data": {}}),
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

    def test_create_and_update_cannot_bypass_dedicated_status_action(self):
        record_id = uuid4().hex
        forged_create = self.post_integration(
            "create",
            self.action_body(
                "create",
                uuid4().hex,
                {"id": record_id, "data": {"status": "approved"}},
            ),
        )
        self.assertEqual(forged_create.status_code, 422, forged_create.text)

        created = self.post_integration(
            "create",
            self.action_body("create", uuid4().hex, {"id": record_id, "data": {}}),
        )
        self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(created.json()["status"], "draft")

        forged_update = self.post_integration(
            "update",
            self.action_body(
                "update",
                uuid4().hex,
                {"id": record_id, "changes": {"status": "approved"}},
                expected_version=1,
            ),
        )
        self.assertEqual(forged_update.status_code, 422, forged_update.text)
        with closing(sqlite3.connect(os.environ["DATABASE_PATH"])) as connection:
            status, version = connection.execute(
                "SELECT status, version FROM records WHERE id=?",
                (record_id,),
            ).fetchone()
        self.assertEqual((status, version), ("draft", 1))

    def test_page_high_risk_action_requires_issued_matching_confirmation(self):
        record_id = uuid4().hex
        created = self.post_integration(
            "create",
            self.action_body("create", uuid4().hex, {"id": record_id, "data": {}}),
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
            self.action_body("create", uuid4().hex, {"id": record_id, "data": {}}),
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
