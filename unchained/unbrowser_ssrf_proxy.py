"""Minimal outbound proxy for the hosted unbrowser MCP service.

The public unbrowser endpoint accepts arbitrary URLs through MCP tools. Keep the
browser process off the app network and force web fetches through this proxy so
private, loopback, link-local, and metadata targets are rejected after DNS
resolution.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import select
import socket
import socketserver
import sys
from urllib.parse import urlsplit


HEADER_LIMIT = 64 * 1024
BUFFER_SIZE = 64 * 1024
CONNECT_TIMEOUT = 10
TUNNEL_TIMEOUT = 60

BLOCKED_HOSTS = {
    "localhost",
    "localhost.localdomain",
    "metadata.google.internal",
}


def _allowed_ports() -> set[int]:
    raw = os.environ.get("UNBROWSER_PROXY_ALLOWED_PORTS", "80,443")
    ports: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        port = int(part)
        if port < 1 or port > 65535:
            raise ValueError(f"Invalid allowed port: {port}")
        ports.add(port)
    return ports


def _split_authority(authority: str, default_port: int) -> tuple[str, int]:
    authority = authority.strip()
    if not authority:
        raise ValueError("Missing target host")
    if "@" in authority:
        authority = authority.rsplit("@", 1)[1]
    if authority.startswith("["):
        end = authority.find("]")
        if end == -1:
            raise ValueError("Invalid IPv6 authority")
        host = authority[1:end]
        rest = authority[end + 1 :]
        if rest.startswith(":"):
            return host, int(rest[1:])
        return host, default_port
    if authority.count(":") == 1:
        host, port = authority.rsplit(":", 1)
        return host, int(port)
    return authority, default_port


def _is_allowed_ip(raw_ip: str) -> bool:
    ip = ipaddress.ip_address(raw_ip)
    return ip.is_global


def _resolve_allowed(host: str, port: int) -> str:
    normalized = host.strip().strip(".").lower()
    if not normalized or normalized in BLOCKED_HOSTS:
        raise ValueError(f"Blocked host: {host}")

    try:
        ip = ipaddress.ip_address(normalized)
    except ValueError:
        ip = None
    if ip is not None:
        if not _is_allowed_ip(str(ip)):
            raise ValueError(f"Blocked address: {ip}")
        return str(ip)

    infos = socket.getaddrinfo(normalized, port, type=socket.SOCK_STREAM)
    addresses = sorted({info[4][0] for info in infos})
    if not addresses:
        raise ValueError(f"No addresses for host: {host}")
    blocked = [addr for addr in addresses if not _is_allowed_ip(addr)]
    if blocked:
        raise ValueError(f"Blocked resolved address for {host}: {blocked[0]}")
    return addresses[0]


def _parse_headers(raw: bytes) -> list[tuple[str, str]]:
    headers: list[tuple[str, str]] = []
    for line in raw.split(b"\r\n"):
        if not line or b":" not in line:
            continue
        name, value = line.split(b":", 1)
        headers.append((name.decode("latin1").strip(), value.decode("latin1").strip()))
    return headers


def _headers_to_bytes(headers: list[tuple[str, str]]) -> bytes:
    return b"".join(f"{name}: {value}\r\n".encode("latin1") for name, value in headers)


class ThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


class ProxyHandler(socketserver.BaseRequestHandler):
    allowed_ports: set[int] = set()

    def handle(self) -> None:
        try:
            header, body = self._read_header()
            if not header:
                return
            request_line, raw_headers = header.split(b"\r\n", 1)
            method, target, version = request_line.decode("latin1").split(" ", 2)
            if method.upper() == "CONNECT":
                self._handle_connect(target, version)
            else:
                self._handle_http(method, target, version, raw_headers, body)
        except Exception as exc:  # noqa: BLE001 - proxy must fail closed.
            print(f"[unbrowser-egress] denied request: {exc}", file=sys.stderr, flush=True)
            self._send_error(403, "Forbidden")

    def _read_header(self) -> tuple[bytes, bytes]:
        self.request.settimeout(CONNECT_TIMEOUT)
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.request.recv(4096)
            if not chunk:
                return b"", b""
            data += chunk
            if len(data) > HEADER_LIMIT:
                raise ValueError("Header too large")
        header, body = data.split(b"\r\n\r\n", 1)
        return header, body

    def _check_target(self, host: str, port: int) -> str:
        if port not in self.allowed_ports:
            raise ValueError(f"Blocked port: {port}")
        return _resolve_allowed(host, port)

    def _handle_connect(self, target: str, version: str) -> None:
        host, port = _split_authority(target, 443)
        upstream_ip = self._check_target(host, port)
        upstream = socket.create_connection((upstream_ip, port), timeout=CONNECT_TIMEOUT)
        try:
            self.request.sendall(f"{version} 200 Connection Established\r\n\r\n".encode("latin1"))
            self._tunnel(self.request, upstream)
        finally:
            upstream.close()

    def _handle_http(
        self,
        method: str,
        target: str,
        version: str,
        raw_headers: bytes,
        body: bytes,
    ) -> None:
        parsed = urlsplit(target)
        headers = _parse_headers(raw_headers)
        if parsed.scheme:
            if parsed.scheme.lower() != "http":
                raise ValueError(f"Unsupported proxy scheme: {parsed.scheme}")
            host = parsed.hostname or ""
            port = parsed.port or 80
            path = parsed.path or "/"
            if parsed.query:
                path += f"?{parsed.query}"
        else:
            host_header = next((value for name, value in headers if name.lower() == "host"), "")
            host, port = _split_authority(host_header, 80)
            path = target or "/"

        upstream_ip = self._check_target(host, port)
        upstream = socket.create_connection((upstream_ip, port), timeout=CONNECT_TIMEOUT)
        try:
            filtered = [(name, value) for name, value in headers if name.lower() != "proxy-connection"]
            if not any(name.lower() == "host" for name, _ in filtered):
                host_value = host if port == 80 else f"{host}:{port}"
                filtered.insert(0, ("Host", host_value))
            outbound = f"{method} {path} {version}\r\n".encode("latin1")
            upstream.sendall(outbound + _headers_to_bytes(filtered) + b"\r\n" + body)
            self._tunnel(self.request, upstream)
        finally:
            upstream.close()

    def _tunnel(self, client: socket.socket, upstream: socket.socket) -> None:
        client.settimeout(TUNNEL_TIMEOUT)
        upstream.settimeout(TUNNEL_TIMEOUT)
        sockets = [client, upstream]
        while True:
            readable, _, _ = select.select(sockets, [], [], TUNNEL_TIMEOUT)
            if not readable:
                return
            for sock in readable:
                data = sock.recv(BUFFER_SIZE)
                if not data:
                    return
                peer = upstream if sock is client else client
                peer.sendall(data)

    def _send_error(self, status: int, reason: str) -> None:
        body = f"{status} {reason}\n".encode("utf-8")
        response = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Content-Type: text/plain\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("latin1")
        try:
            self.request.sendall(response + body)
        except OSError:
            pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8888)
    args = parser.parse_args()

    ProxyHandler.allowed_ports = _allowed_ports()
    with ThreadingTCPServer((args.host, args.port), ProxyHandler) as server:
        print(
            f"[unbrowser-egress] listening on {args.host}:{args.port} "
            f"allowed_ports={sorted(ProxyHandler.allowed_ports)}",
            file=sys.stderr,
            flush=True,
        )
        server.serve_forever()


if __name__ == "__main__":
    main()
