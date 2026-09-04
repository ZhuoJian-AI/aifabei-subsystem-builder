from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path


class ConfigurationError(RuntimeError):
    """Raised when gateway configuration is missing or unsafe."""


_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
_BUCKET_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")


@dataclass(frozen=True)
class GatewaySettings:
    db_path: Path
    apps_env_dir: Path
    internal_url: str
    root_prefix: str
    oss_secrets_file: Path
    max_upload_bytes: int
    spool_memory_bytes: int
    io_chunk_bytes: int
    spool_dir: Path = Path("/var/lib/zhuojian-storage-gateway/tmp")
    minimum_free_bytes: int = 5 * 1024**3

    @classmethod
    def from_environment(cls) -> "GatewaySettings":
        root_prefix = _normalise_root_prefix(os.environ.get("GATEWAY_ROOT_PREFIX", "apps"))
        internal_url = os.environ.get(
            "GATEWAY_INTERNAL_URL", "http://zhuojian-storage-gateway:8080"
        ).strip()
        if not internal_url.startswith(("http://", "https://")) or any(
            char in internal_url for char in "\r\n"
        ):
            raise ConfigurationError("GATEWAY_INTERNAL_URL is invalid")

        return cls(
            db_path=Path(
                os.environ.get(
                    "GATEWAY_DB_PATH",
                    "/var/lib/zhuojian-storage-gateway/registry.sqlite3",
                )
            ),
            apps_env_dir=Path(os.environ.get("GATEWAY_APPS_ENV_DIR", "/etc/zhuojian/apps")),
            internal_url=internal_url.rstrip("/"),
            root_prefix=root_prefix,
            oss_secrets_file=Path(
                os.environ.get(
                    "OSS_SECRETS_FILE", "/run/secrets/zhuojian-oss-gateway.env"
                )
            ),
            max_upload_bytes=_positive_int("GATEWAY_MAX_UPLOAD_BYTES", 512 * 1024**2),
            spool_memory_bytes=_positive_int("GATEWAY_SPOOL_MEMORY_BYTES", 8 * 1024**2),
            io_chunk_bytes=_positive_int("GATEWAY_IO_CHUNK_BYTES", 1024**2),
            spool_dir=Path(
                os.environ.get(
                    "GATEWAY_SPOOL_DIR",
                    "/var/lib/zhuojian-storage-gateway/tmp",
                )
            ),
            minimum_free_bytes=_positive_int(
                "GATEWAY_MINIMUM_FREE_BYTES", 5 * 1024**3
            ),
        )


@dataclass(frozen=True)
class OssCredentials:
    endpoint: str
    bucket: str
    access_key_id: str
    access_key_secret: str
    security_token: str | None = None


def load_oss_credentials(path: Path) -> OssCredentials:
    """Read the OSS credential file without evaluating shell syntax.

    On POSIX the file must be a root-owned regular file inaccessible to group/other.
    The gateway intentionally has no environment-variable fallback for AK material.
    """

    try:
        file_stat = path.lstat()
    except FileNotFoundError as exc:
        raise ConfigurationError(f"OSS secrets file does not exist: {path}") from exc

    if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
        raise ConfigurationError("OSS secrets path must be a regular file, not a symlink")
    if os.name == "posix":
        if file_stat.st_uid != 0:
            raise ConfigurationError("OSS secrets file must be owned by root")
        if stat.S_IMODE(file_stat.st_mode) & 0o077:
            raise ConfigurationError("OSS secrets file must have mode 0600 or stricter")

    values = _parse_env_file(path)
    missing = [
        key
        for key in (
            "OSS_ENDPOINT",
            "OSS_BUCKET",
            "OSS_ACCESS_KEY_ID",
            "OSS_ACCESS_KEY_SECRET",
        )
        if not values.get(key)
    ]
    if missing:
        raise ConfigurationError(f"OSS secrets file is missing: {', '.join(missing)}")

    endpoint = values["OSS_ENDPOINT"].rstrip("/")
    if not endpoint.startswith("https://") or any(char in endpoint for char in "\r\n"):
        raise ConfigurationError("OSS_ENDPOINT must be an HTTPS URL")
    bucket = values["OSS_BUCKET"]
    if not _BUCKET_NAME.fullmatch(bucket):
        raise ConfigurationError("OSS_BUCKET is invalid")

    return OssCredentials(
        endpoint=endpoint,
        bucket=bucket,
        access_key_id=values["OSS_ACCESS_KEY_ID"],
        access_key_secret=values["OSS_ACCESS_KEY_SECRET"],
        security_token=values.get("OSS_SECURITY_TOKEN") or None,
    )


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, 1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                raise ConfigurationError(f"Invalid OSS secrets line {line_number}")
            name, value = line.split("=", 1)
            name = name.strip()
            value = value.strip()
            if not _ENV_NAME.fullmatch(name):
                raise ConfigurationError(f"Invalid OSS secrets name on line {line_number}")
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            if any(char in value for char in "\r\n\x00"):
                raise ConfigurationError(f"Invalid OSS secrets value on line {line_number}")
            values[name] = value
    return values


def _normalise_root_prefix(value: str) -> str:
    prefix = value.strip().strip("/")
    if not prefix or "\\" in prefix:
        raise ConfigurationError("GATEWAY_ROOT_PREFIX is invalid")
    segments = prefix.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise ConfigurationError("GATEWAY_ROOT_PREFIX is invalid")
    return "/".join(segments)


def _positive_int(name: str, default: int) -> int:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ConfigurationError(f"{name} must be positive")
    return value
