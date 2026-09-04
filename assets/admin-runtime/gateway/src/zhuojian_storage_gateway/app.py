from __future__ import annotations

import hashlib
import asyncio
import os
import shutil
import tempfile
from collections.abc import Iterator
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from .config import ConfigurationError, GatewaySettings, load_oss_credentials
from .registry import (
    ApplicationScope,
    ApplicationUnavailable,
    CredentialRegistry,
    InvalidCredential,
)
from .storage import (
    ObjectDownload,
    ObjectMetadata,
    ObjectNotFound,
    Oss2StorageAdapter,
    StorageAdapter,
    StorageUnavailable,
)


def create_app(
    *,
    registry: CredentialRegistry | None = None,
    storage: StorageAdapter | None = None,
    settings: GatewaySettings | None = None,
) -> FastAPI:
    settings = settings or GatewaySettings.from_environment()
    registry = registry or CredentialRegistry(settings.db_path)
    storage = storage or Oss2StorageAdapter(load_oss_credentials(settings.oss_secrets_file))

    api = FastAPI(
        title="ZhuoJian Storage Gateway",
        version="1",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    api.state.registry = registry
    api.state.storage = storage
    api.state.settings = settings
    api.state.upload_slots = asyncio.Semaphore(settings.max_concurrent_uploads)
    _prepare_spool_directory(settings)

    def authenticate(
        authorization: str | None = Header(default=None),
    ) -> ApplicationScope:
        if not authorization:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Bearer credential required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        scheme, separator, token = authorization.partition(" ")
        if not separator or scheme.lower() != "bearer" or not token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid bearer credential",
                headers={"WWW-Authenticate": "Bearer"},
            )
        try:
            return registry.resolve(token)
        except InvalidCredential as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid bearer credential",
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc
        except ApplicationUnavailable as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Application storage is unavailable",
            ) from exc

    @api.get("/healthz", include_in_schema=False)
    def healthcheck() -> dict[str, str]:
        registry.healthcheck()
        return {"status": "ok"}

    @api.get("/v1/health", include_in_schema=False)
    def versioned_healthcheck() -> dict[str, str]:
        registry.healthcheck()
        return {"status": "ok"}

    @api.put("/v1/objects/{key:path}")
    async def put_object(
        key: str,
        request: Request,
        scope: ApplicationScope = Depends(authenticate),
    ) -> JSONResponse:
        relative_key = normalise_object_key(key)
        full_key = _scoped_key(settings.root_prefix, scope.slug, relative_key)
        digest = hashlib.sha256()
        size = 0
        content_type = request.headers.get("content-type")
        announced_size = _announced_size(request, settings.max_upload_bytes)
        _ensure_spool_capacity(settings, announced_size or 0)

        async with api.state.upload_slots:
            with tempfile.SpooledTemporaryFile(
                max_size=settings.spool_memory_bytes,
                dir=settings.spool_dir,
            ) as body:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > settings.max_upload_bytes:
                        raise HTTPException(
                            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            detail="Object exceeds the configured upload limit",
                        )
                    _ensure_spool_capacity(settings)
                    digest.update(chunk)
                    body.write(chunk)
                if announced_size is not None and size != announced_size:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Content-Length does not match the received object",
                    )
                body.seek(0)
                try:
                    metadata = await run_in_threadpool(
                        storage.put_object,
                        full_key,
                        body,
                        content_type=content_type,
                        sha256=digest.hexdigest(),
                        size=size,
                    )
                except StorageUnavailable as exc:
                    raise HTTPException(
                        status_code=status.HTTP_502_BAD_GATEWAY,
                        detail="Object storage is unavailable",
                    ) from exc

        return JSONResponse(
            status_code=status.HTTP_201_CREATED,
            content={
                "key": relative_key,
                "size": metadata.size,
                "sha256": metadata.sha256,
                "etag": metadata.etag,
            },
        )

    @api.get("/v1/objects/{key:path}")
    async def get_object(
        key: str,
        scope: ApplicationScope = Depends(authenticate),
    ) -> StreamingResponse:
        relative_key = normalise_object_key(key)
        full_key = _scoped_key(settings.root_prefix, scope.slug, relative_key)
        try:
            download = await run_in_threadpool(storage.get_object, full_key)
        except ObjectNotFound as exc:
            raise HTTPException(status_code=404, detail="Object not found") from exc
        except StorageUnavailable as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Object storage is unavailable",
            ) from exc

        return StreamingResponse(
            _stream_and_close(download, settings.io_chunk_bytes),
            media_type=download.metadata.content_type or "application/octet-stream",
            headers=_metadata_headers(download.metadata),
        )

    @api.head("/v1/objects/{key:path}")
    async def head_object(
        key: str,
        scope: ApplicationScope = Depends(authenticate),
    ) -> Response:
        relative_key = normalise_object_key(key)
        full_key = _scoped_key(settings.root_prefix, scope.slug, relative_key)
        try:
            metadata = await run_in_threadpool(storage.head_object, full_key)
        except ObjectNotFound as exc:
            raise HTTPException(status_code=404, detail="Object not found") from exc
        except StorageUnavailable as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Object storage is unavailable",
            ) from exc
        return Response(status_code=200, headers=_metadata_headers(metadata))

    @api.delete("/v1/objects/{key:path}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_object(
        key: str,
        scope: ApplicationScope = Depends(authenticate),
    ) -> Response:
        relative_key = normalise_object_key(key)
        full_key = _scoped_key(settings.root_prefix, scope.slug, relative_key)
        try:
            await run_in_threadpool(storage.delete_object, full_key)
        except ObjectNotFound:
            pass  # DELETE is deliberately idempotent.
        except StorageUnavailable as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Object storage is unavailable",
            ) from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return api


def _prepare_spool_directory(settings: GatewaySettings) -> None:
    if not settings.spool_dir.is_absolute():
        raise ConfigurationError("GATEWAY_SPOOL_DIR must be an absolute path")
    settings.spool_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        os.chmod(settings.spool_dir, 0o700)


def _announced_size(request: Request, maximum: int) -> int | None:
    raw_value = request.headers.get("content-length")
    if raw_value is None:
        return None
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid Content-Length") from exc
    if value < 0:
        raise HTTPException(status_code=400, detail="Invalid Content-Length")
    if value > maximum:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Object exceeds the configured upload limit",
        )
    return value


def _ensure_spool_capacity(settings: GatewaySettings, incoming_bytes: int = 0) -> None:
    try:
        free = shutil.disk_usage(settings.spool_dir).free
    except OSError as exc:
        raise HTTPException(status_code=503, detail="Upload capacity is unavailable") from exc
    if free - incoming_bytes < settings.minimum_free_bytes:
        raise HTTPException(
            status_code=507,
            detail="Insufficient storage for upload buffering",
        )


def normalise_object_key(value: str) -> str:
    if not value or len(value.encode("utf-8")) > 1024:
        raise HTTPException(status_code=400, detail="Invalid object key")
    if value.startswith(("/", "\\")) or "\\" in value or "\x00" in value:
        raise HTTPException(status_code=400, detail="Invalid object key")
    segments = value.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise HTTPException(status_code=400, detail="Invalid object key")
    return "/".join(segments)


def _scoped_key(root_prefix: str, slug: str, relative_key: str) -> str:
    # Neither the caller nor an application-provided field can affect this prefix.
    full_key = f"{root_prefix}/{slug}/{relative_key}"
    if len(full_key.encode("utf-8")) > 1024:
        raise HTTPException(status_code=400, detail="Invalid object key")
    return full_key


def _metadata_headers(metadata: ObjectMetadata) -> dict[str, str]:
    headers = {"Content-Length": str(metadata.size)}
    if metadata.content_type:
        headers["Content-Type"] = metadata.content_type
    if metadata.etag:
        headers["ETag"] = f'"{metadata.etag.strip(chr(34))}"'
    if metadata.sha256:
        headers["X-Storage-Sha256"] = metadata.sha256
    return headers


def _stream_and_close(download: ObjectDownload, chunk_size: int) -> Iterator[bytes]:
    try:
        while True:
            chunk = download.body.read(chunk_size)
            if not chunk:
                break
            yield chunk
    finally:
        close = getattr(download.body, "close", None)
        if close:
            close()
