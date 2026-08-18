"""
Connection manager for the TUI multi-connection feature.

Manages multiple named connections to hermes serve backends.
Maintains a single active connection at a time; switching tears down
the current backend session and dials the new one.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import urllib.request
import urllib.error
import subprocess
import json
from typing import Any, Callable, Optional

from tui_gateway.connection_config import (
    ConnectionConfig,
    ConnectionsFile,
    get_active_connection,
    load_connections,
    save_connections,
    set_active,
    add_connection,
    remove_connection,
    get_default_local_connection,
)

logger = logging.getLogger(__name__)


class ConnectionManager:
    """
    Manages multiple named hermes serve connections for the TUI.

    The manager holds a single active connection. Switching connections
    tears down the current backend and dials the new one.
    """

    def __init__(self) -> None:
        self._connections: ConnectionsFile = load_connections()
        self._active_connection: Optional[ConnectionConfig] = None
        self._active_url: Optional[str] = None
        self._listeners: list[Callable[[], None]] = []
        self._lock = threading.Lock()

        # Ensure we have at least the local connection
        if not self._connections.connections:
            default = get_default_local_connection()
            add_connection(self._connections, default)
            self._connections.active = default.name
            save_connections(self._connections)

        self._active_connection = get_active_connection(self._connections)
        if self._active_connection:
            self._active_url = self._active_connection.url

    @property
    def active_url(self) -> Optional[str]:
        """URL of the currently active connection."""
        return self._active_url

    @property
    def active_connection(self) -> Optional[ConnectionConfig]:
        """Currently active connection config."""
        return self._active_connection

    @property
    def connections(self) -> list[ConnectionConfig]:
        """All configured connections."""
        return list(self._connections.connections)

    def get_connection(self, name: str) -> Optional[ConnectionConfig]:
        """Get a connection by name."""
        for c in self._connections.connections:
            if c.name == name:
                return c
        return None

    def add(
        self, name: str, url: str, mode: str = "remote", auth: str = "tailscale", token: Optional[str] = None
    ) -> tuple[bool, str]:
        """
        Add a new connection. Returns (success, message).
        Does NOT switch to it (call switch() separately).
        """
        with self._lock:
            conn = ConnectionConfig(name=name, url=url, mode=mode, auth=auth, token=token)
            if add_connection(self._connections, conn):
                self._notify_listeners()
                return True, f"Connection '{name}' added."
            return False, f"Connection '{name}' already exists."

    def remove(self, name: str) -> tuple[bool, str]:
        """Remove a connection. Returns (success, message)."""
        with self._lock:
            if self._connections.active == name:
                return False, f"Cannot remove active connection '{name}'. Switch first."
            if remove_connection(self._connections, name):
                self._notify_listeners()
                return True, f"Connection '{name}' removed."
            return False, f"Connection '{name}' not found."

    def switch(self, name: str) -> tuple[bool, str]:
        """
        Switch to a different connection. Returns (success, message).
        The caller is responsible for re-establishing the session.
        """
        with self._lock:
            conn = self.get_connection(name)
            if not conn:
                return False, f"Connection '{name}' not found."

            if not set_active(self._connections, name):
                return False, f"Failed to activate '{name}'."

            old_name = self._active_connection.name if self._active_connection else "none"
            self._active_connection = conn
            self._active_url = conn.url
            self._notify_listeners()

            return True, f"Switched from '{old_name}' to '{name}' ({conn.url})."

    def test_connection(self, name: str) -> tuple[bool, str]:
        """Test a connection by probing its health endpoint."""
        conn = self.get_connection(name)
        if not conn:
            return False, f"Connection '{name}' not found."

        try:
            url = conn.url.rstrip("/") + "/api/status"
            req = urllib.request.Request(url, method="GET")
            # Add auth header if using token
            token = conn.get_effective_token()
            if token and conn.auth == "token":
                req.add_header("X-Hermes-Session-Token", token)
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    conn.status = "online"
                    return True, f"Connection '{name}' is online."
                else:
                    conn.status = "offline"
                    conn.last_error = f"HTTP {resp.status}"
                    return False, f"Connection '{name}' returned HTTP {resp.status}."
        except urllib.error.URLError as e:
            conn.status = "offline"
            conn.last_error = str(e.reason)
            return False, f"Connection '{name}' unreachable: {e.reason}."
        except Exception as e:
            conn.status = "offline"
            conn.last_error = str(e)
            return False, f"Connection '{name}' error: {e}."

    def discover_tailscale(self) -> tuple[int, str]:
        """
        Scan tailscale for hermes instances.
        Returns (count, message).
        """
        try:
            result = subprocess.run(
                ["tailscale", "status", "--json"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                return 0, f"tailscale not available: {result.stderr.strip()}"

            status = json.loads(result.stdout)
            peers = status.get("Peer", {})
            added = 0

            for peer_id, peer in peers.items():
                hostname = peer.get("HostName", "")
                tailscale_ip = peer.get("TailscaleIPs", [""])[0]
                if not tailscale_ip:
                    continue

                # Check if this peer has a hermes serve port open
                # Tailscale Serve typically exposes on 443 or a high port
                url = f"https://{hostname}" if hostname else f"https://{tailscale_ip}"

                conn_name = f"ts-{hostname}" if hostname else f"ts-{tailscale_ip.replace('.', '-')}"
                if self.get_connection(conn_name):
                    continue

                # Quick check if port 443 is responding
                ok, _msg = self._quick_probe(url)
                if ok:
                    self.add(conn_name, url, mode="remote", auth="tailscale")
                    added += 1

            return added, f"Discovered {added} Tailscale-hosted Hermes instance(s)."

        except FileNotFoundError:
            return 0, "tailscale CLI not found in PATH."
        except Exception as e:
            return 0, f"Discovery failed: {e}."

    def _quick_probe(self, url: str) -> tuple[bool, str]:
        """Quick HTTP probe to check if a URL responds."""
        try:
            import urllib.request

            req = urllib.request.Request(url.rstrip("/") + "/api/status", method="GET")
            with urllib.request.urlopen(req, timeout=3) as resp:
                return resp.status == 200, ""
        except Exception:
            return False, ""

    def list_status(self) -> list[dict[str, Any]]:
        """Return a summary of all connections and their status."""
        result = []
        for c in self._connections.connections:
            is_active = self._active_connection and c.name == self._active_connection.name
            result.append(
                {
                    "name": c.name,
                    "url": c.url,
                    "mode": c.mode,
                    "auth": c.auth,
                    "active": is_active,
                    "status": c.status,
                    "last_error": c.last_error,
                }
            )
        return result

    def add_listener(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Add a listener that fires when connections change. Returns a disposer."""
        self._listeners.append(callback)
        return lambda: self._listeners.remove(callback) if callback in self._listeners else None

    def _notify_listeners(self) -> None:
        for listener in self._listeners:
            try:
                listener()
            except Exception:
                pass

    def format_list(self) -> str:
        """Format connections as a human-readable string for the TUI."""
        lines = []
        lines.append("Configured connections:")
        lines.append("")
        for s in self.list_status():
            active_marker = " * " if s["active"] else "   "
            status_marker = s["status"]
            lines.append(
                f"{active_marker}{s['name']:15s} {s['url']:45s} [{status_marker}]"
            )
            if s["last_error"]:
                lines.append(f"    └─ last error: {s['last_error']}")
        lines.append("")
        lines.append("Use '/pool switch <name>' to switch active connection.")
        return "\n".join(lines)


# Singleton instance for the TUI gateway process
_manager: Optional[ConnectionManager] = None


def get_connection_manager() -> ConnectionManager:
    """Get or create the singleton ConnectionManager."""
    global _manager
    if _manager is None:
        _manager = ConnectionManager()
    return _manager


def reset_connection_manager() -> None:
    """Reset the singleton (mainly for testing)."""
    global _manager
    _manager = None
