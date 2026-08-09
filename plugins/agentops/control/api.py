"""Local Unix-domain health endpoint for the observe-only control plane."""

from __future__ import annotations

import json
import os
import socket
import socketserver
import threading
from pathlib import Path
from typing import Any, Callable


class SocketPathInUseError(RuntimeError):
    """Raised rather than deleting a socket that could belong to another daemon."""


class _ThreadingUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False


class _ControlAPIHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        raw_request = self.rfile.readline(8192)
        try:
            method, path, _protocol = raw_request.decode("ascii").strip().split(" ", 2)
        except ValueError:
            self._respond(400, {"error": "bad_request"})
            return
        if method != "GET":
            self._respond(405, {"error": "method_not_allowed"})
            return
        if path != "/v1/health":
            self._respond(404, {"error": "not_found"})
            return
        try:
            body = self.server.health_provider()  # type: ignore[attr-defined]
        except Exception:
            body = {
                "ready": False,
                "authority_mode": "observe_only",
                "safe_start_reasons": ["health_unavailable"],
                "store_available": False,
                "audit_chain_valid": None,
                "event_count": 0,
                "spool_depth": 0,
                "global_write_enabled": False,
            }
        self._respond(200, body)

    def _respond(self, status: int, body: dict[str, Any]) -> None:
        encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        reason = {200: "OK", 400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed"}[status]
        headers = (
            f"HTTP/1.1 {status} {reason}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(encoded)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        self.wfile.write(headers + encoded)


class ControlAPI:
    """Starts an API with exactly one read-only endpoint."""

    def __init__(self, socket_path: Path, health_provider: Callable[[], dict[str, Any]]):
        self.socket_path = Path(socket_path)
        self.health_provider = health_provider
        self._server: _ThreadingUnixServer | None = None
        self._thread: threading.Thread | None = None
        self._socket_inode: int | None = None

    def start(self) -> None:
        if self.socket_path.exists():
            raise SocketPathInUseError("control socket path already exists")
        self._server = _ThreadingUnixServer(str(self.socket_path), _ControlAPIHandler)
        self._server.health_provider = self.health_provider  # type: ignore[attr-defined]
        try:
            os.chmod(self.socket_path, 0o600)
        except OSError:
            pass
        try:
            self._socket_inode = self.socket_path.stat().st_ino
        except OSError:
            self._socket_inode = None
        self._thread = threading.Thread(target=self._server.serve_forever, name="agentops-uds", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        try:
            if self.socket_path.exists() and self._socket_inode == self.socket_path.stat().st_ino:
                self.socket_path.unlink()
        except OSError:
            pass


def request_control_api(socket_path: Path, method: str, path: str) -> tuple[int, dict[str, Any]]:
    """Small test/operator client; it never sends a request body or credentials."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(2)
        client.connect(str(socket_path))
        client.sendall(f"{method} {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n".encode("ascii"))
        chunks: list[bytes] = []
        while True:
            chunk = client.recv(8192)
            if not chunk:
                break
            chunks.append(chunk)
    raw = b"".join(chunks)
    headers, body = raw.split(b"\r\n\r\n", 1)
    status = int(headers.splitlines()[0].split()[1])
    return status, json.loads(body.decode("utf-8"))


def request_health(socket_path: Path) -> dict[str, Any]:
    status, body = request_control_api(socket_path, "GET", "/v1/health")
    if status != 200:
        raise RuntimeError("health endpoint unavailable")
    return body
