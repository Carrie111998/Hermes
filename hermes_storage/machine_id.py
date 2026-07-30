"""Stable per-machine fingerprint for Hermes cluster overlays."""

from __future__ import annotations

import hashlib
import os
import platform
import re
import socket
import sys
from pathlib import Path
from typing import Optional

_SAFE_RE = re.compile(r"[^a-zA-Z0-9_-]+")


def _read_text(path: Path) -> Optional[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore").strip()
        return text or None
    except OSError:
        return None


def _windows_machine_guid() -> Optional[str]:
    if sys.platform != "win32":
        return None
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "MachineGuid")
            return str(value).strip() or None
    except OSError:
        return None


def _linux_machine_id() -> Optional[str]:
    for candidate in (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id")):
        value = _read_text(candidate)
        if value:
            return value
    return None


def raw_machine_fingerprint() -> str:
    """Return a stable raw fingerprint string for this host."""
    parts = [
        platform.system(),
        platform.machine(),
        socket.gethostname(),
    ]
    guid = _windows_machine_guid() or _linux_machine_id()
    if guid:
        parts.insert(0, guid)
    else:
        # Last-resort entropy that is usually stable for a given install.
        parts.append(str(Path.home()))
    return "|".join(parts)


def compute_machine_id(*, override: Optional[str] = None) -> str:
    """Return a filesystem/collection-safe machine id."""
    if override and override.strip():
        slug = _SAFE_RE.sub("_", override.strip()).strip("_")
        return slug[:64] or "machine"
    digest = hashlib.sha256(raw_machine_fingerprint().encode("utf-8")).hexdigest()[:16]
    host = _SAFE_RE.sub("_", socket.gethostname()).strip("_")[:24] or "host"
    return f"{host}_{digest}"


def detect_capabilities() -> list[str]:
    """Best-effort capability tags for cluster presence."""
    caps: list[str] = ["terminal.local"]
    if sys.platform == "win32":
        caps.append("os.windows")
    elif sys.platform == "darwin":
        caps.append("os.macos")
    else:
        caps.append("os.linux")
    # Docker availability
    try:
        import shutil

        if shutil.which("docker"):
            caps.append("docker")
    except Exception:
        pass
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        caps.append("browser")
    return caps


def machine_collection_name(machine_id: str) -> str:
    safe = _SAFE_RE.sub("_", machine_id).strip("_") or "unknown"
    return f"machine_{safe}"
