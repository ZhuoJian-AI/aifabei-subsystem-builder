from __future__ import annotations

import hashlib
import io
import os
import sqlite3
import stat
import sys
from types import SimpleNamespace
from urllib.error import HTTPError
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from zhuojian_storage_gateway.app import create_app, normalise_object_key
from zhuojian_storage_gateway.cli import (
    GatewayProbeError,
    _assert_anonymous_read_denied,
    _assert_outside_prefix_denied,
    probe_gateway,
)
from zhuojian_storage_gateway.config import GatewaySettings
from zhuojian_storage_gateway.config import OssCredentials
from zhuojian_storage_gateway.registry import (
    ApplicationUnavailable,
    CredentialRegistry,
    RegistryError,
    _render_env,
)
from zhuojian_storage_gateway.storage import (
    ObjectDownload,
    ObjectMetadata,
    ObjectNotFound,
)


@dataclass
class StoredObject:
    body: bytes
    content_type: str | None
    sha256: str
    etag: str


class FakeStorageAdapter:
    def __init__(self) -> None:
        self.objects: dict[str, StoredObject] = {}

    def put_object(
        self,
        key: str,
        body,
        *,
        content_type: str | None,
        sha256: str,
        size: int,
    ) -> ObjectMetadata:
        value = body.read()
        assert len(value) == size
        assert hashlib.sha256(value).hexdigest() == sha256
        etag = hashlib.md5(value, usedforsecurity=False).hexdigest()
        self.objects[key] = StoredObject(value, content_type, sha256, etag)
        return ObjectMetadata(size, content_type, etag, sha256)

    def get_object(self, key: str) -> ObjectDownload:
        item = self.objects.get(key)
        if item is None:
            raise ObjectNotFound
        return ObjectDownload(
            io.BytesIO(item.body),
            ObjectMetadata(
                len(item.body), item.content_type, item.etag, item.sha256
            ),
        )

    def head_object(self, key: str) -> ObjectMetadata:
        item = self.objects.get(key)
        if item is None:
            raise ObjectNotFound
        return ObjectMetadata(len(item.body), item.content_type, item.etag, item.sha256)

    def delete_object(self, key: str) -> None:
        self.objects.pop(key, None)


@pytest.mark.parametrize(
    ("region", "endpoint"),
    [
        ("oss-cn-hongkong", "https://oss-cn-hongkong.aliyuncs.com"),
        ("cn-hongkong", "https://oss-cn-hongkong.aliyuncs.com"),
    ],
)
def test_probe_accepts_aliyun_region_id_with_or_without_oss_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, region: str, endpoint: str
) -> None:
    secret_path = tmp_path / "oss.env"
    secret_path.write_text(
        "\n".join(
            [
                f"OSS_ENDPOINT={endpoint}",
                "OSS_BUCKET=company-files",
                "OSS_ACCESS_KEY_ID=LTAI00000000000000000000",
                "OSS_ACCESS_KEY_SECRET=000000000000000000000000000000",
            ]
        ),
        encoding="utf-8",
    )
    settings = GatewaySettings(
        db_path=tmp_path / "registry.sqlite3",
        apps_env_dir=tmp_path / "apps",
        internal_url="http://gateway:8080",
        root_prefix="apps",
        oss_secrets_file=secret_path,
        max_upload_bytes=1024,
        spool_memory_bytes=128,
        io_chunk_bytes=64,
        spool_dir=tmp_path / "spool",
        minimum_free_bytes=1,
    )
    monkeypatch.setattr(
        "zhuojian_storage_gateway.cli._assert_outside_prefix_denied",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(GatewayProbeError("accepted")),
    )

    with pytest.raises(GatewayProbeError, match="accepted"):
        probe_gateway(
            registry=CredentialRegistry(tmp_path / "registry.sqlite3"),
            settings=settings,
            apps_env_dir=tmp_path / "apps",
            gateway_url="http://gateway:8080",
            expected_bucket="company-files",
            expected_region=region,
        )


def test_probe_reports_cleanup_failure_even_when_body_already_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class BrokenCleanupRegistry:
        def __init__(self):
            self.revoked = []

        def ensure_app(self, slug, _apps_env_dir, _gateway_url):
            return SimpleNamespace(slug=slug, env_path=tmp_path / f"{slug}.env")

        def revoke_app(self, slug, _apps_env_dir):
            self.revoked.append(slug)
            raise RegistryError("simulated cleanup failure")

    registry = BrokenCleanupRegistry()
    settings = GatewaySettings(
        db_path=tmp_path / "registry.sqlite3",
        apps_env_dir=tmp_path / "apps",
        internal_url="http://gateway:8080",
        root_prefix="apps",
        oss_secrets_file=tmp_path / "unused.env",
        max_upload_bytes=1024,
        spool_memory_bytes=128,
        io_chunk_bytes=64,
        spool_dir=tmp_path / "spool",
        minimum_free_bytes=1,
    )
    monkeypatch.setattr(
        "zhuojian_storage_gateway.cli.load_oss_credentials",
        lambda _path: OssCredentials(
            endpoint="https://oss-cn-hongkong.aliyuncs.com",
            bucket="company-files",
            access_key_id="test-id",
            access_key_secret="test-secret",
        ),
    )
    monkeypatch.setattr(
        "zhuojian_storage_gateway.cli._assert_outside_prefix_denied",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        "zhuojian_storage_gateway.cli._read_generated_token",
        lambda _path: "temporary-token",
    )
    monkeypatch.setattr(
        "zhuojian_storage_gateway.cli._request_object",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(GatewayProbeError("primary failure")),
    )

    with pytest.raises(GatewayProbeError, match="cleanup failed") as caught:
        probe_gateway(
            registry=registry,  # type: ignore[arg-type]
            settings=settings,
            apps_env_dir=tmp_path / "apps",
            gateway_url="http://gateway:8080",
            expected_bucket="company-files",
            expected_region="oss-cn-hongkong",
        )
    assert "primary failure" not in str(caught.value)
    assert len(registry.revoked) == 2
    assert registry.revoked[0].startswith("storage-probe-a-")
    assert registry.revoked[1].startswith("storage-probe-b-")


@pytest.fixture()
def gateway(tmp_path: Path):
    registry = CredentialRegistry(tmp_path / "registry.sqlite3")
    env_dir = tmp_path / "apps"
    url = "http://zhuojian-storage-gateway:8080"
    alpha = registry.ensure_app("alpha-orders", env_dir, url)
    beta = registry.ensure_app("beta-design", env_dir, url)
    storage = FakeStorageAdapter()
    settings = GatewaySettings(
        db_path=tmp_path / "registry.sqlite3",
        apps_env_dir=env_dir,
        internal_url=url,
        root_prefix="apps",
        oss_secrets_file=tmp_path / "unused.env",
        max_upload_bytes=1024 * 1024,
        spool_memory_bytes=1024,
        io_chunk_bytes=128,
        spool_dir=tmp_path / "spool",
        minimum_free_bytes=1,
    )
    client = TestClient(create_app(registry=registry, storage=storage, settings=settings))
    return {
        "registry": registry,
        "env_dir": env_dir,
        "client": client,
        "storage": storage,
        "alpha": _read_token(alpha.env_path),
        "beta": _read_token(beta.env_path),
    }


def test_two_projects_are_bound_to_distinct_server_side_prefixes(gateway) -> None:
    client: TestClient = gateway["client"]
    storage: FakeStorageAdapter = gateway["storage"]
    alpha_headers = _auth(gateway["alpha"])
    beta_headers = _auth(gateway["beta"])

    alpha_put = client.put(
        "/v1/objects/private/report.txt",
        headers={**alpha_headers, "Content-Type": "text/plain"},
        content=b"alpha-only",
    )
    beta_put = client.put(
        "/v1/objects/private/report.txt",
        headers={**beta_headers, "Content-Type": "text/plain"},
        content=b"beta-only",
    )

    assert alpha_put.status_code == 201
    assert beta_put.status_code == 201
    assert set(storage.objects) == {
        "apps/alpha-orders/private/report.txt",
        "apps/beta-design/private/report.txt",
    }
    assert client.get(
        "/v1/objects/private/report.txt", headers=alpha_headers
    ).content == b"alpha-only"
    assert client.get(
        "/v1/objects/private/report.txt", headers=beta_headers
    ).content == b"beta-only"

    # A never receives an API primitive capable of naming B's server-side prefix.
    with pytest.raises(HTTPException) as error:
        normalise_object_key("../beta-design/private/report.txt")
    assert error.value.status_code == 400
    assert client.get(
        "/v1/objects/beta-design/private/report.txt", headers=alpha_headers
    ).status_code == 404


def test_head_delete_authentication_and_suspension(gateway) -> None:
    client: TestClient = gateway["client"]
    token = gateway["alpha"]
    headers = {**_auth(token), "Content-Type": "application/pdf"}
    payload = b"sample-pdf"
    assert client.put("/v1/objects/a.pdf", headers=headers, content=payload).status_code == 201

    head = client.head("/v1/objects/a.pdf", headers=_auth(token))
    assert head.status_code == 200
    assert head.headers["content-length"] == str(len(payload))
    assert head.headers["content-type"] == "application/pdf"
    assert head.headers["x-storage-sha256"] == hashlib.sha256(payload).hexdigest()
    assert client.get("/v1/objects/a.pdf").status_code == 401
    assert client.get(
        "/v1/objects/a.pdf", headers=_auth("not-a-real-token")
    ).status_code == 401

    gateway["registry"].suspend_app("alpha-orders")
    assert client.get("/v1/objects/a.pdf", headers=_auth(token)).status_code == 403
    with pytest.raises(ApplicationUnavailable):
        gateway["registry"].resolve(token)


@pytest.mark.parametrize(
    "key",
    [
        "",
        "/absolute",
        "\\absolute",
        "a\\b",
        ".",
        "..",
        "a/../b",
        "a/./b",
        "a//b",
        "nul\x00byte",
    ],
)
def test_path_traversal_and_ambiguous_keys_are_rejected(key: str) -> None:
    with pytest.raises(HTTPException) as error:
        normalise_object_key(key)
    assert error.value.status_code == 400


def test_total_scoped_oss_key_cannot_exceed_provider_limit(gateway) -> None:
    relative_key = "a" * 1024
    response = gateway["client"].put(
        f"/v1/objects/{relative_key}",
        headers=_auth(gateway["alpha"]),
        content=b"x",
    )
    assert response.status_code == 400
    assert gateway["storage"].objects == {}


def test_ensure_app_is_idempotent_and_database_never_stores_plain_token(
    tmp_path: Path,
) -> None:
    registry = CredentialRegistry(tmp_path / "registry.sqlite3")
    env_dir = tmp_path / "apps"
    first = registry.ensure_app("same-app", env_dir, "http://gateway:8080")
    token_before = _read_token(first.env_path)
    second = registry.ensure_app("same-app", env_dir, "http://gateway:8080")
    token_after = _read_token(second.env_path)

    assert first.reused is False
    assert second.reused is True
    assert token_after == token_before
    assert token_before.encode() not in (tmp_path / "registry.sqlite3").read_bytes()
    assert "apps/same-app" not in first.env_path.read_text(encoding="utf-8")
    assert "FILE_STORAGE_GATEWAY_TIMEOUT_SECONDS=900\n" in first.env_path.read_text(encoding="utf-8")
    if os.name == "posix":
        assert stat.S_IMODE(first.env_path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX ownership/mode enforcement")
@pytest.mark.parametrize("operation", ["ensure", "rotate"])
def test_unsafe_existing_env_is_preserved_on_validation_failure(
    tmp_path: Path,
    operation: str,
) -> None:
    registry = CredentialRegistry(tmp_path / "registry.sqlite3")
    env_dir = tmp_path / "apps"
    created = registry.ensure_app("protected-app", env_dir, "http://gateway:8080")
    original = created.env_path.read_bytes()
    os.chmod(created.env_path, 0o644)

    with pytest.raises(RegistryError, match="mode 0600"):
        if operation == "ensure":
            registry.ensure_app("protected-app", env_dir, "http://gateway:8080")
        else:
            registry.rotate_app("protected-app", env_dir, "http://gateway:8080")

    assert created.env_path.is_file()
    assert not created.env_path.is_symlink()
    assert created.env_path.read_bytes() == original
    assert stat.S_IMODE(created.env_path.stat().st_mode) == 0o644


def test_rotation_has_grace_and_revoke_removes_env(gateway) -> None:
    registry: CredentialRegistry = gateway["registry"]
    env_dir: Path = gateway["env_dir"]
    old_token = gateway["beta"]

    rotated = registry.rotate_app(
        "beta-design", env_dir, "http://zhuojian-storage-gateway:8080", grace_seconds=60
    )
    new_token = _read_token(rotated.env_path)
    assert new_token != old_token
    assert registry.resolve(old_token).slug == "beta-design"
    assert registry.resolve(new_token).slug == "beta-design"

    registry.revoke_app("beta-design", env_dir)
    assert not rotated.env_path.exists()
    with pytest.raises(Exception):
        registry.resolve(old_token)
    with pytest.raises(Exception):
        registry.resolve(new_token)


def test_two_phase_rotation_keeps_old_token_until_idempotent_commit(gateway) -> None:
    registry: CredentialRegistry = gateway["registry"]
    env_dir: Path = gateway["env_dir"]
    old_token = gateway["alpha"]
    operation_id = "a" * 32

    prepared = registry.prepare_rotation(
        "alpha-orders",
        env_dir,
        "http://zhuojian-storage-gateway:8080",
        operation_id=operation_id,
        grace_seconds=120,
    )
    new_token = _read_token(prepared.env_path)
    assert prepared.phase == "prepared"
    assert prepared.reused is False
    assert new_token != old_token
    assert registry.resolve(old_token).slug == "alpha-orders"
    assert registry.resolve(new_token).slug == "alpha-orders"

    replay = registry.prepare_rotation(
        "alpha-orders",
        env_dir,
        "http://zhuojian-storage-gateway:8080",
        operation_id=operation_id,
        grace_seconds=120,
    )
    assert replay.reused is True
    assert replay.phase == "prepared"
    assert _read_token(replay.env_path) == new_token

    with sqlite3.connect(registry.db_path) as connection:
        before_commit = connection.execute(
            "SELECT expires_at,revoked_at FROM credentials WHERE slug='alpha-orders' "
            "ORDER BY created_at"
        ).fetchall()
    assert before_commit == [(None, None), (None, None)]

    committed = registry.commit_rotation(
        "alpha-orders", env_dir, operation_id=operation_id
    )
    commit_replay = registry.commit_rotation(
        "alpha-orders", env_dir, operation_id=operation_id
    )
    assert committed.phase == "committed"
    assert committed.reused is False
    assert committed.grace_until is not None
    assert commit_replay.reused is True
    assert commit_replay.grace_until == committed.grace_until
    assert registry.resolve(old_token).slug == "alpha-orders"
    assert registry.resolve(new_token).slug == "alpha-orders"

    with sqlite3.connect(registry.db_path) as connection:
        rows = connection.execute(
            "SELECT token_hash,expires_at,revoked_at FROM credentials WHERE slug='alpha-orders'"
        ).fetchall()
    expiring = [row for row in rows if row[1] == committed.grace_until]
    current = [row for row in rows if row[1] is None and row[2] is None]
    assert len(expiring) == 1
    assert len(current) == 1


def test_prepare_rotation_recovers_env_replace_before_database_commit(tmp_path: Path) -> None:
    registry = CredentialRegistry(tmp_path / "registry.sqlite3")
    env_dir = tmp_path / "apps"
    url = "http://gateway:8080"
    created = registry.ensure_app("crash-app", env_dir, url)
    old_token = _read_token(created.env_path)
    operation_id = "b" * 32
    interrupted_token = "interrupted-rotation-token-" + ("x" * 48)
    created.env_path.write_bytes(
        _render_env(
            interrupted_token,
            url,
            rotation_operation_id=operation_id,
        )
    )
    if os.name == "posix":
        os.chmod(created.env_path, 0o600)

    # An ordinary ensure must not revoke the still-running container's old
    # token when it observes the cross-file crash marker.
    with pytest.raises(RegistryError, match="interrupted storage rotation"):
        registry.ensure_app("crash-app", env_dir, url)
    assert _read_token(created.env_path) == interrupted_token
    assert registry.resolve(old_token).slug == "crash-app"

    recovered = registry.prepare_rotation(
        "crash-app",
        env_dir,
        url,
        operation_id=operation_id,
        grace_seconds=300,
    )
    assert recovered.reused is True
    assert _read_token(recovered.env_path) == interrupted_token
    assert registry.resolve(old_token).slug == "crash-app"
    assert registry.resolve(interrupted_token).slug == "crash-app"
    assert interrupted_token.encode() not in registry.db_path.read_bytes()


def test_prepare_rotation_rejects_second_operation_while_first_is_pending(gateway) -> None:
    registry: CredentialRegistry = gateway["registry"]
    env_dir: Path = gateway["env_dir"]
    registry.prepare_rotation(
        "beta-design",
        env_dir,
        "http://zhuojian-storage-gateway:8080",
        operation_id="c" * 32,
        grace_seconds=300,
    )
    with pytest.raises(RegistryError, match="another storage rotation"):
        registry.prepare_rotation(
            "beta-design",
            env_dir,
            "http://zhuojian-storage-gateway:8080",
            operation_id="d" * 32,
            grace_seconds=300,
        )


@pytest.mark.parametrize("state", ["suspended", "revoked"])
def test_rotation_cannot_reactivate_unavailable_application(
    tmp_path: Path, state: str
) -> None:
    registry = CredentialRegistry(tmp_path / "registry.sqlite3")
    env_dir = tmp_path / "apps"
    registry.ensure_app("controlled-app", env_dir, "http://gateway:8080")
    if state == "suspended":
        registry.suspend_app("controlled-app")
    else:
        registry.revoke_app("controlled-app", env_dir)
    with pytest.raises(ApplicationUnavailable):
        registry.rotate_app("controlled-app", env_dir, "http://gateway:8080")


def test_ensure_never_reactivates_suspended_or_revoked_apps(tmp_path: Path) -> None:
    registry = CredentialRegistry(tmp_path / "registry.sqlite3")
    env_dir = tmp_path / "apps"
    registry.ensure_app("controlled-app", env_dir, "http://gateway:8080")
    registry.suspend_app("controlled-app")
    with pytest.raises(ApplicationUnavailable):
        registry.ensure_app("controlled-app", env_dir, "http://gateway:8080")
    registry.revoke_app("controlled-app", env_dir)
    with pytest.raises(ApplicationUnavailable):
        registry.ensure_app("controlled-app", env_dir, "http://gateway:8080")


def test_only_suspended_application_can_be_explicitly_resumed(tmp_path: Path) -> None:
    registry = CredentialRegistry(tmp_path / "registry.sqlite3")
    env_dir = tmp_path / "apps"
    created = registry.ensure_app("controlled-app", env_dir, "http://gateway:8080")
    token = _read_token(created.env_path)

    registry.suspend_app("controlled-app")
    with pytest.raises(ApplicationUnavailable):
        registry.resolve(token)
    registry.resume_app("controlled-app", env_dir)
    assert registry.resolve(token).slug == "controlled-app"
    assert registry.ensure_app("controlled-app", env_dir, "http://gateway:8080").reused

    registry.revoke_app("controlled-app", env_dir)
    with pytest.raises(ApplicationUnavailable):
        registry.resume_app("controlled-app", env_dir)


def test_concurrent_ensure_converges_on_one_credential(tmp_path: Path) -> None:
    registry = CredentialRegistry(tmp_path / "registry.sqlite3")
    env_dir = tmp_path / "apps"

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda _: registry.ensure_app(
                    "concurrent-app", env_dir, "http://gateway:8080"
                ),
                range(16),
            )
        )

    assert sum(result.reused is False for result in results) == 1
    token = _read_token(env_dir / "concurrent-app.storage.env")
    assert registry.resolve(token).slug == "concurrent-app"


def test_outside_prefix_probe_checks_all_four_permissions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []

    class Denied(Exception):
        status = 403

    class Bucket:
        def __init__(self, *_args) -> None:
            pass

        def list_objects_v2(self, *, prefix: str, max_keys: int):
            calls.append(("list", (prefix, max_keys)))
            raise Denied

        def head_object(self, key: str):
            calls.append(("read", key))
            raise Denied

        def delete_object(self, key: str):
            calls.append(("delete", key))
            raise Denied

        def put_object(self, key: str, body: bytes, *, headers: dict[str, str]):
            calls.append(("write", (key, body, headers)))
            raise Denied

    fake_oss2 = SimpleNamespace(
        Auth=lambda *_args: object(),
        StsAuth=lambda *_args: object(),
        Bucket=Bucket,
    )
    monkeypatch.setitem(sys.modules, "oss2", fake_oss2)
    credentials = OssCredentials("https://oss-cn-test.aliyuncs.com", "bucket-a", "id", "secret")

    _assert_outside_prefix_denied(credentials, "run")

    assert [name for name, _value in calls] == ["list", "read", "delete", "write"]
    write_headers = calls[-1][1][2]
    assert write_headers["Content-MD5"] == "AAAAAAAAAAAAAAAAAAAAAA=="


def test_outside_prefix_probe_rejects_overbroad_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Denied(Exception):
        status = 403

    class Bucket:
        def __init__(self, *_args) -> None:
            pass

        def list_objects_v2(self, **_kwargs):
            raise Denied

        def head_object(self, _key: str):
            raise Denied

        def delete_object(self, _key: str):
            raise Denied

        def put_object(self, *_args, **_kwargs):
            return object()

    monkeypatch.setitem(
        sys.modules,
        "oss2",
        SimpleNamespace(Auth=lambda *_args: object(), StsAuth=lambda *_args: object(), Bucket=Bucket),
    )
    with pytest.raises(GatewayProbeError, match="permits outside-prefix write"):
        _assert_outside_prefix_denied(
            OssCredentials("https://oss-cn-test.aliyuncs.com", "bucket-a", "id", "secret"),
            "run",
        )


def test_anonymous_read_probe_requires_http_403() -> None:
    class PrivateOpener:
        def open(self, request, timeout: int):
            raise HTTPError(request.full_url, 403, "Forbidden", {}, None)

    credentials = OssCredentials(
        "https://oss-cn-test.aliyuncs.com", "private-bucket", "id", "secret"
    )
    _assert_anonymous_read_denied(PrivateOpener(), credentials, "apps/a/key")

    class PublicResponse:
        status = 206

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _size: int) -> bytes:
            return b"x"

    class PublicOpener:
        def open(self, _request, timeout: int):
            return PublicResponse()

    with pytest.raises(GatewayProbeError, match="expected 403"):
        _assert_anonymous_read_denied(PublicOpener(), credentials, "apps/a/key")


def _read_token(path: Path) -> str:
    values = dict(
        line.split("=", 1)
        for line in path.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    assert values["FILE_STORAGE_DRIVER"] == "oss-gateway"
    assert values["FILE_STORAGE_TOKEN"] == values["STORAGE_PROJECT_TOKEN"]
    return values["FILE_STORAGE_TOKEN"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
