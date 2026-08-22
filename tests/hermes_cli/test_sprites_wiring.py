"""Cross-surface wiring tests for the Fly Sprites terminal backend."""

from __future__ import annotations

import sys
import types


def test_sprites_dependency_is_exact_and_lazy():
    import tomllib

    from tools.lazy_deps import LAZY_DEPS

    with open("pyproject.toml", "rb") as handle:
        metadata = tomllib.load(handle)

    assert metadata["project"]["optional-dependencies"]["sprites"] == [
        "sprites-py==0.5.0"
    ]
    assert LAZY_DEPS["terminal.sprites"] == ("sprites-py==0.5.0",)


def test_sprites_is_enumerated_as_remote_container_backend():
    from agent.prompt_builder import _REMOTE_TERMINAL_BACKENDS
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    from hermes_cli.web_server import CONFIG_SCHEMA, _TERMINAL_BACKEND_NAMES
    from tools.terminal_tool import _CONTAINER_BACKENDS

    assert DEFAULT_CONFIG["terminal"]["backend"] == "local"
    assert "sprites" in _CONTAINER_BACKENDS
    assert "sprites" in _REMOTE_TERMINAL_BACKENDS
    assert "sprites" in CONFIG_SCHEMA["terminal.backend"]["options"]
    assert "sprites" in _TERMINAL_BACKEND_NAMES


def test_setup_sprites_saves_host_credentials(tmp_path, monkeypatch):
    import hermes_cli.setup as setup

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setitem(sys.modules, "sprites", types.ModuleType("sprites"))
    monkeypatch.setattr(setup, "prompt_choice", lambda *args, **kwargs: 6)
    answers = iter(["sprite-token"])
    monkeypatch.setattr(setup, "prompt", lambda *args, **kwargs: next(answers))
    monkeypatch.setattr(setup, "save_config", lambda config: None)
    monkeypatch.setattr(setup, "print_header", lambda *args, **kwargs: None)
    monkeypatch.setattr(setup, "print_info", lambda *args, **kwargs: None)
    monkeypatch.setattr(setup, "print_success", lambda *args, **kwargs: None)

    config = {"terminal": {"backend": "local"}}
    setup.setup_terminal_backend(config)

    assert config["terminal"]["backend"] == "sprites"
    assert setup.get_env_value("SPRITE_TOKEN") == "sprite-token"
    assert setup.get_env_value("TERMINAL_ENV") == "sprites"


def test_status_reports_sprites_without_exposing_token(monkeypatch):
    import hermes_cli.status as status

    monkeypatch.setenv("SPRITE_TOKEN", "super-secret-token")
    monkeypatch.setenv("SPRITES_API_URL", "https://sprites.example/api")

    lines = status._sprites_status_lines()
    output = "\n".join(lines)

    assert "configured" in output
    assert "https://sprites.example/api" in output
    assert "super-secret-token" not in output
