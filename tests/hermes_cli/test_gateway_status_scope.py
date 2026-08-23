"""Tests for gateway status HERMES_HOME scoping (#4671).

Tests the env-based process filter that prevents gateway status from reporting
processes outside the active HERMES_HOME.
"""

from __future__ import annotations

import subprocess
import sys
import os
from pathlib import Path

import pytest


# =============================================================================
# Tests for _pid_hermes_home_matches (hermes_cli.gateway)
# =============================================================================


class TestPidHermesHomeMatches:
    """Tests for the _pid_hermes_home_matches helper."""

    def test_matches_exact_home(self, monkeypatch):
        """Returns True when the process's HERMES_HOME matches expected."""
        import hermes_cli.gateway as gateway

        expected = "/home/user/.hermes"

        class MockProcess:
            def environ(self):
                return {"HERMES_HOME": expected}

        import psutil

        monkeypatch.setattr(psutil, "Process", lambda pid: MockProcess())

        result = gateway._pid_hermes_home_matches(1234, expected)
        assert result is True, f"Expected True for matching HERMES_HOME, got {result}"

    def test_rejects_different_home(self, monkeypatch):
        """Returns False when the process's HERMES_HOME differs."""
        import hermes_cli.gateway as gateway

        class MockProcess:
            def environ(self):
                return {"HERMES_HOME": "/other/home/.hermes"}

        import psutil

        monkeypatch.setattr(psutil, "Process", lambda pid: MockProcess())

        result = gateway._pid_hermes_home_matches(1234, "/home/user/.hermes")
        assert result is False

    def test_rejects_unset_home_with_nondefault_expected(self, monkeypatch):
        """Returns False when HERMES_HOME is unset in process but expected is non-default."""
        import hermes_cli.gateway as gateway

        class MockProcess:
            def environ(self):
                return {}  # No HERMES_HOME set

        import psutil

        monkeypatch.setattr(psutil, "Process", lambda pid: MockProcess())

        # Expected is a non-default home
        result = gateway._pid_hermes_home_matches(1234, "/custom/path/.hermes")
        assert result is False

    def test_accepts_unset_home_with_default_expected(self, monkeypatch):
        """Returns True when HERMES_HOME is unset and expected is platform default."""
        import hermes_cli.gateway as gateway
        from hermes_constants import _get_platform_default_hermes_home

        default = str(Path(_get_platform_default_hermes_home()).resolve())

        class MockProcess:
            def environ(self):
                return {}  # No HERMES_HOME set

        import psutil

        monkeypatch.setattr(psutil, "Process", lambda pid: MockProcess())

        result = gateway._pid_hermes_home_matches(1234, default)
        assert result is True

    def test_handles_dead_process(self, monkeypatch):
        """Returns False when psutil raises NoSuchProcess."""
        import hermes_cli.gateway as gateway
        import psutil

        monkeypatch.setattr(
            psutil,
            "Process",
            lambda pid: (_ for _ in ()).throw(psutil.NoSuchProcess(pid)),
        )

        result = gateway._pid_hermes_home_matches(99999, "/home/user/.hermes")
        assert result is False

    def test_handles_access_denied(self, monkeypatch):
        """Returns False when access to process env is denied."""
        import hermes_cli.gateway as gateway
        import psutil

        monkeypatch.setattr(
            psutil,
            "Process",
            lambda pid: (_ for _ in ()).throw(psutil.AccessDenied(pid)),
        )

        result = gateway._pid_hermes_home_matches(1, "/home/user/.hermes")
        assert result is False

    def test_fails_open_when_psutil_unavailable(self, monkeypatch):
        """Returns True (fail-open) when psutil cannot be imported."""
        import hermes_cli.gateway as gateway

        # Simulate psutil not installed by making the import raise
        import builtins

        _real_import = builtins.__import__

        def _no_psutil(name, *args, **kwargs):
            if name == "psutil":
                raise ImportError("no psutil")
            return _real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _no_psutil)

        result = gateway._pid_hermes_home_matches(1234, "/home/user/.hermes")
        assert result is True, "must fail open when psutil is unavailable"


# =============================================================================
# Tests for find_gateway_pids env-based post-filter
# =============================================================================


class TestFindGatewayPidsHomeScoping:
    """Tests that find_gateway_pids correctly filters by HERMES_HOME."""

    def test_all_profiles_skips_home_filter(self, monkeypatch):
        """When all_profiles=True, the env home filter is NOT applied."""
        import hermes_cli.gateway as gateway

        # Stub out the internal sources so we control the PID set.
        # Use string-based patching for lazily-imported functions.
        monkeypatch.setattr(gateway, "_get_service_pids", lambda **kw: set())
        monkeypatch.setattr(
            "gateway.status.get_running_pid", lambda: None
        )
        monkeypatch.setattr(
            gateway, "_scan_gateway_pids", lambda *a, **kw: [100, 200]
        )
        # Spy on _pid_hermes_home_matches to ensure it's NOT called
        calls = []

        def spy_matches(pid, expected):
            calls.append((pid, expected))
            return True

        monkeypatch.setattr(gateway, "_pid_hermes_home_matches", spy_matches)

        pids = gateway.find_gateway_pids(all_profiles=True)

        assert len(calls) == 0, (
            f"_pid_hermes_home_matches should NOT be called when "
            f"all_profiles=True, but was called {len(calls)} time(s): {calls}"
        )
        assert 100 in pids
        assert 200 in pids

    def test_default_scope_applies_home_filter(self, monkeypatch):
        """When all_profiles=False (default), the env home filter IS applied."""
        import hermes_cli.gateway as gateway
        from hermes_cli.config import get_hermes_home

        current_home = str(get_hermes_home().resolve())

        # Stub out the internal sources
        monkeypatch.setattr(gateway, "_get_service_pids", lambda **kw: set())
        monkeypatch.setattr(
            "gateway.status.get_running_pid", lambda: None
        )
        monkeypatch.setattr(
            gateway, "_scan_gateway_pids", lambda *a, **kw: [100, 200, 300]
        )

        # Only PID 100 matches the current HERMES_HOME
        def mock_matches(pid, expected_home):
            return pid == 100

        monkeypatch.setattr(gateway, "_pid_hermes_home_matches", mock_matches)

        pids = gateway.find_gateway_pids(all_profiles=False)

        assert pids == [100], (
            f"Expected only PID 100 (matching HERMES_HOME), got {pids}"
        )


# =============================================================================
# Tests for _get_service_pids systemd wildcard
# =============================================================================


class TestGetServicePidsSystemdScope:
    """Tests that _get_service_pids uses correct systemd wildcard."""

    def test_all_profiles_uses_wildcard(self, monkeypatch):
        """When all_profiles=True, the systemd branch uses 'hermes-gateway*'."""
        import hermes_cli.gateway as gateway

        recorded_calls = []

        def mock_run(cmd, *args, **kwargs):
            recorded_calls.append(cmd)
            # Return an empty result so the function returns quickly
            return type(
                "Result",
                (object,),
                {
                    "stdout": "",
                    "returncode": 0,
                    "stderr": "",
                },
            )()

        monkeypatch.setattr(gateway, "supports_systemd_services", lambda: True)
        monkeypatch.setattr(gateway, "is_macos", lambda: False)
        monkeypatch.setattr(gateway, "is_windows", lambda: False)
        monkeypatch.setattr(subprocess, "run", mock_run)

        gateway._get_service_pids(all_profiles=True)

        # Find calls to list-units
        list_units_calls = [c for c in recorded_calls if "list-units" in c]
        assert len(list_units_calls) > 0, (
            f"No list-units calls were made. All calls: {recorded_calls}"
        )
        # Every list-units call should use the wildcard
        for call in list_units_calls:
            assert "hermes-gateway*" in call, (
                f"Expected wildcard 'hermes-gateway*' in {call}"
            )

    def test_default_scope_uses_specific_service(self, monkeypatch):
        """When all_profiles=False, systemd uses get_service_name() not wildcard."""
        import hermes_cli.gateway as gateway

        expected_service = "hermes-gateway-test-profile"
        recorded_calls = []

        def mock_run(cmd, *args, **kwargs):
            recorded_calls.append(cmd)
            return type(
                "Result",
                (object,),
                {
                    "stdout": "",
                    "returncode": 0,
                    "stderr": "",
                },
            )()

        monkeypatch.setattr(
            gateway, "get_service_name", lambda: expected_service
        )
        monkeypatch.setattr(gateway, "supports_systemd_services", lambda: True)
        monkeypatch.setattr(gateway, "is_macos", lambda: False)
        monkeypatch.setattr(gateway, "is_windows", lambda: False)
        monkeypatch.setattr(subprocess, "run", mock_run)

        gateway._get_service_pids(all_profiles=False)

        list_units_calls = [c for c in recorded_calls if "list-units" in c]
        assert len(list_units_calls) > 0, (
            f"No list-units calls were made. All calls: {recorded_calls}"
        )
        for call in list_units_calls:
            assert any(expected_service in arg for arg in call), (
                f"Expected specific service name '{expected_service}' in {call}"
            )
            assert not any("hermes-gateway*" in arg for arg in call), (
                f"Wildcard 'hermes-gateway*' should NOT appear when "
                f"all_profiles=False, but was found in {call}"
            )


# =============================================================================
# Tests for _command_line_belongs_to_profile env check (gateway/status.py)
# =============================================================================


class TestCommandLineBelongsToProfileEnvCheck:
    """Tests the env-based HERMES_HOME check in _command_line_belongs_to_profile."""

    def test_default_profile_accepts_without_conflicting_env(self):
        """Default profile accepts a command with no explicit profile or HERMES_HOME."""
        from gateway.status import _command_line_belongs_to_profile

        home = Path("/home/user/.hermes")
        cmd = "python -m hermes_cli.main gateway run"

        result = _command_line_belongs_to_profile(cmd, home)
        assert result is True

    def test_default_profile_rejects_other_profile(self):
        """Default profile rejects a command advertising another profile."""
        from gateway.status import _command_line_belongs_to_profile

        home = Path("/home/user/.hermes")
        cmd = "python -m hermes_cli.main --profile work gateway run"

        result = _command_line_belongs_to_profile(cmd, home)
        assert result is False

    def test_named_profile_accepts_matching_profile(self):
        """Named profile accepts a command with matching --profile flag."""
        from gateway.status import _command_line_belongs_to_profile

        home = Path("/home/user/.hermes/profiles/coder")
        cmd = "python -m hermes_cli.main --profile coder gateway run"

        result = _command_line_belongs_to_profile(cmd, home)
        assert result is True

    def test_named_profile_rejects_other_profile(self):
        """Named profile rejects a command with a different --profile flag."""
        from gateway.status import _command_line_belongs_to_profile

        home = Path("/home/user/.hermes/profiles/coder")
        cmd = "python -m hermes_cli.main --profile work gateway run"

        result = _command_line_belongs_to_profile(cmd, home)
        assert result is False


# =============================================================================
# Integration tests: _record_matches_live_gateway_pid + _command_line_belongs_to_profile
# =============================================================================


class TestRecordMatchesLiveGatewayPidHomeScope:
    """Integration check that _record_matches_live_gateway_pid calls profile check."""

    def test_record_rejects_wrong_profile_pid(self, monkeypatch):
        """When a PID's command line belongs to a different profile, reject it."""
        from gateway.status import _record_matches_live_gateway_pid

        monkeypatch.setattr(
            "gateway.status._read_process_cmdline",
            lambda pid: "python -m hermes_cli.main --profile work gateway run",
        )
        monkeypatch.setattr(
            "gateway.status.looks_like_gateway_runtime_command_line",
            lambda cmd: True,
        )

        record = {"kind": "hermes-gateway", "pid": 1234}
        result = _record_matches_live_gateway_pid(
            record,
            1234,
            expected_home=Path("/home/user/.hermes/profiles/coder"),
        )

        assert result is False, (
            "Should reject PID whose command line belongs to a different profile"
        )