#!/usr/bin/env python3
"""Probe direct SSH banners without sending or storing a password."""

from __future__ import annotations

import argparse
import json
import socket
import time
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ProbeResult:
    port: int
    reachable: bool
    ssh_banner: bool
    banner: str
    error: str
    elapsed_ms: int


def parse_ports(value: str) -> list[int]:
    ports: list[int] = []
    for raw in value.split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            port = int(raw)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"无效端口: {raw}") from exc
        if not 1 <= port <= 65535:
            raise argparse.ArgumentTypeError(f"端口超出范围: {port}")
        if port not in ports:
            ports.append(port)
    if not ports:
        raise argparse.ArgumentTypeError("至少提供一个端口")
    return ports


def _safe_error(exc: BaseException) -> str:
    message = str(exc).strip().replace("\r", " ").replace("\n", " ")
    return f"{type(exc).__name__}: {message}"[:240]


def probe_ssh_banner(
    host: str,
    port: int,
    connect_timeout: float,
    banner_timeout: float,
) -> ProbeResult:
    started = time.monotonic()
    sock: socket.socket | None = None
    reachable = False
    banner = b""
    error = ""
    try:
        sock = socket.create_connection((host, port), timeout=connect_timeout)
        reachable = True
        sock.settimeout(banner_timeout)
        while len(banner) < 255 and b"\n" not in banner:
            chunk = sock.recv(255 - len(banner))
            if not chunk:
                break
            banner += chunk
    except (OSError, TimeoutError) as exc:
        error = _safe_error(exc)
    finally:
        if sock is not None:
            sock.close()
    elapsed_ms = round((time.monotonic() - started) * 1000)
    decoded = banner.decode("ascii", errors="replace").strip()
    return ProbeResult(
        port=port,
        reachable=reachable,
        ssh_banner=decoded.startswith("SSH-"),
        banner=decoded[:160],
        error=error,
        elapsed_ms=elapsed_ms,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "依次探测 SSH 22/443；只读取服务端 Banner，不发送账号或密码"
        )
    )
    parser.add_argument("--host", required=True, help="ECS 公网 IP 或域名")
    parser.add_argument(
        "--ports",
        type=parse_ports,
        default=parse_ports("22,443"),
        help="按顺序探测，默认 22,443",
    )
    parser.add_argument(
        "--require-port",
        type=int,
        help="管理员交付时通常要求 443 必须返回 SSH Banner",
    )
    parser.add_argument("--connect-timeout", type=float, default=5.0)
    parser.add_argument(
        "--banner-timeout",
        type=float,
        default=8.0,
        help="需覆盖 sslh 将无首包连接转给 SSH 的探测超时",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.connect_timeout <= 0 or args.banner_timeout <= 0:
        parser.error("超时时间必须大于 0")
    if args.require_port is not None and not 1 <= args.require_port <= 65535:
        parser.error("--require-port 必须在 1–65535")

    results = [
        probe_ssh_banner(
            args.host,
            port,
            args.connect_timeout,
            args.banner_timeout,
        )
        for port in args.ports
    ]
    successful = [result.port for result in results if result.ssh_banner]
    selected_port = successful[0] if successful else None
    passed = bool(successful)
    if args.require_port is not None:
        passed = args.require_port in successful
        if passed:
            selected_port = args.require_port

    payload = {
        "status": "pass" if passed else "fail",
        "host": args.host,
        "selectedPort": selected_port,
        "requiredPort": args.require_port,
        "results": [asdict(result) for result in results],
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for result in results:
            state = "SSH" if result.ssh_banner else (
                "TCP only" if result.reachable else "unreachable"
            )
            detail = result.banner or result.error or "未收到 SSH Banner"
            print(f"{args.host}:{result.port} {state} - {detail}")
        if passed:
            print(
                f"SSH BANNER PASS: use port {selected_port}; "
                "a real interactive login is still required"
            )
        elif args.require_port is not None:
            print(f"SSH ACCESS FAILED: port {args.require_port} is required")
        else:
            print("SSH ACCESS FAILED: no SSH banner on the requested ports")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
