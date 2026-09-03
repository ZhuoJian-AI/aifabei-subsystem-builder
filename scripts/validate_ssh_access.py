#!/usr/bin/env python3
"""Probe SSH banners directly or through HTTP CONNECT without using a password."""

from __future__ import annotations

import argparse
import json
import socket
import time
from dataclasses import asdict, dataclass

from http_connect_tunnel import open_http_connect_tunnel, parse_proxy_url


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
    proxy_url: str | None = None,
) -> ProbeResult:
    started = time.monotonic()
    sock: socket.socket | None = None
    reachable = False
    banner = b""
    error = ""
    try:
        pending = b""
        if proxy_url:
            sock, pending = open_http_connect_tunnel(
                proxy_url,
                host,
                port,
                connect_timeout,
            )
        else:
            sock = socket.create_connection((host, port), timeout=connect_timeout)
        reachable = True
        sock.settimeout(banner_timeout)
        banner += pending
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
        help="仅在管理员明确要求固定端口时使用，例如验证已配置的 443 复用",
    )
    parser.add_argument("--connect-timeout", type=float, default=5.0)
    parser.add_argument(
        "--banner-timeout",
        type=float,
        default=8.0,
        help="需覆盖 sslh 将无首包连接转给 SSH 的探测超时",
    )
    parser.add_argument(
        "--proxy-url",
        help="可选的无认证 HTTP CONNECT 代理，例如 http://127.0.0.1:7897",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.connect_timeout <= 0 or args.banner_timeout <= 0:
        parser.error("超时时间必须大于 0")
    if args.require_port is not None and not 1 <= args.require_port <= 65535:
        parser.error("--require-port 必须在 1–65535")
    if args.proxy_url:
        try:
            parse_proxy_url(args.proxy_url)
        except ValueError as exc:
            parser.error(str(exc))

    results: list[ProbeResult] = []
    for port in args.ports:
        result = probe_ssh_banner(
            args.host,
            port,
            args.connect_timeout,
            args.banner_timeout,
            args.proxy_url,
        )
        results.append(result)
        if result.ssh_banner and (
            args.require_port is None or port == args.require_port
        ):
            break
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
        "route": "http-proxy" if args.proxy_url else "direct-or-transparent-vpn",
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
