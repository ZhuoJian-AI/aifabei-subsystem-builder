from __future__ import annotations

import re
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class TemplateSecurityTests(unittest.TestCase):
    def test_frontend_uses_dom_text_apis_for_business_values(self):
        html = (PROJECT_ROOT / "static" / "index.html").read_text(encoding="utf-8")

        for unsafe_sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
            self.assertNotIn(unsafe_sink, html)
        self.assertIn(".textContent=", html)
        self.assertIn("replaceChildren", html)
        self.assertNotIn("__APPLICATION_NAME__", html)
        self.assertNotIn("__MODULE_NAME__", html)

    def test_uvicorn_access_log_is_disabled_to_protect_sso_query_tickets(self):
        dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn('"--no-access-log"', dockerfile)

    def test_compose_keeps_the_bounded_oss_upload_timeout_in_sync(self):
        compose = (PROJECT_ROOT / "compose.yaml").read_text(encoding="utf-8")

        self.assertIn(
            "FILE_STORAGE_GATEWAY_TIMEOUT_SECONDS: ${FILE_STORAGE_GATEWAY_TIMEOUT_SECONDS:-900}",
            compose,
        )

    def test_storage_recovery_never_blocks_application_startup(self):
        source = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
        startup = re.search(
            r"(?ms)^async def start_storage_recovery\(\) -> None:.*?^\s*DELETION_RECOVERY_TASK = .*?$",
            source,
        )
        self.assertIsNotNone(startup)
        self.assertIn("asyncio.create_task(storage_recovery_loop())", startup.group(0))
        self.assertNotIn("await recover_uploads_once", startup.group(0))

    def test_upload_recovery_grace_starts_after_the_full_request_body(self):
        source = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
        upload = source[source.index("async def upload_file("):source.index("@app.get(\"/api/ui/files/{file_id}\")")]

        stream_position = upload.index("async for chunk in request.stream()")
        recorded_position = upload.index("upload_recorded_at = datetime.now(timezone.utc)")
        insert_position = upload.index("INSERT INTO stored_files")
        backend_position = upload.index("adapter.put(storage_key, spool")
        assert stream_position < recorded_position < insert_position < backend_position


if __name__ == "__main__":
    unittest.main()
