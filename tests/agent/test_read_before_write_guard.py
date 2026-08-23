"""Tests for the background-review read-before-write guard config escape hatch.

Mirrors test_curator.py's env-isolation pattern (tmp_path + monkeypatch of
Path.home / HERMES_HOME, reload of the module under test). No LLM is spawned.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from hermes_cli.config import load_config


@pytest.fixture
def guard_env(tmp_path, monkeypatch):
    """Isolate HERMES_HOME and reload the skill_manager_tool module."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    import tools.skill_manager_tool as smt
    importlib.reload(smt)
    return smt


def test_guard_default_on_when_config_absent(guard_env):
    """Shipped-default behaviour: guard active even with no config key set."""
    smt = guard_env
    # Patch is_background_review to True and simulate an unread target.
    monkeypatch_is_bg(smt, True)
    result = smt._background_review_read_before_write_guard(
        name="demo-skill", target=Path("/x/SKILL.md"),
        action="patch", file_label="SKILL.md",
    )
    assert result is not None
    assert result.get("_read_before_write_required") is True


def test_guard_off_when_config_false(guard_env, monkeypatch):
    """Escape hatch: skills.background_review.require_read_before_write: false
    lets the fork patch without re-reading."""
    smt = guard_env
    monkeypatch_is_bg(smt, True)
    # Force the config key to False regardless of on-disk config.
    import hermes_cli.config as cfg_mod
    orig = cfg_mod.cfg_get

    def fake_cfg(cfg, *keys, default=None):
        if keys == ("skills", "background_review", "require_read_before_write"):
            return False
        return orig(cfg, *keys, default=default)

    monkeypatch.setattr(cfg_mod, "cfg_get", fake_cfg)
    importlib.reload(smt)
    result = smt._background_review_read_before_write_guard(
        name="demo-skill", target=Path("/x/SKILL.md"),
        action="patch", file_label="SKILL.md",
    )
    assert result is None  # guard bypassed


def test_guard_skipped_for_foreground(guard_env, monkeypatch):
    """Foreground turns (not background review) are never gated."""
    smt = guard_env
    monkeypatch_is_bg(smt, False)
    result = smt._background_review_read_before_write_guard(
        name="demo-skill", target=Path("/x/SKILL.md"),
        action="patch", file_label="SKILL.md",
    )
    assert result is None


def test_config_default_present():
    """DEFAULT_CONFIG carries the new key defaulting to True."""
    from hermes_cli import config_defaults
    assert config_defaults.DEFAULT_CONFIG["skills"]["background_review"][
        "require_read_before_write"] is True


def monkeypatch_is_bg(smt, value):
    """Override is_background_review() import used inside the guard."""
    import sys
    import types
    fake_mod = types.ModuleType("tools.skill_provenance")
    fake_mod.is_background_review = lambda: value  # type: ignore[attr-defined]
    sys.modules["tools.skill_provenance"] = fake_mod
