"""Tests for Fluxer's interactive gateway setup."""

import hermes_cli.cli_output as cli_output_mod
import hermes_cli.config as config_mod


def test_interactive_setup_saves_token_allowlist_and_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    saved = {}
    removed = []
    prompts = iter(["bot-token", "user-1,user-2", "channel-1"])

    monkeypatch.setattr(config_mod, "get_env_value", lambda _key: "")
    monkeypatch.setattr(config_mod, "save_env_value", lambda k, v: saved.update({k: v}))
    monkeypatch.setattr(
        config_mod, "remove_env_value", lambda key: removed.append(key) or False
    )
    monkeypatch.setattr(cli_output_mod, "prompt", lambda *_a, **_kw: next(prompts))
    monkeypatch.setattr(cli_output_mod, "prompt_yes_no", lambda *_a, **_kw: False)
    for name in ("print_header", "print_info", "print_success", "print_warning"):
        monkeypatch.setattr(cli_output_mod, name, lambda *_a, **_kw: None)

    from plugins.platforms.fluxer.adapter import interactive_setup

    interactive_setup()

    assert saved["FLUXER_BOT_TOKEN"] == "bot-token"
    assert saved["FLUXER_ALLOWED_USERS"] == "user-1,user-2"
    assert saved["FLUXER_HOME_CHANNEL"] == "channel-1"
    assert "FLUXER_API_URL" not in saved
    assert "FLUXER_ALLOW_ALL_USERS" in removed


def test_interactive_setup_blank_home_clears_existing(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    removed = []
    prompts = iter(["bot-token", "", ""])

    monkeypatch.setattr(
        config_mod,
        "get_env_value",
        lambda key: "old-channel" if key == "FLUXER_HOME_CHANNEL" else "",
    )
    monkeypatch.setattr(config_mod, "save_env_value", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        config_mod, "remove_env_value", lambda key: removed.append(key) or True
    )
    monkeypatch.setattr(cli_output_mod, "prompt", lambda *_a, **_kw: next(prompts))
    monkeypatch.setattr(cli_output_mod, "prompt_yes_no", lambda *_a, **_kw: False)
    for name in ("print_header", "print_info", "print_success", "print_warning"):
        monkeypatch.setattr(cli_output_mod, name, lambda *_a, **_kw: None)

    from plugins.platforms.fluxer.adapter import interactive_setup

    interactive_setup()

    assert "FLUXER_HOME_CHANNEL" in removed
    assert "FLUXER_ALLOW_ALL_USERS" in removed
