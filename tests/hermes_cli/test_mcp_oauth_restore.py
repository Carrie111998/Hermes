"""Regression tests for #98759 / PR #98765: MCP OAuth credential restore.

Tests the _reauth_oauth_server snapshot/restore mechanism: when the OAuth
flow fails (or is interrupted via Ctrl-C / KeyboardInterrupt), prior
credentials must be restored so the server remains usable headlessly.
"""

import pytest
from unittest.mock import patch, MagicMock


class TestReauthOauthRestore:
    """Snapshot/restore behavior for _reauth_oauth_server."""

    def _make_server_config(self, url="http://localhost:9000"):
        return {"url": url, "auth": "oauth", "connect_timeout": 60}

    def test_restores_backup_on_exception(self):
        """When _probe_single_server raises RuntimeError, credential backup is restored."""
        from hermes_cli.mcp_config import _reauth_oauth_server

        cfg = self._make_server_config()
        snapshot_called = []
        restore_called = []

        class FakeStorage:
            def snapshot(self):
                snapshot_called.append(True)
                return {"token": {"value": "old-token"}}
            def restore(self, backup, only_if_absent=False):
                restore_called.append(backup)

        class FakeManager:
            def remove(self, name):
                return {"entry": "cached"}

        with (
            patch("hermes_cli.mcp_config.HermesTokenStorage", return_value=FakeStorage()),
            patch("hermes_cli.mcp_config._probe_single_server", side_effect=RuntimeError("boom")),
            patch("hermes_cli.mcp_config._oauth_tokens_present", return_value=True),
            patch("hermes_cli.mcp_config.force_interactive_oauth"),
            patch("hermes_cli.mcp_config.get_manager", return_value=FakeManager()),
            patch("hermes_cli.mcp_config.humanize_oauth_registration_error", return_value="humanized"),
        ):
            result = _reauth_oauth_server("test-server", cfg)

        assert result is False
        assert len(snapshot_called) == 1
        assert len(restore_called) == 1
        assert restore_called[0] == {"token": {"value": "old-token"}}

    def test_does_not_restore_on_success(self):
        """When the flow succeeds, the backup is NOT restored — committed=True."""
        from hermes_cli.mcp_config import _reauth_oauth_server

        cfg = self._make_server_config()
        restore_called = []

        class FakeStorage:
            def snapshot(self):
                return {"token": {"value": "old-token"}}
            def restore(self, backup, only_if_absent=False):
                restore_called.append(backup)

        class FakeManager:
            def remove(self, name):
                return {"entry": "cached"}

        with (
            patch("hermes_cli.mcp_config.HermesTokenStorage", return_value=FakeStorage()),
            patch("hermes_cli.mcp_config._probe_single_server", return_value={"tool1": {}}),
            patch("hermes_cli.mcp_config._oauth_tokens_present", return_value=True),
            patch("hermes_cli.mcp_config.force_interactive_oauth"),
            patch("hermes_cli.mcp_config.get_manager", return_value=FakeManager()),
        ):
            result = _reauth_oauth_server("test-server", cfg)

        assert result is True
        assert len(restore_called) == 0

    def test_restores_on_no_token_obtained(self):
        """When _oauth_tokens_present returns False, backup is restored."""
        from hermes_cli.mcp_config import _reauth_oauth_server

        cfg = self._make_server_config()
        restore_called = []

        class FakeStorage:
            def snapshot(self):
                return {"token": {"value": "old-token"}}
            def restore(self, backup, only_if_absent=False):
                restore_called.append(backup)

        class FakeManager:
            def remove(self, name):
                return {"entry": "cached"}

        with (
            patch("hermes_cli.mcp_config.HermesTokenStorage", return_value=FakeStorage()),
            patch("hermes_cli.mcp_config._probe_single_server", return_value={"tool1": {}}),
            patch("hermes_cli.mcp_config._oauth_tokens_present", return_value=False),
            patch("hermes_cli.mcp_config.force_interactive_oauth"),
            patch("hermes_cli.mcp_config.get_manager", return_value=FakeManager()),
        ):
            result = _reauth_oauth_server("test-server", cfg)

        assert result is False
        assert len(restore_called) == 1

    def test_restores_on_keyboard_interrupt(self):
        """Ctrl-C during OAuth callback wait restores credentials via finally."""
        from hermes_cli.mcp_config import _reauth_oauth_server

        cfg = self._make_server_config()
        restore_called = []

        class FakeStorage:
            def snapshot(self):
                return {"token": {"value": "old-token"}}
            def restore(self, backup, only_if_absent=False):
                restore_called.append(backup)

        class FakeManager:
            def remove(self, name):
                return {"entry": "cached"}

        with (
            patch("hermes_cli.mcp_config.HermesTokenStorage", return_value=FakeStorage()),
            patch("hermes_cli.mcp_config._probe_single_server", side_effect=KeyboardInterrupt),
            patch("hermes_cli.mcp_config._oauth_tokens_present", return_value=True),
            patch("hermes_cli.mcp_config.force_interactive_oauth"),
            patch("hermes_cli.mcp_config.get_manager", return_value=FakeManager()),
            patch("hermes_cli.mcp_config.humanize_oauth_registration_error"),
        ):
            result = _reauth_oauth_server("test-server", cfg)

        # KeyboardInterrupt is a BaseException — the finally block restores.
        # The function returns False (not raised) because except Exception
        # doesn't catch it, but the finally guard ensures restore happens.
        # Whether the function returns or propagates depends on the caller;
        # the key assertion is that restore was called.
        assert len(restore_called) == 1

    def test_rejects_non_oauth_config(self):
        """Servers without auth=oauth are rejected early (no backup/restore)."""
        from hermes_cli.mcp_config import _reauth_oauth_server

        cfg = {"url": "http://localhost:9000", "auth": "none"}
        result = _reauth_oauth_server("test-server", cfg)
        assert result is False
