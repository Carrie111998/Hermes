"""Tests for macOS launchd multi-profile gateway fleet discovery and restart (#19784, #91277)."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import hermes_cli.gateway as gateway_cli


def test_find_all_launchd_gateway_services_discovers_all_profiles(tmp_path, monkeypatch):
    launch_agents = tmp_path / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    (launch_agents / "ai.hermes.gateway.plist").write_text("<plist></plist>", encoding="utf-8")
    (launch_agents / "ai.hermes.gateway-coder.plist").write_text("<plist></plist>", encoding="utf-8")
    (launch_agents / "ai.hermes.gateway-research.plist").write_text("<plist></plist>", encoding="utf-8")
    (launch_agents / "com.other.app.plist").write_text("<plist></plist>", encoding="utf-8")

    monkeypatch.setattr(gateway_cli, "is_macos", lambda: True)
    monkeypatch.setattr(gateway_cli, "_launchd_user_home", lambda: tmp_path)

    services = gateway_cli.find_all_launchd_gateway_services()
    labels = [s[0] for s in services]
    assert "ai.hermes.gateway" in labels
    assert "ai.hermes.gateway-coder" in labels
    assert "ai.hermes.gateway-research" in labels
    assert "com.other.app" not in labels


def test_find_all_launchd_gateway_services_empty_on_non_macos(monkeypatch):
    monkeypatch.setattr(gateway_cli, "is_macos", lambda: False)
    assert gateway_cli.find_all_launchd_gateway_services() == []


def test_launchd_restart_service_kickstarts_target(monkeypatch):
    calls = []

    def fake_run(cmd, check=False, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(gateway_cli, "_launchd_domain", lambda: "gui/501")
    monkeypatch.setattr(gateway_cli.subprocess, "run", fake_run)

    ok = gateway_cli.launchd_restart_service("ai.hermes.gateway-coder")
    assert ok is True
    assert calls == [["launchctl", "kickstart", "-k", "gui/501/ai.hermes.gateway-coder"]]
