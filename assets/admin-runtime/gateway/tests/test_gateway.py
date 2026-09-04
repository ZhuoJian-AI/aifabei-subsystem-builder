from __future__ import annotations

import hashlib
import io
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from zhuojian_storage_gateway.app import create_app, normalise_object_key
from zhuojian_storage_gateway.cli import GatewayProbeError, probe_gateway
from zhuojian_storage_gateway.config import GatewaySettings
from zhuojian_storage_gateway.registry import (
    ApplicationUnavailable,
    CredentialRegistry,
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
    if os.name == "posix":
        assert stat.S_IMODE(first.env_path.stat().st_mode) == 0o600


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
