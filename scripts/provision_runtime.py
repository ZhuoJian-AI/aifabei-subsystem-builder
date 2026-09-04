#!/usr/bin/env python3
"""Provision one least-privilege ZhuoJian ECS publisher runtime."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import stat
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


STORAGE_GATEWAY_URL = "http://zhuojian-storage-gateway:8080"
STORAGE_CREDENTIAL_REF = Path("/etc/zhuojian/oss-gateway.env")
LOCAL_STORAGE_ROOT = Path("/srv/zhuojian/data")


def call_json(url: str, token: str, body: dict) -> dict:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Alphabet-Runtime-Provisioner/1.0",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            return json.load(response)
    except HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:1000]
        raise SystemExit(f"灼见 API 返回 HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise SystemExit(f"无法连接灼见 API: {exc.reason}") from exc


def secure_write(path: Path, content: str) -> None:
    _prepare_private_parent(path.parent)
    if path.exists() or path.is_symlink():
        raise FileExistsError(f"refusing to replace existing file: {path}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, stat.S_IRUSR | stat.S_IWUSR)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
            metadata = path.stat()
            if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
                raise PermissionError(f"private output metadata is unsafe: {path}")
        _fsync_directory(path.parent)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _prepare_private_parent(path: Path) -> None:
    """Create or verify a private output directory without following its leaf."""

    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_dir():
            raise PermissionError(f"private output parent is unsafe: {path}")
    else:
        path.mkdir(parents=True, mode=0o700)
    if os.name == "nt":
        return
    metadata = path.stat()
    if metadata.st_uid != os.geteuid():
        raise PermissionError(f"private output parent has an unexpected owner: {path}")
    mode = stat.S_IMODE(metadata.st_mode)
    if mode != 0o700:
        managed_default = os.geteuid() == 0 and path == Path("/etc/zhuojian")
        if not managed_default:
            raise PermissionError(f"private output parent must have mode 0700: {path}")
        os.chmod(path, 0o700)


def _preflight_output(path: Path) -> None:
    _prepare_private_parent(path.parent)
    if path.exists() or path.is_symlink():
        raise FileExistsError(f"refusing to replace existing file: {path}")
    probe = path.parent / f".zhuojian-provision-write-{secrets.token_hex(8)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(probe, flags, 0o600)
    try:
        os.write(descriptor, b"preflight\n")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
        probe.unlink(missing_ok=True)
        _fsync_directory(path.parent)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def verify_local_storage(root: Path, minimum_free_gib: int) -> None:
    """Perform a reversible real write/read/delete and disk-capacity probe."""

    if root.exists() or root.is_symlink():
        if root.is_symlink() or not root.is_dir():
            raise SystemExit(f"本地文件根目录不存在或不安全: {root}")
    else:
        try:
            root.mkdir(parents=True, mode=0o750)
            if os.name != "nt":
                os.chmod(root, 0o750)
        except OSError as exc:
            raise SystemExit(f"无法建立本地文件根目录: {root}: {exc}") from exc
    if os.name != "nt":
        metadata = root.stat()
        if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o022:
            raise SystemExit(f"本地文件根目录必须由当前管理员拥有且不可被组/其他用户写入: {root}")
    minimum_bytes = int(minimum_free_gib * 1024**3)
    if shutil.disk_usage(root).free < minimum_bytes:
        raise SystemExit("本地文件根目录的可用空间低于配置的最小余量")

    run_id = secrets.token_hex(8)
    first = root / f".zhuojian-storage-probe-a-{run_id}"
    second = root / f".zhuojian-storage-probe-b-{run_id}"
    payload = f"alphabet-local-storage:{run_id}\n"
    created: list[Path] = []
    probe_file = first / "probe.txt"
    try:
        first.mkdir(mode=0o700)
        created.append(first)
        second.mkdir(mode=0o700)
        created.append(second)
        secure_write(probe_file, payload)
        if probe_file.read_text(encoding="utf-8") != payload:
            raise SystemExit("本地文件写入后读取校验失败")
        if (second / "probe.txt").exists():
            raise SystemExit("本地文件测试目录发生串用")
        probe_file.unlink()
        _fsync_directory(first)
    finally:
        if first in created and (probe_file.exists() or probe_file.is_symlink()):
            if probe_file.is_symlink() or not probe_file.is_file():
                raise SystemExit(f"本地文件验收资源不是普通文件: {probe_file}")
            probe_file.unlink()
            _fsync_directory(first)
        for candidate in reversed(created):
            try:
                candidate.rmdir()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise SystemExit(f"本地文件验收资源无法安全清理: {candidate}") from exc


def _https_platform(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname:
        raise argparse.ArgumentTypeError("platform-url 必须是公网 HTTPS 地址")
    return value.rstrip("/")


def _storage_bucket(value: str) -> str:
    value = value.strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", value):
        raise argparse.ArgumentTypeError("storage-bucket 必须是 3–63 位小写字母、数字或连字符")
    return value


def _storage_region(value: str) -> str:
    value = value.strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]+", value):
        raise argparse.ArgumentTypeError("storage-region 必须是有效的阿里云地域 ID")
    return value


def _storage_gateway(value: str) -> str:
    parsed = urlsplit(value)
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    managed_docker_service = (
        parsed.scheme == "http"
        and parsed.hostname == "zhuojian-storage-gateway"
        and parsed.port == 8080
    )
    if (
        not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or (
            parsed.scheme != "https"
            and not (parsed.scheme == "http" and loopback)
            and not managed_docker_service
        )
    ):
        raise argparse.ArgumentTypeError(
            "storage-gateway-url 必须是 HTTPS、ECS 回环 HTTP，"
            "或受管 Docker 网关 http://zhuojian-storage-gateway:8080"
        )
    return value.rstrip("/")


def _management_host(value: str) -> str:
    value = value.strip()
    if not value or "://" in value or any(char.isspace() for char in value):
        raise argparse.ArgumentTypeError(
            "management-access-host 必须是公网 IP 或域名，不能包含协议和路径"
        )
    return value


def _is_absolute_path(path: Path) -> bool:
    return path.is_absolute() or PurePosixPath(path.as_posix()).is_absolute()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="管理员为一台企业 ECS 签发最小权限 Runtime 登记凭证"
    )
    parser.add_argument(
        "--platform-url",
        type=_https_platform,
        default="https://ai-platform.staging.zhuojianai.com",
    )
    parser.add_argument("--organization-id", required=True)
    parser.add_argument("--runtime-key", required=True)
    parser.add_argument("--enterprise-key", default="aifabei")
    parser.add_argument(
        "--environment",
        choices=("development", "staging", "production"),
        default="staging",
    )
    parser.add_argument("--domain-suffix", required=True)
    parser.add_argument("--public-address", type=_management_host)
    parser.add_argument(
        "--management-access-mode",
        choices=("standard-ssh", "ssh-https-multiplex"),
        default="standard-ssh",
        help="默认通过业务 VPN 使用 SSH 22；仅在需要时选择 443 与 HTTPS 复用",
    )
    parser.add_argument(
        "--management-access-requires-vpn",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="业务 AI 的已验证 SSH 路径是否需要 VPN/受管代理；默认需要",
    )
    parser.add_argument(
        "--management-access-host",
        type=_management_host,
        help="默认复用 --public-address",
    )
    parser.add_argument(
        "--management-access-verified",
        action="store_true",
        required=True,
        help="仅在外部 Codex 已读取 SSH Banner 并用 root 密码真实登录后传入",
    )
    parser.add_argument(
        "--storage-mode",
        choices=("local", "oss"),
        default="local",
        help="初始登记固定为 local；OSS 必须在网关真实验收后单独启用",
    )
    parser.add_argument(
        "--local-storage-root",
        type=Path,
        default=LOCAL_STORAGE_ROOT,
        help="固定为 /srv/zhuojian/data，必须与主机 Runtime 的实际挂载根一致",
    )
    parser.add_argument("--storage-warning-used-percent", type=int, default=80)
    parser.add_argument("--storage-stop-upload-used-percent", type=int, default=90)
    parser.add_argument("--storage-minimum-free-gib", type=int, default=5)
    parser.add_argument("--storage-bucket", type=_storage_bucket)
    parser.add_argument("--storage-region", type=_storage_region)
    parser.add_argument(
        "--storage-gateway-url",
        type=_storage_gateway,
        default=STORAGE_GATEWAY_URL,
    )
    parser.add_argument(
        "--storage-verified",
        action="store_true",
        help="兼容旧命令；本地目录现在始终由脚本真实写入、读取、删除后才标记 verified",
    )
    parser.add_argument(
        "--storage-credential-ref",
        type=Path,
        default=STORAGE_CREDENTIAL_REF,
    )
    parser.add_argument("--admin-token-env", default="ZHUOJIAN_ADMIN_TOKEN")
    parser.add_argument(
        "--profile-out",
        type=Path,
        default=Path("/etc/zhuojian/runtime.json"),
    )
    parser.add_argument(
        "--credential-out",
        type=Path,
        default=Path("/etc/zhuojian/runtime-registration.key"),
    )
    args = parser.parse_args()

    if not _is_absolute_path(args.local_storage_root):
        parser.error("--local-storage-root 必须是绝对路径")
    if args.local_storage_root != LOCAL_STORAGE_ROOT:
        parser.error("--local-storage-root 固定为 /srv/zhuojian/data，不能验证一处再写入另一处")
    if not 1 <= args.storage_warning_used_percent < args.storage_stop_upload_used_percent < 100:
        parser.error("磁盘阈值必须满足 1 <= warning < stop < 100")
    if args.storage_minimum_free_gib < 1:
        parser.error("--storage-minimum-free-gib 必须大于或等于 1")
    if not _is_absolute_path(args.storage_credential_ref):
        parser.error("--storage-credential-ref 必须是绝对路径")
    management_host = args.management_access_host or args.public_address
    if not management_host:
        parser.error(
            "必须提供 --public-address 或 --management-access-host，"
            "用于业务 AI 的 SSH 入口"
        )
    if args.profile_out.resolve(strict=False) == args.credential_out.resolve(strict=False):
        parser.error("--profile-out 与 --credential-out 必须是两个不同文件")
    if args.storage_mode == "oss":
        parser.error(
            "初始 Runtime 登记不接受 --storage-mode oss。先以 local 完成登记，"
            "再安装企业 OSS 网关，并由 zhuojian-runtime configure-oss-gateway "
            "执行真实 PUT/GET/DELETE 与双应用隔离验收后切换默认存储。"
        )

    admin_token = os.getenv(args.admin_token_env, "").strip()
    if not admin_token:
        raise SystemExit(f"管理员 Token 必须通过环境变量 {args.admin_token_env} 提供")
    for output in (args.profile_out, args.credential_out):
        if not _is_absolute_path(output):
            parser.error("--profile-out 和 --credential-out 必须是绝对路径")
        try:
            _preflight_output(output)
        except (OSError, PermissionError) as exc:
            raise SystemExit(f"输出位置预检失败: {output}: {exc}") from exc
    verify_local_storage(args.local_storage_root, args.storage_minimum_free_gib)

    endpoint = (
        f"{args.platform_url}/api/v1/ecs-publisher/organizations/"
        f"{args.organization_id}/runtimes"
    )
    payload = {
        "runtime_key": args.runtime_key,
        "enterprise_key": args.enterprise_key,
        "environment": args.environment,
        "domain_suffix": args.domain_suffix,
        "public_address": args.public_address,
    }
    result = call_json(endpoint, admin_token, payload)
    credential = result.get("credential")
    profile = result.get("runtime_profile")
    runtime = result.get("runtime") or {}
    if not isinstance(credential, str) or not credential or not isinstance(profile, dict):
        raise SystemExit("灼见 API 未返回有效的 Runtime 凭证和环境档案")

    profile.setdefault("deployment", {})["registrationCredentialRef"] = str(
        args.credential_out
    )
    capabilities = profile.setdefault("capabilities", {})
    capabilities["fileStorage"] = True
    capabilities["objectStorage"] = False
    capabilities["passwordSshAccess"] = True
    network = profile.setdefault("network", {})
    network["publicPorts"] = (
        [80, 443]
        if args.management_access_mode == "ssh-https-multiplex"
        else [22, 80, 443]
    )
    network["managementAccess"] = {
        "mode": args.management_access_mode,
        "host": management_host,
        "connectionOrder": (
            [22, 443]
            if args.management_access_mode == "ssh-https-multiplex"
            else [22]
        ),
        "businessAiPort": (
            443 if args.management_access_mode == "ssh-https-multiplex" else 22
        ),
        "requiresVpn": args.management_access_requires_vpn,
        "requiresCloudConsole": False,
        "verified": args.management_access_verified,
    }
    profile["fileStorage"] = {
        "provider": "local-disk",
        "mode": "local-managed",
        "defaultMode": "local-managed",
        "root": args.local_storage_root.as_posix(),
        "pathTemplate": "{applicationSlug}/files",
        "warningUsedPercent": args.storage_warning_used_percent,
        "stopUploadUsedPercent": args.storage_stop_upload_used_percent,
        "minimumFreeGiB": args.storage_minimum_free_gib,
        "verified": True,
    }
    profile.pop("objectStorage", None)
    secret_refs = profile.setdefault("secretRefs", [])
    if not isinstance(secret_refs, list) or not all(
        isinstance(item, str) for item in secret_refs
    ):
        raise SystemExit("灼见 API 返回的 runtime_profile.secretRefs 格式无效")
    secure_write(args.credential_out, credential + "\n")
    try:
        secure_write(
            args.profile_out,
            json.dumps(profile, ensure_ascii=False, indent=2) + "\n",
        )
    except OSError:
        raise SystemExit(
            f"环境档案写入失败；Runtime 凭证已安全保存在 {args.credential_out}，"
            "不要再次创建 Runtime。请修复目录权限，用管理员会话按 runtime_key "
            "查询已建 Runtime，并通过查询返回的档案和一次凭证轮换完成恢复。"
        )

    print(
        json.dumps(
            {
                "status": "provisioned",
                "runtimeId": str(runtime.get("id") or ""),
                "runtimeKey": runtime.get("runtime_key") or args.runtime_key,
                "organizationId": args.organization_id,
                "domainSuffix": args.domain_suffix,
                "managementAccess": args.management_access_mode,
                "managementAccessRequiresVpn": args.management_access_requires_vpn,
                "businessAiSshPort": network["managementAccess"]["businessAiPort"],
                "fileStorage": "local",
                "objectStorage": "disabled",
                "profile": str(args.profile_out),
                "credential": "installed",
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
