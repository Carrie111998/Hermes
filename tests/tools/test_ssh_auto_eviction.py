"""Tests for auto-eviction of stale SSH environments on connection failure."""

import json
from unittest.mock import patch, MagicMock

import pytest


class TestSSHAutoEviction:
    """Verify terminal_tool auto-evicts stale SSH envs on connection errors."""

    def _make_ssh_config(self):
        return {
            "env_type": "ssh", "ssh_host": "1.2.3.4", "ssh_user": "root",
            "ssh_port": 22, "ssh_key": "", "ssh_persistent": False,
            "timeout": 5, "lifetime_seconds": 300, "cwd": "~",
            "host_cwd": None,
        }

    @patch("time.sleep")
    @patch("tools.terminal_tool._get_env_config")
    def test_ssh_connection_failed_evicts_env(self, mock_config, mock_sleep):
        """SSH connection failure should evict the cached environment."""
        from tools.terminal_tool import (
            terminal_tool, _active_environments, _last_activity, _env_lock,
        )

        mock_config.return_value = self._make_ssh_config()
        task_id = "default"  # _resolve_container_task_id collapses to this

        fake_env = MagicMock()
        fake_env.execute.side_effect = RuntimeError(
            "SSH connection failed: ssh: connect to host 1.2.3.4 port 22: Operation timed out"
        )

        with _env_lock:
            _active_environments[task_id] = fake_env
            _last_activity[task_id] = 999

        try:
            result = json.loads(terminal_tool(command="echo hi", task_id=None))
            assert "SSH connection failed" in result["error"]
            assert "HINT" in result["error"]
            assert task_id not in _active_environments
        finally:
            _active_environments.pop(task_id, None)

    @patch("time.sleep")
    @patch("tools.terminal_tool._get_env_config")
    def test_connection_refused_evicts_env(self, mock_config, mock_sleep):
        """Connection refused should also trigger eviction."""
        from tools.terminal_tool import (
            terminal_tool, _active_environments, _last_activity, _env_lock,
        )

        mock_config.return_value = self._make_ssh_config()
        task_id = "default"

        fake_env = MagicMock()
        fake_env.execute.side_effect = RuntimeError("Connection refused")

        with _env_lock:
            _active_environments[task_id] = fake_env
            _last_activity[task_id] = 999

        try:
            result = json.loads(terminal_tool(command="echo hi", task_id=None))
            assert "Connection refused" in result["error"]
            assert "HINT" in result["error"]
            assert task_id not in _active_environments
        finally:
            _active_environments.pop(task_id, None)

    @patch("time.sleep")
    @patch("tools.terminal_tool._get_env_config")
    def test_non_ssh_error_does_not_evict(self, mock_config, mock_sleep):
        """Non-SSH errors should NOT evict the environment."""
        from tools.terminal_tool import (
            terminal_tool, _active_environments, _last_activity, _env_lock,
        )

        mock_config.return_value = self._make_ssh_config()
        task_id = "default"

        fake_env = MagicMock()
        fake_env.execute.side_effect = RuntimeError("some other error")

        with _env_lock:
            _active_environments[task_id] = fake_env
            _last_activity[task_id] = 999

        try:
            result = json.loads(terminal_tool(command="echo hi", task_id=None))
            assert "some other error" in result["error"]
            assert "HINT" not in result["error"]
            assert task_id in _active_environments
        finally:
            _active_environments.pop(task_id, None)

    @patch("time.sleep")
    @patch("tools.terminal_tool._get_env_config")
    def test_non_ssh_backend_network_error_does_not_evict(
        self, mock_config, mock_sleep
    ):
        """Generic network text from another backend must not evict its cache."""
        from tools.terminal_tool import (
            terminal_tool, _active_environments, _last_activity, _env_lock,
        )

        config = self._make_ssh_config()
        config["env_type"] = "local"
        mock_config.return_value = config
        task_id = "default"
        fake_env = MagicMock()
        fake_env.execute.side_effect = RuntimeError("Connection refused")

        with _env_lock:
            _active_environments[task_id] = fake_env
            _last_activity[task_id] = 999

        try:
            result = json.loads(terminal_tool(command="echo hi", task_id=None))
            assert "Connection refused" in result["error"]
            assert "HINT" not in result["error"]
            assert _active_environments[task_id] is fake_env
        finally:
            _active_environments.pop(task_id, None)

    @patch("time.sleep")
    @patch("tools.terminal_tool._get_env_config")
    def test_eviction_includes_actionable_hint(self, mock_config, mock_sleep):
        """SSH errors should include actionable hint for the agent."""
        from tools.terminal_tool import (
            terminal_tool, _active_environments, _last_activity, _env_lock,
        )

        mock_config.return_value = self._make_ssh_config()
        task_id = "default"

        fake_env = MagicMock()
        fake_env.execute.side_effect = RuntimeError("No route to host")

        with _env_lock:
            _active_environments[task_id] = fake_env
            _last_activity[task_id] = 999

        try:
            result = json.loads(terminal_tool(command="echo hi", task_id=None))
            error = result["error"]
            assert "/reload" in error
            assert "configured SSH host and port" in error
        finally:
            _active_environments.pop(task_id, None)
