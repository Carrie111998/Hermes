"""Read-only relay from the authenticated iPad surface to Desktop runtimes.

Desktop profile backends are loopback-only and own their live in-memory session
registries.  The browser spectator runs in a separate authenticated dashboard
process.  This module bridges only the two subscription RPCs into the matching
Desktop profile backend; both sides enforce the same method allowlist.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from utils import atomic_json_write

_log = logging.getLogger("hermes_cli.spectator_relay")

_DESCRIPTOR_SCHEMA = "hermes-desktop-spectator-relay-v1"
_DESCRIPTOR_DIR_ENV = "HERMES_SPECTATOR_DESCRIPTOR_DIR"
_RELAY_TOKEN_ENV = "HERMES_SPECTATOR_RELAY_TOKEN"
_PROFILE_ENV = "HERMES_DESKTOP_PROFILE"
_ALLOWED_METHODS = frozenset({"session.subscribe", "session.unsubscribe"})


@dataclass(frozen=True)
class DesktopSpectatorDescriptor:
    path: Path
    pid: int
    port: int
    profile: str
    token: str


def _descriptor_dir() -> Path:
    configured = os.environ.get(_DESCRIPTOR_DIR_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()

    from hermes_constants import get_hermes_home

    return get_hermes_home() / "runtime" / "desktop-spectator"


def _profile_key(profile: str) -> str:
    normalized = profile.strip() or "default"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def descriptor_path(profile: str) -> Path:
    return _descriptor_dir() / f"{_profile_key(profile)}.json"


def write_desktop_descriptor(port: int) -> DesktopSpectatorDescriptor | None:
    """Publish this Desktop backend's loopback relay endpoint, mode 0600."""
    token = os.environ.get(_RELAY_TOKEN_ENV, "").strip()
    profile = os.environ.get(_PROFILE_ENV, "").strip() or "default"
    if not token or not (0 < int(port) <= 65535):
        return None

    path = descriptor_path(profile)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass

    payload = {
        "schema": _DESCRIPTOR_SCHEMA,
        "pid": os.getpid(),
        "port": int(port),
        "profile": profile,
        "token": token,
        "created_at": time.time(),
    }
    atomic_json_write(path, payload, mode=0o600)
    return DesktopSpectatorDescriptor(path, os.getpid(), int(port), profile, token)


def remove_desktop_descriptor(descriptor: DesktopSpectatorDescriptor | None) -> None:
    """Remove only the descriptor still owned by this exact process/token."""
    if descriptor is None:
        return
    try:
        raw = json.loads(descriptor.path.read_text(encoding="utf-8"))
        if raw.get("pid") != descriptor.pid or not secrets.compare_digest(
            str(raw.get("token", "")), descriptor.token
        ):
            return
        descriptor.path.unlink(missing_ok=True)
    except (OSError, ValueError, TypeError):
        return


def _pid_is_live(pid: int) -> bool:
    """Fail closed through the shared cross-platform process probe."""
    try:
        from gateway.status import _pid_exists

        return bool(_pid_exists(pid))
    except Exception:
        return False


def load_desktop_descriptor(profile: str) -> DesktopSpectatorDescriptor | None:
    """Load a live, private descriptor for *profile*; stale entries fail closed."""
    normalized = profile.strip() or "default"
    path = descriptor_path(normalized)
    try:
        stat = path.stat()
        if stat.st_mode & 0o077:
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        pid = int(raw.get("pid", 0))
        port = int(raw.get("port", 0))
        token = str(raw.get("token", ""))
        recorded_profile = str(raw.get("profile", ""))
        if (
            raw.get("schema") != _DESCRIPTOR_SCHEMA
            or recorded_profile != normalized
            or pid <= 0
            or not (0 < port <= 65535)
            or len(token) < 32
            or not _pid_is_live(pid)
        ):
            return None
        return DesktopSpectatorDescriptor(path, pid, port, recorded_profile, token)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def has_live_desktop_descriptors() -> bool:
    """True when at least one Desktop-owned profile backend is currently live."""
    try:
        paths = tuple(_descriptor_dir().glob("*.json"))
    except OSError:
        return False
    for path in paths:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            profile = str(raw.get("profile", ""))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
        if profile and load_desktop_descriptor(profile) is not None:
            return True
    return False


async def _send_json(ws: Any, lock: asyncio.Lock, payload: dict[str, Any]) -> None:
    async with lock:
        await ws.send_text(json.dumps(payload, separators=(",", ":")))


async def relay_spectator_ws(ws: Any) -> None:
    """Relay one browser spectator to the profile selected by subscribe params.

    The browser connection is accepted immediately with a minimal ready frame.
    An upstream Desktop connection is opened only after a subscription names a
    profile.  No backend is spawned and no method outside `_ALLOWED_METHODS` is
    ever forwarded.
    """
    await ws.accept()
    send_lock = asyncio.Lock()
    await _send_json(
        ws,
        send_lock,
        {
            "jsonrpc": "2.0",
            "method": "event",
            "params": {
                "type": "gateway.ready",
                "payload": {"change_events": False, "session_event_replay": True},
            },
        },
    )

    upstream: Any = None
    upstream_profile: str | None = None
    upstream_reader: asyncio.Task[None] | None = None

    async def close_upstream() -> None:
        nonlocal upstream, upstream_profile, upstream_reader
        reader = upstream_reader
        upstream_reader = None
        if reader is not None:
            reader.cancel()
            try:
                await reader
            except (asyncio.CancelledError, Exception):
                pass
        current = upstream
        upstream = None
        upstream_profile = None
        if current is not None:
            try:
                await current.close()
            except Exception:
                pass

    async def forward_upstream(current: Any) -> None:
        try:
            async for frame in current:
                async with send_lock:
                    await ws.send_text(frame if isinstance(frame, str) else frame.decode("utf-8"))
        except asyncio.CancelledError:
            raise
        except Exception:
            # Closing the browser makes JsonRpcGatewayClient use its normal
            # bounded reconnect path and prevents a silently stale observer.
            try:
                await ws.close(code=1012, reason="Desktop spectator runtime disconnected")
            except Exception:
                pass

    async def connect_profile(profile: str) -> bool:
        nonlocal upstream, upstream_profile, upstream_reader
        normalized = profile.strip() or "default"
        if upstream is not None and upstream_profile == normalized:
            return True
        await close_upstream()
        descriptor = load_desktop_descriptor(normalized)
        if descriptor is None or descriptor.pid == os.getpid():
            return False

        import websockets

        query = urllib.parse.urlencode({"spectator_relay": descriptor.token})
        url = f"ws://127.0.0.1:{descriptor.port}/api/ws?{query}"
        try:
            candidate = await websockets.connect(url, open_timeout=5, close_timeout=2)
            first = await asyncio.wait_for(candidate.recv(), timeout=5)
            ready = json.loads(first)
            if not (
                isinstance(ready, dict)
                and ready.get("method") == "event"
                and isinstance(ready.get("params"), dict)
                and ready["params"].get("type") == "gateway.ready"
            ):
                await candidate.close()
                return False
        except Exception as exc:
            _log.warning(
                "Desktop spectator relay unavailable profile=%s error=%s",
                normalized,
                type(exc).__name__,
            )
            return False

        upstream = candidate
        upstream_profile = normalized
        upstream_reader = asyncio.create_task(forward_upstream(candidate))
        return True

    try:
        while True:
            raw = await ws.receive_text()
            try:
                request = json.loads(raw)
            except json.JSONDecodeError:
                await _send_json(
                    ws,
                    send_lock,
                    {"jsonrpc": "2.0", "error": {"code": -32700, "message": "parse error"}, "id": None},
                )
                continue

            if not isinstance(request, dict):
                request = {}
            request_id = request.get("id")
            method = request.get("method")
            if method not in _ALLOWED_METHODS:
                await _send_json(
                    ws,
                    send_lock,
                    {
                        "jsonrpc": "2.0",
                        "error": {"code": -32601, "message": "method not allowed on read-only transport"},
                        "id": request_id,
                    },
                )
                continue

            params = request.get("params") if isinstance(request.get("params"), dict) else {}
            profile = str(params.get("profile", upstream_profile or "default"))
            if not await connect_profile(profile):
                await _send_json(
                    ws,
                    send_lock,
                    {
                        "jsonrpc": "2.0",
                        "error": {"code": -32004, "message": "Desktop runtime is not live for this profile"},
                        "id": request_id,
                    },
                )
                continue

            await upstream.send(json.dumps(request, separators=(",", ":")))
    except Exception as exc:
        # Starlette and test doubles use different disconnect exception classes;
        # any receive failure ends only this spectator relay.
        _log.debug("Desktop spectator browser disconnected: %s", type(exc).__name__)
    finally:
        await close_upstream()
