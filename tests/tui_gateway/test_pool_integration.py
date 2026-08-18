"""Integration tests for /pool feature — exercises HTTP probing, token auth,
JSON-RPC handlers, and Tailscale discovery."""

import json
import os
import tempfile
from unittest.mock import patch, MagicMock

import pytest

from tui_gateway.connection_config import (
    ConnectionConfig,
    ConnectionsFile,
    add_connection,
)
from tui_gateway.connection_manager import (
    ConnectionManager,
    get_connection_manager,
    reset_connection_manager,
)


@pytest.fixture
def tmp_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    reset_connection_manager()
    yield tmp_path
    reset_connection_manager()


class TestTestConnection:
    """Test the actual HTTP health probe."""

    def test_online_backend(self, tmp_home):
        """When /api/status returns 200, mark as online."""
        mgr = get_connection_manager()
        mgr.add("test", "http://127.0.0.1:9999")

        # Mock urllib to simulate online backend
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            ok, msg = mgr.test_connection("test")
        assert ok is True
        assert "online" in msg.lower()

    def test_offline_backend(self, tmp_home):
        """When connection refused, mark as offline."""
        mgr = get_connection_manager()
        mgr.add("test", "http://127.0.0.1:19999")

        import urllib.error
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("Connection refused")):
            ok, msg = mgr.test_connection("test")
        assert ok is False
        assert "unreachable" in msg.lower() or "refused" in msg.lower()

    def test_offline_http_error(self, tmp_home):
        """Non-200 HTTP status = offline."""
        mgr = get_connection_manager()
        mgr.add("test", "http://127.0.0.1:9999")

        mock_resp = MagicMock()
        mock_resp.status = 503
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            ok, msg = mgr.test_connection("test")
        assert ok is False
        assert "503" in msg


class TestTokenAuth:
    """Verify token is sent in header when auth='token'."""

    def test_token_sent_in_header(self, tmp_home):
        """Token auth connections emit X-Hermes-Session-Token header."""
        mgr = get_connection_manager()
        mgr.add("token-conn", "http://127.0.0.1:9999", auth="token", token="my-secret")

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
            mgr.test_connection("token-conn")

        # Verify the request had the token header
        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        assert req.get_header("X-hermes-session-token") == "my-secret"

    def test_tailscale_no_token_header(self, tmp_home):
        """Tailscale auth connections don't emit token header."""
        mgr = get_connection_manager()
        mgr.add("ts-conn", "http://127.0.0.1:9999", auth="tailscale")

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
            mgr.test_connection("ts-conn")

        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        assert req.get_header("X-hermes-session-token") is None

    def test_auto_detect_token_auth(self, tmp_home):
        """If token is provided without explicit auth, auth defaults to 'token'."""
        mgr = get_connection_manager()
        ok, msg = mgr.add("auto", "http://127.0.0.1:9999", token="some-token")
        assert ok is True
        conn = mgr.get_connection("auto")
        assert conn.auth == "token"
        assert conn.token == "some-token"


class TestTailscaleDiscovery:
    """Test Tailscale peer discovery with realistic JSON."""

    def test_discover_with_fqdn(self, tmp_home):
        """Discovery uses MagicDNSSuffix for proper FQDN."""
        mgr = get_connection_manager()
        # Pre-add local so we don't interfere
        mgr.add("local", "http://127.0.0.1:3000")

        tailscale_json = {
            "MagicDNSSuffix": "tailnet-1234.ts.net",
            "Peer": {
                "peer1": {
                    "HostName": "vm-debian",
                    "TailscaleIPs": ["100.64.0.1"],
                    "Online": True,
                }
            },
        }

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(tailscale_json)

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("subprocess.run", return_value=mock_result):
            with patch("urllib.request.urlopen", return_value=mock_resp):
                count, msg = mgr.discover_tailscale()

        assert count == 1
        conn = mgr.get_connection("ts-vm-debian")
        assert conn is not None
        assert conn.url == "https://vm-debian.tailnet-1234.ts.net"

    def test_discover_offline_peer_skipped(self, tmp_home):
        """Offline peers that don't respond are skipped."""
        mgr = get_connection_manager()
        mgr.add("local", "http://127.0.0.1:3000")

        tailscale_json = {
            "MagicDNSSuffix": "tailnet-1234.ts.net",
            "Peer": {
                "peer1": {
                    "HostName": "offline-vm",
                    "TailscaleIPs": ["100.64.0.2"],
                    "Online": False,
                }
            },
        }

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(tailscale_json)

        import urllib.error
        with patch("subprocess.run", return_value=mock_result):
            with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
                with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
                    count, msg = mgr.discover_tailscale()

        assert count == 0

    def test_discover_fallback_ports(self, tmp_home):
        """Discovery falls back to HTTP on ports 8080/3000 if HTTPS fails."""
        mgr = get_connection_manager()
        mgr.add("local", "http://127.0.0.1:3000")

        tailscale_json = {
            "MagicDNSSuffix": "tailnet-1234.ts.net",
            "Peer": {
                "peer1": {
                    "HostName": "http-vm",
                    "TailscaleIPs": ["100.64.0.3"],
                    "Online": True,
                }
            },
        }

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(tailscale_json)

        # HTTPS fails, HTTP on 8080 succeeds
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        import urllib.error
        def urlopen_side_effect(req, *args, **kwargs):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if url.startswith("https://"):
                raise urllib.error.URLError("cert verify failed")
            return mock_resp

        with patch("subprocess.run", return_value=mock_result):
            with patch("urllib.request.urlopen", side_effect=urlopen_side_effect):
                count, msg = mgr.discover_tailscale()

        assert count == 1
        conn = mgr.get_connection("ts-http-vm")
        assert conn is not None
        assert "http://" in conn.url


class TestRPCHandlers:
    """Test the JSON-RPC pool handlers."""

    def test_pool_methods_registered(self):
        """Verify all pool handlers are registered via HandlerRegistry."""
        from tui_gateway.methods_tools import _registry
        # _registry stores pending handlers as (name, fn) tuples
        registered_names = [name for name, _ in _registry._pending]
        expected = ["pool.list", "pool.add", "pool.remove", "pool.switch", "pool.test", "pool.discover"]
        for method_name in expected:
            assert method_name in registered_names, f"Missing handler: {method_name}"

    def test_pool_list_signature(self):
        """pool.list handler should be callable."""
        from tui_gateway.methods_tools import _registry
        handlers = dict(_registry._pending)
        handler = handlers.get("pool.list")
        assert handler is not None
        assert callable(handler)


class TestSwitchSemantics:
    """Verify switch doesn't claim session migration."""

    def test_switch_updates_active_only(self, tmp_home):
        """Switch changes active connection, not sessions."""
        mgr = get_connection_manager()
        mgr.add("second", "http://127.0.0.1:9999")
        mgr.switch("second")
        assert mgr.active_connection.name == "second"
        assert mgr.active_url == "http://127.0.0.1:9999"

    def test_switch_persists(self, tmp_home):
        """Switch persists to disk."""
        mgr = get_connection_manager()
        mgr.add("persist", "http://127.0.0.1:9999")
        mgr.switch("persist")

        # Reload from disk
        from tui_gateway.connection_config import load_connections
        cf = load_connections()
        assert cf.active == "persist"
