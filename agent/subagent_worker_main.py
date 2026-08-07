"""Minimal worker-side bootstrap for owned Hermes subagent processes.

The capability secret crosses only an inherited Unix socket.  It is consumed
before authenticated, length-prefixed broker frames are accepted and is never
placed in argv, the environment, diagnostics, or returned payloads.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import socket
import struct
from collections.abc import Callable, Mapping
from typing import Any, NoReturn, Protocol

MAX_BROKER_FRAME_BYTES = 1_048_576
MIN_CAPABILITY_SECRET_BYTES = 32
MAX_CAPABILITY_SECRET_BYTES = 128
_SECRET_MAGIC = b"HSEC1"
_LENGTH = struct.Struct("!I")
_SECRET_LENGTH = struct.Struct("!H")


class BrokerFrameError(ValueError):
    """A worker broker frame is malformed, oversized, or unauthenticated."""


class WorkerRequestHandler(Protocol):
    """Narrow integration seam for the serial lifecycle lane."""

    def __call__(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise BrokerFrameError("broker frame body is not canonical JSON") from exc


def _validate_secret(secret: bytes) -> bytes:
    if not isinstance(secret, bytes):
        raise BrokerFrameError("capability secret must be bytes")
    if not MIN_CAPABILITY_SECRET_BYTES <= len(secret) <= MAX_CAPABILITY_SECRET_BYTES:
        raise BrokerFrameError("capability secret length is outside the allowed bounds")
    return secret


def _recv_exact(channel: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = channel.recv(remaining)
        if not chunk:
            raise BrokerFrameError("broker frame is truncated")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def send_capability_secret(channel: socket.socket, secret: bytes) -> None:
    """Bootstrap ``secret`` over an already inherited descriptor."""

    validated = _validate_secret(secret)
    channel.sendall(_SECRET_MAGIC + _SECRET_LENGTH.pack(len(validated)) + validated)


def read_capability_secret(channel: socket.socket) -> bytes:
    """Read the one-time capability bootstrap without rendering its value."""

    magic = _recv_exact(channel, len(_SECRET_MAGIC))
    if not hmac.compare_digest(magic, _SECRET_MAGIC):
        raise BrokerFrameError("invalid capability bootstrap")
    (length,) = _SECRET_LENGTH.unpack(_recv_exact(channel, _SECRET_LENGTH.size))
    if not MIN_CAPABILITY_SECRET_BYTES <= length <= MAX_CAPABILITY_SECRET_BYTES:
        raise BrokerFrameError("capability secret length is outside the allowed bounds")
    return _recv_exact(channel, length)


def send_authenticated_frame(
    channel: socket.socket,
    body: Mapping[str, Any],
    secret: bytes,
    *,
    max_frame_bytes: int = MAX_BROKER_FRAME_BYTES,
) -> None:
    """Send one canonical JSON body authenticated with HMAC-SHA256."""

    validated = _validate_secret(secret)
    body_bytes = _canonical_json(body)
    mac = hmac.new(validated, body_bytes, hashlib.sha256).hexdigest()
    outer = _canonical_json({"body": dict(body), "mac": mac})
    if not 0 < len(outer) <= max_frame_bytes:
        raise BrokerFrameError("broker frame length exceeds the configured bound")
    channel.sendall(_LENGTH.pack(len(outer)) + outer)


def read_authenticated_frame(
    channel: socket.socket,
    secret: bytes,
    *,
    max_frame_bytes: int = MAX_BROKER_FRAME_BYTES,
) -> dict[str, Any]:
    """Receive and authenticate one bounded canonical JSON broker frame."""

    validated = _validate_secret(secret)
    (length,) = _LENGTH.unpack(_recv_exact(channel, _LENGTH.size))
    if not 0 < length <= max_frame_bytes:
        raise BrokerFrameError("broker frame length exceeds the configured bound")
    raw = _recv_exact(channel, length)
    try:
        outer = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BrokerFrameError("broker frame is not valid UTF-8 JSON") from exc
    if not isinstance(outer, dict) or set(outer) != {"body", "mac"}:
        raise BrokerFrameError("broker frame envelope has an invalid shape")
    body = outer["body"]
    mac = outer["mac"]
    if not isinstance(body, dict) or not isinstance(mac, str):
        raise BrokerFrameError("broker frame envelope has invalid field types")
    expected = hmac.new(validated, _canonical_json(body), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, expected):
        raise BrokerFrameError("broker frame authentication failed")
    return body


def serve_one(channel: socket.socket, handler: WorkerRequestHandler) -> None:
    """Authenticate one request and response around a secret-blind handler."""

    secret = read_capability_secret(channel)
    request = read_authenticated_frame(channel, secret)
    response = handler(request)
    if not isinstance(response, Mapping):
        raise BrokerFrameError("worker handler response must be a mapping")
    send_authenticated_frame(channel, response, secret)


def _default_handler(request: Mapping[str, Any]) -> Mapping[str, Any]:
    """Readiness-only default; real execution is supplied by serial integration."""

    if request == {"operation": "ping"}:
        return {"ok": True, "operation": "pong"}
    return {"ok": False, "error": "unsupported-operation"}


def _write_all(fd: int, value: bytes) -> None:
    view = memoryview(value)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("cgroup.procs write made no progress")
        view = view[written:]


def _enter_cgroup_and_exec(
    cgroup_procs_fd: int,
    argv: list[str],
    environment: Mapping[str, str],
) -> NoReturn:
    """Enter a prepared cgroup before exec, so every descendant is owned.

    The launcher and bwrap use the same PID because this ends in ``execvpe``.
    No worker instruction can run before the kernel has accepted the PID in
    the dedicated cgroup.
    """

    if cgroup_procs_fd < 0:
        raise ValueError("cgroup_procs_fd must be non-negative")
    if not argv or any(not isinstance(item, str) or "\x00" in item for item in argv):
        raise ValueError("exec argv must contain safe strings")
    if not os.path.isabs(argv[0]):
        raise ValueError("exec executable must be absolute")
    try:
        _write_all(cgroup_procs_fd, str(os.getpid()).encode("ascii"))
    finally:
        os.close(cgroup_procs_fd)
    os.execvpe(argv[0], argv, dict(environment))
    raise AssertionError("os.execvpe unexpectedly returned")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--capability-fd", type=int)
    parser.add_argument("--enter-cgroup-fd", type=int)
    parser.add_argument("--exec", dest="exec_argv", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.enter_cgroup_fd is not None:
        if not args.exec_argv:
            raise SystemExit("--enter-cgroup-fd requires --exec argv")
        _enter_cgroup_and_exec(args.enter_cgroup_fd, args.exec_argv, os.environ)
    if args.capability_fd is None or args.exec_argv:
        raise SystemExit("exactly one worker mode is required")
    with socket.socket(fileno=args.capability_fd) as channel:
        serve_one(channel, _default_handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
