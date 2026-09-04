#!/usr/bin/env python3
"""Controlled, local-only deployment foundation for a ZhuoJian enterprise ECS.

This program deliberately has no command that creates or rotates an ECS Runtime
credential.  It never opens the registration credential; ``doctor`` only uses
``lstat`` to verify its ownership and mode.
"""

from __future__ import annotations

import argparse
import copy
import contextlib
import datetime as dt
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any, Iterator

try:  # Linux production dependency; the fallback permits local syntax/unit tests.
    import fcntl
except ImportError:  # pragma: no cover - Windows development host
    fcntl = None


MANAGED_BY = "zhuojian-runtime-admin/v1"
SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
STORAGE_ROTATION_OPERATION_RE = re.compile(r"^[0-9a-f]{32}$")
DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
PORT_MIN = 18000
PORT_MAX = 18999
GIB = 1024**3
LOCAL_STORAGE_MODE = "local-managed"
OSS_STORAGE_MODE = "oss-gateway"
STORAGE_NETWORK = "zhuojian-storage"
STORAGE_GATEWAY_SERVICE = "zhuojian-storage-gateway"
STORAGE_GATEWAY_URL = f"http://{STORAGE_GATEWAY_SERVICE}:8080"
STORAGE_ADMIN = Path("/usr/local/sbin/zhuojian-storage-gateway-admin")
STORAGE_CREDENTIAL = Path("/etc/zhuojian/oss-gateway.env")
STORAGE_APP_NETWORK_PREFIX = "zhuojian-storage-"
STORAGE_NETWORK_ROLE_LABEL = "application-storage"
BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
REGION_RE = re.compile(r"^[a-z0-9][a-z0-9-]+$")
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DEFAULT_PLATFORM_ORIGIN = "https://ai-platform.staging.zhuojianai.com"
NGINX_CLIENT_MAX_BODY_SIZE = "512m"
NGINX_APPLICATION_RESPONSE_TIMEOUT = "900s"


class AdminError(RuntimeError):
    """Expected, user-actionable refusal."""


@dataclass(frozen=True)
class Paths:
    runtime: Path = Path("/etc/zhuojian/runtime.json")
    credential: Path = Path("/etc/zhuojian/runtime-registration.key")
    apps_env: Path = Path("/etc/zhuojian/apps")
    storage_apps: Path = Path("/etc/zhuojian/storage-apps")
    repositories: Path = Path("/srv/zhuojian/repositories")
    deployments: Path = Path("/srv/zhuojian/deployments")
    data: Path = Path("/srv/zhuojian/data")
    backups: Path = Path("/srv/zhuojian/backups")
    nginx: Path = Path("/etc/nginx/conf.d")
    acme: Path = Path("/var/lib/zhuojian/acme")
    state: Path = Path("/run/zhuojian/storage-state.json")
    upload_lock: Path = Path("/run/zhuojian/upload.lock")
    lock: Path = Path("/run/lock/zhuojian-runtime-admin.lock")


PATHS = Paths()


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def validate_slug(value: str) -> str:
    if not SLUG_RE.fullmatch(value):
        raise AdminError(
            "applicationSlug must be one lowercase DNS label (letters, digits, hyphens; max 63)"
        )
    return value


def validate_storage_rotation_operation_id(value: str) -> str:
    if not isinstance(value, str) or not STORAGE_ROTATION_OPERATION_RE.fullmatch(value):
        raise AdminError("storage rotation operation id must be 32 lowercase hexadecimal characters")
    return value


def validate_management_host(value: str) -> str:
    if not value or value != value.strip() or any(ch in value for ch in "/\\:@[] \t\r\n"):
        raise AdminError("management host must be one plain IP address or DNS name")
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        if not DOMAIN_RE.fullmatch(value.lower()):
            raise AdminError("management host must be one plain IP address or DNS name") from None
        return value.lower()
    if address.is_unspecified or address.is_multicast:
        raise AdminError("management host cannot be unspecified or multicast")
    return str(address)


def platform_origin(profile: dict[str, Any]) -> str:
    """Return one canonical HTTPS origin safe for env and Nginx contexts."""

    platform = profile.get("platform")
    value = platform.get("baseUrl", DEFAULT_PLATFORM_ORIGIN) if isinstance(platform, dict) else DEFAULT_PLATFORM_ORIGIN
    if not isinstance(value, str) or value != value.strip() or len(value) > 2048:
        raise AdminError("runtime platform.baseUrl must be one HTTPS origin")
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError:
        raise AdminError("runtime platform.baseUrl must be one HTTPS origin") from None
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise AdminError("runtime platform.baseUrl must be one HTTPS origin")
    hostname = parsed.hostname.lower()
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        if not DOMAIN_RE.fullmatch(hostname):
            raise AdminError("runtime platform.baseUrl must be one HTTPS origin") from None
        rendered_host = hostname
    else:
        if address.is_unspecified or address.is_multicast:
            raise AdminError("runtime platform.baseUrl must be one HTTPS origin")
        rendered_host = f"[{address}]" if address.version == 6 else str(address)
    if port is not None and not 1 <= port <= 65535:
        raise AdminError("runtime platform.baseUrl must be one HTTPS origin")
    return f"https://{rendered_host}{f':{port}' if port is not None else ''}"


def normalize_aliyun_region(value: Any) -> str:
    """Canonicalize the accepted `oss-<region>` alias to Alibaba's region ID."""

    if not isinstance(value, str) or value != value.strip():
        raise AdminError("OSS region must be an Alibaba Cloud region ID")
    normalized = value[4:] if value.startswith("oss-") else value
    if normalized.startswith("oss-") or not REGION_RE.fullmatch(normalized):
        raise AdminError("OSS region must be an Alibaba Cloud region ID")
    return normalized


def assert_plain_file(path: Path, *, may_not_exist: bool = False) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        if may_not_exist:
            return
        raise AdminError(f"required file is missing: {path}") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise AdminError(f"refusing non-regular file: {path}")


def assert_directory(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise AdminError(f"required directory is missing: {path}") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise AdminError(f"refusing non-directory path: {path}")


def atomic_write(path: Path, payload: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, mode)
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if not hasattr(os, "fchmod"):  # Windows-only unit-test fallback.
            os.chmod(name, mode)
        os.replace(name, path)
        if os.name != "nt":
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)
        raise


def atomic_json(path: Path, value: Any, mode: int = 0o640) -> None:
    raw = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    atomic_write(path, raw, mode)


@contextlib.contextmanager
def locked(paths: Paths = PATHS) -> Iterator[None]:
    paths.lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(paths.lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if fcntl is None:
            if os.environ.get("ZHUOJIAN_ADMIN_TESTING") != "1":
                raise AdminError("runtime administrator requires Linux file locking")
        else:
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def run(argv: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(argv, text=True, capture_output=True, check=False)
    if check and result.returncode:
        detail = (result.stderr or result.stdout).strip()[-2000:]
        raise AdminError(f"command failed ({argv[0]}): {detail}")
    return result


def load_runtime(paths: Paths = PATHS) -> dict[str, Any]:
    assert_plain_file(paths.runtime)
    try:
        profile = json.loads(paths.runtime.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdminError(f"invalid runtime profile: {exc}") from exc
    if profile.get("schemaVersion") != 2:
        raise AdminError("runtime profile schemaVersion must be 2")
    enterprise = profile.get("enterpriseKey")
    if enterprise != "aifabei":
        raise AdminError("this administrator bundle only accepts enterpriseKey=aifabei")
    organization_id = profile.get("organizationId")
    try:
        uuid.UUID(str(organization_id))
    except (ValueError, TypeError, AttributeError):
        raise AdminError("runtime organizationId must be a UUID") from None
    suffix = profile.get("domains", {}).get("suffix")
    if not isinstance(suffix, str) or not DOMAIN_RE.fullmatch(suffix):
        raise AdminError("runtime domain suffix is invalid")
    deployment = profile.get("deployment", {})
    expected = {
        "repositoriesRoot": str(paths.repositories),
        "deploymentsRoot": str(paths.deployments),
        "dataRoot": str(paths.data),
        "backupsRoot": str(paths.backups),
        "nginxConfigRoot": str(paths.nginx),
        "registrationCredentialRef": str(paths.credential),
    }
    for key, value in expected.items():
        if deployment.get(key) != value:
            raise AdminError(f"runtime deployment.{key} must equal {value}")
    configured_range = profile.get("resources", {}).get("appPortRange", [PORT_MIN, PORT_MAX])
    if (
        not isinstance(configured_range, list)
        or len(configured_range) != 2
        or not all(isinstance(item, int) for item in configured_range)
        or configured_range[0] < PORT_MIN
        or configured_range[1] > PORT_MAX
        or configured_range[0] > configured_range[1]
    ):
        raise AdminError(f"runtime appPortRange must stay inside {PORT_MIN}-{PORT_MAX}")
    mode = default_storage_mode(profile)
    if mode == OSS_STORAGE_MODE:
        file_storage = profile.get("fileStorage", {})
        if file_storage.get("provider") != "aliyun-oss":
            raise AdminError("fileStorage.provider must be aliyun-oss for oss-gateway")
        if file_storage.get("verified") is not True:
            raise AdminError("fileStorage must be verified before oss-gateway deployment")
        if profile.get("capabilities", {}).get("objectStorage") is not True:
            raise AdminError("runtime capability objectStorage must be enabled for oss-gateway")
        validate_oss_profile(profile)
    return profile


def credential_metadata(paths: Paths = PATHS) -> dict[str, Any]:
    """Inspect metadata only.  Never open or read the credential."""
    assert_plain_file(paths.credential)
    info = paths.credential.lstat()
    mode = stat.S_IMODE(info.st_mode)
    return {
        "exists": True,
        "ownerUid": info.st_uid,
        "mode": f"{mode:04o}",
        "secure": info.st_uid == 0 and mode == 0o600,
    }


def secure_file_metadata(path: Path, label: str) -> dict[str, Any]:
    """Inspect a secret file without opening it or returning its contents."""
    assert_plain_file(path)
    info = path.lstat()
    mode = stat.S_IMODE(info.st_mode)
    secure_owner = os.name == "nt" or info.st_uid == 0
    if not secure_owner or mode != 0o600:
        raise AdminError(f"{label} must be root-owned mode 0600")
    return {"exists": True, "ownerUid": info.st_uid, "mode": f"{mode:04o}", "secure": True}


def validate_storage_mode(value: Any) -> str:
    aliases = {"local": LOCAL_STORAGE_MODE, "oss": OSS_STORAGE_MODE}
    value = aliases.get(value, value)
    if value not in {LOCAL_STORAGE_MODE, OSS_STORAGE_MODE}:
        raise AdminError("storage mode must be local-managed or oss-gateway")
    return value


def default_storage_mode(profile: dict[str, Any]) -> str:
    storage = profile.get("fileStorage")
    if not isinstance(storage, dict):
        return LOCAL_STORAGE_MODE
    return validate_storage_mode(storage.get("defaultMode", storage.get("mode", LOCAL_STORAGE_MODE)))


def validate_oss_profile(profile: dict[str, Any]) -> dict[str, Any]:
    storage = profile.get("objectStorage")
    if not isinstance(storage, dict):
        raise AdminError("objectStorage profile is required for oss-gateway")
    if storage.get("provider") != "aliyun-oss":
        raise AdminError("objectStorage.provider must be aliyun-oss")
    if storage.get("mode") not in {"gateway-api-v1", "gateway-signed-url"}:
        raise AdminError("objectStorage.mode must use the managed gateway")
    bucket = storage.get("bucket")
    region = storage.get("region")
    if not isinstance(bucket, str) or not BUCKET_RE.fullmatch(bucket):
        raise AdminError("objectStorage bucket is invalid")
    try:
        normalize_aliyun_region(region)
    except AdminError:
        raise AdminError("objectStorage region is invalid") from None
    if storage.get("rootPrefix") != "apps":
        raise AdminError("objectStorage.rootPrefix must equal apps")
    if storage.get("gatewayBaseUrl") != STORAGE_GATEWAY_URL:
        raise AdminError(f"objectStorage.gatewayBaseUrl must equal {STORAGE_GATEWAY_URL}")
    if storage.get("credentialRef") != str(STORAGE_CREDENTIAL):
        raise AdminError(f"objectStorage.credentialRef must equal {STORAGE_CREDENTIAL}")
    if storage.get("verified") is not True:
        raise AdminError("objectStorage must be verified before oss-gateway deployment")
    return storage


def oss_releases_for_scope_change(paths: Paths = PATHS) -> list[str]:
    """List OSS releases, refusing when any release marker cannot be trusted."""

    assert_directory(paths.deployments)
    oss_releases: list[str] = []
    for candidate in sorted(paths.deployments.glob("*/release.json")):
        try:
            validate_slug(candidate.parent.name)
            assert_plain_file(candidate)
            marker = json.loads(candidate.read_text(encoding="utf-8"))
            if not isinstance(marker, dict) or marker.get("managedBy") != MANAGED_BY:
                raise AdminError("release marker is not managed by this Runtime")
            storage_mode = validate_storage_mode(
                marker.get("storageMode", LOCAL_STORAGE_MODE)
            )
        except (AdminError, OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AdminError(
                f"refusing OSS scope change because a release record is invalid: {candidate}"
            ) from exc
        if storage_mode == OSS_STORAGE_MODE:
            oss_releases.append(candidate.parent.name)
    return oss_releases


def storage_env_path(slug: str, paths: Paths = PATHS) -> Path:
    return paths.storage_apps / f"{validate_slug(slug)}.storage.env"


def legacy_storage_env_path(slug: str, paths: Paths = PATHS) -> Path:
    return paths.apps_env / f"{validate_slug(slug)}.storage.env"


def installed_storage_env_path(
    slug: str,
    paths: Paths = PATHS,
    *,
    expected: str | None = None,
) -> Path:
    """Resolve the one supported storage env across a host-first gateway upgrade."""

    candidates = (storage_env_path(slug, paths), legacy_storage_env_path(slug, paths))
    present = [path for path in candidates if path.exists() or path.is_symlink()]
    if len(present) != 1:
        if present:
            raise AdminError("both legacy and current application storage env files exist")
        raise AdminError("application storage environment was not created by the gateway")
    actual = present[0]
    secure_file_metadata(actual, "application storage environment")
    if expected is not None:
        allowed = {str(path) for path in candidates}
        if expected not in allowed:
            raise AdminError("release storageEnvFile is not a supported managed path")
        if expected != str(actual):
            raise AdminError("release storageEnvFile does not match the installed managed secret")
    return actual


def storage_network_name(slug: str) -> str:
    return f"{STORAGE_APP_NETWORK_PREFIX}{validate_slug(slug)}"


def storage_network_labels(slug: str, profile: dict[str, Any]) -> dict[str, str]:
    return {
        "com.zhuojian.managed-by": MANAGED_BY,
        "com.zhuojian.enterprise": profile["enterpriseKey"],
        "com.zhuojian.application": validate_slug(slug),
        "com.zhuojian.network-role": STORAGE_NETWORK_ROLE_LABEL,
    }


def inspect_storage_network(
    name: str, slug: str, profile: dict[str, Any]
) -> dict[str, Any] | None:
    result = run(
        ["docker", "network", "inspect", "--format", "{{json .}}", name],
        check=False,
    )
    if result.returncode:
        return None
    try:
        network = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AdminError(f"managed storage network returned invalid metadata: {name}") from exc
    if not isinstance(network, dict):
        raise AdminError(f"managed storage network returned invalid metadata: {name}")
    labels = network.get("Labels")
    expected = storage_network_labels(slug, profile)
    if not isinstance(labels, dict) or any(labels.get(key) != value for key, value in expected.items()):
        raise AdminError(f"refusing storage network without exact managed ownership: {name}")
    if network.get("Driver") != "bridge" or network.get("Scope") != "local":
        raise AdminError(f"managed application storage network must be a local bridge: {name}")
    containers = network.get("Containers")
    if containers is None:
        containers = {}
    if not isinstance(containers, dict):
        raise AdminError(f"managed storage network returned invalid membership: {name}")
    canonical = expected_names(slug, profile)["containerName"]
    allowed_names = {STORAGE_GATEWAY_SERVICE, canonical}
    for item in containers.values():
        member_name = item.get("Name") if isinstance(item, dict) else None
        if member_name in allowed_names:
            continue
        rollback_name = isinstance(member_name, str) and re.fullmatch(
            rf"{re.escape(canonical)}-rollback-(?:(?:storage|current)-)?[0-9a-f]{{12}}",
            member_name,
        )
        if not rollback_name:
            raise AdminError(f"refusing storage network with an unknown member: {name}")
        rollback = run(
            [
                "docker",
                "inspect",
                "--format",
                '{{ index .Config.Labels "com.zhuojian.managed-by" }}|'
                '{{ index .Config.Labels "com.zhuojian.application" }}|'
                '{{ index .Config.Labels "com.zhuojian.enterprise" }}|{{.State.Running}}',
                member_name,
            ],
            check=False,
        )
        expected_rollback = f"{MANAGED_BY}|{slug}|{profile['enterpriseKey']}|false"
        if rollback.returncode or rollback.stdout.strip() != expected_rollback:
            raise AdminError(
                f"refusing running or incorrectly labeled rollback member: {name}"
            )
    return network


def ensure_storage_network(slug: str, profile: dict[str, Any]) -> str:
    """Create/verify one app-only bridge and attach only the shared gateway."""

    name = storage_network_name(slug)
    network = inspect_storage_network(name, slug, profile)
    if network is None:
        label_args: list[str] = []
        for key, value in storage_network_labels(slug, profile).items():
            label_args += ["--label", f"{key}={value}"]
        run(["docker", "network", "create", "--driver", "bridge", *label_args, name])
        network = inspect_storage_network(name, slug, profile)
        if network is None:
            raise AdminError(f"managed application storage network was not created: {name}")
    members = {
        item.get("Name")
        for item in network.get("Containers", {}).values()
        if isinstance(item, dict)
    }
    if STORAGE_GATEWAY_SERVICE not in members:
        run(
            [
                "docker",
                "network",
                "connect",
                "--alias",
                STORAGE_GATEWAY_SERVICE,
                name,
                STORAGE_GATEWAY_SERVICE,
            ]
        )
        network = inspect_storage_network(name, slug, profile)
        members = {
            item.get("Name")
            for item in (network or {}).get("Containers", {}).values()
            if isinstance(item, dict)
        }
        if STORAGE_GATEWAY_SERVICE not in members:
            raise AdminError(f"storage gateway did not join the application network: {name}")
    return name


def storage_foundation_ready(profile: dict[str, Any]) -> None:
    validate_oss_profile(profile)
    secure_file_metadata(STORAGE_CREDENTIAL, "OSS gateway credential")
    assert_plain_file(STORAGE_ADMIN)
    if os.name != "nt" and not os.access(STORAGE_ADMIN, os.X_OK):
        raise AdminError(f"storage gateway administrator is not executable: {STORAGE_ADMIN}")
    if run(["docker", "network", "inspect", STORAGE_NETWORK], check=False).returncode:
        raise AdminError(f"Docker storage network is missing: {STORAGE_NETWORK}")
    gateway = run(
        [
            "docker",
            "inspect",
            "--format",
            "{{.State.Running}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}",
            STORAGE_GATEWAY_SERVICE,
        ],
        check=False,
    )
    if gateway.returncode or gateway.stdout.strip() != "true|healthy":
        raise AdminError("managed storage gateway is not running and healthy")


def verify_storage_foundation(bucket: str, region: str) -> None:
    """Run real OSS CRUD and two-app isolation before enabling the Runtime."""

    result = subprocess.run(
        [
            str(STORAGE_ADMIN),
            "probe",
            "--expected-bucket",
            bucket,
            "--expected-region",
            region,
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        # The probe is designed to print no credential, but suppress its output
        # here as a second barrier against future regressions.
        raise AdminError(f"OSS gateway acceptance probe failed (exit {result.returncode})")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AdminError("OSS gateway acceptance probe returned invalid output") from exc
    checks = payload.get("checks") if isinstance(payload, dict) else None
    required = {
        "anonymousReadDenied",
        "put",
        "get",
        "delete",
        "twoApplicationIsolation",
        "temporaryCredentialsRevoked",
        "outsidePrefixDenied",
    }
    if not isinstance(payload, dict) or payload.get("ok") is not True or not isinstance(checks, dict) or any(
        checks.get(name) is not True for name in required
    ):
        raise AdminError("OSS gateway acceptance probe did not pass every required check")


def ensure_storage_identity(slug: str, profile: dict[str, Any], paths: Paths = PATHS) -> Path:
    """Ask the root-only gateway CLI to write the app secret; never read it here."""
    storage_foundation_ready(profile)
    result = subprocess.run(
        [str(STORAGE_ADMIN), "ensure-app", "--application-slug", validate_slug(slug)],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        # Gateway output is deliberately suppressed: a future buggy gateway must
        # not be able to leak a project token through this administrator's error.
        raise AdminError(f"storage gateway refused ensure-app (exit {result.returncode})")
    target = installed_storage_env_path(slug, paths)
    ensure_storage_network(slug, profile)
    return target


def rotate_storage_identity(
    slug: str, grace_seconds: int, profile: dict[str, Any], paths: Paths = PATHS
) -> Path:
    """Rotate via the root-only gateway without relaying any command output."""

    storage_foundation_ready(profile)
    ensure_storage_network(slug, profile)
    result = subprocess.run(
        [
            str(STORAGE_ADMIN),
            "rotate",
            "--application-slug",
            validate_slug(slug),
            "--grace-seconds",
            str(grace_seconds),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise AdminError(f"storage gateway refused credential rotation (exit {result.returncode})")
    return installed_storage_env_path(slug, paths)


def prepare_storage_rotation(
    slug: str,
    operation_id: str,
    grace_seconds: int,
    profile: dict[str, Any],
    paths: Paths = PATHS,
) -> Path:
    """Idempotently install a new token while the old token remains current."""

    storage_foundation_ready(profile)
    ensure_storage_network(slug, profile)
    result = subprocess.run(
        [
            str(STORAGE_ADMIN),
            "prepare-rotate",
            "--application-slug",
            validate_slug(slug),
            "--operation-id",
            validate_storage_rotation_operation_id(operation_id),
            "--grace-seconds",
            str(grace_seconds),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise AdminError(f"storage gateway refused rotation prepare (exit {result.returncode})")
    return installed_storage_env_path(slug, paths)


def commit_storage_rotation(
    slug: str,
    operation_id: str,
    profile: dict[str, Any],
    paths: Paths = PATHS,
) -> int:
    """Commit a prepared rotation and return its persisted grace deadline."""

    storage_foundation_ready(profile)
    result = subprocess.run(
        [
            str(STORAGE_ADMIN),
            "commit-rotate",
            "--application-slug",
            validate_slug(slug),
            "--operation-id",
            validate_storage_rotation_operation_id(operation_id),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise AdminError(f"storage gateway refused rotation commit (exit {result.returncode})")
    try:
        payload = json.loads(result.stdout)
        grace_until = payload.get("graceUntil")
        if (
            not isinstance(payload, dict)
            or payload.get("ok") is not True
            or payload.get("action") != "commit-rotate"
            or payload.get("applicationSlug") != slug
            or payload.get("operationId") != operation_id
            or payload.get("phase") != "committed"
            or isinstance(grace_until, bool)
            or not isinstance(grace_until, int)
            or grace_until <= 0
        ):
            raise ValueError("unexpected safe result")
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        # Never relay stdout: only the fixed safe fields are accepted, so a
        # future gateway regression cannot leak a token through this process.
        raise AdminError("storage gateway returned an invalid rotation commit result") from exc
    installed_storage_env_path(slug, paths)
    return grace_until


def patch_runtime(args: argparse.Namespace, paths: Paths = PATHS) -> dict[str, Any]:
    with locked(paths):
        profile = load_runtime(paths)
        management_host = validate_management_host(args.host)
        credential = credential_metadata(paths)
        if not credential["secure"]:
            raise AdminError("registration credential must be root-owned mode 0600")
        original = paths.runtime.read_bytes()
        profile.setdefault("capabilities", {})["passwordSshAccess"] = True
        profile.setdefault("network", {})["managementAccess"] = {
            "mode": "ssh-https-multiplex",
            "host": management_host,
            "connectionOrder": [443],
            "businessAiPort": 443,
            "requiresVpn": True,
            "requiresCloudConsole": False,
            "verified": True,
        }
        profile["verifiedAt"] = utc_now()
        backup_dir = paths.backups / "runtime-profile"
        backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        assert_directory(backup_dir)
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = backup_dir / f"runtime-before-admin-{stamp}.json"
        atomic_write(backup, original, 0o600)
        old_mode = stat.S_IMODE(paths.runtime.stat().st_mode)
        try:
            atomic_json(paths.runtime, profile, old_mode)
            # Re-parse before reporting success; the credential remains unopened.
            load_runtime(paths)
        except BaseException:
            atomic_write(paths.runtime, original, old_mode)
            raise
    return {
        "ok": True,
        "runtimeProfile": str(paths.runtime),
        "backup": str(backup),
        "managementAccess": profile["network"]["managementAccess"],
        "passwordSshAccess": True,
        "credentialPreserved": True,
    }


def configure_oss_gateway(args: argparse.Namespace, paths: Paths = PATHS) -> dict[str, Any]:
    """Atomically set the default for future apps without touching old releases."""
    with locked(paths):
        profile = load_runtime(paths)
        credential = credential_metadata(paths)
        if not credential["secure"]:
            raise AdminError("registration credential must be root-owned mode 0600")
        bucket = args.bucket.strip()
        region = normalize_aliyun_region(args.region)
        if not BUCKET_RE.fullmatch(bucket):
            raise AdminError("OSS bucket must be 3-63 lowercase letters, digits, or hyphens")
        if args.gateway_url != STORAGE_GATEWAY_URL:
            raise AdminError(f"gateway URL must equal {STORAGE_GATEWAY_URL}")
        if args.credential_ref != str(STORAGE_CREDENTIAL):
            raise AdminError(f"gateway credential reference must equal {STORAGE_CREDENTIAL}")
        current_storage = profile.get("objectStorage")
        if isinstance(current_storage, dict):
            current_scope = (
                current_storage.get("bucket"),
                normalize_aliyun_region(current_storage.get("region")),
            )
            requested_scope = (bucket, region)
            if current_scope != requested_scope:
                oss_releases = oss_releases_for_scope_change(paths)
                if oss_releases:
                    raise AdminError(
                        "refusing to change the OSS bucket or region while managed "
                        "OSS releases exist: " + ", ".join(oss_releases)
                    )
        original = paths.runtime.read_bytes()
        updated = copy.deepcopy(profile)
        capabilities = updated.setdefault("capabilities", {})
        capabilities["fileStorage"] = True
        capabilities["objectStorage"] = True
        file_storage = updated.setdefault("fileStorage", {})
        # Keep the local root and disk thresholds because existing releases stay
        # local-managed and the host still needs space for Git, images and DBs.
        file_storage.update(
            {
                "provider": "aliyun-oss",
                "mode": OSS_STORAGE_MODE,
                "defaultMode": OSS_STORAGE_MODE,
                "verified": True,
            }
        )
        updated["objectStorage"] = {
            "provider": "aliyun-oss",
            "mode": "gateway-api-v1",
            "bucket": bucket,
            "region": region,
            "rootPrefix": "apps",
            "gatewayBaseUrl": STORAGE_GATEWAY_URL,
            "credentialRef": str(STORAGE_CREDENTIAL),
            "verified": True,
        }
        refs = updated.setdefault("secretRefs", [])
        if not isinstance(refs, list) or not all(isinstance(item, str) for item in refs):
            raise AdminError("runtime secretRefs must be a list of file or environment references")
        if str(STORAGE_CREDENTIAL) not in refs:
            refs.append(str(STORAGE_CREDENTIAL))
        # Validate all gateway prerequisites before replacing the profile.  The
        # Runtime publisher credential is only stat'ed and is never opened.
        storage_foundation_ready(updated)
        verify_storage_foundation(bucket, region)
        comparable_existing = copy.deepcopy(profile)
        comparable_updated = copy.deepcopy(updated)
        comparable_existing.pop("verifiedAt", None)
        comparable_updated.pop("verifiedAt", None)
        if comparable_updated == comparable_existing:
            return {
                "ok": True,
                "changed": False,
                "storageMode": OSS_STORAGE_MODE,
                "credentialPreserved": True,
            }
        updated["verifiedAt"] = utc_now()
        backup_dir = paths.backups / "runtime-profile"
        backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        assert_directory(backup_dir)
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = backup_dir / f"runtime-before-oss-gateway-{stamp}.json"
        atomic_write(backup, original, 0o600)
        old_mode = stat.S_IMODE(paths.runtime.stat().st_mode)
        try:
            atomic_json(paths.runtime, updated, old_mode)
            load_runtime(paths)
        except BaseException:
            atomic_write(paths.runtime, original, old_mode)
            raise
    return {
        "ok": True,
        "changed": True,
        "runtimeProfile": str(paths.runtime),
        "backup": str(backup),
        "storageMode": OSS_STORAGE_MODE,
        "credentialPreserved": True,
    }


def disk_state(paths: Paths = PATHS) -> dict[str, Any]:
    profile = load_runtime(paths)
    storage = profile.get("fileStorage", {})
    warning = int(storage.get("warningUsedPercent", 80))
    stop = int(storage.get("stopUploadUsedPercent", 90))
    minimum = storage.get("minimumFreeGiB", 5)
    if (
        isinstance(minimum, bool)
        or not isinstance(minimum, int)
        or not (1 <= warning < stop <= 100 and minimum >= 1)
    ):
        raise AdminError("invalid file-storage thresholds in runtime profile")
    usage = shutil.disk_usage(paths.data)
    used_percent = round((usage.used / usage.total) * 100, 2)
    free_gib = round(usage.free / GIB, 2)
    blocked = used_percent >= stop or usage.free < minimum * GIB
    level = "critical" if blocked else "warning" if used_percent >= warning else "ok"
    return {
        "schemaVersion": 1,
        "checkedAt": utc_now(),
        "level": level,
        "uploadsAllowed": not blocked,
        "usedPercent": used_percent,
        "freeGiB": free_gib,
        "thresholds": {
            "warningUsedPercent": warning,
            "stopUploadUsedPercent": stop,
            "minimumFreeGiB": minimum,
        },
    }


def write_disk_state(state: dict[str, Any], paths: Paths = PATHS) -> None:
    atomic_json(paths.state, state, 0o644)


def release_path(slug: str, paths: Paths = PATHS) -> Path:
    return paths.deployments / validate_slug(slug) / "release.json"


def expected_names(slug: str, profile: dict[str, Any], paths: Paths = PATHS) -> dict[str, Any]:
    enterprise = profile["enterpriseKey"]
    host = f"{slug}.{profile['domains']['suffix']}"
    if not DOMAIN_RE.fullmatch(host):
        raise AdminError("application hostname exceeds DNS limits")
    return {
        "applicationSlug": slug,
        "enterpriseKey": enterprise,
        "hostname": host,
        "containerName": f"zhuojian-{enterprise}-{slug}",
        "projectDir": str(paths.repositories / f"{enterprise}-{slug}"),
        "dataDir": str(paths.data / slug),
        "envFile": str(paths.apps_env / f"{slug}.env"),
        "nginxConfig": str(paths.nginx / f"zhuojian-{enterprise}-{slug}.conf"),
    }


def load_release(slug: str, profile: dict[str, Any], paths: Paths = PATHS) -> dict[str, Any]:
    path = release_path(slug, paths)
    assert_plain_file(path)
    try:
        release = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdminError(f"invalid release record for {slug}: {exc}") from exc
    if release.get("managedBy") != MANAGED_BY:
        raise AdminError(f"refusing unmanaged release record: {path}")
    for key, value in expected_names(slug, profile, paths).items():
        if release.get(key) != value:
            raise AdminError(f"release record {key} does not match fixed runtime path")
    port = release.get("port")
    if not isinstance(port, int) or not PORT_MIN <= port <= PORT_MAX:
        raise AdminError("release record contains an invalid port")
    # Records created before OSS support intentionally remain local after the
    # Runtime default changes.  A normal deploy must never perform migration.
    storage_mode = validate_storage_mode(release.get("storageMode", LOCAL_STORAGE_MODE))
    release["storageMode"] = storage_mode
    if storage_mode == OSS_STORAGE_MODE:
        validate_oss_profile(profile)
        recorded_storage_env = release.get("storageEnvFile")
        if not isinstance(recorded_storage_env, str):
            raise AdminError("release storageEnvFile is missing")
        installed_storage_env_path(slug, paths, expected=recorded_storage_env)
        expected_storage_network = storage_network_name(slug)
        recorded_storage_network = release.get("storageNetwork")
        if recorded_storage_network not in {None, expected_storage_network}:
            raise AdminError("release storageNetwork does not match the fixed application network")
        # Records from before per-app bridge isolation are upgraded in memory and
        # are persisted by the next successful deploy/rollback operation.
        release["storageNetwork"] = expected_storage_network
    elif "storageEnvFile" in release:
        raise AdminError("local-managed release must not carry an OSS storage environment")
    elif "storageNetwork" in release:
        raise AdminError("local-managed release must not carry an OSS storage network")
    return release


def port_available(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def allocate_port(profile: dict[str, Any], paths: Paths = PATHS) -> int:
    low, high = profile.get("resources", {}).get("appPortRange", [PORT_MIN, PORT_MAX])
    reserved: set[int] = set()
    if paths.deployments.exists():
        for item in paths.deployments.glob("*/release.json"):
            with contextlib.suppress(OSError, json.JSONDecodeError):
                value = json.loads(item.read_text(encoding="utf-8")).get("port")
                if isinstance(value, int):
                    reserved.add(value)
    for port in range(low, high + 1):
        if port not in reserved and port_available(port):
            return port
    raise AdminError(f"no free loopback application port in {low}-{high}")


def git_release(project: Path) -> tuple[str, str]:
    assert_directory(project)
    if run(["git", "-C", str(project), "rev-parse", "--is-inside-work-tree"]).stdout.strip() != "true":
        raise AdminError(f"not a Git repository: {project}")
    top_level = Path(run(["git", "-C", str(project), "rev-parse", "--show-toplevel"]).stdout.strip()).resolve()
    if top_level != project.resolve():
        raise AdminError("project directory must be the root of its own local Git repository")
    dirty = run(["git", "-C", str(project), "status", "--porcelain=v1", "--untracked-files=all"]).stdout
    if dirty.strip():
        raise AdminError("deployment refused: local Git worktree is not clean")
    commit = run(["git", "-C", str(project), "rev-parse", "HEAD"]).stdout.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise AdminError("Git HEAD is not a full immutable commit SHA")
    return commit, commit[:12]


def managed_labels(slug: str, profile: dict[str, Any]) -> list[str]:
    return [
        "--label", f"com.zhuojian.managed-by={MANAGED_BY}",
        "--label", f"com.zhuojian.enterprise={profile['enterpriseKey']}",
        "--label", f"com.zhuojian.application={slug}",
    ]


def container_exists(name: str) -> bool:
    return run(["docker", "container", "inspect", name], check=False).returncode == 0


def assert_managed_container(name: str, slug: str, profile: dict[str, Any]) -> None:
    result = run(
        ["docker", "inspect", "--format", "{{ index .Config.Labels \"com.zhuojian.managed-by\" }}|{{ index .Config.Labels \"com.zhuojian.application\" }}|{{ index .Config.Labels \"com.zhuojian.enterprise\" }}", name]
    ).stdout.strip()
    expected = f"{MANAGED_BY}|{slug}|{profile['enterpriseKey']}"
    if result != expected:
        raise AdminError(f"refusing to alter unknown container: {name}")


def container_running(name: str) -> bool:
    if not container_exists(name):
        return False
    return run(["docker", "inspect", "--format", "{{.State.Running}}", name]).stdout.strip() == "true"


def container_storage_rotation_operation(name: str) -> str | None:
    value = run(
        [
            "docker",
            "inspect",
            "--format",
            '{{ index .Config.Labels "com.zhuojian.storage-rotation" }}',
            name,
        ]
    ).stdout.strip()
    if not value or value == "<no value>":
        return None
    return validate_storage_rotation_operation_id(value)


def wait_for_health(port: int, host: str, timeout: int = 60) -> None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + timeout
    last = "no response"
    while time.monotonic() < deadline:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/health", headers={"Host": host, "User-Agent": "zhuojian-runtime-admin/1"}
        )
        try:
            with opener.open(request, timeout=3) as response:
                if response.status == 200:
                    response.read(65536)
                    return
                last = f"HTTP {response.status}"
        except (OSError, urllib.error.URLError) as exc:
            last = str(exc)
        time.sleep(2)
    raise AdminError(f"health check failed after {timeout}s: {last}")


def read_secure_application_env(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise AdminError(f"cannot securely open application env: {path}") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise AdminError(f"refusing non-regular application env: {path}")
        if os.name != "nt" and info.st_uid != 0:
            raise AdminError(f"application env must be root-owned mode 0600: {path}")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise AdminError(f"application env must be root-owned mode 0600: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            payload = handle.read(1024 * 1024 + 1)
        if len(payload) > 1024 * 1024:
            raise AdminError(f"application env is unexpectedly large: {path}")
        return payload
    finally:
        os.close(descriptor)


def verify_upload_lock(paths: Paths = PATHS) -> dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(paths.upload_lock, flags)
    except OSError as exc:
        raise AdminError("shared upload lock is unavailable") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise AdminError("shared upload lock must be a regular file")
        if os.name != "nt" and info.st_uid != 0:
            raise AdminError("shared upload lock must be root-owned mode 0444")
        if stat.S_IMODE(info.st_mode) != 0o444:
            raise AdminError("shared upload lock must be root-owned mode 0444")
        return {"secure": True, "path": str(paths.upload_lock)}
    finally:
        os.close(descriptor)


def upsert_env_value(payload: bytes, key: str, value: str) -> bytes:
    """Update one Docker env-file key without interpreting or exposing secrets."""

    if not ENV_KEY_RE.fullmatch(key) or any(ch in value for ch in "\x00\r\n"):
        raise AdminError("refusing unsafe application environment value")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AdminError("application env must be valid UTF-8") from exc
    if "\x00" in text:
        raise AdminError("application env contains a NUL byte")
    lines = text.splitlines(keepends=True)
    found = 0
    updated: list[str] = []
    for line in lines:
        body = line.rstrip("\r\n")
        ending = line[len(body) :]
        if not body or body.startswith("#"):
            updated.append(line)
            continue
        if "=" not in body:
            raise AdminError("application env contains an invalid entry")
        name, _ = body.split("=", 1)
        if not ENV_KEY_RE.fullmatch(name):
            raise AdminError("application env contains an invalid key")
        if name == key:
            found += 1
            updated.append(f"{key}={value}{ending}")
        else:
            updated.append(line)
    if found > 1:
        raise AdminError(f"application env contains duplicate managed key: {key}")
    if found == 0:
        if updated and not updated[-1].endswith(("\n", "\r")):
            updated[-1] += "\n"
        updated.append(f"{key}={value}\n")
    return "".join(updated).encode("utf-8")


def ensure_env(slug: str, profile: dict[str, Any], paths: Paths = PATHS) -> Path:
    slug = validate_slug(slug)
    target = paths.apps_env / f"{slug}.env"
    saas_origin = platform_origin(profile)
    public_origin = f"https://{expected_names(slug, profile, paths)['hostname']}"
    if target.exists() or target.is_symlink():
        previous = read_secure_application_env(target)
        updated = upsert_env_value(previous, "ZHUOJIAN_SAAS_ORIGINS", saas_origin)
        updated = upsert_env_value(updated, "ZHUOJIAN_PUBLIC_ORIGIN", public_origin)
        updated = upsert_env_value(
            updated,
            "FILE_STORAGE_UPLOAD_LOCK_FILE",
            "/run/zhuojian/upload.lock",
        )
        if updated != previous:
            atomic_write(target, updated, 0o600)
        return target
    values = {
        "ZHUOJIAN_ENTERPRISE_KEY": profile["enterpriseKey"],
        "ZHUOJIAN_ORGANIZATION_ID": profile["organizationId"],
        "ZHUOJIAN_APPLICATION_SLUG": slug,
        "ZHUOJIAN_PUBLIC_ORIGIN": public_origin,
        "ZHUOJIAN_SAAS_ORIGINS": saas_origin,
        "ZHUOJIAN_INTEGRATION_SECRET": secrets.token_urlsafe(48),
        "SESSION_SECRET": secrets.token_urlsafe(48),
        "FILE_STORAGE_DRIVER": "local",
        "FILE_STORAGE_ROOT": "/data/files",
        "FILE_STORAGE_STATE_FILE": "/run/zhuojian/storage-state.json",
        "FILE_STORAGE_UPLOAD_LOCK_FILE": "/run/zhuojian/upload.lock",
    }
    atomic_write(target, "".join(f"{key}={value}\n" for key, value in values.items()).encode(), 0o600)
    return target


def tls_listens(profile: dict[str, Any]) -> tuple[str, str]:
    mode = profile.get("network", {}).get("managementAccess", {}).get("mode")
    if mode == "ssh-https-multiplex":
        return "listen 127.0.0.1:8443 ssl;", "listen [::1]:8443 ssl;"
    if mode == "standard-ssh":
        return "listen 443 ssl;", "listen [::]:443 ssl;"
    raise AdminError("runtime network.managementAccess.mode is unsupported")


def nginx_text(slug: str, port: int, profile: dict[str, Any], paths: Paths = PATHS) -> str:
    host = expected_names(slug, profile, paths)["hostname"]
    cert_dir = Path("/etc/letsencrypt/live") / host
    if not (cert_dir / "fullchain.pem").is_file() or not (cert_dir / "privkey.pem").is_file():
        raise AdminError(f"HTTPS certificate is missing for {host}; run certify first")
    listen4, listen6 = tls_listens(profile)
    platform = platform_origin(profile)
    return f"""# Managed by {MANAGED_BY}; application={slug}
server {{
    listen 80;
    listen [::]:80;
    server_name {host};
    location ^~ /.well-known/acme-challenge/ {{ root {paths.acme}; try_files $uri =404; }}
    location / {{ return 308 https://$host$request_uri; }}
}}

server {{
    {listen4}
    {listen6}
    server_name {host};
    ssl_certificate {cert_dir}/fullchain.pem;
    ssl_certificate_key {cert_dir}/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    add_header Content-Security-Policy \"frame-ancestors 'self' {platform}\" always;
    add_header X-Content-Type-Options nosniff always;
    client_max_body_size {NGINX_CLIENT_MAX_BODY_SIZE};
    client_body_timeout 30s;
    # Forward request bodies as they arrive.  Nginx must not spool a permitted
    # 512 MiB upload to its unmanaged client_body_temp directory before the
    # application/gateway authentication and disk gates can run.
    proxy_request_buffering off;

    # The one-time SSO ticket is carried in the query string by contract.
    # Never let Nginx persist it in the default request log.
    location = /api/integration/sso {{
        access_log off;
        proxy_pass http://127.0.0.1:{port};
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout 120s;
    }}

    location / {{
        proxy_pass http://127.0.0.1:{port};
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        # A bounded 512 MiB upload is acknowledged only after the private
        # gateway has committed it to OSS.  Keep the public response window in
        # sync with FILE_STORAGE_GATEWAY_TIMEOUT_SECONDS so a valid slow OSS
        # write is not reported as a failure after 120 seconds.
        proxy_read_timeout {NGINX_APPLICATION_RESPONSE_TIMEOUT};
    }}
}}
"""


def challenge_text(host: str, paths: Paths = PATHS) -> str:
    return f"""# Temporary ACME host managed by {MANAGED_BY}
server {{
    listen 80;
    listen [::]:80;
    server_name {host};
    location ^~ /.well-known/acme-challenge/ {{ root {paths.acme}; try_files $uri =404; }}
    location / {{ return 404; }}
}}
"""


def read_optional_plain(path: Path) -> bytes | None:
    if path.exists() or path.is_symlink():
        assert_plain_file(path)
        return path.read_bytes()
    return None


def replace_nginx(path: Path, content: bytes | None) -> bytes | None:
    previous = read_optional_plain(path)
    if content is None:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
    else:
        atomic_write(path, content, 0o644)
    test = run(["nginx", "-t"], check=False)
    if test.returncode:
        if previous is None:
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
        else:
            atomic_write(path, previous, 0o644)
        raise AdminError(f"Nginx validation failed: {(test.stderr or test.stdout).strip()[-2000:]}")
    reload_result = run(["systemctl", "reload", "nginx"], check=False)
    if reload_result.returncode:
        if previous is None:
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
        else:
            atomic_write(path, previous, 0o644)
        run(["nginx", "-t"])
        run(["systemctl", "reload", "nginx"])
        raise AdminError("Nginx reload failed; previous configuration restored")
    return previous


def restore_nginx(path: Path, previous: bytes | None) -> None:
    if previous is None:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
    else:
        atomic_write(path, previous, 0o644)
    run(["nginx", "-t"])
    run(["systemctl", "reload", "nginx"])


def issue_certificate(host: str, email: str | None, paths: Paths = PATHS) -> None:
    command = [
        "certbot", "certonly", "--webroot", "--webroot-path", str(paths.acme),
        "--domain", host, "--non-interactive", "--agree-tos", "--keep-until-expiring",
    ]
    if email:
        command += ["--email", email]
    else:
        command += ["--register-unsafely-without-email"]
    run(command)


def cmd_certify(args: argparse.Namespace, paths: Paths = PATHS) -> dict[str, Any]:
    slug = validate_slug(args.application_slug)
    with locked(paths):
        profile = load_runtime(paths)
        names = expected_names(slug, profile, paths)
        nginx_path = Path(names["nginxConfig"])
        release_exists = release_path(slug, paths).exists()
        if nginx_path.exists() and not release_exists:
            raise AdminError(f"refusing unknown Nginx configuration: {nginx_path}")
        previous = replace_nginx(nginx_path, challenge_text(names["hostname"], paths).encode())
        try:
            issue_certificate(names["hostname"], args.email, paths)
        finally:
            restore_nginx(nginx_path, previous)
    return {"ok": True, "hostname": names["hostname"], "certificateReady": True}


def provision_release(slug: str, profile: dict[str, Any], paths: Paths = PATHS) -> dict[str, Any]:
    target = release_path(slug, paths)
    if target.exists():
        release = load_release(slug, profile, paths)
        require_no_pending_storage_rotation(release)
        # Runtime-owned non-secret settings evolve with the template.  Refresh
        # them for existing releases before a new image is started, while
        # preserving integration/session secrets and the frozen storage mode.
        ensure_env(slug, profile, paths)
        if release["storageMode"] == OSS_STORAGE_MODE:
            ensure_storage_identity(slug, profile, paths)
        return release
    names = expected_names(slug, profile, paths)
    deploy_dir = target.parent
    conflicts = [Path(names[key]) for key in ("envFile", "dataDir", "nginxConfig")]
    if deploy_dir.exists() or any(path.exists() or path.is_symlink() for path in conflicts) or container_exists(names["containerName"]):
        raise AdminError("first deployment refused because same-slug unmanaged resources already exist")
    port = allocate_port(profile, paths)
    storage_mode = default_storage_mode(profile)
    storage_env: Path | None = None
    if storage_mode == OSS_STORAGE_MODE:
        # The gateway operation is idempotent.  It happens before claiming host
        # paths so an interrupted first attempt can safely be retried.
        storage_env = ensure_storage_identity(slug, profile, paths)
    deploy_dir.mkdir(mode=0o750, parents=False)
    data_dir = Path(names["dataDir"])
    (data_dir / "files" / ".tmp").mkdir(parents=True, mode=0o700)
    os.chmod(data_dir, 0o700)
    ensure_env(slug, profile, paths)
    release = {
        "schemaVersion": 1,
        "managedBy": MANAGED_BY,
        **names,
        "port": port,
        "containerPort": 8000,
        "current": None,
        "history": [],
        "status": "provisioned",
        "storageMode": storage_mode,
        "updatedAt": utc_now(),
    }
    if storage_env is not None:
        release["storageEnvFile"] = str(storage_env)
        release["storageNetwork"] = storage_network_name(slug)
    atomic_json(target, release, 0o640)
    return release


def start_container(
    name: str,
    image: str,
    release: dict[str, Any],
    slug: str,
    profile: dict[str, Any],
    paths: Paths = PATHS,
    *,
    storage_rotation_operation_id: str | None = None,
) -> None:
    if container_exists(name):
        raise AdminError(f"container name is already occupied: {name}")
    commit = image.rsplit(":", 1)[-1].lower()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise AdminError("managed container image must end in an immutable commit SHA")
    if not paths.state.exists():
        write_disk_state(disk_state(paths), paths)
    verify_upload_lock(paths)
    command = [
        "docker", "run", "--detach", "--name", name, "--restart", "unless-stopped",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
        *managed_labels(slug, profile),
        "--label", f"com.zhuojian.commit={commit}",
        "--env-file", release["envFile"],
        "--mount", f"type=bind,src={release['dataDir']},dst=/data",
        # Mount the directory, not the state file inode: disk-monitor atomically
        # replaces the JSON file and running containers must observe that update.
        "--mount", f"type=bind,src={paths.state.parent},dst=/run/zhuojian,readonly",
        "--publish", f"127.0.0.1:{release['port']}:8000",
    ]
    if storage_rotation_operation_id is not None:
        command += [
            "--label",
            "com.zhuojian.storage-rotation="
            + validate_storage_rotation_operation_id(storage_rotation_operation_id),
        ]
    storage_mode = validate_storage_mode(release.get("storageMode", LOCAL_STORAGE_MODE))
    if storage_mode == OSS_STORAGE_MODE:
        storage_env = ensure_storage_identity(slug, profile, paths)
        if str(storage_env) != release.get("storageEnvFile"):
            raise AdminError("release storage environment does not match the managed gateway identity")
        # ensure_storage_identity has already created/verified the app-only
        # bridge and attached the gateway before returning the env path.
        storage_network = storage_network_name(slug)
        if storage_network != release.get("storageNetwork", storage_network_name(slug)):
            raise AdminError("release storage network does not match the managed application network")
        command += ["--env-file", str(storage_env), "--network", storage_network]
    command.append(image)
    run(command)


def rollback_container(
    canonical: str,
    failed: str | None,
    previous_name: str | None,
    release: dict[str, Any],
    profile: dict[str, Any],
) -> None:
    if failed and container_exists(failed):
        assert_managed_container(failed, release["applicationSlug"], profile)
        run(["docker", "rm", "--force", failed])
    if previous_name and container_exists(previous_name):
        assert_managed_container(previous_name, release["applicationSlug"], profile)
        run(["docker", "rename", previous_name, canonical])
        run(["docker", "start", canonical])
        wait_for_health(release["port"], release["hostname"])


def cmd_deploy(args: argparse.Namespace, paths: Paths = PATHS) -> dict[str, Any]:
    slug = validate_slug(args.application_slug)
    with locked(paths):
        profile = load_runtime(paths)
        state = disk_state(paths)
        write_disk_state(state, paths)
        if not state["uploadsAllowed"]:
            raise AdminError("deployment refused by disk policy (90% used or less than 5 GiB free)")
        names = expected_names(slug, profile, paths)
        project = Path(names["projectDir"])
        commit, _ = git_release(project)
        image = f"zhuojian/{profile['enterpriseKey']}/{slug}:{commit}"
        cert_dir = Path("/etc/letsencrypt/live") / names["hostname"]
        if not (cert_dir / "fullchain.pem").is_file() and not args.issue_certificate:
            raise AdminError("HTTPS certificate is missing; rerun deploy with --issue-certificate")
        # Claim only an entirely new namespace or a record already owned by this
        # tool before installing an ACME challenge virtual host.
        release = provision_release(slug, profile, paths)
        nginx_path = Path(release["nginxConfig"])
        if not (cert_dir / "fullchain.pem").is_file():
            previous_nginx = replace_nginx(
                nginx_path, challenge_text(names["hostname"], paths).encode()
            )
            try:
                issue_certificate(names["hostname"], args.email, paths)
            finally:
                # Certificate issuance is separate from a release switch.  A
                # build/health failure must never leave the challenge-only host.
                restore_nginx(nginx_path, previous_nginx)
        canonical = release["containerName"]
        had_existing = container_exists(canonical)
        if had_existing:
            assert_managed_container(canonical, slug, profile)
            if not release.get("current"):
                raise AdminError("managed container exists without a current release record")
            if not container_running(canonical):
                raise AdminError("current managed container is stopped; refusing an update switch")
            wait_for_health(release["port"], release["hostname"], timeout=10)
        run([
            "docker", "build", "--label", f"com.zhuojian.managed-by={MANAGED_BY}",
            "--label", f"com.zhuojian.enterprise={profile['enterpriseKey']}",
            "--label", f"com.zhuojian.application={slug}",
            "--label", f"com.zhuojian.commit={commit}", "--tag", image, str(project),
        ])
        previous_name: str | None = None
        if had_existing:
            previous_name = f"{canonical}-rollback-{secrets.token_hex(6)}"
            if container_exists(previous_name):
                raise AdminError(f"rollback container name collision: {previous_name}")
        old_nginx = read_optional_plain(nginx_path)
        original_release = copy.deepcopy(release)
        old_stopped = False
        old_renamed = False
        try:
            if had_existing:
                run(["docker", "stop", "--time", "30", canonical])
                old_stopped = True
                assert previous_name is not None
                run(["docker", "rename", canonical, previous_name])
                old_renamed = True
            start_container(canonical, image, release, slug, profile, paths)
            wait_for_health(release["port"], release["hostname"], args.health_timeout)
            replace_nginx(nginx_path, nginx_text(slug, release["port"], profile, paths).encode())
            previous = release.get("current")
            history = list(release.get("history") or [])
            if previous and previous.get("commit") != commit:
                history.append(previous)
            release["history"] = history[-20:]
            release["current"] = {"commit": commit, "image": image, "deployedAt": utc_now()}
            release["status"] = "healthy"
            release["updatedAt"] = utc_now()
            release.pop("lastFailure", None)
            atomic_json(release_path(slug, paths), release, 0o640)
        except BaseException:
            # The canonical name is safe to remove only for a new app, or after
            # the previous container was positively renamed out of that name.
            if (not had_existing or old_renamed) and container_exists(canonical):
                assert_managed_container(canonical, slug, profile)
                run(["docker", "rm", "--force", canonical], check=False)
            if old_renamed and previous_name:
                rollback_container(canonical, None, previous_name, release, profile)
            elif old_stopped and container_exists(canonical):
                run(["docker", "start", canonical])
                wait_for_health(release["port"], release["hostname"])
            if nginx_path.exists() or old_nginx is not None:
                restore_nginx(nginx_path, old_nginx)
            failed_release = original_release
            failed_release["status"] = "healthy" if failed_release.get("current") else "failed"
            failed_release["updatedAt"] = utc_now()
            failed_release["lastFailure"] = {"attemptedCommit": commit, "at": utc_now()}
            atomic_json(release_path(slug, paths), failed_release, 0o640)
            raise
        if previous_name and container_exists(previous_name):
            assert_managed_container(previous_name, slug, profile)
            # Cleanup failure cannot turn an already committed healthy release
            # into a false deployment failure; the stopped, labeled container is
            # safe for a later exact cleanup.
            run(["docker", "rm", previous_name], check=False)
    return {
        "ok": True,
        "applicationSlug": slug,
        "hostname": release["hostname"],
        "commit": commit,
        "image": image,
        "loopbackPort": release["port"],
        "status": "healthy",
    }


STORAGE_ROTATION_PHASES = {"preparing", "prepared", "switching", "healthy", "committed"}


def validated_storage_rotation_marker(
    release: dict[str, Any], profile: dict[str, Any]
) -> dict[str, Any] | None:
    marker = release.get("storageRotation")
    if marker is None:
        return None
    if not isinstance(marker, dict):
        raise AdminError("release contains an invalid storage rotation marker")
    operation_id = validate_storage_rotation_operation_id(marker.get("operationId"))
    phase = marker.get("phase")
    grace_seconds = marker.get("graceSeconds")
    health_timeout = marker.get("healthTimeout")
    if phase not in STORAGE_ROTATION_PHASES:
        raise AdminError("release contains an invalid storage rotation phase")
    if isinstance(grace_seconds, bool) or not isinstance(grace_seconds, int) or not 60 <= grace_seconds <= 86400:
        raise AdminError("release contains an invalid storage rotation grace")
    if isinstance(health_timeout, bool) or not isinstance(health_timeout, int) or not 1 <= health_timeout <= 300:
        raise AdminError("release contains an invalid storage rotation health timeout")
    if grace_seconds < health_timeout + 60:
        raise AdminError("release storage rotation grace is shorter than its health window")
    current = release.get("current")
    if not isinstance(current, dict):
        raise AdminError("storage rotation has no frozen healthy release")
    commit = marker.get("commit")
    image = marker.get("image")
    expected_image = f"zhuojian/{profile['enterpriseKey']}/{release['applicationSlug']}:{commit}"
    if (
        not isinstance(commit, str)
        or not re.fullmatch(r"[0-9a-f]{40}", commit)
        or image != expected_image
        or current.get("commit") != commit
        or current.get("image") != image
    ):
        raise AdminError("storage rotation no longer matches the frozen release")
    expected_rollback = f"{release['containerName']}-rollback-storage-{operation_id[:12]}"
    if marker.get("rollbackContainer") != expected_rollback:
        raise AdminError("release contains an invalid storage rotation rollback name")
    return marker


def require_no_pending_storage_rotation(release: dict[str, Any]) -> None:
    if release.get("storageRotation") is not None:
        raise AdminError(
            "storage credential rotation is pending; rerun rotate-app-storage before this operation"
        )


def restore_prepared_rotation_container(
    canonical: str,
    previous_name: str,
    operation_id: str,
    release: dict[str, Any],
    profile: dict[str, Any],
) -> None:
    """Best-effort restore the old process while its credential is still current."""

    if container_exists(canonical):
        assert_managed_container(canonical, release["applicationSlug"], profile)
        if container_storage_rotation_operation(canonical) == operation_id:
            run(["docker", "rm", "--force", canonical], check=False)
    if container_exists(previous_name):
        assert_managed_container(previous_name, release["applicationSlug"], profile)
        if not container_exists(canonical):
            rollback_container(canonical, None, previous_name, release, profile)
    elif container_exists(canonical) and not container_running(canonical):
        run(["docker", "start", canonical])
        wait_for_health(release["port"], release["hostname"])


def cmd_rotate_app_storage(args: argparse.Namespace, paths: Paths = PATHS) -> dict[str, Any]:
    """Durably prepare, activate, then commit an OSS application token rotation."""

    slug = validate_slug(args.application_slug)
    requested_grace = args.grace_seconds
    requested_health_timeout = args.health_timeout
    if (
        isinstance(requested_grace, bool)
        or not isinstance(requested_grace, int)
        or not 60 <= requested_grace <= 86400
    ):
        raise AdminError("storage rotation grace must be between 60 and 86400 seconds")
    if (
        isinstance(requested_health_timeout, bool)
        or not isinstance(requested_health_timeout, int)
        or not 1 <= requested_health_timeout <= 300
    ):
        raise AdminError("storage rotation health timeout must be between 1 and 300 seconds")
    if requested_grace < requested_health_timeout + 60:
        raise AdminError("storage rotation grace must exceed the health timeout by at least 60 seconds")

    with locked(paths):
        profile = load_runtime(paths)
        release = load_release(slug, profile, paths)
        if release["storageMode"] != OSS_STORAGE_MODE:
            raise AdminError("storage credential rotation is only available for OSS releases")
        current = release.get("current")
        if not isinstance(current, dict):
            raise AdminError("application has no frozen healthy release to recreate")

        marker = validated_storage_rotation_marker(release, profile)
        canonical = release["containerName"]
        if marker is None:
            commit = current.get("commit")
            image = current.get("image")
            expected_image = f"zhuojian/{profile['enterpriseKey']}/{slug}:{commit}"
            if (
                not isinstance(commit, str)
                or not re.fullmatch(r"[0-9a-f]{40}", commit)
                or image != expected_image
            ):
                raise AdminError("current release does not contain an exact managed SHA image")
            if run(["docker", "image", "inspect", image], check=False).returncode:
                raise AdminError("current frozen image is not present; refusing storage rotation")
            if not container_exists(canonical):
                raise AdminError("current managed container is missing; refusing storage rotation")
            assert_managed_container(canonical, slug, profile)
            if not container_running(canonical):
                raise AdminError("current managed container is stopped; refusing storage rotation")
            wait_for_health(release["port"], release["hostname"], timeout=10)
            operation_id = secrets.token_hex(16)
            previous_name = f"{canonical}-rollback-storage-{operation_id[:12]}"
            if container_exists(previous_name):
                raise AdminError(f"storage rotation rollback container name collision: {previous_name}")
            marker = {
                "operationId": operation_id,
                "phase": "preparing",
                "graceSeconds": requested_grace,
                "healthTimeout": requested_health_timeout,
                "commit": commit,
                "image": image,
                "rollbackContainer": previous_name,
                "requestedAt": utc_now(),
            }
            release["storageRotation"] = marker
            release["updatedAt"] = utc_now()
            atomic_json(release_path(slug, paths), release, 0o640)
        else:
            operation_id = marker["operationId"]
            previous_name = marker["rollbackContainer"]
            commit = marker["commit"]
            image = marker["image"]

        grace_seconds = marker["graceSeconds"]
        health_timeout = marker["healthTimeout"]
        if run(["docker", "image", "inspect", image], check=False).returncode:
            raise AdminError("frozen rotation image is missing; pending rotation was preserved")

        storage_env = prepare_storage_rotation(
            slug,
            operation_id,
            grace_seconds,
            profile,
            paths,
        )
        if str(storage_env) != release.get("storageEnvFile"):
            raise AdminError("prepared storage environment does not match the frozen release")
        if marker["phase"] == "preparing":
            marker["phase"] = "prepared"
            release["updatedAt"] = utc_now()
            atomic_json(release_path(slug, paths), release, 0o640)

        if marker.get("phase") != "committed":
            marker["phase"] = "switching"
            release["updatedAt"] = utc_now()
            atomic_json(release_path(slug, paths), release, 0o640)
            try:
                previous_exists = container_exists(previous_name)
                if previous_exists:
                    assert_managed_container(previous_name, slug, profile)
                    if container_running(previous_name):
                        raise AdminError("storage rotation rollback container is unexpectedly running")

                canonical_exists = container_exists(canonical)
                current_operation = None
                if canonical_exists:
                    assert_managed_container(canonical, slug, profile)
                    current_operation = container_storage_rotation_operation(canonical)

                if current_operation == operation_id:
                    if not container_running(canonical):
                        run(["docker", "start", canonical])
                else:
                    if previous_exists:
                        if canonical_exists:
                            raise AdminError("canonical container is not the prepared rotation container")
                    else:
                        if not canonical_exists:
                            raise AdminError("both current and rollback containers are missing")
                        if container_running(canonical):
                            run(["docker", "stop", "--time", "30", canonical])
                        run(["docker", "rename", canonical, previous_name])
                        previous_exists = True
                    start_container(
                        canonical,
                        image,
                        release,
                        slug,
                        profile,
                        paths,
                        storage_rotation_operation_id=operation_id,
                    )

                wait_for_health(release["port"], release["hostname"], health_timeout)
                marker["phase"] = "healthy"
                release["status"] = "healthy"
                release["updatedAt"] = utc_now()
                atomic_json(release_path(slug, paths), release, 0o640)
            except BaseException as exc:
                with contextlib.suppress(BaseException):
                    restore_prepared_rotation_container(
                        canonical,
                        previous_name,
                        operation_id,
                        release,
                        profile,
                    )
                    marker["phase"] = "prepared"
                    release["status"] = "healthy"
                    release["updatedAt"] = utc_now()
                    atomic_json(release_path(slug, paths), release, 0o640)
                raise AdminError(
                    "replacement container did not become healthy; the old token remains current "
                    "and the durable rotation can be resumed by rerunning rotate-app-storage"
                ) from exc
        else:
            if not container_exists(canonical):
                raise AdminError("committed storage rotation has no canonical container")
            assert_managed_container(canonical, slug, profile)
            if container_storage_rotation_operation(canonical) != operation_id:
                raise AdminError("committed storage rotation container identity does not match")
            if not container_running(canonical):
                run(["docker", "start", canonical])
            wait_for_health(release["port"], release["hostname"], health_timeout)

        try:
            grace_until_epoch = commit_storage_rotation(
                slug, operation_id, profile, paths
            )
        except BaseException as exc:
            raise AdminError(
                "replacement container is healthy but storage rotation commit is pending; "
                "rerun rotate-app-storage with the same application slug"
            ) from exc
        marker["phase"] = "committed"
        marker["graceUntil"] = grace_until_epoch
        release["updatedAt"] = utc_now()
        atomic_json(release_path(slug, paths), release, 0o640)

        if container_exists(previous_name):
            assert_managed_container(previous_name, slug, profile)
            if container_running(previous_name):
                raise AdminError("refusing to remove a running storage rotation rollback container")
            run(["docker", "rm", previous_name])

        committed_at = dt.datetime.fromtimestamp(
            grace_until_epoch - grace_seconds, dt.timezone.utc
        )
        grace_until = dt.datetime.fromtimestamp(grace_until_epoch, dt.timezone.utc)
        release.pop("storageRotation", None)
        release["status"] = "healthy"
        release["updatedAt"] = utc_now()
        release["storageCredentialRotatedAt"] = committed_at.isoformat(timespec="seconds")
        atomic_json(release_path(slug, paths), release, 0o640)

    return {
        "ok": True,
        "applicationSlug": slug,
        "commit": commit,
        "image": image,
        "storageCredentialRotatedAt": committed_at.isoformat(timespec="seconds"),
        "graceUntil": grace_until.isoformat(timespec="seconds"),
        "status": "healthy",
    }


def cmd_rollback(args: argparse.Namespace, paths: Paths = PATHS) -> dict[str, Any]:
    slug = validate_slug(args.application_slug)
    with locked(paths):
        profile = load_runtime(paths)
        release = load_release(slug, profile, paths)
        require_no_pending_storage_rotation(release)
        current = release.get("current")
        history = list(release.get("history") or [])
        if not current or not history:
            raise AdminError("no previous healthy release is recorded")
        target = history[-1]
        if args.commit:
            matches = [entry for entry in history if entry.get("commit") == args.commit]
            if not matches:
                raise AdminError("requested commit is not in this application's release history")
            target = matches[-1]
        target_commit = target.get("commit", "")
        image = target.get("image", "")
        expected_image = f"zhuojian/{profile['enterpriseKey']}/{slug}:{target_commit}"
        if not re.fullmatch(r"[0-9a-f]{40}", target_commit) or image != expected_image:
            raise AdminError("release history does not contain an exact managed SHA image")
        if run(["docker", "image", "inspect", image], check=False).returncode:
            raise AdminError("exact rollback image is not present; refusing to use latest or rebuild")
        canonical = release["containerName"]
        assert_managed_container(canonical, slug, profile)
        if not container_running(canonical):
            raise AdminError("current managed container is stopped; refusing rollback")
        wait_for_health(release["port"], release["hostname"], timeout=10)
        previous_name = f"{canonical}-rollback-current-{secrets.token_hex(6)}"
        if container_exists(previous_name):
            raise AdminError(f"rollback container name collision: {previous_name}")
        old_stopped = False
        old_renamed = False
        original_release = copy.deepcopy(release)
        try:
            run(["docker", "stop", "--time", "30", canonical])
            old_stopped = True
            run(["docker", "rename", canonical, previous_name])
            old_renamed = True
            start_container(canonical, image, release, slug, profile, paths)
            wait_for_health(release["port"], release["hostname"], args.health_timeout)
            remaining = [entry for entry in history if entry is not target]
            remaining.append(current)
            release["history"] = remaining[-20:]
            release["current"] = {**target, "deployedAt": utc_now()}
            release["status"] = "healthy"
            release["updatedAt"] = utc_now()
            atomic_json(release_path(slug, paths), release, 0o640)
        except BaseException:
            if old_renamed and container_exists(canonical):
                assert_managed_container(canonical, slug, profile)
                run(["docker", "rm", "--force", canonical], check=False)
            if old_renamed:
                rollback_container(canonical, None, previous_name, release, profile)
            elif old_stopped and container_exists(canonical):
                run(["docker", "start", canonical])
                wait_for_health(release["port"], release["hostname"])
            # If release.json replacement itself failed, make a best-effort
            # restoration of its exact pre-rollback state.
            with contextlib.suppress(BaseException):
                atomic_json(release_path(slug, paths), original_release, 0o640)
            raise
        run(["docker", "rm", previous_name], check=False)
    return {"ok": True, "applicationSlug": slug, "commit": target["commit"], "status": "healthy"}


def reject_symlinks(root: Path) -> None:
    for base, directories, files in os.walk(root, followlinks=False):
        for name in directories + files:
            if (Path(base) / name).is_symlink():
                raise AdminError(f"backup refused: symlink in managed data: {Path(base) / name}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def backup_one(slug: str, profile: dict[str, Any], paths: Paths = PATHS) -> dict[str, Any]:
    release = load_release(slug, profile, paths)
    if not release.get("current"):
        raise AdminError(f"{slug} has no healthy release to back up")
    data_dir = Path(release["dataDir"])
    assert_directory(data_dir)
    reject_symlinks(data_dir)
    canonical = release["containerName"]
    was_running = container_running(canonical)
    if container_exists(canonical):
        assert_managed_container(canonical, slug, profile)
    app_backup = paths.backups / slug
    app_backup.mkdir(parents=True, exist_ok=True, mode=0o700)
    assert_directory(app_backup)
    assert_plain_file(Path(release["envFile"]))
    if Path(release["nginxConfig"]).exists() or Path(release["nginxConfig"]).is_symlink():
        assert_plain_file(Path(release["nginxConfig"]))
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive = app_backup / f"{stamp}-{release['current']['commit'][:12]}.tar.gz"
    fd, temp_name = tempfile.mkstemp(prefix=".backup-", suffix=".tar.gz", dir=app_backup)
    os.close(fd)
    temp = Path(temp_name)
    try:
        if was_running:
            run(["docker", "stop", "--time", "30", canonical])
        with tarfile.open(temp, "w:gz", format=tarfile.PAX_FORMAT) as bundle:
            bundle.add(data_dir, arcname="data", recursive=True)
            bundle.add(release_path(slug, paths), arcname="config/release.json", recursive=False)
            bundle.add(Path(release["envFile"]), arcname="config/app.env", recursive=False)
            if Path(release["nginxConfig"]).is_file():
                bundle.add(Path(release["nginxConfig"]), arcname="config/nginx.conf", recursive=False)
        os.chmod(temp, 0o600)
        os.replace(temp, archive)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temp.unlink()
        if was_running:
            run(["docker", "start", canonical])
            wait_for_health(release["port"], release["hostname"])
    metadata = {
        "schemaVersion": 1,
        "managedBy": MANAGED_BY,
        "applicationSlug": slug,
        "createdAt": utc_now(),
        "commit": release["current"]["commit"],
        "archive": archive.name,
        "sha256": sha256_file(archive),
        "consistentOfflineSnapshot": True,
    }
    atomic_json(archive.with_suffix(archive.suffix + ".json"), metadata, 0o600)
    return {"applicationSlug": slug, "archive": str(archive), "sha256": metadata["sha256"]}


def cmd_backup(args: argparse.Namespace, paths: Paths = PATHS) -> dict[str, Any]:
    slug = validate_slug(args.application_slug)
    with locked(paths):
        return {"ok": True, **backup_one(slug, load_runtime(paths), paths)}


def cmd_backup_all(args: argparse.Namespace, paths: Paths = PATHS) -> dict[str, Any]:
    del args
    results: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    with locked(paths):
        profile = load_runtime(paths)
        for candidate in sorted(paths.deployments.glob("*/release.json")):
            slug = candidate.parent.name
            if not SLUG_RE.fullmatch(slug):
                continue
            # Coexist with pre-existing applications.  The scheduled job owns
            # only records carrying our exact marker; unknown or legacy
            # release files are neither opened by load_release nor treated as
            # a failure of the managed backup set.
            try:
                info = candidate.lstat()
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_size > 131_072:
                    continue
                marker = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if not isinstance(marker, dict) or marker.get("managedBy") != MANAGED_BY:
                continue
            try:
                release = load_release(slug, profile, paths)
                if release.get("current"):
                    results.append(backup_one(slug, profile, paths))
            except AdminError as exc:
                failures.append({"applicationSlug": slug, "error": str(exc)})
    if failures:
        raise AdminError("one or more managed backups failed: " + json.dumps(failures, ensure_ascii=False))
    return {"ok": True, "backups": results}


def safe_extract_data(archive: Path, destination: Path) -> Path:
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        for member in members:
            pure = Path(member.name)
            if pure.is_absolute() or ".." in pure.parts or not pure.parts or pure.parts[0] not in {"data", "config"}:
                raise AdminError("backup contains an unsafe path")
            if member.issym() or member.islnk() or member.isdev():
                raise AdminError("backup contains a link or device and cannot be restored")
        data_members = [member for member in members if Path(member.name).parts[0] == "data"]
        if not data_members:
            raise AdminError("backup does not contain module data")
        bundle.extractall(destination, members=data_members)
    return destination / "data"


def cmd_restore(args: argparse.Namespace, paths: Paths = PATHS) -> dict[str, Any]:
    slug = validate_slug(args.application_slug)
    if args.confirm_application != slug:
        raise AdminError("restore requires --confirm-application matching applicationSlug")
    with locked(paths):
        profile = load_runtime(paths)
        release = load_release(slug, profile, paths)
        require_no_pending_storage_rotation(release)
        app_backup = (paths.backups / slug).resolve()
        archive = Path(args.archive).resolve()
        if archive.parent != app_backup:
            raise AdminError("restore archive must be directly inside this application's backup directory")
        assert_plain_file(archive)
        sidecar = archive.with_suffix(archive.suffix + ".json")
        assert_plain_file(sidecar)
        metadata = json.loads(sidecar.read_text(encoding="utf-8"))
        if metadata.get("managedBy") != MANAGED_BY or metadata.get("applicationSlug") != slug:
            raise AdminError("backup metadata does not belong to this application")
        recorded_sha = metadata.get("sha256")
        if not isinstance(recorded_sha, str) or not secrets.compare_digest(recorded_sha, sha256_file(archive)):
            raise AdminError("backup SHA-256 mismatch")
        # A complete safety backup is created before replacing any data.
        safety = backup_one(slug, profile, paths)
        temp = Path(tempfile.mkdtemp(prefix=f".{slug}-restore-", dir=paths.data))
        restored = safe_extract_data(archive, temp)
        target = Path(release["dataDir"])
        rollback_dir = paths.data / f".{slug}-pre-restore-{int(time.time())}"
        canonical = release["containerName"]
        was_running = container_running(canonical)
        try:
            if was_running:
                run(["docker", "stop", "--time", "30", canonical])
            os.replace(target, rollback_dir)
            os.replace(restored, target)
            if was_running:
                run(["docker", "start", canonical])
                wait_for_health(release["port"], release["hostname"])
        except BaseException:
            if container_running(canonical):
                run(["docker", "stop", "--time", "10", canonical], check=False)
            failed_dir = paths.data / f".{slug}-failed-restore-{int(time.time())}"
            if target.exists():
                os.replace(target, failed_dir)
            if rollback_dir.exists():
                os.replace(rollback_dir, target)
            if was_running:
                run(["docker", "start", canonical])
                wait_for_health(release["port"], release["hostname"])
            raise
        finally:
            shutil.rmtree(temp, ignore_errors=True)
        # Keep the pre-restore directory on the same filesystem.  This makes the
        # final preservation step atomic even when backupsRoot is a separate disk.
        preserved = rollback_dir
    return {"ok": True, "applicationSlug": slug, "restoredFrom": str(archive), "safetyBackup": safety["archive"], "previousDataPreservedAt": str(preserved)}


def cmd_doctor(args: argparse.Namespace, paths: Paths = PATHS) -> dict[str, Any]:
    del args
    profile = load_runtime(paths)
    checks: dict[str, Any] = {
        "runtimeProfile": True,
        "credential": credential_metadata(paths),
        "directories": {},
        "commands": {},
        "uploadLock": None,
    }
    for path in (
        paths.repositories,
        paths.deployments,
        paths.data,
        paths.backups,
        paths.apps_env,
        paths.storage_apps,
        paths.nginx,
    ):
        try:
            assert_directory(path)
            checks["directories"][str(path)] = True
        except AdminError as exc:
            checks["directories"][str(path)] = str(exc)
    for command in ("git", "docker", "nginx", "certbot", "systemctl"):
        checks["commands"][command] = shutil.which(command) is not None
    checks["disk"] = disk_state(paths)
    try:
        checks["uploadLock"] = verify_upload_lock(paths)
    except AdminError as exc:
        checks["uploadLock"] = str(exc)
    checks["managementAccess"] = profile.get("network", {}).get("managementAccess")
    storage_mode = default_storage_mode(profile)
    checks["storage"] = {"defaultMode": storage_mode, "gatewayReady": None}
    if storage_mode == OSS_STORAGE_MODE:
        try:
            storage_foundation_ready(profile)
            checks["storage"]["gatewayReady"] = True
        except AdminError as exc:
            checks["storage"]["gatewayReady"] = str(exc)
    checks["ok"] = (
        checks["credential"]["secure"]
        and all(value is True for value in checks["directories"].values())
        and all(checks["commands"].values())
        and isinstance(checks["uploadLock"], dict)
        and checks["uploadLock"].get("secure") is True
        and checks["storage"]["gatewayReady"] in {None, True}
    )
    return checks


def cmd_preflight(args: argparse.Namespace, paths: Paths = PATHS) -> dict[str, Any]:
    """Read-only deploy preflight; never allocates a port or creates app state."""
    slug = validate_slug(args.application_slug)
    profile = load_runtime(paths)
    verify_upload_lock(paths)
    names = expected_names(slug, profile, paths)
    commit, _ = git_release(Path(names["projectDir"]))
    state = disk_state(paths)
    conflicts: list[str] = []
    record = release_path(slug, paths)
    if record.exists():
        release = load_release(slug, profile, paths)
        storage_mode = release["storageMode"]
    else:
        storage_mode = default_storage_mode(profile)
        for key in ("dataDir", "envFile", "nginxConfig"):
            candidate = Path(names[key])
            if candidate.exists() or candidate.is_symlink():
                conflicts.append(str(candidate))
        if record.parent.exists():
            conflicts.append(str(record.parent))
        if container_exists(names["containerName"]):
            conflicts.append(names["containerName"])
    if conflicts:
        raise AdminError("same-slug unmanaged resources exist: " + ", ".join(conflicts))
    if storage_mode == OSS_STORAGE_MODE:
        # Preflight remains read-only: validate the shared foundation without
        # creating the per-app identity.  prepare/ensure-app performs creation.
        storage_foundation_ready(profile)
        if record.exists():
            secure_file_metadata(Path(release["storageEnvFile"]), "application storage environment")
    if not state["uploadsAllowed"]:
        raise AdminError("deployment refused by disk policy")
    return {
        "ok": True,
        "applicationSlug": slug,
        "hostname": names["hostname"],
        "sourceCommit": commit,
        "disk": state,
        "storageMode": storage_mode,
        "namespaceAvailable": True,
    }


def cmd_prepare(args: argparse.Namespace, paths: Paths = PATHS) -> dict[str, Any]:
    """Idempotently allocate fixed app paths/port without building or routing."""
    slug = validate_slug(args.application_slug)
    with locked(paths):
        profile = load_runtime(paths)
        names = expected_names(slug, profile, paths)
        commit, _ = git_release(Path(names["projectDir"]))
        state = disk_state(paths)
        write_disk_state(state, paths)
        if not state["uploadsAllowed"]:
            raise AdminError("prepare refused by disk policy")
        release = provision_release(slug, profile, paths)
    return {
        "ok": True,
        "applicationSlug": slug,
        "hostname": release["hostname"],
        "loopbackPort": release["port"],
        "sourceCommit": commit,
        "storageMode": release["storageMode"],
        "status": release["status"],
    }


def cmd_status(args: argparse.Namespace, paths: Paths = PATHS) -> dict[str, Any]:
    slug = validate_slug(args.application_slug)
    profile = load_runtime(paths)
    release = load_release(slug, profile, paths)
    canonical = release["containerName"]
    exists = container_exists(canonical)
    if exists:
        assert_managed_container(canonical, slug, profile)
    running = container_running(canonical) if exists else False
    healthy = False
    if running:
        try:
            wait_for_health(release["port"], release["hostname"], timeout=3)
            healthy = True
        except AdminError:
            healthy = False
    return {
        "ok": True,
        "applicationSlug": slug,
        "hostname": release["hostname"],
        "loopbackPort": release["port"],
        "recordedStatus": release["status"],
        "containerExists": exists,
        "containerRunning": running,
        "health": "healthy" if healthy else "unhealthy-or-stopped",
        "storageMode": release["storageMode"],
        "current": release.get("current"),
    }


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Controlled ZhuoJian direct-ECS runtime administrator")
    sub = root.add_subparsers(dest="command", required=True)
    patch = sub.add_parser("patch-runtime", help="atomically add the verified VPN SSH/HTTPS 443 path")
    patch.add_argument("--host", required=True, help="ECS public IP or administrator-approved hostname")
    patch.set_defaults(func=patch_runtime)
    storage = sub.add_parser(
        "configure-oss-gateway",
        help="atomically make the verified company OSS gateway the default for future apps",
    )
    storage.add_argument("--bucket", required=True)
    storage.add_argument("--region", required=True)
    storage.add_argument("--gateway-url", default=STORAGE_GATEWAY_URL)
    storage.add_argument("--credential-ref", default=str(STORAGE_CREDENTIAL))
    storage.set_defaults(func=configure_oss_gateway)
    disk = sub.add_parser("disk-check", help="write the upload/deployment disk gate")
    disk.add_argument("--write-state", action="store_true")
    disk.add_argument("--always-success", action="store_true")
    disk.set_defaults(func=None)
    doctor = sub.add_parser("doctor", help="safe foundation audit (credential metadata only)")
    doctor.set_defaults(func=cmd_doctor)
    preflight = sub.add_parser("preflight", help="read-only Git, namespace, Runtime, and disk checks")
    preflight.add_argument("application_slug")
    preflight.set_defaults(func=cmd_preflight)
    prepare = sub.add_parser("prepare", help="allocate fixed paths and loopback port without deployment")
    prepare.add_argument("application_slug")
    prepare.set_defaults(func=cmd_prepare)
    ensure_app = sub.add_parser(
        "ensure-app",
        help="idempotently reserve an app and create its isolated storage identity",
    )
    ensure_app.add_argument("application_slug")
    ensure_app.set_defaults(func=cmd_prepare)
    status = sub.add_parser("status", help="show non-secret state for one managed application")
    status.add_argument("application_slug")
    status.set_defaults(func=cmd_status)
    certify = sub.add_parser("certify", help="obtain/reuse a Let's Encrypt certificate via HTTP-01")
    certify.add_argument("application_slug")
    certify.add_argument("--email")
    certify.set_defaults(func=cmd_certify)
    deploy = sub.add_parser("deploy", help="build and atomically deploy one fixed local Git application")
    deploy.add_argument("application_slug")
    deploy.add_argument("--issue-certificate", action="store_true")
    deploy.add_argument("--email")
    deploy.add_argument("--health-timeout", type=int, default=60)
    deploy.set_defaults(func=cmd_deploy)
    rotate_storage = sub.add_parser(
        "rotate-app-storage",
        help="rotate one OSS credential and health-check the frozen release on a new container",
    )
    rotate_storage.add_argument("application_slug")
    rotate_storage.add_argument("--grace-seconds", type=int, default=300)
    rotate_storage.add_argument("--health-timeout", type=int, default=60)
    rotate_storage.set_defaults(func=cmd_rotate_app_storage)
    rollback = sub.add_parser("rollback", help="switch to an exact previously healthy image")
    rollback.add_argument("application_slug")
    rollback.add_argument("--commit")
    rollback.add_argument("--health-timeout", type=int, default=60)
    rollback.set_defaults(func=cmd_rollback)
    backup = sub.add_parser("backup", help="consistent offline database+file backup for one managed app")
    backup.add_argument("application_slug")
    backup.set_defaults(func=cmd_backup)
    backup_all = sub.add_parser("backup-all", help="back up only applications managed by this tool")
    backup_all.set_defaults(func=cmd_backup_all)
    restore = sub.add_parser("restore", help="restore one verified app backup with automatic safety backup")
    restore.add_argument("application_slug")
    restore.add_argument("--archive", required=True)
    restore.add_argument("--confirm-application", required=True)
    restore.set_defaults(func=cmd_restore)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "disk-check":
            state = disk_state(PATHS)
            if args.write_state:
                write_disk_state(state, PATHS)
            print(json.dumps(state, ensure_ascii=False, sort_keys=True))
            return 0 if args.always_success or state["uploadsAllowed"] else 75
        result = args.func(args, PATHS)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except AdminError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(json.dumps({"ok": False, "error": "interrupted"}), file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
