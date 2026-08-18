"""Tests for the TUI connection manager and /pool feature."""

import os
import tempfile
import threading
import time
from unittest.mock import patch

import pytest

from tui_gateway.connection_config import (
    ConnectionConfig,
    ConnectionsFile,
    add_connection,
    get_active_connection,
    get_connections_path,
    load_connections,
    remove_connection,
    save_connections,
    set_active,
    get_default_local_connection,
)
from tui_gateway.connection_manager import (
    ConnectionManager,
    get_connection_manager,
    reset_connection_manager,
)


@pytest.fixture
def tmp_connections_file(tmp_path, monkeypatch):
    """Use a temp file for connections.yaml."""
    fake_home = str(tmp_path)
    monkeypatch.setenv("HERMES_HOME", fake_home)
    reset_connection_manager()
    yield tmp_path
    reset_connection_manager()


class TestConnectionConfig:
    def test_default_local_connection(self):
        conn = get_default_local_connection()
        assert conn.name == "local"
        assert conn.url == "http://127.0.0.1:3000"
        assert conn.mode == "local"

    def test_load_empty(self, tmp_connections_file):
        cf = load_connections()
        assert cf.connections == []
        assert cf.active is None

    def test_add_connection(self, tmp_connections_file):
        cf = ConnectionsFile()
        conn = ConnectionConfig(name="test", url="https://test.ts.net")
        assert add_connection(cf, conn) is True
        assert len(cf.connections) == 1
        assert cf.connections[0].name == "test"

    def test_add_duplicate(self, tmp_connections_file):
        cf = ConnectionsFile()
        conn = ConnectionConfig(name="test", url="https://test.ts.net")
        add_connection(cf, conn)
        assert add_connection(cf, conn) is False

    def test_remove_connection(self, tmp_connections_file):
        cf = ConnectionsFile()
        add_connection(cf, ConnectionConfig(name="test", url="https://test.ts.net"))
        assert remove_connection(cf, "test") is True
        assert len(cf.connections) == 0

    def test_remove_nonexistent(self, tmp_connections_file):
        cf = ConnectionsFile()
        assert remove_connection(cf, "nonexistent") is False

    def test_set_active(self, tmp_connections_file):
        cf = ConnectionsFile()
        add_connection(cf, ConnectionConfig(name="test", url="https://test.ts.net"))
        assert set_active(cf, "test") is True
        assert cf.active == "test"

    def test_set_active_nonexistent(self, tmp_connections_file):
        cf = ConnectionsFile()
        assert set_active(cf, "nonexistent") is False

    def test_get_active_connection(self, tmp_connections_file):
        cf = ConnectionsFile()
        add_connection(cf, ConnectionConfig(name="test", url="https://test.ts.net"))
        set_active(cf, "test")
        active = get_active_connection(cf)
        assert active is not None
        assert active.name == "test"

    def test_persistence(self, tmp_connections_file):
        cf = ConnectionsFile()
        add_connection(cf, ConnectionConfig(name="test", url="https://test.ts.net"))
        set_active(cf, "test")
        save_connections(cf)

        cf2 = load_connections()
        assert len(cf2.connections) == 1
        assert cf2.connections[0].name == "test"
        assert cf2.active == "test"


class TestConnectionManager:
    def test_singleton(self):
        mgr1 = get_connection_manager()
        mgr2 = get_connection_manager()
        assert mgr1 is mgr2

    def test_default_local_created(self, tmp_connections_file):
        mgr = get_connection_manager()
        assert mgr.get_connection("local") is not None
        assert mgr.active_connection.name == "local"

    def test_add(self, tmp_connections_file):
        mgr = get_connection_manager()
        ok, msg = mgr.add("homelab", "https://homelab.ts.net")
        assert ok is True
        assert "homelab" in msg
        assert mgr.get_connection("homelab") is not None

    def test_add_duplicate(self, tmp_connections_file):
        mgr = get_connection_manager()
        mgr.add("homelab", "https://homelab.ts.net")
        ok, msg = mgr.add("homelab", "https://other.ts.net")
        assert ok is False

    def test_remove(self, tmp_connections_file):
        mgr = get_connection_manager()
        mgr.add("homelab", "https://homelab.ts.net")
        ok, msg = mgr.remove("homelab")
        assert ok is True
        assert mgr.get_connection("homelab") is None

    def test_remove_active_fails(self, tmp_connections_file):
        mgr = get_connection_manager()
        ok, msg = mgr.remove("local")
        assert ok is False
        assert "active" in msg.lower()

    def test_switch(self, tmp_connections_file):
        mgr = get_connection_manager()
        mgr.add("homelab", "https://homelab.ts.net")
        ok, msg = mgr.switch("homelab")
        assert ok is True
        assert mgr.active_connection.name == "homelab"
        assert mgr.active_url == "https://homelab.ts.net"

    def test_switch_nonexistent(self, tmp_connections_file):
        mgr = get_connection_manager()
        ok, msg = mgr.switch("nonexistent")
        assert ok is False

    def test_list_status(self, tmp_connections_file):
        mgr = get_connection_manager()
        mgr.add("homelab", "https://homelab.ts.net")
        status = mgr.list_status()
        assert len(status) >= 2
        names = [s["name"] for s in status]
        assert "local" in names
        assert "homelab" in names

    def test_format_list(self, tmp_connections_file):
        mgr = get_connection_manager()
        output = mgr.format_list()
        assert "local" in output
        assert "switch" in output.lower()

    def test_listener(self, tmp_connections_file):
        mgr = get_connection_manager()
        events = []
        mgr.add_listener(lambda: events.append(1))
        mgr.add("homelab", "https://homelab.ts.net")
        assert len(events) == 1

    def test_discover_tailscale_no_cli(self, tmp_connections_file):
        """Test discovery when tailscale CLI is not available."""
        mgr = get_connection_manager()
        count, msg = mgr.discover_tailscale()
        # Should fail gracefully
        assert count == 0
        assert "not found" in msg.lower() or "not available" in msg.lower()
