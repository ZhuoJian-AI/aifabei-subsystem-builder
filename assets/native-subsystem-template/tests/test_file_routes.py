from __future__ import annotations

import importlib
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(
    (PROJECT_ROOT / "subsystem.json").is_file(),
    "route integration test runs in a project produced by scaffold_subsystem.py",
)
class FileRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import jwt
            from fastapi.testclient import TestClient
        except ImportError as exc:  # pragma: no cover - explains a missing dev install
            raise unittest.SkipTest("install requirements-dev.txt to run route tests") from exc

        cls.temporary = tempfile.TemporaryDirectory()
        temporary_root = Path(cls.temporary.name)
        cls.previous_environment = {
            key: os.environ.get(key)
            for key in (
                "ZHUOJIAN_INTEGRATION_SECRET",
                "SESSION_SECRET",
                "ZHUOJIAN_ORGANIZATION_ID",
                "DATABASE_PATH",
                "FILE_STORAGE_DRIVER",
                "FILE_STORAGE_ROOT",
            )
        }
        cls.integration_secret = "integration-secret-used-only-by-route-tests"
        cls.organization_id = "test-organization"
        os.environ.update({
            "ZHUOJIAN_INTEGRATION_SECRET": cls.integration_secret,
            "SESSION_SECRET": "session-secret-used-only-by-route-tests-123",
            "ZHUOJIAN_ORGANIZATION_ID": cls.organization_id,
            "DATABASE_PATH": str(temporary_root / "subsystem.db"),
            "FILE_STORAGE_DRIVER": "local",
            "FILE_STORAGE_ROOT": str(temporary_root / "files"),
        })
        sys.path.insert(0, str(PROJECT_ROOT))
        sys.modules.pop("app", None)
        cls.application = importlib.import_module("app")
        cls.client_context = TestClient(cls.application.app, base_url="https://testserver")
        cls.client = cls.client_context.__enter__()

        module = next(iter(cls.application.MODULES.values()))
        cls.module_key = module["moduleKey"]
        page = module["pages"][0]
        cls.page_key = page["pageKey"]
        cls.query_action = page["queryActionKey"]
        cls.create_action = next(
            action["actionKey"] for action in module["actions"] if action["operation"] == "create"
        )
        action_keys = [action["actionKey"] for action in module["actions"]]
        now = int(time.time())
        ticket = jwt.encode({
            "iss": "zhuojian-saas",
            "typ": "zhuojian-sso",
            "aud": cls.application.APP_SLUG,
            "sub": "route-test-user",
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
            "exp": now + 300,
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

    def test_upload_list_download_and_delete_use_opaque_file_id(self):
        payload = "一份测试附件".encode()
        upload = self.client.post(
            "/api/ui/files",
            params={
                "moduleKey": self.module_key,
                "pageKey": self.page_key,
                "actionKey": self.create_action,
                "filename": "测试附件.txt",
            },
            headers={"Content-Type": "text/plain; charset=utf-8"},
            content=payload,
        )
        self.assertEqual(upload.status_code, 201, upload.text)
        metadata = upload.json()
        self.assertEqual(metadata["storageBackend"], "local")
        self.assertRegex(metadata["fileId"], r"^[0-9a-f]{32}$")
        self.assertNotIn("storageKey", metadata)
        self.assertNotIn("FILE_STORAGE_ROOT", upload.text)

        listing = self.client.get(
            "/api/ui/files",
            params={
                "moduleKey": self.module_key,
                "pageKey": self.page_key,
                "actionKey": self.query_action,
            },
        )
        self.assertEqual(listing.status_code, 200, listing.text)
        self.assertEqual(listing.json()["items"][0]["fileId"], metadata["fileId"])
        self.assertNotIn("storageKey", listing.json()["items"][0])

        download = self.client.get(
            f"/api/ui/files/{metadata['fileId']}",
            params={
                "moduleKey": self.module_key,
                "pageKey": self.page_key,
                "actionKey": self.query_action,
            },
        )
        self.assertEqual(download.status_code, 200, download.text)
        self.assertEqual(download.content, payload)

        with sqlite3.connect(os.environ["DATABASE_PATH"]) as connection:
            row = connection.execute(
                "SELECT file_id,storage_key,storage_backend,deletion_state FROM stored_files"
            ).fetchone()
        self.assertEqual(row[0], metadata["fileId"])
        self.assertNotIn("测试附件", row[1])
        self.assertEqual(row[2:], ("local", "active"))

        deleted = self.client.delete(
            f"/api/ui/files/{metadata['fileId']}",
            params={
                "moduleKey": self.module_key,
                "pageKey": self.page_key,
                "actionKey": next(
                    action["actionKey"]
                    for action in next(iter(self.application.MODULES.values()))["actions"]
                    if action["operation"] == "delete"
                ),
            },
        )
        self.assertEqual(deleted.status_code, 204, deleted.text)
        self.assertEqual(
            self.client.get(
                f"/api/ui/files/{metadata['fileId']}",
                params={
                    "moduleKey": self.module_key,
                    "pageKey": self.page_key,
                    "actionKey": self.query_action,
                },
            ).status_code,
            404,
        )
        with sqlite3.connect(os.environ["DATABASE_PATH"]) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT deletion_state FROM stored_files WHERE file_id=?",
                    (metadata["fileId"],),
                ).fetchone()[0],
                "deleted",
            )


if __name__ == "__main__":
    unittest.main()
