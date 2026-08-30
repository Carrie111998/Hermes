"""Tests for hermes_cli/skills_manager.py — skills manager helpers."""


def test_load_skill_names_returns_list():
    from hermes_cli.skills_manager import _load_skill_names
    names = _load_skill_names()
    assert isinstance(names, list)
