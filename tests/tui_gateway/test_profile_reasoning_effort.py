from __future__ import annotations

from pathlib import Path

import yaml

import tui_gateway.server as server


def _profile_env(tmp_path: Path, monkeypatch, *, reasoning_effort: object = "") -> Path:
    home = tmp_path / ".hermes"
    profile = home / "profiles" / "bot"
    profile.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    config = {"model": {"provider": "openrouter", "default": "test/model"}}
    if reasoning_effort != "":
        config["agent"] = {"reasoning_effort": reasoning_effort}
    (profile / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    return profile


def _result(method: str, params: dict) -> dict:
    envelope = server._methods[method]("test", params)
    assert "error" not in envelope, envelope
    return envelope["result"]


def test_profiles_describe_reports_reasoning_effort(tmp_path, monkeypatch):
    _profile_env(tmp_path, monkeypatch, reasoning_effort="high")

    result = _result("profiles.describe", {"name": "bot"})

    assert result["reasoning_effort"] == "high"


def test_profiles_describe_normalizes_disabled_reasoning(tmp_path, monkeypatch):
    _profile_env(tmp_path, monkeypatch, reasoning_effort=False)

    result = _result("profiles.describe", {"name": "bot"})

    assert result["reasoning_effort"] == "none"


def test_profiles_configure_persists_reasoning_effort(tmp_path, monkeypatch):
    profile = _profile_env(tmp_path, monkeypatch)

    result = _result(
        "profiles.configure",
        {"name": "bot", "reasoning_effort": "xhigh"},
    )

    assert result["ok"] is True
    assert result["applied"]["reasoning_effort"] is True
    raw = yaml.safe_load((profile / "config.yaml").read_text(encoding="utf-8"))
    assert raw["agent"]["reasoning_effort"] == "xhigh"


def test_profiles_configure_rejects_invalid_reasoning_effort(tmp_path, monkeypatch):
    profile = _profile_env(tmp_path, monkeypatch, reasoning_effort="low")

    result = _result(
        "profiles.configure",
        {"name": "bot", "reasoning_effort": "impossible"},
    )

    assert result["ok"] is False
    assert result["applied"]["reasoning_effort"] is False
    raw = yaml.safe_load((profile / "config.yaml").read_text(encoding="utf-8"))
    assert raw["agent"]["reasoning_effort"] == "low"


def test_profiles_configure_clears_reasoning_effort(tmp_path, monkeypatch):
    profile = _profile_env(tmp_path, monkeypatch, reasoning_effort="high")

    result = _result(
        "profiles.configure",
        {"name": "bot", "reasoning_effort": ""},
    )

    assert result["ok"] is True
    raw = yaml.safe_load((profile / "config.yaml").read_text(encoding="utf-8"))
    assert "reasoning_effort" not in raw.get("agent", {})


def test_profiles_create_persists_reasoning_effort(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))

    result = _result(
        "profiles.create",
        {
            "name": "deep-bot",
            "reasoning_effort": "ultra",
            "mirror_credentials": False,
            "no_skills": True,
        },
    )

    assert result["ok"] is True
    profile = home / "profiles" / "deep-bot"
    raw = yaml.safe_load((profile / "config.yaml").read_text(encoding="utf-8"))
    assert raw["agent"]["reasoning_effort"] == "ultra"
