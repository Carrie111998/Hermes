"""Tests for hermes_cli.env_loader — fail-open boundary for managed .env.

Regression tests for PR #73408 — _apply_managed_env must never crash
on filesystem permission errors during startup.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock

import pytest

# Make the worktree importable without depending on the installed wheel.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class TestManagedEnvFailOpen:
    """Regression tests for PR #73408 — _apply_managed_env must never crash."""

    def test_permission_error_on_exists_is_swallowed(self, tmp_path, monkeypatch):
        """When managed_env.exists() raises PermissionError, startup continues.

        This reproduces the Docker s6-setuidgid scenario: /etc/hermes/.env is
        root-owned mode 0700, process runs as non-root, Path.exists() raises
        PermissionError instead of returning False.
        """
        from hermes_cli.env_loader import _apply_managed_env

        fake_managed_dir = Path("/etc/hermes")

        with mock.patch(
            "hermes_cli.managed_scope.get_managed_dir", return_value=fake_managed_dir
        ):
            with mock.patch.object(Path, "exists", side_effect=PermissionError(
                "[Errno 13] Permission denied: '/etc/hermes/.env'"
            )):
                _apply_managed_env()
                assert "MANAGED_TEST_VAR" not in os.environ

    def test_os_error_on_exists_is_swallowed(self, tmp_path, monkeypatch):
        """OSError on exists() must also be swallowed (fail-open)."""
        from hermes_cli.env_loader import _apply_managed_env

        fake_managed_dir = Path("/etc/hermes")

        with mock.patch(
            "hermes_cli.managed_scope.get_managed_dir", return_value=fake_managed_dir
        ):
            with mock.patch.object(Path, "exists", side_effect=OSError(
                "[Errno 38] Function not implemented"
            )):
                _apply_managed_env()
                assert "MANAGED_TEST_VAR" not in os.environ

    def test_missing_managed_dir_returns_cleanly(self, tmp_path, monkeypatch):
        """When get_managed_dir returns None, _apply_managed_env returns early."""
        from hermes_cli.env_loader import _apply_managed_env

        with mock.patch(
            "hermes_cli.managed_scope.get_managed_dir", return_value=None
        ):
            _apply_managed_env()

    def test_managed_dir_import_failure_returns_cleanly(self, tmp_path, monkeypatch):
        """When managed_scope module can't be imported, returns early."""
        from hermes_cli.env_loader import _apply_managed_env

        with mock.patch.dict("sys.modules", {"hermes_cli.managed_scope": None}):
            _apply_managed_env()