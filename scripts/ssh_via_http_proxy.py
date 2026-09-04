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
        "--batch-mode",
        action="store_true",
        help=argparse.SUPPRESS,
    )
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

    tunnel_script = Path(__file__).with_name("http_connect_tunnel.py").resolve()
    proxy_command = _proxy_command(
        [
            sys.executable,
            str(tunnel_script),
            "--proxy-url",
            args.proxy_url,
            "--host",
            target_host,
            "--port",
            str(args.port),
            "--connect-timeout",
            str(args.connect_timeout),
        ]
    )
    command = [
        "ssh",
        "-o",
        f"ProxyCommand={proxy_command}",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"ConnectTimeout={args.connect_timeout}",
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
        "-p",
        str(args.port),
    ]
    if args.batch_mode:
        command.extend(["-o", "BatchMode=yes"])
    command.append(f"{args.user}@{target_host}")
    try:
        return subprocess.call(command)
    except FileNotFoundError:
        print("未找到系统 OpenSSH 客户端 ssh", file=sys.stderr)
        return 127


if __name__ == "__main__":
    raise SystemExit(main())
