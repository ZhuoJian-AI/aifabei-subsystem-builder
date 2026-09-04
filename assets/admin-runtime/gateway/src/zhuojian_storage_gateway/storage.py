from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import BinaryIO, Protocol

from .config import OssCredentials


class ObjectNotFound(Exception):
    pass


class StorageUnavailable(Exception):
    pass


@dataclass(frozen=True)
class ObjectMetadata:
    size: int
    content_type: str | None = None
    etag: str | None = None
    sha256: str | None = None
    last_modified: datetime | None = None


@dataclass
class ObjectDownload:
    body: BinaryIO
    metadata: ObjectMetadata


class StorageAdapter(Protocol):
    def put_object(
        self,
        key: str,
        body: BinaryIO,
        *,
        content_type: str | None,
        sha256: str,
        size: int,
    ) -> ObjectMetadata: ...

    def get_object(self, key: str) -> ObjectDownload: ...

    def head_object(self, key: str) -> ObjectMetadata: ...

    def delete_object(self, key: str) -> None: ...


class Oss2StorageAdapter:
    """Synchronous adapter around Alibaba Cloud's official ``oss2`` SDK."""

    def __init__(self, credentials: OssCredentials) -> None:
        import oss2

        if credentials.security_token:
            auth = oss2.StsAuth(
                credentials.access_key_id,
                credentials.access_key_secret,
                credentials.security_token,
            )
        else:
            auth = oss2.Auth(credentials.access_key_id, credentials.access_key_secret)
        self._oss2 = oss2
        self._bucket = oss2.Bucket(auth, credentials.endpoint, credentials.bucket)

    def put_object(
        self,
        key: str,
        body: BinaryIO,
        *,
        content_type: str | None,
        sha256: str,
        size: int,
    ) -> ObjectMetadata:
        headers = {
            "Content-Length": str(size),
            "x-oss-meta-sha256": sha256,
        }
        if content_type:
            headers["Content-Type"] = content_type
        try:
            result = self._bucket.put_object(key, body, headers=headers)
        except Exception as exc:  # oss2 has several transport/server exception types
            raise StorageUnavailable("OSS put failed") from exc
        return ObjectMetadata(
            size=size,
            content_type=content_type,
            etag=_clean_etag(getattr(result, "etag", None)),
            sha256=sha256,
        )

    def get_object(self, key: str) -> ObjectDownload:
        try:
            result = self._bucket.get_object(key)
        except Exception as exc:
            self._raise_mapped(exc, "OSS get failed")
        metadata = _metadata_from_oss_result(result)
        return ObjectDownload(body=result, metadata=metadata)

    def head_object(self, key: str) -> ObjectMetadata:
        try:
            result = self._bucket.head_object(key)
        except Exception as exc:
            self._raise_mapped(exc, "OSS head failed")
        return _metadata_from_oss_result(result)

    def delete_object(self, key: str) -> None:
        try:
            self._bucket.delete_object(key)
        except Exception as exc:
            self._raise_mapped(exc, "OSS delete failed")

    def _raise_mapped(self, exc: Exception, message: str) -> None:
        status = getattr(exc, "status", None)
        if status == 404 or isinstance(exc, self._oss2.exceptions.NoSuchKey):
            raise ObjectNotFound from exc
        raise StorageUnavailable(message) from exc


def _metadata_from_oss_result(result: object) -> ObjectMetadata:
    headers = getattr(result, "headers", {}) or {}
    size_value = getattr(result, "content_length", None)
    if size_value is None:
        size_value = headers.get("Content-Length", 0)
    return ObjectMetadata(
        size=int(size_value),
        content_type=getattr(result, "content_type", None) or headers.get("Content-Type"),
        etag=_clean_etag(getattr(result, "etag", None) or headers.get("ETag")),
        sha256=headers.get("x-oss-meta-sha256") or headers.get("X-Oss-Meta-Sha256"),
        last_modified=getattr(result, "last_modified", None),
    )


def _clean_etag(value: object) -> str | None:
    if value is None:
        return None
    return str(value).strip().strip('"') or None
