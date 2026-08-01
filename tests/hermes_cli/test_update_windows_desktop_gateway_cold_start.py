"""Regression tests for Windows desktop-owned gateway cold starts (#76129)."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hermes_cli import update_cmd


@pytest.fixture
def windows_update_module(monkeypatch):
    """Make the focused update helpers execute their native-Windows paths."""
    fake_main = SimpleNamespace(_is_windows=lambda: True)
    monkeypatch.setattr(update_cmd, "_m", lambda: fake_main)
    return update_cmd


@pytest.mark.parametrize(
    "cmdline",
    [
        r'C:\Hermes\venv\Scripts\python.exe -m hermes_cli.main serve --host 127.0.0.1 --port 0',
        r'C:\Hermes\venv\Scripts\hermes.exe serve --host 127.0.0.1 --port 0',
        "/opt/hermes/venv/bin/python hermes_cli/main.py serve --host 127.0.0.1 --port 0",
    ],
)
def test_serve_backend_detection_recognizes_desktop_commands(
    windows_update_module, monkeypatch, cmdline
):
    monkeypatch.setattr(
        windows_update_module,
        "_detect_venv_python_processes",
        lambda: [(4242, "python.exe", cmdline)],
    )

    assert windows_update_module._windows_serve_backend_is_running() is True


def test_serve_backend_detection_ignores_plain_dashboard(
    windows_update_module, monkeypatch
):
    monkeypatch.setattr(
        windows_update_module,
        "_detect_venv_python_processes",
        lambda: [
            (
                4242,
                "python.exe",
                "python -m hermes_cli.main dashboard --port 8080",
            ),
        ],
    )

    assert windows_update_module._windows_serve_backend_is_running() is False


def test_serve_backend_detection_fails_open_on_scan_error(
    windows_update_module, monkeypatch
):
    def _raise():
        raise OSError("process table unavailable")

    monkeypatch.setattr(
        windows_update_module, "_detect_venv_python_processes", _raise
    )

    assert windows_update_module._windows_serve_backend_is_running() is False


def test_pause_does_not_arm_cold_start_when_desktop_serve_is_running(
    windows_update_module, monkeypatch
):
    from hermes_cli import gateway as gateway_cli
    from hermes_cli import gateway_windows

    installed = Mock(return_value=True)
    monkeypatch.setattr(
        gateway_cli, "find_gateway_pids", lambda *, all_profiles: []
    )
    monkeypatch.setattr(gateway_windows, "is_installed", installed)
    monkeypatch.setattr(
        windows_update_module,
        "_windows_serve_backend_is_running",
        lambda: True,
    )

    token = windows_update_module._pause_windows_gateways_for_update()

    assert token is None
    installed.assert_not_called()


def test_pause_keeps_existing_cold_start_for_installed_gateway_without_serve(
    windows_update_module, monkeypatch
):
    from hermes_cli import gateway as gateway_cli
    from hermes_cli import gateway_windows

    monkeypatch.setattr(
        gateway_cli, "find_gateway_pids", lambda *, all_profiles: []
    )
    monkeypatch.setattr(gateway_windows, "is_installed", lambda: True)
    monkeypatch.setattr(
        windows_update_module,
        "_windows_serve_backend_is_running",
        lambda: False,
    )

    token = windows_update_module._pause_windows_gateways_for_update()

    assert token == {
        "resume_needed": True,
        "profiles": {},
        "unmapped_pids": [],
        "unmapped": [],
        "cold_start_if_installed": True,
    }


def test_cold_start_skips_spawn_when_desktop_serve_started_during_update(
    windows_update_module, monkeypatch
):
    from hermes_cli import gateway as gateway_cli
    from hermes_cli import gateway_windows

    spawn = Mock(return_value=12345)
    monkeypatch.setattr(
        gateway_cli, "find_gateway_pids", lambda *, all_profiles: []
    )
    monkeypatch.setattr(gateway_windows, "_spawn_detached", spawn)
    monkeypatch.setattr(
        windows_update_module,
        "_windows_serve_backend_is_running",
        lambda: True,
    )

    windows_update_module._cold_start_windows_gateway_after_update()

    spawn.assert_not_called()


def test_cold_start_still_spawns_when_runtime_role_is_unserved(
    windows_update_module, monkeypatch
):
    from hermes_cli import gateway as gateway_cli
    from hermes_cli import gateway_windows

    spawn = Mock(return_value=12345)
    monkeypatch.setattr(
        gateway_cli, "find_gateway_pids", lambda *, all_profiles: []
    )
    monkeypatch.setattr(gateway_windows, "_spawn_detached", spawn)
    monkeypatch.setattr(
        windows_update_module,
        "_windows_serve_backend_is_running",
        lambda: False,
    )

    windows_update_module._cold_start_windows_gateway_after_update()

    spawn.assert_called_once_with()
