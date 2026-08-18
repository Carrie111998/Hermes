"""
Connection config schema and helpers for the TUI multi-connection feature.

Defines the ~/.hermes/connections.yaml format shared between the TUI
and the desktop plugin. Pure Python, no electron/dependencies.

Security model:
- Tokens are stored in 0o600 YAML by default.
- Optional OS keychain integration via `keyring` (pip install keyring).
- If keyring is available, tokens are stored under service "hermes-agent-pool"
  with key `connection.<name>.token`. The YAML file then stores only metadata.
- Connections with auth="tailscale" rely on WireGuard identity (no token needed).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from typing import Optional
import yaml

# Optional OS keychain integration
_keyring_available = False
try:
    import keyring
    _keyring_available = True
except ImportError:
    pass


def _keyring_service() -> str:
    return "hermes-agent-pool"


def _token_key(name: str) -> str:
    return f"connection.{name}.token"


def store_token(name: str, token: Optional[str]) -> None:
    """Store a token in the OS keyring if available, otherwise returns silently."""
    if not _keyring_available:
        return
    if token:
        keyring.set_password(_keyring_service(), _token_key(name), token)
    else:
        try:
            keyring.delete_password(_keyring_service(), _token_key(name))
        except Exception:
            pass


def retrieve_token(name: str) -> Optional[str]:
    """Retrieve a token from the OS keyring. Returns None if unavailable."""
    if not _keyring_available:
        return None
    try:
        return keyring.get_password(_keyring_service(), _token_key(name))
    except Exception:
        return None


@dataclass
class ConnectionConfig:
    """Single connection entry."""
    name: str
    url: str  # e.g. "https://homelab.tailnet-xxxx.ts.net"
    mode: str = "remote"  # "local" | "remote"
    auth: str = "tailscale"  # "tailscale" | "token" | "oauth"
    token: Optional[str] = None  # only for token auth (deprecated, prefer keyring)
    # Health status (not persisted)
    status: str = "unknown"  # "online" | "offline" | "unknown"
    last_error: Optional[str] = None

    def get_effective_token(self) -> Optional[str]:
        """Get the token, preferring keyring over the YAML field."""
        keyring_token = retrieve_token(self.name)
        if keyring_token is not None:
            return keyring_token
        return self.token


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
    """Persist connections to disk with restrictive permissions.
    
    Tokens are stored separately in the OS keyring (if available).
    The YAML file stores token=None when keyring is available to avoid
    persisting secrets in plaintext.
    """
    path = get_connections_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = {
        "connections": [],
        "active": cf.active,
    }
    for c in cf.connections:
        d = asdict(c)
        # If keyring is available, don't persist tokens in YAML
        if _keyring_available and c.token:
            store_token(c.name, c.token)
            d["token"] = None  # don't persist in plaintext
        data["connections"].append(d)
    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)
    # Owner-only read/write
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
    # Clean up keyring entry
    store_token(name, None)
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
