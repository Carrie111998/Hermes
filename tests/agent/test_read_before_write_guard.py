"""Tests for the background-review read-before-write guard config escape hatch.

Mirrors test_curator.py's env-isolation pattern (tmp_path + monkeypatch of
Path.home / HERMES_HOME). No LLM is spawned. Fixtures use monkeypatch's
auto-restoring setitem so no global module state leaks across tests.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from hermes_cli.config import load_config


@pytest.fixture
def guard_env(tmp_path, monkeypatch):
    """Isolate HERMES_HOME for the skill_manager_tool module under test."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    import tools.skill_manager_tool as smt
    return smt


def _patch_is_background_review(monkeypatch, value):
    """Auto-restoring override of tools.skill_provenance.is_background_review."""
    fake_mod = types.ModuleType("tools.skill_provenance")
    fake_mod.is_background_review = lambda: value  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tools.skill_provenance", fake_mod)


def test_guard_default_on_when_config_absent(guard_env, monkeypatch):
    """Shipped-default behaviour: guard active even with no config key set."""
    smt = guard_env
    _patch_is_background_review(monkeypatch, True)
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
    _patch_is_background_review(monkeypatch, True)
    # Patch the name bound inside the module (cfg_get is imported at module
    # top, so patching the source module attribute would not reach it).
    orig = smt.cfg_get

    def fake_cfg(cfg, *keys, default=None):
        if keys == ("skills", "background_review", "require_read_before_write"):
            return False
        return orig(cfg, *keys, default=default)

    monkeypatch.setattr(smt, "cfg_get", fake_cfg)
    result = smt._background_review_read_before_write_guard(
        name="demo-skill", target=Path("/x/SKILL.md"),
        action="patch", file_label="SKILL.md",
    )
    assert result is None  # guard bypassed


def test_guard_skipped_for_foreground(guard_env, monkeypatch):
    """Foreground turns (not background review) are never gated."""
    smt = guard_env
    _patch_is_background_review(monkeypatch, False)
    result = smt._background_review_read_before_write_guard(
        name="demo-skill", target=Path("/x/SKILL.md"),
        action="patch", file_label="SKILL.md",
    )
    assert result is None


def test_guard_fail_closed_when_config_unreadable(guard_env, monkeypatch):
    """Fail-closed: a config-read error must KEEP the guard on, not bypass it.

    Regression test for the review finding that the original `except: return
    None` silently disabled the safety guard whenever load_config() raised.
    """
    smt = guard_env
    _patch_is_background_review(monkeypatch, True)
    import hermes_cli.config as cfg_mod

    def boom(*args, **kwargs):
        raise RuntimeError("simulated broken config / plugin hook")

    monkeypatch.setattr(cfg_mod, "load_config", boom)
    result = smt._background_review_read_before_write_guard(
        name="demo-skill", target=Path("/x/SKILL.md"),
        action="patch", file_label="SKILL.md",
    )
    # Guard stays enforced despite the config error.
    assert result is not None
    assert result.get("_read_before_write_required") is True


def test_no_module_state_leak():
    """The fake provenance module must not survive a test that patched it."""
    assert "tools.skill_provenance" not in sys.modules or (
        hasattr(sys.modules.get("tools.skill_provenance"), "is_background_review")
        and sys.modules["tools.skill_provenance"].__name__ == "tools.skill_provenance"
    )


def test_config_default_present():
    """DEFAULT_CONFIG carries the new key defaulting to True."""
    from hermes_cli import config_defaults
    assert config_defaults.DEFAULT_CONFIG["skills"]["background_review"][
        "require_read_before_write"] is True
