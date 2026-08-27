"""Unit tests for `hermes skills active` and `/skills active` (issue #24817)."""

import argparse
import json
from io import StringIO
from unittest.mock import patch
import pytest
from rich.console import Console

from hermes_cli.skills_hub import do_active, handle_skills_slash
from hermes_cli.subcommands.skills import build_skills_parser


class _DummyLockFile:
    def __init__(self, installed):
        self._installed = installed

    def list_installed(self):
        return self._installed


_SAMPLE_SKILLS = [
    {
        "name": "active-builtin",
        "category": "research",
        "description": "An active builtin skill.",
        "platforms": ["linux", "macos", "windows"],
    },
    {
        "name": "active-hub",
        "category": "productivity",
        "description": "An active hub skill.",
        "platforms": ["macos", "linux"],
    },
    {
        "name": "disabled-skill",
        "category": "research",
        "description": "A disabled skill.",
        "platforms": ["linux", "macos", "windows"],
    },
    {
        "name": "incompatible-platform-skill",
        "category": "apple",
        "description": "Skill only for un-matched platform.",
        "platforms": ["nonexistent_os"],
    },
]

_HUB_INSTALLED = [
    {"name": "active-hub", "source": "github", "trust_level": "community"}
]

_BUILTIN_MANIFEST = {"active-builtin": "sha123", "disabled-skill": "sha456"}


@pytest.fixture()
def mock_skills_env(monkeypatch):
    import tools.skills_hub as hub
    import tools.skills_sync as skills_sync
    import tools.skills_tool as skills_tool
    import agent.skill_utils as skill_utils

    monkeypatch.setattr(hub, "HubLockFile", lambda: _DummyLockFile(_HUB_INSTALLED))
    monkeypatch.setattr(skills_tool, "_find_all_skills", lambda **_kwargs: list(_SAMPLE_SKILLS))
    monkeypatch.setattr(skills_sync, "_read_manifest", lambda: dict(_BUILTIN_MANIFEST))
    monkeypatch.setattr(skill_utils, "get_disabled_skill_names", lambda *args, **kwargs: {"disabled-skill"})
    monkeypatch.setattr(
        skill_utils,
        "skill_matches_platform_list",
        lambda platforms: "nonexistent_os" not in platforms,
    )


def test_do_active_filters_disabled_and_incompatible(mock_skills_env):
    sink = StringIO()
    console = Console(file=sink, force_terminal=False, color_system=None)

    do_active(console=console)
    output = sink.getvalue()

    assert "Active Skills" in output
    assert "active-builtin" in output
    assert "active-hub" in output
    assert "disabled-skill" not in output
    assert "incompatible-platform-skill" not in output
    assert "2 active skill(s) loaded" in output


def test_do_active_as_json(mock_skills_env):
    sink = StringIO()
    console = Console(file=sink, force_terminal=False, color_system=None)

    do_active(console=console, as_json=True)
    output = sink.getvalue()

    data = json.loads(output)
    names = [entry["name"] for entry in data]
    assert "active-builtin" in names
    assert "active-hub" in names
    assert "disabled-skill" not in names
    assert "incompatible-platform-skill" not in names


def test_do_active_source_filter(mock_skills_env):
    sink = StringIO()
    console = Console(file=sink, force_terminal=False, color_system=None)

    do_active(source_filter="builtin", console=console)
    output = sink.getvalue()

    assert "active-builtin" in output
    assert "active-hub" not in output


def test_handle_skills_slash_active(mock_skills_env):
    sink = StringIO()
    console = Console(file=sink, force_terminal=False, color_system=None)

    handle_skills_slash("/skills active", console=console)
    output = sink.getvalue()

    assert "Active Skills" in output
    assert "active-builtin" in output
    assert "active-hub" in output


def test_handle_skills_slash_active_json(mock_skills_env):
    sink = StringIO()
    console = Console(file=sink, force_terminal=False, color_system=None)

    handle_skills_slash("/skills active --json", console=console)
    output = sink.getvalue()

    data = json.loads(output)
    assert len(data) == 2


def test_skills_active_subparser():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    build_skills_parser(subparsers, cmd_skills=lambda args: None)

    args = parser.parse_args(["skills", "active", "--source", "builtin", "--json"])
    assert args.command == "skills"
    assert args.skills_action == "active"
    assert args.source == "builtin"
    assert args.json is True
