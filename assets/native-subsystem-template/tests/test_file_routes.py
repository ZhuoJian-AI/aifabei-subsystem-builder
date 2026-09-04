from __future__ import annotations

import asyncio
import hashlib
import importlib
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
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
                "ZHUOJIAN_PUBLIC_ORIGIN",
                "DATABASE_PATH",
                "FILE_STORAGE_DRIVER",
                "FILE_STORAGE_ROOT",
                "FILE_STORAGE_STATE_FILE",
                "FILE_STORAGE_UPLOAD_LOCK_FILE",
            )
        }
        cls.integration_secret = "integration-secret-used-only-by-route-tests"
        cls.organization_id = "test-organization"
        upload_lock = temporary_root / "upload.lock"
        upload_lock.touch()
        os.environ.update({
            "ZHUOJIAN_INTEGRATION_SECRET": cls.integration_secret,
            "SESSION_SECRET": "session-secret-used-only-by-route-tests-123",
            "ZHUOJIAN_ORGANIZATION_ID": cls.organization_id,
            "ZHUOJIAN_PUBLIC_ORIGIN": "https://testserver",
            "DATABASE_PATH": str(temporary_root / "subsystem.db"),
            "FILE_STORAGE_DRIVER": "local",
            "FILE_STORAGE_ROOT": str(temporary_root / "files"),
            "FILE_STORAGE_UPLOAD_LOCK_FILE": str(upload_lock),
        })
        sys.path.insert(0, str(PROJECT_ROOT))
        sys.modules.pop("app", None)
        cls.application = importlib.import_module("app")
        cls.application.DELETION_RECOVERY_INTERVAL_SECONDS = 3600
        healthy_usage = type(
            "DiskUsage",
            (),
            {
                "total": 100 * 1024**3,
                "used": 10 * 1024**3,
                "free": 90 * 1024**3,
            },
        )()
        cls.disk_usage_patcher = mock.patch.object(
            cls.application.shutil, "disk_usage", return_value=healthy_usage
        )
        cls.disk_usage_patcher.start()
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
        cls.query_action = page["queryActionKey"]
        cls.create_action = next(
            action["actionKey"] for action in module["actions"] if action["operation"] == "create"
        )
        cls.delete_action = next(
            action["actionKey"] for action in module["actions"] if action["operation"] == "delete"
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
        cls.disk_usage_patcher.stop()
        sys.modules.pop("app", None)
        if sys.path and sys.path[0] == str(PROJECT_ROOT):
            sys.path.pop(0)
        for key, value in cls.previous_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        cls.temporary.cleanup()

    def upload(self, payload: bytes, filename: str = "test-upload.bin"):
        return self.client.post(
            "/api/ui/files",
            params={
                "moduleKey": self.module_key,
                "pageKey": self.page_key,
                "actionKey": self.create_action,
                "filename": filename,
            },
            headers={"Content-Type": "application/octet-stream"},
            content=payload,
        )

    def test_recovery_grace_cannot_be_shorter_than_the_late_commit_window(self):
        environment = os.environ.copy()
        environment["FILE_STORAGE_RECOVERY_GRACE_SECONDS"] = "1799"
        rejected = subprocess.run(
            [sys.executable, "-c", "import app"],
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("must be at least 1800", rejected.stderr)

        environment["FILE_STORAGE_RECOVERY_GRACE_SECONDS"] = "1800"
        accepted = subprocess.run(
            [sys.executable, "-c", "import app"],
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)

    def test_upload_requires_an_exact_content_length(self):
        def chunks():
            yield b"chunked-body"

        missing = self.client.post(
            "/api/ui/files",
            params={
                "moduleKey": self.module_key,
                "pageKey": self.page_key,
                "actionKey": self.create_action,
                "filename": "chunked.bin",
            },
            headers={"Content-Type": "application/octet-stream"},
            content=chunks(),
        )
        self.assertEqual(missing.status_code, 411, missing.text)

        too_small = self.client.post(
            "/api/ui/files",
            params={
                "moduleKey": self.module_key,
                "pageKey": self.page_key,
                "actionKey": self.create_action,
                "filename": "length-small.bin",
            },
            headers={"Content-Type": "application/octet-stream", "Content-Length": "2"},
            content=b"three",
        )
        self.assertEqual(too_small.status_code, 400, too_small.text)

        too_large = self.client.post(
            "/api/ui/files",
            params={
                "moduleKey": self.module_key,
                "pageKey": self.page_key,
                "actionKey": self.create_action,
                "filename": "length-large.bin",
            },
            headers={"Content-Type": "application/octet-stream", "Content-Length": "20"},
            content=b"short",
        )
        self.assertEqual(too_large.status_code, 400, too_large.text)

    def test_upload_reserves_both_disk_copies_before_reading_the_body(self):
        payload = b"12345678"
        floor = 5 * 1024**3
        free = floor + (2 * len(payload)) - 1
        disk_usage = type(
            "DiskUsage", (), {"total": 100 * 1024**3, "used": 10 * 1024**3, "free": free}
        )()
        with mock.patch.object(self.application.shutil, "disk_usage", return_value=disk_usage):
            response = self.upload(payload, "capacity.bin")
        self.assertEqual(response.status_code, 507, response.text)
        with closing(sqlite3.connect(os.environ["DATABASE_PATH"])) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM stored_files WHERE original_name='capacity.bin'"
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_live_used_percent_closes_a_stale_host_state_window(self):
        state_path = Path(os.environ["DATABASE_PATH"]).parent / "stale-storage-state.json"
        state_path.write_text(
            '{"uploadsAllowed":true,"thresholds":'
            '{"minimumFreeGiB":5,"stopUploadUsedPercent":90}}',
            encoding="utf-8",
        )
        old_state_path = os.environ.get("FILE_STORAGE_STATE_FILE")
        os.environ["FILE_STORAGE_STATE_FILE"] = str(state_path)
        usage = type(
            "DiskUsage",
            (),
            {
                "total": 100 * 1024**3,
                "used": 91 * 1024**3,
                "free": 9 * 1024**3,
            },
        )()
        try:
            with mock.patch.object(self.application.shutil, "disk_usage", return_value=usage):
                response = self.upload(b"x", "live-percent.bin")
        finally:
            if old_state_path is None:
                os.environ.pop("FILE_STORAGE_STATE_FILE", None)
            else:
                os.environ["FILE_STORAGE_STATE_FILE"] = old_state_path
        self.assertEqual(response.status_code, 507, response.text)

    def test_host_upload_lock_serializes_concurrent_backend_commits(self):
        adapter = self.application.storage_adapter()
        original_put = adapter.put
        first_entered = threading.Event()
        release_first = threading.Event()
        counter_lock = threading.Lock()
        active = 0
        maximum_active = 0

        def delayed_put(*args, **kwargs):
            nonlocal active, maximum_active
            with counter_lock:
                active += 1
                maximum_active = max(maximum_active, active)
                call_number = maximum_active
            if call_number == 1 and not first_entered.is_set():
                first_entered.set()
                if not release_first.wait(timeout=5):
                    raise AssertionError("test did not release the first upload")
            try:
                return original_put(*args, **kwargs)
            finally:
                with counter_lock:
                    active -= 1

        adapter.put = delayed_put
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(self.upload, b"first", "serialized-first.bin")
                self.assertTrue(first_entered.wait(timeout=5))
                second = pool.submit(self.upload, b"second", "serialized-second.bin")
                time.sleep(0.2)
                self.assertFalse(second.done())
                release_first.set()
                first_response = first.result(timeout=5)
                second_response = second.result(timeout=5)
        finally:
            release_first.set()
            adapter.put = original_put
        self.assertEqual(first_response.status_code, 201, first_response.text)
        self.assertEqual(second_response.status_code, 201, second_response.text)
        self.assertEqual(maximum_active, 1)

    @unittest.skipIf(os.name == "nt", "POSIX flock semantics")
    def test_upload_lock_is_shared_with_another_process(self):
        lock_path = os.environ["FILE_STORAGE_UPLOAD_LOCK_FILE"]
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import fcntl,os,sys,time; "
                    "fd=os.open(sys.argv[1], os.O_RDONLY); "
                    "fcntl.flock(fd, fcntl.LOCK_EX); "
                    "print('locked', flush=True); time.sleep(1)"
                ),
                lock_path,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        original_timeout = self.application.FILE_STORAGE_UPLOAD_LOCK_TIMEOUT_SECONDS
        try:
            self.assertEqual(child.stdout.readline().strip(), "locked")
            self.application.FILE_STORAGE_UPLOAD_LOCK_TIMEOUT_SECONDS = 0.2
            with self.assertRaises(self.application.HTTPException) as caught:
                self.application.acquire_upload_lock()
            self.assertEqual(caught.exception.status_code, 503)
        finally:
            self.application.FILE_STORAGE_UPLOAD_LOCK_TIMEOUT_SECONDS = original_timeout
            child.wait(timeout=5)

    def test_committed_upload_is_recovered_when_backend_response_is_lost(self):
        adapter = self.application.storage_adapter()
        original_put = adapter.put

        def commit_then_disconnect(*args, **kwargs):
            original_put(*args, **kwargs)
            raise self.application.StorageUnavailableError("simulated lost response")

        adapter.put = commit_then_disconnect
        try:
            response = self.upload(b"recover-me", "recover-upload.bin")
        finally:
            adapter.put = original_put
        self.assertEqual(response.status_code, 503, response.text)
        with closing(sqlite3.connect(os.environ["DATABASE_PATH"])) as connection:
            before = connection.execute(
                """
                SELECT file_id,deletion_state FROM stored_files
                WHERE original_name='recover-upload.bin'
                """
            ).fetchone()
        self.assertEqual(before[1], "uploading")

        upload_lock = self.application.acquire_upload_lock()
        try:
            self.application.reconcile_uploading_files()
        finally:
            self.application.release_upload_lock(upload_lock)
        with closing(sqlite3.connect(os.environ["DATABASE_PATH"])) as connection:
            after = connection.execute(
                "SELECT deletion_state FROM stored_files WHERE file_id=?",
                (before[0],),
            ).fetchone()
        self.assertEqual(after, ("active",))

    def test_recovery_removes_local_adapter_temporary_files(self):
        temporary = (
            Path(os.environ["FILE_STORAGE_ROOT"])
            / ".zhuojian-upload-staging"
            / ".upload-orphan"
        )
        temporary.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_bytes(b"orphan")
        stale = time.time() - self.application.FILE_STORAGE_RECOVERY_GRACE_SECONDS - 1
        os.utime(temporary, (stale, stale))
        upload_lock = self.application.acquire_upload_lock()
        try:
            self.application.reconcile_uploading_files()
        finally:
            self.application.release_upload_lock(upload_lock)
        self.assertFalse(temporary.exists())

    def test_recent_missing_upload_is_kept_for_a_late_backend_commit(self):
        file_id = uuid4().hex
        storage_key = f"{self.module_key}/2099/01/{file_id}"
        now = datetime.now(timezone.utc).isoformat()
        with self.application.db() as connection:
            connection.execute(
                """
                INSERT INTO stored_files(
                  storage_key,file_id,module_key,original_name,mime_type,size,sha256,
                  storage_backend,created_by,deletion_state,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,'uploading',?)
                """,
                (
                    storage_key,
                    file_id,
                    self.module_key,
                    "late-commit.bin",
                    "application/octet-stream",
                    4,
                    hashlib.sha256(b"late").hexdigest(),
                    "local",
                    "route-test-user",
                    now,
                ),
            )

        upload_lock = self.application.acquire_upload_lock()
        try:
            self.application.reconcile_uploading_files()
        finally:
            self.application.release_upload_lock(upload_lock)
        with closing(sqlite3.connect(os.environ["DATABASE_PATH"])) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT deletion_state FROM stored_files WHERE file_id=?",
                    (file_id,),
                ).fetchone(),
                ("uploading",),
            )

        old = (
            datetime.now(timezone.utc)
            - timedelta(seconds=self.application.FILE_STORAGE_RECOVERY_GRACE_SECONDS + 1)
        ).isoformat()
        with self.application.db() as connection:
            connection.execute(
                "UPDATE stored_files SET created_at=?,recovery_after=NULL WHERE file_id=?",
                (old, file_id),
            )
        upload_lock = self.application.acquire_upload_lock()
        try:
            self.application.reconcile_uploading_files()
        finally:
            self.application.release_upload_lock(upload_lock)
        with closing(sqlite3.connect(os.environ["DATABASE_PATH"])) as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM stored_files WHERE file_id=?",
                    (file_id,),
                ).fetchone()
            )

    def test_mismatched_upload_keeps_cleanup_metadata_when_delete_is_unavailable(self):
        adapter = self.application.storage_adapter()
        file_id = uuid4().hex
        storage_key = f"{self.module_key}/2000/01/{file_id}"
        adapter.put(storage_key, b"wrong", content_type="application/octet-stream")
        old = (
            datetime.now(timezone.utc)
            - timedelta(seconds=self.application.FILE_STORAGE_RECOVERY_GRACE_SECONDS + 1)
        ).isoformat()
        with self.application.db() as connection:
            connection.execute(
                """
                INSERT INTO stored_files(
                  storage_key,file_id,module_key,original_name,mime_type,size,sha256,
                  storage_backend,created_by,deletion_state,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,'uploading',?)
                """,
                (
                    storage_key,
                    file_id,
                    self.module_key,
                    "mismatch.bin",
                    "application/octet-stream",
                    5,
                    hashlib.sha256(b"right").hexdigest(),
                    "local",
                    "route-test-user",
                    old,
                ),
            )
        original_delete = adapter.delete

        def unavailable_delete(_storage_key):
            raise self.application.StorageUnavailableError("temporary delete outage")

        adapter.delete = unavailable_delete
        upload_lock = self.application.acquire_upload_lock()
        try:
            self.application.reconcile_uploading_files()
        finally:
            self.application.release_upload_lock(upload_lock)
            adapter.delete = original_delete
        with closing(sqlite3.connect(os.environ["DATABASE_PATH"])) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT deletion_state FROM stored_files WHERE file_id=?",
                    (file_id,),
                ).fetchone(),
                ("uploading",),
            )

        with self.application.db() as connection:
            connection.execute(
                "UPDATE stored_files SET recovery_after=? WHERE file_id=?",
                ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), file_id),
            )

        upload_lock = self.application.acquire_upload_lock()
        try:
            self.application.reconcile_uploading_files()
        finally:
            self.application.release_upload_lock(upload_lock)
        self.assertFalse(adapter.exists(storage_key))
        with closing(sqlite3.connect(os.environ["DATABASE_PATH"])) as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM stored_files WHERE file_id=?",
                    (file_id,),
                ).fetchone()
            )

    def test_failed_upload_recovery_batch_does_not_starve_a_later_healthy_row(self):
        old_base = datetime(2000, 1, 1, tzinfo=timezone.utc)
        original_resolver = self.application.recovery_storage_adapter

        class UnavailableAdapter:
            backend = "always-unavailable"

            def stat(self, _storage_key):
                raise self.application.StorageUnavailableError("simulated outage")

        unavailable = UnavailableAdapter()
        unavailable.application = self.application
        with self.application.db() as connection:
            for index in range(self.application.STORAGE_RECOVERY_BATCH_SIZE):
                file_id = uuid4().hex
                connection.execute(
                    """
                    INSERT INTO stored_files(
                      storage_key,file_id,module_key,original_name,mime_type,size,sha256,
                      storage_backend,created_by,deletion_state,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,'uploading',?)
                    """,
                    (
                        f"{self.module_key}/failed/{file_id}",
                        file_id,
                        self.module_key,
                        f"failed-{index}.bin",
                        "application/octet-stream",
                        1,
                        hashlib.sha256(b"x").hexdigest(),
                        "always-unavailable",
                        "route-test-user",
                        (old_base + timedelta(seconds=index)).isoformat(),
                    ),
                )
            healthy_id = uuid4().hex
            healthy_key = f"{self.module_key}/healthy/{healthy_id}"
            connection.execute(
                """
                INSERT INTO stored_files(
                  storage_key,file_id,module_key,original_name,mime_type,size,sha256,
                  storage_backend,created_by,deletion_state,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,'uploading',?)
                """,
                (
                    healthy_key,
                    healthy_id,
                    self.module_key,
                    "healthy-after-failures.bin",
                    "application/octet-stream",
                    2,
                    hashlib.sha256(b"ok").hexdigest(),
                    "local",
                    "route-test-user",
                    (old_base + timedelta(seconds=100)).isoformat(),
                ),
            )
        original_resolver("local").put(healthy_key, b"ok")

        def resolver(backend):
            return unavailable if backend == "always-unavailable" else original_resolver(backend)

        with mock.patch.object(
            self.application, "recovery_storage_adapter", side_effect=resolver
        ):
            self.application.reconcile_uploading_files()
            self.application.reconcile_uploading_files()
        with closing(sqlite3.connect(os.environ["DATABASE_PATH"])) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT deletion_state FROM stored_files WHERE file_id=?", (healthy_id,)
                ).fetchone(),
                ("active",),
            )

    def test_background_upload_recovery_releases_the_global_lock_between_items(self):
        lock_handles = [object(), object(), object()]
        with mock.patch.object(
            self.application,
            "acquire_upload_lock",
            side_effect=lock_handles,
        ) as acquire, mock.patch.object(
            self.application,
            "release_upload_lock",
        ) as release, mock.patch.object(
            self.application,
            "reconcile_uploading_files",
            side_effect=[1, 1, 0],
        ) as reconcile:
            asyncio.run(self.application.recover_uploads_once())

        self.assertEqual(acquire.call_count, 3)
        self.assertEqual(release.call_args_list, [mock.call(item) for item in lock_handles])
        self.assertEqual(reconcile.call_args_list, [mock.call(1), mock.call(1), mock.call(1)])

    def test_failed_delete_recovery_batch_does_not_starve_a_later_healthy_row(self):
        old_base = datetime(2001, 1, 1, tzinfo=timezone.utc)
        original_resolver = self.application.recovery_storage_adapter

        class UnavailableAdapter:
            def delete(self, _storage_key):
                raise self.application.StorageUnavailableError("simulated outage")

        unavailable = UnavailableAdapter()
        unavailable.application = self.application
        with self.application.db() as connection:
            for index in range(self.application.STORAGE_RECOVERY_BATCH_SIZE):
                file_id = uuid4().hex
                connection.execute(
                    """
                    INSERT INTO stored_files(
                      storage_key,file_id,module_key,original_name,mime_type,size,sha256,
                      storage_backend,created_by,deletion_state,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,'pending',?)
                    """,
                    (
                        f"{self.module_key}/failed-delete/{file_id}",
                        file_id,
                        self.module_key,
                        f"failed-delete-{index}.bin",
                        "application/octet-stream",
                        1,
                        hashlib.sha256(b"x").hexdigest(),
                        "always-unavailable",
                        "route-test-user",
                        (old_base + timedelta(seconds=index)).isoformat(),
                    ),
                )
            healthy_id = uuid4().hex
            healthy_key = f"{self.module_key}/healthy-delete/{healthy_id}"
            connection.execute(
                """
                INSERT INTO stored_files(
                  storage_key,file_id,module_key,original_name,mime_type,size,sha256,
                  storage_backend,created_by,deletion_state,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,'pending',?)
                """,
                (
                    healthy_key,
                    healthy_id,
                    self.module_key,
                    "healthy-delete-after-failures.bin",
                    "application/octet-stream",
                    2,
                    hashlib.sha256(b"ok").hexdigest(),
                    "local",
                    "route-test-user",
                    (old_base + timedelta(seconds=100)).isoformat(),
                ),
            )
        original_resolver("local").put(healthy_key, b"ok")

        def resolver(backend):
            return unavailable if backend == "always-unavailable" else original_resolver(backend)

        with mock.patch.object(
            self.application, "recovery_storage_adapter", side_effect=resolver
        ):
            asyncio.run(self.application.recover_pending_deletions_once())
            asyncio.run(self.application.recover_pending_deletions_once())
        with closing(sqlite3.connect(os.environ["DATABASE_PATH"])) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT deletion_state FROM stored_files WHERE file_id=?", (healthy_id,)
                ).fetchone(),
                ("deleted",),
            )

    def test_recovery_finishes_delete_after_object_was_already_removed(self):
        upload = self.upload(b"delete-before-finalize", "delete-recovery.bin")
        self.assertEqual(upload.status_code, 201, upload.text)
        file_id = upload.json()["fileId"]
        request_id = uuid4().hex
        owner = uuid4().hex
        stale = (
            datetime.now(timezone.utc)
            - timedelta(seconds=self.application.ACTION_LEASE_SECONDS + 1)
        ).isoformat()
        with self.application.db() as connection:
            row = connection.execute(
                "SELECT storage_key,storage_backend FROM stored_files WHERE file_id=?",
                (file_id,),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO request_results(
                  request_id,action_key,request_hash,state,result,created_at,updated_at,
                  lease_owner,lease_expires_at
                ) VALUES(?,?,?,'in_progress','{}',?,?,?,?)
                """,
                (
                    request_id,
                    self.delete_action,
                    "a" * 64,
                    stale,
                    stale,
                    owner,
                    stale,
                ),
            )
            connection.execute(
                """
                UPDATE stored_files SET deletion_state='pending',deletion_owner=?
                WHERE file_id=?
                """,
                (owner, file_id),
            )
        self.application.storage_adapter(row["storage_backend"]).delete(row["storage_key"])
        asyncio.run(self.application.recover_pending_deletions_once())
        with closing(sqlite3.connect(os.environ["DATABASE_PATH"])) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT deletion_state,version,deletion_owner FROM stored_files WHERE file_id=?",
                    (file_id,),
                ).fetchone(),
                ("deleted", 2, None),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT state,lease_owner FROM request_results WHERE request_id=?",
                    (request_id,),
                ).fetchone(),
                ("completed", None),
            )

    def test_recovery_finishes_legacy_pending_delete_without_browser_state(self):
        upload = self.upload(b"legacy-pending", "legacy-pending.bin")
        self.assertEqual(upload.status_code, 201, upload.text)
        file_id = upload.json()["fileId"]
        with self.application.db() as connection:
            connection.execute(
                """
                UPDATE stored_files
                SET deletion_state='pending',deletion_owner=NULL
                WHERE file_id=?
                """,
                (file_id,),
            )
        asyncio.run(self.application.recover_pending_deletions_once())
        with closing(sqlite3.connect(os.environ["DATABASE_PATH"])) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT deletion_state,version FROM stored_files WHERE file_id=?",
                    (file_id,),
                ).fetchone(),
                ("deleted", 2),
            )
            recovery = connection.execute(
                """
                SELECT state FROM request_results
                WHERE action_key='internal.file.delete'
                ORDER BY created_at DESC LIMIT 1
                """
            ).fetchone()
        self.assertEqual(recovery, ("completed",))

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
        self.assertEqual(listing.json()["items"][0]["version"], 1)
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

        with closing(sqlite3.connect(os.environ["DATABASE_PATH"])) as connection:
            row = connection.execute(
                """
                SELECT file_id,storage_key,storage_backend,deletion_state
                FROM stored_files WHERE file_id=?
                """,
                (metadata["fileId"],),
            ).fetchone()
        self.assertEqual(row[0], metadata["fileId"])
        self.assertNotIn("测试附件", row[1])
        self.assertEqual(row[2:], ("local", "active"))

        unconfirmed = self.client.delete(
            f"/api/ui/files/{metadata['fileId']}",
            params={
                "moduleKey": self.module_key,
                "pageKey": self.page_key,
                "actionKey": self.delete_action,
            },
        )
        self.assertEqual(unconfirmed.status_code, 422, unconfirmed.text)

        request_id = uuid4().hex
        params = {"fileId": metadata["fileId"]}
        confirmation_response = self.client.post(
            "/api/ui/confirmations",
            json={
                "requestId": request_id,
                "moduleKey": self.module_key,
                "pageKey": self.page_key,
                "actionKey": self.delete_action,
                "operation": "delete",
                "expectedVersion": 1,
                "params": params,
                "confirmed": True,
                "subject": "stored-file",
            },
        )
        self.assertEqual(confirmation_response.status_code, 201, confirmation_response.text)
        confirmation = confirmation_response.json()
        self.assertEqual(confirmation["requestId"], request_id)
        self.assertEqual(confirmation["confirmedBy"], "route-test-user")
        self.assertEqual(confirmation["paramsHash"], self.application.canonical_hash(params))
        datetime.fromisoformat(confirmation["confirmedAt"])

        delete_body = {
            "requestId": request_id,
            "moduleKey": self.module_key,
            "pageKey": self.page_key,
            "operation": "delete",
            "expectedVersion": 1,
            "params": params,
            "confirmation": confirmation,
        }
        tampered = dict(delete_body)
        tampered["confirmation"] = {**confirmation, "paramsHash": "0" * 64}
        rejected = self.client.request(
            "DELETE",
            f"/api/ui/files/{metadata['fileId']}",
            params={
                "moduleKey": self.module_key,
                "pageKey": self.page_key,
                "actionKey": self.delete_action,
            },
            json=tampered,
        )
        self.assertEqual(rejected.status_code, 403, rejected.text)

        deleted = self.client.request(
            "DELETE",
            f"/api/ui/files/{metadata['fileId']}",
            params={
                "moduleKey": self.module_key,
                "pageKey": self.page_key,
                "actionKey": self.delete_action,
            },
            json=delete_body,
        )
        self.assertEqual(deleted.status_code, 204, deleted.text)
        repeated = self.client.request(
            "DELETE",
            f"/api/ui/files/{metadata['fileId']}",
            params={
                "moduleKey": self.module_key,
                "pageKey": self.page_key,
                "actionKey": self.delete_action,
            },
            json=delete_body,
        )
        self.assertEqual(repeated.status_code, 204, repeated.text)
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
        with closing(sqlite3.connect(os.environ["DATABASE_PATH"])) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT deletion_state FROM stored_files WHERE file_id=?",
                    (metadata["fileId"],),
                ).fetchone()[0],
                "deleted",
            )
            self.assertEqual(
                connection.execute(
                    "SELECT version FROM stored_files WHERE file_id=?",
                    (metadata["fileId"],),
                ).fetchone()[0],
                2,
            )
            request_row = connection.execute(
                "SELECT state,result FROM request_results WHERE request_id=?",
                (request_id,),
            ).fetchone()
            self.assertEqual(request_row[0], "completed")
            self.assertIn('"deleted": true', request_row[1])

    def test_delete_resumes_same_request_after_process_crash(self):
        payload = b"recoverable-delete"
        upload = self.client.post(
            "/api/ui/files",
            params={
                "moduleKey": self.module_key,
                "pageKey": self.page_key,
                "actionKey": self.create_action,
                "filename": "resume.txt",
            },
            headers={"Content-Type": "text/plain"},
            content=payload,
        )
        self.assertEqual(upload.status_code, 201, upload.text)
        file_id = upload.json()["fileId"]
        request_id = uuid4().hex
        params = {"fileId": file_id}
        confirmation_response = self.client.post(
            "/api/ui/confirmations",
            json={
                "requestId": request_id,
                "moduleKey": self.module_key,
                "pageKey": self.page_key,
                "actionKey": self.delete_action,
                "operation": "delete",
                "expectedVersion": 1,
                "params": params,
                "confirmed": True,
                "subject": "stored-file",
            },
        )
        self.assertEqual(confirmation_response.status_code, 201, confirmation_response.text)
        confirmation = confirmation_response.json()
        request_hash = self.application.action_request_hash(
            self.delete_action,
            self.module_key,
            self.page_key,
            "route-test-user",
            params,
            1,
            subject="stored-file",
        )
        stale_time = (
            datetime.now(timezone.utc)
            - timedelta(seconds=self.application.ACTION_LEASE_SECONDS + 1)
        ).isoformat()
        with self.application.db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO request_results(
                  request_id,action_key,request_hash,state,result,created_at,updated_at
                ) VALUES(?,?,?,'in_progress','{}',?,?)
                """,
                (request_id, self.delete_action, request_hash, stale_time, stale_time),
            )
            self.application.consume_confirmation(
                connection,
                confirmation,
                actor="route-test-user",
                request_id=request_id,
                action_key=self.delete_action,
                request_hash=request_hash,
                params=params,
                require_page_issued=True,
            )
            connection.execute(
                "UPDATE stored_files SET deletion_state='pending' WHERE file_id=?",
                (file_id,),
            )

        delete_body = {
            "requestId": request_id,
            "moduleKey": self.module_key,
            "pageKey": self.page_key,
            "operation": "delete",
            "expectedVersion": 1,
            "params": params,
            "confirmation": confirmation,
        }
        resumed = self.client.request(
            "DELETE",
            f"/api/ui/files/{file_id}",
            params={
                "moduleKey": self.module_key,
                "pageKey": self.page_key,
                "actionKey": self.delete_action,
            },
            json=delete_body,
        )
        self.assertEqual(resumed.status_code, 204, resumed.text)
        with closing(sqlite3.connect(os.environ["DATABASE_PATH"])) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT deletion_state,version FROM stored_files WHERE file_id=?",
                    (file_id,),
                ).fetchone(),
                ("deleted", 2),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT state FROM request_results WHERE request_id=?",
                    (request_id,),
                ).fetchone()[0],
                "completed",
            )

    def test_slow_delete_losing_lease_cannot_finalize_twice(self):
        upload = self.client.post(
            "/api/ui/files",
            params={
                "moduleKey": self.module_key,
                "pageKey": self.page_key,
                "actionKey": self.create_action,
                "filename": "slow-delete.txt",
            },
            headers={"Content-Type": "text/plain"},
            content=b"slow-delete",
        )
        self.assertEqual(upload.status_code, 201, upload.text)
        file_id = upload.json()["fileId"]
        request_id = uuid4().hex
        params = {"fileId": file_id}
        issued = self.client.post(
            "/api/ui/confirmations",
            json={
                "requestId": request_id,
                "moduleKey": self.module_key,
                "pageKey": self.page_key,
                "actionKey": self.delete_action,
                "operation": "delete",
                "expectedVersion": 1,
                "params": params,
                "confirmed": True,
                "subject": "stored-file",
            },
        )
        self.assertEqual(issued.status_code, 201, issued.text)
        delete_body = {
            "requestId": request_id,
            "moduleKey": self.module_key,
            "pageKey": self.page_key,
            "operation": "delete",
            "expectedVersion": 1,
            "params": params,
            "confirmation": issued.json(),
        }

        adapter = self.application.storage_adapter()
        original_delete = adapter.delete
        original_lease_seconds = self.application.ACTION_LEASE_SECONDS
        first_started = threading.Event()
        release_first = threading.Event()
        call_lock = threading.Lock()
        call_count = 0

        def delayed_delete(storage_key):
            nonlocal call_count
            with call_lock:
                call_count += 1
                this_call = call_count
            if this_call == 1:
                first_started.set()
                if not release_first.wait(timeout=5):
                    raise AssertionError("test did not release the first delete")
            return original_delete(storage_key)

        adapter.delete = delayed_delete
        self.application.ACTION_LEASE_SECONDS = 0.1

        def submit_delete():
            return self.client.request(
                "DELETE",
                f"/api/ui/files/{file_id}",
                params={
                    "moduleKey": self.module_key,
                    "pageKey": self.page_key,
                    "actionKey": self.delete_action,
                },
                json=delete_body,
            )

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                first_future = pool.submit(submit_delete)
                self.assertTrue(first_started.wait(timeout=5))
                time.sleep(0.2)
                second_future = pool.submit(submit_delete)
                second = second_future.result(timeout=5)
                release_first.set()
                first = first_future.result(timeout=5)
        finally:
            release_first.set()
            adapter.delete = original_delete
            self.application.ACTION_LEASE_SECONDS = original_lease_seconds

        self.assertEqual(second.status_code, 204, second.text)
        self.assertEqual(first.status_code, 204, first.text)
        self.assertEqual(call_count, 2)
        with closing(sqlite3.connect(os.environ["DATABASE_PATH"])) as connection:
            file_row = connection.execute(
                "SELECT deletion_state,version,deletion_owner FROM stored_files WHERE file_id=?",
                (file_id,),
            ).fetchone()
            request_row = connection.execute(
                """
                SELECT state,lease_owner,lease_expires_at
                FROM request_results WHERE request_id=?
                """,
                (request_id,),
            ).fetchone()
        self.assertEqual(file_row, ("deleted", 2, None))
        self.assertEqual(request_row, ("completed", None, None))


if __name__ == "__main__":
    unittest.main()
