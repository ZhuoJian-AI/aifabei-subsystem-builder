#!/usr/bin/env python3
"""Open an unauthenticated HTTP CONNECT tunnel for OpenSSH ProxyCommand."""

from __future__ import annotations

import argparse
import ipaddress
import re
import socket
import sys
import threading
from urllib.parse import urlsplit


def parse_proxy_url(value: str) -> tuple[str, int]:
    parsed = urlsplit(value.strip())
    if parsed.scheme != "http" or not parsed.hostname:
        raise ValueError("代理地址必须是 http://主机:端口")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("不接受命令行代理凭证；请使用无认证的本机代理")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("代理地址不能包含路径、查询参数或片段")
    try:
        port = parsed.port if parsed.port is not None else 80
    except ValueError as exc:
        raise ValueError("代理端口无效") from exc
    if not 1 <= port <= 65535:
        raise ValueError("代理端口必须在 1–65535")
    return parsed.hostname, port


def normalize_target_host(host: str) -> str:
    clean_host = host.strip()
    if clean_host.startswith("[") and clean_host.endswith("]"):
        clean_host = clean_host[1:-1]
    if not clean_host:
        raise ValueError("目标主机无效")
    try:
        return str(ipaddress.ip_address(clean_host))
    except ValueError:
        pass
    try:
        ascii_host = clean_host.encode("idna").decode("ascii").rstrip(".")
    except UnicodeError as exc:
        raise ValueError("目标主机无效") from exc
    if len(ascii_host) > 253:
        raise ValueError("目标域名过长")
    labels = ascii_host.split(".")
    if not labels or any(
        len(label) > 63
        or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label)
        for label in labels
    ):
        raise ValueError("目标主机必须是 IP 或有效域名")
    return ascii_host.lower()


def _target_authority(host: str, port: int) -> str:
    clean_host = normalize_target_host(host)
    if not 1 <= port <= 65535:
        raise ValueError("目标端口必须在 1–65535")
    authority_host = f"[{clean_host}]" if ":" in clean_host else clean_host
    return f"{authority_host}:{port}"


def open_http_connect_tunnel(
    proxy_url: str,
    target_host: str,
    target_port: int,
    timeout: float,
) -> tuple[socket.socket, bytes]:
    """Return a connected socket and any bytes received after CONNECT headers."""

    proxy_host, proxy_port = parse_proxy_url(proxy_url)
    authority = _target_authority(target_host, target_port)
    sock = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
    try:
        request = (
            f"CONNECT {authority} HTTP/1.1\r\n"
            f"Host: {authority}\r\n"
            "Proxy-Connection: Keep-Alive\r\n\r\n"
        ).encode("ascii")
        sock.sendall(request)
        response = bytearray()
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                raise OSError("HTTP 代理在 CONNECT 完成前关闭连接")
            response.extend(chunk)
            if len(response) > 16384:
                raise OSError("HTTP 代理响应头过大")
        header, pending = bytes(response).split(b"\r\n\r\n", 1)
        status_line = header.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
        parts = status_line.split()
        if len(parts) < 2 or not parts[1].isdigit():
            raise OSError("HTTP 代理返回了无效的 CONNECT 响应")
        if int(parts[1]) != 200:
            raise OSError(f"HTTP 代理拒绝 CONNECT，状态码 {parts[1]}")
        return sock, pending
    except Exception:
        sock.close()
        raise


def _stdin_to_socket(sock: socket.socket) -> None:
    try:
        read_stdin = getattr(sys.stdin.buffer, "read1", sys.stdin.buffer.read)
        while True:
            chunk = read_stdin(65536)
            if not chunk:
                break
            sock.sendall(chunk)
    except (BrokenPipeError, OSError):
        pass
    finally:
        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def relay_stdio(sock: socket.socket, pending: bytes = b"") -> None:
    sock.settimeout(None)
    sender = threading.Thread(target=_stdin_to_socket, args=(sock,), daemon=True)
    sender.start()
    try:
        if pending:
            sys.stdout.buffer.write(pending)
            sys.stdout.buffer.flush()
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
    except (BrokenPipeError, OSError):
        pass
    finally:
        sock.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="通过无认证 HTTP CONNECT 代理转发 OpenSSH 标准输入输出"
    )
    parser.add_argument("--proxy-url", required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--connect-timeout", type=float, default=10.0)
    args = parser.parse_args()
    if args.connect_timeout <= 0:
        parser.error("--connect-timeout 必须大于 0")
    try:
        sock, pending = open_http_connect_tunnel(
            args.proxy_url,
            args.host,
            args.port,
            args.connect_timeout,
        )
    except (OSError, ValueError) as exc:
        print(f"HTTP CONNECT 失败: {exc}", file=sys.stderr)
        return 1
    relay_stdio(sock, pending)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
