"""Stable file-storage API for native Alphabet subsystems.

Business code only sees a ``storage_key``.  The runtime chooses either a
fixed local data directory or the administrator-operated OSS gateway.  OSS
credentials, bucket names and server-side object prefixes never enter the
application container.
"""

from __future__ import annotations

import hashlib
import io
import json
import mimetypes
import os
import stat
import tempfile
import time
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Iterator, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


CHUNK_SIZE = 1024 * 1024
MAX_STORAGE_KEY_BYTES = 1024
LOCAL_STAGING_DIRECTORY = ".zhuojian-upload-staging"


class StorageError(RuntimeError):
    """Base class for storage failures safe to surface without credentials."""


class StorageConfigurationError(StorageError):
    """The deployment did not provide a complete storage configuration."""


class InvalidStorageKey(StorageError, ValueError):
    """A logical storage key was unsafe or malformed."""


class StorageObjectNotFound(StorageError, FileNotFoundError):
    """The requested logical object does not exist."""


class StorageAuthorizationError(StorageError):
    """The application-specific gateway token was rejected."""


class StorageUnavailableError(StorageError):
    """The selected storage backend could not complete the request."""


@dataclass(frozen=True)
class StorageStat:
    """Backend-neutral metadata saved next to the business record."""

    storage_key: str
    backend: str
    size: int
    sha256: str | None = None
    content_type: str | None = None
    etag: str | None = None
    updated_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}


def normalize_storage_key(storage_key: str) -> str:
    """Validate a logical key without silently changing its identity."""

    if not isinstance(storage_key, str) or not storage_key:
        raise InvalidStorageKey("storageKey must be a non-empty string")
    if storage_key != storage_key.strip():
        raise InvalidStorageKey("storageKey cannot start or end with whitespace")
    if len(storage_key.encode("utf-8")) > MAX_STORAGE_KEY_BYTES:
        raise InvalidStorageKey(f"storageKey exceeds {MAX_STORAGE_KEY_BYTES} UTF-8 bytes")
    if storage_key.startswith(("/", "\\")) or "\\" in storage_key:
        raise InvalidStorageKey("storageKey must be a relative POSIX-style key")
    if any(ord(character) < 32 or ord(character) == 127 for character in storage_key):
        raise InvalidStorageKey("storageKey cannot contain control characters")
    parts = storage_key.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise InvalidStorageKey("storageKey cannot contain empty, '.' or '..' segments")
    if parts[0] == LOCAL_STAGING_DIRECTORY:
        raise InvalidStorageKey("storageKey uses a runtime-reserved namespace")
    return "/".join(parts)


def _encoded_object_url(base_url: str, storage_key: str) -> str:
    key = normalize_storage_key(storage_key)
    return f"{base_url}/v1/objects/{'/'.join(quote(part, safe='-._~') for part in key.split('/'))}"


def _guess_content_type(storage_key: str) -> str:
    return mimetypes.guess_type(storage_key)[0] or "application/octet-stream"


def _sha256_stream(stream: BinaryIO) -> tuple[int, str]:
    size = 0
    digest = hashlib.sha256()
    while True:
        chunk = stream.read(CHUNK_SIZE)
        if not chunk:
            break
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise TypeError("file data must be opened in binary mode")
        digest.update(chunk)
        size += len(chunk)
    return size, digest.hexdigest()


@contextmanager
def _prepared_payload(data: bytes | bytearray | memoryview | BinaryIO) -> Iterator[tuple[BinaryIO, int, str]]:
    """Yield a rewindable binary stream together with length and SHA-256."""

    if isinstance(data, (bytes, bytearray, memoryview)):
        payload = bytes(data)
        yield io.BytesIO(payload), len(payload), hashlib.sha256(payload).hexdigest()
        return
    if not hasattr(data, "read"):
        raise TypeError("file data must be bytes or a binary file-like object")

    try:
        position = data.tell()
        data.seek(position)
        size, digest = _sha256_stream(data)
        data.seek(position)
    except (AttributeError, OSError, io.UnsupportedOperation):
        with tempfile.SpooledTemporaryFile(max_size=8 * CHUNK_SIZE, mode="w+b") as spool:
            digest_state = hashlib.sha256()
            size = 0
            while True:
                chunk = data.read(CHUNK_SIZE)
                if not chunk:
                    break
                if not isinstance(chunk, (bytes, bytearray, memoryview)):
                    raise TypeError("file data must be opened in binary mode")
                spool.write(chunk)
                digest_state.update(chunk)
                size += len(chunk)
            spool.seek(0)
            yield spool, size, digest_state.hexdigest()
        return
    yield data, size, digest


class StorageAdapter(ABC):
    """The only persistent-file API business code should call."""

    backend: str

    @abstractmethod
    def put(
        self,
        storage_key: str,
        data: bytes | bytearray | memoryview | BinaryIO,
        *,
        content_type: str | None = None,
    ) -> StorageStat:
        raise NotImplementedError

    @abstractmethod
    def open(self, storage_key: str) -> BinaryIO:
        raise NotImplementedError

    @abstractmethod
    def delete(self, storage_key: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def exists(self, storage_key: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    def stat(self, storage_key: str) -> StorageStat:
        raise NotImplementedError


class LocalStorageAdapter(StorageAdapter):
    backend = "local"

    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path_for_read(self, storage_key: str) -> tuple[str, Path]:
        key = normalize_storage_key(storage_key)
        candidate = self.root.joinpath(*key.split("/"))
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(self.root)
        except FileNotFoundError as exc:
            raise StorageObjectNotFound(f"storage object not found: {key}") from exc
        except ValueError as exc:
            raise InvalidStorageKey("storageKey resolves outside the configured storage root") from exc
        if not resolved.is_file():
            raise StorageObjectNotFound(f"storage object not found: {key}")
        return key, resolved

    def _path_for_write(self, storage_key: str) -> tuple[str, Path]:
        key = normalize_storage_key(storage_key)
        candidate = self.root.joinpath(*key.split("/"))
        candidate.parent.mkdir(parents=True, exist_ok=True)
        try:
            parent = candidate.parent.resolve(strict=True)
            parent.relative_to(self.root)
        except ValueError as exc:
            raise InvalidStorageKey("storageKey resolves outside the configured storage root") from exc
        return key, parent / candidate.name

    def _staging_directory(self, *, create: bool) -> Path | None:
        staging = self.root / LOCAL_STAGING_DIRECTORY
        try:
            if create:
                staging.mkdir(mode=0o700, exist_ok=True)
            elif not staging.exists() and not staging.is_symlink():
                return None
            if staging.is_symlink() or not staging.is_dir():
                raise StorageUnavailableError("local upload staging path is unsafe")
            resolved = staging.resolve(strict=True)
            resolved.relative_to(self.root)
            return resolved
        except StorageError:
            raise
        except (OSError, ValueError) as exc:
            raise StorageUnavailableError("local upload staging path is unavailable") from exc

    def put(
        self,
        storage_key: str,
        data: bytes | bytearray | memoryview | BinaryIO,
        *,
        content_type: str | None = None,
    ) -> StorageStat:
        key, destination = self._path_for_write(storage_key)
        staging = self._staging_directory(create=True)
        assert staging is not None
        temporary_path: Path | None = None
        try:
            with _prepared_payload(data) as (stream, size, digest):
                with tempfile.NamedTemporaryFile(
                    mode="w+b", prefix=".upload-", dir=staging, delete=False
                ) as temporary:
                    temporary_path = Path(temporary.name)
                    while True:
                        chunk = stream.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        temporary.write(chunk)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                os.replace(temporary_path, destination)
                temporary_path = None
                try:
                    os.chmod(destination, 0o600)
                except OSError:
                    pass

            return StorageStat(
                storage_key=key,
                backend=self.backend,
                size=size,
                sha256=digest,
                content_type=content_type or _guess_content_type(key),
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
        except StorageError:
            raise
        except OSError as exc:
            raise StorageUnavailableError(f"local storage write failed for {key}") from exc
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def cleanup_stale_uploads(self, older_than_seconds: int = 1800) -> int:
        """Remove only adapter-owned temporary files while the host upload lock is held."""

        removed = 0
        cutoff = time.time() - max(0, older_than_seconds)
        try:
            staging = self._staging_directory(create=False)
            if staging is None:
                return 0
            for candidate in staging.glob(".upload-*"):
                try:
                    info = candidate.lstat()
                    if not stat.S_ISREG(info.st_mode) or candidate.is_symlink():
                        continue
                    if info.st_mtime > cutoff:
                        continue
                    if candidate.resolve(strict=True).parent != staging:
                        continue
                    candidate.unlink()
                    removed += 1
                except (FileNotFoundError, ValueError):
                    continue
        except OSError as exc:
            raise StorageUnavailableError("local upload cleanup failed") from exc
        return removed

    def open(self, storage_key: str) -> BinaryIO:
        key, path = self._path_for_read(storage_key)
        try:
            return path.open("rb")
        except OSError as exc:
            raise StorageUnavailableError(f"local storage read failed for {key}") from exc

    def delete(self, storage_key: str) -> None:
        key = normalize_storage_key(storage_key)
        try:
            _, path = self._path_for_read(key)
        except StorageObjectNotFound:
            return
        try:
            path.unlink()
        except OSError as exc:
            raise StorageUnavailableError(f"local storage delete failed for {key}") from exc

    def exists(self, storage_key: str) -> bool:
        try:
            self._path_for_read(storage_key)
            return True
        except StorageObjectNotFound:
            return False

    def stat(self, storage_key: str) -> StorageStat:
        key, path = self._path_for_read(storage_key)
        try:
            info = path.stat()
            with path.open("rb") as stream:
                _, digest = _sha256_stream(stream)
        except OSError as exc:
            raise StorageUnavailableError(f"local storage metadata read failed for {key}") from exc
        return StorageStat(
            storage_key=key,
            backend=self.backend,
            size=info.st_size,
            sha256=digest,
            content_type=_guess_content_type(key),
            updated_at=datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(),
        )


class _RejectRedirects(HTTPRedirectHandler):
    """Never forward an application token to a redirected host."""

    def redirect_request(self, request, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


class GatewayStorageAdapter(StorageAdapter):
    backend = "oss-gateway"

    def __init__(self, base_url: str, token: str, *, timeout: float = 900.0):
        self.base_url = self._validate_base_url(base_url)
        if not token or not token.strip():
            raise StorageConfigurationError("FILE_STORAGE_TOKEN is required for oss-gateway storage")
        if timeout <= 0:
            raise StorageConfigurationError("FILE_STORAGE_GATEWAY_TIMEOUT_SECONDS must be greater than zero")
        self._token = token
        self.timeout = timeout
        # The gateway is an internal runtime service.  Never inherit a machine's
        # HTTP(S)_PROXY settings: doing so can leak the per-application bearer
        # token to a proxy and also breaks private Docker-network addresses.
        self._opener = build_opener(ProxyHandler({}), _RejectRedirects())

    @staticmethod
    def _validate_base_url(value: str) -> str:
        value = (value or "").rstrip("/")
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise StorageConfigurationError(
                "FILE_STORAGE_GATEWAY_URL must be an http(s) URL without credentials, query or fragment"
            )
        return value

    def _request(
        self,
        method: str,
        storage_key: str,
        *,
        data: BinaryIO | None = None,
        headers: Mapping[str, str] | None = None,
        not_found_is_none: bool = False,
    ):
        key = normalize_storage_key(storage_key)
        request_headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            **dict(headers or {}),
        }
        request = Request(
            _encoded_object_url(self.base_url, key),
            data=data,
            headers=request_headers,
            method=method,
        )
        try:
            return self._opener.open(request, timeout=self.timeout)
        except HTTPError as exc:
            status = exc.code
            exc.close()
            if status == 404:
                if not_found_is_none:
                    return None
                raise StorageObjectNotFound(f"storage object not found: {key}") from exc
            if status in {401, 403}:
                raise StorageAuthorizationError("file storage gateway rejected this application's token") from exc
            if 300 <= status < 400:
                raise StorageUnavailableError("file storage gateway redirects are not allowed") from exc
            raise StorageUnavailableError(f"file storage gateway returned HTTP {status}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise StorageUnavailableError("file storage gateway is unavailable") from exc

    def put(
        self,
        storage_key: str,
        data: bytes | bytearray | memoryview | BinaryIO,
        *,
        content_type: str | None = None,
    ) -> StorageStat:
        key = normalize_storage_key(storage_key)
        with _prepared_payload(data) as (stream, size, digest):
            response = self._request(
                "PUT",
                key,
                data=stream,
                headers={
                    "Content-Length": str(size),
                    "Content-Type": content_type or _guess_content_type(key),
                    "X-Storage-Sha256": digest,
                },
            )
            try:
                raw = response.read(64 * 1024)
                response_payload = json.loads(raw) if raw else {}
            except (json.JSONDecodeError, UnicodeDecodeError):
                response_payload = {}
            finally:
                response.close()
        if isinstance(response_payload, dict):
            returned_key = response_payload.get("key")
            returned_size = response_payload.get("size")
            returned_digest = response_payload.get("sha256")
            if returned_key is not None and returned_key != key:
                raise StorageUnavailableError("file storage gateway returned a mismatched storageKey")
            if returned_size is not None and int(returned_size) != size:
                raise StorageUnavailableError("file storage gateway returned a mismatched object size")
            if returned_digest is not None and returned_digest != digest:
                raise StorageUnavailableError("file storage gateway returned a mismatched SHA-256")
        return StorageStat(
            storage_key=key,
            backend=self.backend,
            size=size,
            sha256=digest,
            content_type=content_type or _guess_content_type(key),
            etag=response_payload.get("etag") if isinstance(response_payload, dict) else None,
        )

    def open(self, storage_key: str) -> BinaryIO:
        return self._request("GET", storage_key)

    def delete(self, storage_key: str) -> None:
        response = self._request("DELETE", storage_key, not_found_is_none=True)
        if response is not None:
            response.close()

    def exists(self, storage_key: str) -> bool:
        response = self._request("HEAD", storage_key, not_found_is_none=True)
        if response is None:
            return False
        response.close()
        return True

    def stat(self, storage_key: str) -> StorageStat:
        key = normalize_storage_key(storage_key)
        response = self._request("HEAD", key)
        try:
            try:
                size = int(response.headers["Content-Length"])
            except (KeyError, TypeError, ValueError) as exc:
                raise StorageUnavailableError("file storage gateway omitted a valid Content-Length") from exc
            return StorageStat(
                storage_key=key,
                backend=self.backend,
                size=size,
                sha256=response.headers.get("X-Storage-Sha256"),
                content_type=response.headers.get_content_type(),
                etag=response.headers.get("ETag"),
                updated_at=response.headers.get("Last-Modified"),
            )
        finally:
            response.close()


def storage_for_backend(
    backend: str,
    environment: Mapping[str, str] | None = None,
) -> StorageAdapter:
    """Build one named backend for primary use or a controlled migration read."""

    env = os.environ if environment is None else environment
    driver = (backend or "").strip().lower()
    if driver in {"local", "local-managed"}:
        return LocalStorageAdapter(env.get("FILE_STORAGE_ROOT") or "/data/files")
    if driver in {"oss-gateway", "oss_gateway", "oss"}:
        base_url = env.get("FILE_STORAGE_GATEWAY_URL") or env.get("STORAGE_GATEWAY_URL") or ""
        token = env.get("FILE_STORAGE_TOKEN") or env.get("STORAGE_PROJECT_TOKEN") or ""
        try:
            timeout = float(env.get("FILE_STORAGE_GATEWAY_TIMEOUT_SECONDS") or "900")
        except ValueError as exc:
            raise StorageConfigurationError(
                "FILE_STORAGE_GATEWAY_TIMEOUT_SECONDS must be a number"
            ) from exc
        if not base_url:
            raise StorageConfigurationError("FILE_STORAGE_GATEWAY_URL is required for oss-gateway storage")
        if not token:
            raise StorageConfigurationError("FILE_STORAGE_TOKEN is required for oss-gateway storage")
        return GatewayStorageAdapter(base_url, token, timeout=timeout)
    raise StorageConfigurationError(
        f"unsupported FILE_STORAGE_DRIVER {driver!r}; expected 'local' or 'oss-gateway'"
    )


def storage_from_env(environment: Mapping[str, str] | None = None) -> StorageAdapter:
    """Build the explicit primary backend; failures never fall back."""

    env = os.environ if environment is None else environment
    driver = (env.get("FILE_STORAGE_DRIVER") or "local").strip().lower()
    return storage_for_backend(driver, env)
