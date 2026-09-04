from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from storage import (
    GatewayStorageAdapter,
    InvalidStorageKey,
    LocalStorageAdapter,
    StorageAuthorizationError,
    StorageConfigurationError,
    StorageObjectNotFound,
    StorageUnavailableError,
    storage_for_backend,
    storage_from_env,
)


class _GatewayHandler(BaseHTTPRequestHandler):
    token = "project-token"
    objects: dict[str, tuple[bytes, str, str]] = {}
    prefix = "/internal/v1/objects/"

    def log_message(self, *_args):
        pass

    def _authorize(self) -> bool:
        if self.headers.get("Authorization") != f"Bearer {self.token}":
            self.send_error(403)
            return False
        return True

    def _key(self) -> str | None:
        path = urlsplit(self.path).path
        if not path.startswith(self.prefix):
            self.send_error(404)
            return None
        return "/".join(unquote(part) for part in path[len(self.prefix):].split("/"))

    def _object(self):
        if not self._authorize():
            return None, None
        key = self._key()
        if key is None:
            return None, None
        item = self.objects.get(key)
        if item is None:
            self.send_error(404)
            return key, None
        return key, item

    def do_PUT(self):
        if self.headers.get("Authorization") != f"Bearer {self.token}":
            # Drain the request body before replying so Windows does not turn
            # the intended HTTP 403 into a connection-reset race.
            length = int(self.headers.get("Content-Length") or "0")
            if length:
                self.rfile.read(length)
            self.send_error(403)
            return
        key = self._key()
        if key is None:
            return
        length = int(self.headers.get("Content-Length") or "0")
        data = self.rfile.read(length)
        digest = hashlib.sha256(data).hexdigest()
        content_type = self.headers.get("Content-Type") or "application/octet-stream"
        self.objects[key] = (data, content_type, digest)
        payload = json.dumps({
            "key": key,
            "size": len(data),
            "sha256": digest,
            "etag": f'"{digest[:16]}"',
        }).encode()
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        _key, item = self._object()
        if item is None:
            return
        data, content_type, digest = item
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Storage-Sha256", digest)
        self.send_header("ETag", f'"{digest[:16]}"')
        self.end_headers()
        self.wfile.write(data)

    def do_HEAD(self):
        _key, item = self._object()
        if item is None:
            return
        data, content_type, digest = item
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Storage-Sha256", digest)
        self.send_header("ETag", f'"{digest[:16]}"')
        self.end_headers()

    def do_DELETE(self):
        if not self._authorize():
            return
        key = self._key()
        if key is None:
            return
        if key not in self.objects:
            self.send_error(404)
            return
        del self.objects[key]
        self.send_response(204)
        self.end_headers()


class LocalStorageAdapterTests(unittest.TestCase):
    def test_complete_lifecycle_and_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = LocalStorageAdapter(directory)
            key = "orders/2026/报价单.pdf"
            payload = b"example-pdf-content"

            written = adapter.put(key, payload, content_type="application/pdf")

            self.assertEqual(written.storage_key, key)
            self.assertEqual(written.backend, "local")
            self.assertEqual(written.size, len(payload))
            self.assertEqual(written.sha256, hashlib.sha256(payload).hexdigest())
            self.assertTrue(adapter.exists(key))
            with adapter.open(key) as stream:
                self.assertEqual(stream.read(), payload)
            inspected = adapter.stat(key)
            self.assertEqual(inspected.size, len(payload))
            self.assertEqual(inspected.sha256, written.sha256)
            adapter.delete(key)
            adapter.delete(key)
            self.assertFalse(adapter.exists(key))
            with self.assertRaises(StorageObjectNotFound):
                adapter.open(key)

    def test_atomic_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = LocalStorageAdapter(directory)
            adapter.put("records/report.txt", b"old")
            adapter.put("records/report.txt", b"new")
            with adapter.open("records/report.txt") as stream:
                self.assertEqual(stream.read(), b"new")
            leftovers = list(Path(directory).rglob(".upload-*"))
            self.assertEqual(leftovers, [])

    def test_cleanup_only_removes_old_runtime_staging_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = LocalStorageAdapter(root)
            business_key = "orders/2026/.upload-business-document"
            adapter.put(business_key, b"keep")
            business_path = root / "orders" / "2026" / ".upload-business-document"
            old_time = 1
            os.utime(business_path, (old_time, old_time))

            staging = root / ".zhuojian-upload-staging"
            stale = staging / ".upload-abandoned"
            fresh = staging / ".upload-active"
            stale.write_bytes(b"stale")
            fresh.write_bytes(b"fresh")
            os.utime(stale, (old_time, old_time))

            self.assertEqual(adapter.cleanup_stale_uploads(1800), 1)
            self.assertFalse(stale.exists())
            self.assertTrue(fresh.exists())
            with adapter.open(business_key) as stream:
                self.assertEqual(stream.read(), b"keep")

    def test_runtime_staging_namespace_is_reserved(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = LocalStorageAdapter(directory)
            with self.assertRaises(InvalidStorageKey):
                adapter.put(".zhuojian-upload-staging/forged", b"no")

    def test_unsafe_keys_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = LocalStorageAdapter(directory)
            for key in ("", "/absolute", "../escape", "a/../escape", "a//b", "a/./b", "a\\b", " trailing"):
                with self.subTest(key=key), self.assertRaises(InvalidStorageKey):
                    adapter.put(key, b"no")


class GatewayStorageAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _GatewayHandler.objects = {}
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _GatewayHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}/internal"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        _GatewayHandler.objects.clear()

    def test_complete_lifecycle_encodes_each_key_segment(self):
        adapter = GatewayStorageAdapter(self.base_url, _GatewayHandler.token)
        key = "合同 文件/报价 #1%.pdf"
        payload = b"gateway-object"

        written = adapter.put(key, payload, content_type="application/pdf")

        self.assertEqual(written.storage_key, key)
        self.assertEqual(written.backend, "oss-gateway")
        self.assertEqual(written.size, len(payload))
        self.assertIn(key, _GatewayHandler.objects)
        self.assertTrue(adapter.exists(key))
        inspected = adapter.stat(key)
        self.assertEqual(inspected.sha256, hashlib.sha256(payload).hexdigest())
        self.assertEqual(inspected.content_type, "application/pdf")
        with adapter.open(key) as stream:
            self.assertEqual(stream.read(), payload)
        adapter.delete(key)
        adapter.delete(key)
        self.assertFalse(adapter.exists(key))

    def test_gateway_rejects_wrong_project_token_without_fallback(self):
        adapter = GatewayStorageAdapter(self.base_url, "wrong-token")
        with self.assertRaises(StorageAuthorizationError):
            adapter.put("private/file.bin", b"secret")

    def test_gateway_does_not_follow_redirects(self):
        class RedirectHandler(_GatewayHandler):
            def do_GET(self):
                self.send_response(307)
                self.send_header("Location", "http://127.0.0.1:1/credential-leak")
                self.end_headers()

        server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            adapter = GatewayStorageAdapter(
                f"http://127.0.0.1:{server.server_port}/internal", _GatewayHandler.token
            )
            with self.assertRaises(StorageUnavailableError):
                adapter.open("anything.bin")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


class StorageFactoryTests(unittest.TestCase):
    def test_default_driver_is_local(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = storage_from_env({"FILE_STORAGE_ROOT": directory})
            self.assertIsInstance(adapter, LocalStorageAdapter)

    def test_primary_gateway_environment(self):
        adapter = storage_from_env({
            "FILE_STORAGE_DRIVER": "oss-gateway",
            "FILE_STORAGE_GATEWAY_URL": "http://gateway:8080",
            "FILE_STORAGE_TOKEN": "generated-project-token",
        })
        self.assertIsInstance(adapter, GatewayStorageAdapter)
        # The gateway acknowledges only after its bounded spool is committed to
        # OSS.  A 30-second default can time out a valid 512 MiB transfer and
        # leave the object committed without application metadata.
        self.assertEqual(adapter.timeout, 900.0)

    def test_gateway_timeout_can_be_explicitly_reduced_for_small_workloads(self):
        adapter = storage_from_env({
            "FILE_STORAGE_DRIVER": "oss-gateway",
            "FILE_STORAGE_GATEWAY_URL": "http://gateway:8080",
            "FILE_STORAGE_TOKEN": "generated-project-token",
            "FILE_STORAGE_GATEWAY_TIMEOUT_SECONDS": "45",
        })
        self.assertEqual(adapter.timeout, 45.0)

    def test_legacy_gateway_environment_remains_compatible(self):
        adapter = storage_from_env({
            "FILE_STORAGE_DRIVER": "oss",
            "STORAGE_GATEWAY_URL": "http://gateway:8080",
            "STORAGE_PROJECT_TOKEN": "legacy-project-token",
        })
        self.assertIsInstance(adapter, GatewayStorageAdapter)

    def test_recorded_backends_can_be_resolved_during_a_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = {
                "FILE_STORAGE_ROOT": directory,
                "FILE_STORAGE_GATEWAY_URL": "http://gateway:8080",
                "FILE_STORAGE_TOKEN": "generated-project-token",
            }
            self.assertIsInstance(
                storage_for_backend("local-managed", environment),
                LocalStorageAdapter,
            )
            self.assertIsInstance(
                storage_for_backend("oss-gateway", environment),
                GatewayStorageAdapter,
            )

    def test_missing_gateway_configuration_fails_instead_of_using_disk(self):
        with self.assertRaises(StorageConfigurationError):
            storage_from_env({"FILE_STORAGE_DRIVER": "oss-gateway"})


if __name__ == "__main__":
    unittest.main()
