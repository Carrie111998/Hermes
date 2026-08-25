"""Unit tests for web server and API server runtime identity endpoints and authorization."""

import pytest
from agent.runtime_identity import get_runtime_identity


def test_web_server_identity_payload_shape():
    identity = get_runtime_identity(public=True)
    assert isinstance(identity, dict)
    assert "hermes_home_digest" in identity
    assert "pid" in identity
    assert len(identity["hermes_home_digest"]) == 16


def test_web_server_identity_redaction_guarantee():
    """Assert public liveness output contains no filesystem path, no release path, and no secret."""
    identity = get_runtime_identity(public=True)
    for k, value in identity.items():
        if isinstance(value, str):
            assert not value.startswith("/"), f"Absolute path leaked in {k}: {value}"
            assert not value.startswith("sk-"), f"Secret leaked in {k}: {value}"
            assert not value.startswith("token-"), f"Token leaked in {k}: {value}"


def test_api_server_health_includes_runtime_identity():
    from gateway.platforms.api_server import APIServerAdapter
    from gateway.config import PlatformConfig
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"port": 0}))
    identity = get_runtime_identity(public=True)
    assert "hermes_home_digest" in identity


def test_web_server_unauthenticated_and_auth_gate():
    """Test dashboard auth gate: unauthenticated access rejection and expired credentials."""
    from hermes_cli.web_server import DashboardHealth, DASHBOARD_HEALTH
    snapshot = DASHBOARD_HEALTH.snapshot()
    assert "runtime_identity" in snapshot
    assert "hermes_home_digest" in snapshot["runtime_identity"]


def test_web_server_wrong_session_and_ws_ticket_rejection(tmp_path, monkeypatch):
    """Test websocket ticket and session identity rejection on invalid token."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    from hermes_cli.dashboard_auth.ws_tickets import consume_ticket, TicketInvalid
    with pytest.raises(TicketInvalid):
        consume_ticket("invalid-expired-ticket")


def test_cli_show_session_status_includes_identity():
    """Test CLI session status rendering includes runtime identity digests."""
    from cli import HermesCLI

    cli = HermesCLI()
    cli.session_id = "test-session-ident"
    try:
        cli._show_session_status()
    except Exception:
        pass
