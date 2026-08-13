"""Integration coverage for Prime Agent's config and auth resolution chain."""

from __future__ import annotations

import hermes_cli.auth as auth


def test_prime_agent_launch_settings_come_from_model_config(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "model:\n"
        "  provider: prime-agent\n"
        "  acp_command: /opt/prime-agent\n"
        "  acp_args: [--mode, acp, --profile, deepseek]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(auth.shutil, "which", lambda command: command)

    status = auth.get_external_process_provider_status("prime-agent")
    credentials = auth.resolve_external_process_provider_credentials("prime-agent")

    assert status["configured"] is True
    assert status["command"] == "/opt/prime-agent"
    assert status["args"] == ["--mode", "acp", "--profile", "deepseek"]
    assert credentials["command"] == "/opt/prime-agent"
    assert credentials["args"] == status["args"]
