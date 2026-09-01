"""Regression tests for the gateway startup turn-budget diagnostic."""

import logging

import pytest

from gateway.config import GatewayConfig
from gateway import run as gateway_run


def _isolate_turn_limit_resolution(monkeypatch):
    monkeypatch.setattr(
        gateway_run, "_reload_runtime_env_preserving_config_authority", lambda: None
    )


def test_agent_budget_diagnostic_reports_unlimited(monkeypatch, caplog):
    _isolate_turn_limit_resolution(monkeypatch)
    monkeypatch.delenv("HERMES_MAX_ITERATIONS", raising=False)

    with caplog.at_level(logging.INFO, logger="gateway.run"):
        gateway_run._log_agent_budget()

    assert "max_iterations=unlimited" in caplog.text
    assert "default 500" not in caplog.text


def test_agent_budget_diagnostic_reports_finite_limit(monkeypatch, caplog):
    _isolate_turn_limit_resolution(monkeypatch)
    monkeypatch.setenv("HERMES_MAX_ITERATIONS", "25")

    with caplog.at_level(logging.INFO, logger="gateway.run"):
        gateway_run._log_agent_budget()

    assert "max_iterations=25" in caplog.text


@pytest.mark.asyncio
async def test_gateway_start_uses_runtime_budget_diagnostic(monkeypatch, tmp_path):
    """The production startup path must call the resolver-backed diagnostic."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    calls = []
    monkeypatch.setattr(gateway_run, "_log_agent_budget", lambda: calls.append(True))
    runner = gateway_run.GatewayRunner(
        GatewayConfig(platforms={}, sessions_dir=tmp_path / "sessions")
    )

    await runner.start()

    assert calls == [True]
