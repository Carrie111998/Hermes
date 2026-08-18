"""
Connection config schema and helpers for the TUI multi-connection feature.

Defines the ~/.hermes/connections.yaml format shared between the TUI
and the desktop plugin. Pure Python, no electron/dependencies.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from typing import Optional
import yaml


@dataclass
class ConnectionConfig:
    """Single connection entry."""
    name: str
    url: str  # e.g. "https://homelab.tailnet-xxxx.ts.net"
    mode: str = "remote"  # "local" | "remote"
    auth: str = "tailscale"  # "tailscale" | "token" | "oauth"
    token: Optional[str] = None  # only for token auth
    # Health status (not persisted)
    status: str = "unknown"  # "online" | "offline" | "unknown"
    last_error: Optional[str] = None


@dataclass
class ConnectionsFile:
    """Root object for ~/.hermes/connections.yaml."""
    connections: list[ConnectionConfig] = field(default_factory=list)
    active: Optional[str] = None  # name of active connection


def get_connections_path() -> str:
    """Return the path to the connections config file."""
    hermes_home = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
    return os.path.join(hermes_home, "connections.yaml")


def load_connections() -> ConnectionsFile:
    """Load connections from disk. Returns empty if file doesn't exist."""
    path = get_connections_path()
    if not os.path.exists(path):
        return ConnectionsFile()
    with open(path, "r") as f:
        data = yaml.safe_load(f) or {}
    connections = [ConnectionConfig(**c) for c in data.get("connections", [])]
    return ConnectionsFile(connections=connections, active=data.get("active"))


def save_connections(cf: ConnectionsFile) -> None:
    """Persist connections to disk with restrictive permissions."""
    path = get_connections_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = {
        "connections": [asdict(c) for c in cf.connections if c.status == "unknown" or True],
        "active": cf.active,
    }
    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)
    # Owner-only read/write (contains tokens)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def get_active_connection(cf: ConnectionsFile) -> Optional[ConnectionConfig]:
    """Return the active connection or None."""
    if not cf.active:
        return None
    for c in cf.connections:
        if c.name == cf.active:
            return c
    return None


def add_connection(cf: ConnectionsFile, conn: ConnectionConfig) -> bool:
    """Add a connection. Returns False if name already exists."""
    if any(c.name == conn.name for c in cf.connections):
        return False
    cf.connections.append(conn)
    save_connections(cf)
    return True


def remove_connection(cf: ConnectionsFile, name: str) -> bool:
    """Remove a connection by name. Returns False if not found."""
    original_len = len(cf.connections)
    cf.connections = [c for c in cf.connections if c.name != name]
    if len(cf.connections) == original_len:
        return False
    if cf.active == name:
        cf.active = None
    save_connections(cf)
    return True


def set_active(cf: ConnectionsFile, name: str) -> bool:
    """Set the active connection. Returns False if not found."""
    if not any(c.name == name for c in cf.connections):
        return False
    cf.active = name
    save_connections(cf)
    return True


def get_default_local_connection() -> ConnectionConfig:
    """Return the default local connection."""
    return ConnectionConfig(
        name="local",
        url="http://127.0.0.1:3000",
        mode="local",
        auth="tailscale",
    )
