#!/usr/bin/env python3
"""Create and reuse per-server SSH access without persisting passwords."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from http_connect_tunnel import normalize_target_host, parse_proxy_url
from ssh_via_http_proxy import build_ssh_command as build_proxy_ssh_command


SCHEMA_VERSION = 1
USER_RE = re.compile(r"[A-Za-z0-9_.-]+")


class AccessMemoryError(RuntimeError):
    """The durable access profile cannot be safely used or created."""


def default_store_dir() -> Path:
    configured = os.environ.get("CODEX_HOME")
    codex_home = Path(configured).expanduser() if configured else Path.home() / ".codex"
    return codex_home / "aifabei" / "server-access"


def normalize_user(user: str) -> str:
    if USER_RE.fullmatch(user) is None:
        raise AccessMemoryError("SSH 用户名包含无效字符")
    return user


def access_id(host: str, user: str) -> str:
    normalized_host = normalize_target_host(host)
    normalized_user = normalize_user(user)
    identity = f"{normalized_user}@{normalized_host}".encode("utf-8")
    return hashlib.sha256(identity).hexdigest()[:20]


def access_paths(store_dir: Path, host: str, user: str) -> tuple[Path, Path, Path]:
    identifier = access_id(host, user)
    root = store_dir.expanduser().resolve()
    return (
        root / "keys" / identifier,
        root / "keys" / f"{identifier}.pub",
        root / "profiles" / f"{identifier}.json",
    )


def _chmod_private(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError as exc:
        raise AccessMemoryError(f"无法设置本机访问文件权限：{path}") from exc


def _read_public_key(public_key_path: Path) -> str:
    try:
        value = public_key_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise AccessMemoryError("无法读取服务器专用 SSH 公钥") from exc
    parts = value.split()
    if len(parts) < 2 or parts[0] != "ssh-ed25519" or re.fullmatch(r"[A-Za-z0-9+/=]+", parts[1]) is None:
        raise AccessMemoryError("服务器专用 SSH 公钥格式无效")
    return value


def ensure_keypair(store_dir: Path, host: str, user: str) -> tuple[Path, str]:
    private_key, public_key, _profile = access_paths(store_dir, host, user)
    private_key.parent.mkdir(parents=True, exist_ok=True)
    _chmod_private(private_key.parent, 0o700)
    if private_key.exists() != public_key.exists():
        raise AccessMemoryError("服务器专用 SSH 密钥不完整，需要企业管理员恢复")
    if not private_key.exists():
        identifier = access_id(host, user)
        try:
            result = subprocess.run(
                [
                    "ssh-keygen",
                    "-q",
                    "-t",
                    "ed25519",
                    "-N",
                    "",
                    "-C",
                    f"aifabei-{identifier}",
                    "-f",
                    str(private_key),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
        except FileNotFoundError as exc:
            raise AccessMemoryError("未找到系统 ssh-keygen，无法建立长期访问") from exc
        if result.returncode != 0:
            raise AccessMemoryError("生成服务器专用 SSH 密钥失败")
    if not private_key.is_file() or not public_key.is_file():
        raise AccessMemoryError("服务器专用 SSH 密钥生成不完整")
    _chmod_private(private_key, 0o600)
    _chmod_private(public_key, 0o644)
    return private_key, _read_public_key(public_key)


def _direct_ssh_command(
    *,
    host: str,
    port: int,
    user: str,
    identity_file: Path,
    strict_host_key: str,
    remote_command: list[str] | None = None,
) -> list[str]:
    command = [
        "ssh",
        "-o",
        f"StrictHostKeyChecking={strict_host_key}",
        "-o",
        "ConnectTimeout=12",
        "-o",
        "BatchMode=yes",
        "-o",
        "PasswordAuthentication=no",
        "-o",
        "KbdInteractiveAuthentication=no",
        "-o",
        "PreferredAuthentications=publickey",
        "-o",
        "PubkeyAuthentication=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        f"IdentityFile={identity_file}",
        "-p",
        str(port),
        f"{user}@{host}",
    ]
    if remote_command:
        command.extend(remote_command)
    return command


def build_profile_ssh_command(
    profile: dict,
    *,
    strict_host_key: str = "yes",
    remote_command: list[str] | None = None,
) -> list[str]:
    identity_file = Path(profile["identityFile"])
    proxy_url = profile.get("proxyUrl")
    if proxy_url:
        command = build_proxy_ssh_command(
            proxy_url=proxy_url,
            host=profile["host"],
            port=profile["port"],
            user=profile["user"],
            connect_timeout=12,
            identity_file=identity_file,
            batch_mode=True,
            remote_command=remote_command,
        )
        if strict_host_key == "yes":
            index = command.index("StrictHostKeyChecking=accept-new")
            command[index] = "StrictHostKeyChecking=yes"
        return command
    return _direct_ssh_command(
        host=profile["host"],
        port=profile["port"],
        user=profile["user"],
        identity_file=identity_file,
        strict_host_key=strict_host_key,
        remote_command=remote_command,
    )


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _chmod_private(path.parent, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        _chmod_private(temporary_path, 0o600)
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def load_profile(store_dir: Path, host: str, user: str) -> dict:
    normalized_host = normalize_target_host(host)
    normalized_user = normalize_user(user)
    private_key, _public_key, profile_path = access_paths(
        store_dir, normalized_host, normalized_user
    )
    if not profile_path.is_file():
        raise AccessMemoryError("SERVER_ACCESS_MISSING")
    try:
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AccessMemoryError("服务器长期访问档案损坏，需要企业管理员恢复") from exc
    expected = {
        "schemaVersion": SCHEMA_VERSION,
        "host": normalized_host,
        "user": normalized_user,
        "accessId": access_id(normalized_host, normalized_user),
        "identityFile": str(private_key),
    }
    if any(profile.get(key) != value for key, value in expected.items()):
        raise AccessMemoryError("服务器长期访问档案身份不匹配，需要企业管理员恢复")
    port = profile.get("port")
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise AccessMemoryError("服务器长期访问档案端口无效")
    if not private_key.is_file():
        raise AccessMemoryError("服务器专用 SSH 私钥缺失，需要企业管理员恢复")
    proxy_url = profile.get("proxyUrl")
    if proxy_url is not None:
        if not isinstance(proxy_url, str):
            raise AccessMemoryError("服务器长期访问档案代理配置无效")
        try:
            parse_proxy_url(proxy_url)
        except ValueError as exc:
            raise AccessMemoryError("服务器长期访问档案代理配置无效") from exc
    return profile


def verify_and_remember(
    store_dir: Path,
    host: str,
    user: str,
    port: int,
    *,
    proxy_url: str | None,
    requires_vpn: bool,
) -> dict:
    normalized_host = normalize_target_host(host)
    normalized_user = normalize_user(user)
    if not 1 <= port <= 65535:
        raise AccessMemoryError("SSH 端口必须在 1–65535")
    if proxy_url is not None:
        try:
            parse_proxy_url(proxy_url)
        except ValueError as exc:
            raise AccessMemoryError(str(exc)) from exc
    private_key, _public_key = ensure_keypair(store_dir, normalized_host, normalized_user)
    profile = {
        "schemaVersion": SCHEMA_VERSION,
        "accessId": access_id(normalized_host, normalized_user),
        "host": normalized_host,
        "user": normalized_user,
        "port": port,
        "identityFile": str(private_key),
        "route": "http-proxy" if proxy_url else "direct-or-transparent-vpn",
        "requiresVpn": bool(requires_vpn),
        "proxyUrl": proxy_url,
        "verifiedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    command = build_profile_ssh_command(
        profile,
        strict_host_key="accept-new",
        remote_command=["true"],
    )
    try:
        result = subprocess.run(command, check=False)
    except FileNotFoundError as exc:
        raise AccessMemoryError("未找到系统 OpenSSH 客户端 ssh") from exc
    if result.returncode != 0:
        raise AccessMemoryError("专用 SSH 密钥验证失败，未写入长期访问档案")
    _private_key, _public_key_path, profile_path = access_paths(
        store_dir, normalized_host, normalized_user
    )
    _atomic_write_json(profile_path, profile)
    return profile


def _safe_profile(profile: dict) -> dict:
    return {
        "status": "ready",
        "accessId": profile["accessId"],
        "host": profile["host"],
        "user": profile["user"],
        "port": profile["port"],
        "route": profile["route"],
        "requiresVpn": profile["requiresVpn"],
        "identityFile": profile["identityFile"],
        "verifiedAt": profile["verifiedAt"],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="建立并复用 Alphabet ECS 长期 SSH 访问")
    parser.add_argument("--store-dir", type=Path, default=default_store_dir(), help=argparse.SUPPRESS)
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_identity_arguments(command: argparse.ArgumentParser) -> None:
        command.add_argument("--host", required=True)
        command.add_argument("--user", default="root")

    prepare = subparsers.add_parser("prepare", help="生成服务器专用 SSH 密钥")
    add_identity_arguments(prepare)

    resolve = subparsers.add_parser("resolve", help="查找已验证的长期访问档案")
    add_identity_arguments(resolve)

    verify = subparsers.add_parser("verify", help="验证专用密钥并写入长期访问档案")
    add_identity_arguments(verify)
    verify.add_argument("--port", type=int, required=True)
    verify.add_argument("--proxy-url")
    verify.add_argument("--requires-vpn", action="store_true")

    login = subparsers.add_parser("login", help="使用长期访问档案登录")
    add_identity_arguments(login)
    login.add_argument("remote_command", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "prepare":
            private_key, public_key = ensure_keypair(args.store_dir, args.host, args.user)
            print("SERVER_ACCESS_PREPARED " + json.dumps({
                "accessId": access_id(args.host, args.user),
                "identityFile": str(private_key),
                "publicKey": public_key,
            }, ensure_ascii=False))
            return 0
        if args.command == "verify":
            profile = verify_and_remember(
                args.store_dir,
                args.host,
                args.user,
                args.port,
                proxy_url=args.proxy_url,
                requires_vpn=args.requires_vpn,
            )
            print("SERVER_ACCESS_READY " + json.dumps(_safe_profile(profile), ensure_ascii=False))
            return 0
        profile = load_profile(args.store_dir, args.host, args.user)
        if args.command == "resolve":
            print("SERVER_ACCESS_READY " + json.dumps(_safe_profile(profile), ensure_ascii=False))
            return 0
        remote_command = list(args.remote_command)
        if remote_command[:1] == ["--"]:
            remote_command = remote_command[1:]
        command = build_profile_ssh_command(profile, remote_command=remote_command)
        return subprocess.call(command)
    except (AccessMemoryError, ValueError) as exc:
        message = str(exc)
        if message == "SERVER_ACCESS_MISSING":
            print("SERVER_ACCESS_MISSING 当前环境没有这台服务器的长期访问档案")
            return 2
        print(f"SERVER_ACCESS_ERROR {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
