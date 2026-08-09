"""Regression coverage for gateway restart handoff during Hermes update."""

from unittest.mock import MagicMock

import hermes_cli.gateway as gateway_cli


def test_update_restart_defers_when_gateway_is_process_ancestor(monkeypatch):
    """#82161: a gateway-owned updater must not wait for its parent to exit."""
    graceful = MagicMock()
    monkeypatch.setattr(gateway_cli, "_request_gateway_self_restart", lambda pid: True)
    monkeypatch.setattr(gateway_cli, "_graceful_restart_via_sigusr1", graceful)

    outcome = gateway_cli._restart_gateway_for_update(654, 27.0)

    assert outcome == "deferred"
    graceful.assert_not_called()


def test_update_restart_waits_for_unrelated_gateway(monkeypatch):
    monkeypatch.setattr(gateway_cli, "_request_gateway_self_restart", lambda pid: False)
    monkeypatch.setattr(
        gateway_cli,
        "_graceful_restart_via_sigusr1",
        lambda pid, timeout: (pid, timeout) == (654, 27.0),
    )

    assert gateway_cli._restart_gateway_for_update(654, 27.0) == "exited"


def test_update_restart_reports_failed_graceful_handoff(monkeypatch):
    monkeypatch.setattr(gateway_cli, "_request_gateway_self_restart", lambda pid: False)
    monkeypatch.setattr(
        gateway_cli, "_graceful_restart_via_sigusr1", lambda pid, timeout: False
    )

    assert gateway_cli._restart_gateway_for_update(654, 27.0) == "failed"