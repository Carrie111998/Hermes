"""Tests for ``hermes delegation profiles`` config management."""
from __future__ import annotations

import argparse
import types
from pathlib import Path

import pytest
import yaml


@pytest.fixture()
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _write_config(home: Path, data: dict) -> None:
    (home / "config.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")


def _read_config(home: Path) -> dict:
    return yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8")) or {}


def test_parser_accepts_delegation_profile_commands():
    from hermes_cli.subcommands.delegation import build_delegation_parser

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    build_delegation_parser(subparsers, cmd_delegation=lambda args: None)

    args = parser.parse_args([
        "delegation", "profiles", "add", "fast",
        "--model", "openai/gpt-5-mini", "--provider", "openrouter",
        "--base-url", "https://openrouter.ai/api/v1", "--api-key", "secret",
        "--api-mode", "chat_completions",
    ])

    assert args.command == "delegation"
    assert args.delegation_action == "profiles"
    assert args.profiles_action == "add"
    assert args.profile_name == "fast"
    assert args.model == "openai/gpt-5-mini"
    assert args.provider == "openrouter"
    assert args.base_url == "https://openrouter.ai/api/v1"
    assert args.api_key == "secret"
    assert args.api_mode == "chat_completions"


def test_add_profile_preserves_unrelated_config(isolated_home, capsys):
    _write_config(isolated_home, {"display": {"skin": "default"}, "delegation": {"max_iterations": 20}})
    from hermes_cli.delegation_cmd import cmd_delegation

    cmd_delegation(types.SimpleNamespace(
        delegation_action="profiles",
        profiles_action="add",
        profile_name="fast",
        model="openai/gpt-5-mini",
        provider="openrouter",
        base_url="https://openrouter.ai/api/v1/",
        api_key="secret-value",
        api_mode="chat_completions",
    ))

    config = _read_config(isolated_home)
    assert config["display"] == {"skin": "default"}
    assert config["delegation"]["max_iterations"] == 20
    assert config["delegation"]["profiles"]["fast"] == {
        "model": "openai/gpt-5-mini",
        "provider": "openrouter",
        "base_url": "https://openrouter.ai/api/v1/",
        "api_key": "secret-value",
        "api_mode": "chat_completions",
    }
    assert "secret-value" not in capsys.readouterr().out


def test_add_profile_requires_at_least_one_bundle_field(isolated_home):
    from hermes_cli.delegation_cmd import cmd_delegation

    with pytest.raises(SystemExit) as exc:
        cmd_delegation(types.SimpleNamespace(
            delegation_action="profiles", profiles_action="add", profile_name="empty",
            model=None, provider=None, base_url=None, api_key=None, api_mode=None,
        ))

    assert exc.value.code == 2
    assert not (isolated_home / "config.yaml").exists()


def test_add_profile_rejects_invalid_name(isolated_home):
    from hermes_cli.delegation_cmd import cmd_delegation

    with pytest.raises(SystemExit) as exc:
        cmd_delegation(types.SimpleNamespace(
            delegation_action="profiles", profiles_action="add", profile_name="bad name",
            model="model", provider=None, base_url=None, api_key=None, api_mode=None,
        ))

    assert exc.value.code == 2


def test_list_profiles_sorts_names_and_masks_api_keys(isolated_home, capsys):
    _write_config(isolated_home, {
        "delegation": {"profiles": {
            "zeta": {"model": "z-model", "api_key": "z-secret"},
            "alpha": {"provider": "openrouter", "model": "a-model"},
        }}
    })
    from hermes_cli.delegation_cmd import cmd_delegation

    cmd_delegation(types.SimpleNamespace(delegation_action="profiles", profiles_action="list"))

    out = capsys.readouterr().out
    assert out.index("alpha") < out.index("zeta")
    assert "a-model" in out
    assert "z-model" in out
    assert "z-secret" not in out
    assert "api key: configured" in out


def test_remove_profile_prunes_empty_profiles_mapping(isolated_home):
    _write_config(isolated_home, {
        "delegation": {"model": "default-model", "profiles": {"fast": {"model": "fast-model"}}},
        "display": {"skin": "default"},
    })
    from hermes_cli.delegation_cmd import cmd_delegation

    cmd_delegation(types.SimpleNamespace(
        delegation_action="profiles", profiles_action="remove", profile_name="fast"
    ))

    config = _read_config(isolated_home)
    assert config["delegation"] == {"model": "default-model"}
    assert config["display"] == {"skin": "default"}


def test_remove_unknown_profile_fails_without_writing(isolated_home):
    original = {"delegation": {"profiles": {"fast": {"model": "fast-model"}}}}
    _write_config(isolated_home, original)
    from hermes_cli.delegation_cmd import cmd_delegation

    with pytest.raises(SystemExit) as exc:
        cmd_delegation(types.SimpleNamespace(
            delegation_action="profiles", profiles_action="remove", profile_name="missing"
        ))

    assert exc.value.code == 1
    assert _read_config(isolated_home) == original
