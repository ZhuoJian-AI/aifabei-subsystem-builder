#!/usr/bin/env python3
"""Launch interactive OpenSSH through an unauthenticated HTTP CONNECT proxy."""

from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

from http_connect_tunnel import normalize_target_host, parse_proxy_url


def _proxy_command(parts: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(parts)
    return shlex.join(parts)


def build_ssh_command(
    *,
    proxy_url: str,
    host: str,
    port: int,
    user: str,
    connect_timeout: int,
    identity_file: Path | None = None,
    batch_mode: bool = False,
    remote_command: list[str] | None = None,
) -> list[str]:
    tunnel_script = Path(__file__).with_name("http_connect_tunnel.py").resolve()
    proxy_command = _proxy_command(
        [
            sys.executable,
            str(tunnel_script),
            "--proxy-url",
            proxy_url,
            "--host",
            host,
            "--port",
            str(port),
            "--connect-timeout",
            str(connect_timeout),
        ]
    )
    command = [
        "ssh",
        "-o",
        f"ProxyCommand={proxy_command}",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"ConnectTimeout={connect_timeout}",
    ]
    if identity_file is None:
        command.extend([
            "-o",
            "PasswordAuthentication=yes",
            "-o",
            "KbdInteractiveAuthentication=yes",
            "-o",
            "PreferredAuthentications=password,keyboard-interactive",
            "-o",
            "PubkeyAuthentication=no",
            "-o",
            "NumberOfPasswordPrompts=1",
        ])
    else:
        command.extend([
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
        ])
    if batch_mode:
        command.extend(["-o", "BatchMode=yes"])
    command.extend(["-p", str(port), f"{user}@{host}"])
    if remote_command:
        command.extend(remote_command)
    return command


def main() -> int:
    parser = argparse.ArgumentParser(
        description="通过无认证 HTTP CONNECT 代理启动交互式 OpenSSH"
    )
    parser.add_argument("--proxy-url", required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--user", default="root")
    parser.add_argument("--connect-timeout", type=int, default=12)
    parser.add_argument(
        "--identity-file",
        type=Path,
        help="已验证的专用 SSH 私钥；提供后禁止回退到密码认证",
    )
    parser.add_argument(
        "--batch-mode",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("remote_command", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    try:
        parse_proxy_url(args.proxy_url)
    except ValueError as exc:
        parser.error(str(exc))
    try:
        target_host = normalize_target_host(args.host)
    except ValueError as exc:
        parser.error(str(exc))
    if not 1 <= args.port <= 65535:
        parser.error("--port 必须在 1–65535")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", args.user):
        parser.error("--user 包含无效字符")
    if args.connect_timeout <= 0:
        parser.error("--connect-timeout 必须大于 0")
    identity_file = None
    if args.identity_file is not None:
        identity_file = args.identity_file.expanduser().resolve()
        if not identity_file.is_file():
            parser.error("--identity-file 不存在或不是文件")
    remote_command = list(args.remote_command)
    if remote_command[:1] == ["--"]:
        remote_command = remote_command[1:]
    command = build_ssh_command(
        proxy_url=args.proxy_url,
        host=target_host,
        port=args.port,
        user=args.user,
        connect_timeout=args.connect_timeout,
        identity_file=identity_file,
        batch_mode=args.batch_mode,
        remote_command=remote_command,
    )
    try:
        return subprocess.call(command)
    except FileNotFoundError:
        print("未找到系统 OpenSSH 客户端 ssh", file=sys.stderr)
        return 127


if __name__ == "__main__":
    raise SystemExit(main())
