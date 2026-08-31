"""Tests for display.response_box_width config (issue #37293)."""
import os
import sys
import shutil

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import pytest


def _mock_term_size(cols):
    def _fake(*args, **kwargs):
        return os.terminal_size((cols, 24))
    return _fake


@pytest.fixture(autouse=True)
def _reset_box_width_cap():
    """Reset module-level _active_box_width_cap around every test.

    The CLI constructor mutates this global; without a clean teardown,
    test ordering can leak cap state into subsequent tests.
    """
    import cli as climod
    orig = climod._active_box_width_cap
    climod._active_box_width_cap = None
    yield
    climod._active_box_width_cap = orig


def test_auto_policy_full_terminal(monkeypatch):
    import cli as climod
    monkeypatch.setattr(shutil, "get_terminal_size", _mock_term_size(200))
    cli = climod.HermesCLI.__new__(climod.HermesCLI)
    cli.response_box_width = "auto"
    assert cli._scrollback_box_width() == 200


def test_fixed_80_on_wide_terminal(monkeypatch):
    import cli as climod
    monkeypatch.setattr(shutil, "get_terminal_size", _mock_term_size(200))
    cli = climod.HermesCLI.__new__(climod.HermesCLI)
    cli.response_box_width = "fixed:80"
    assert cli._scrollback_box_width() == 80


def test_fixed_80_on_narrow_terminal_does_not_widen(monkeypatch):
    import cli as climod
    monkeypatch.setattr(shutil, "get_terminal_size", _mock_term_size(40))
    cli = climod.HermesCLI.__new__(climod.HermesCLI)
    cli.response_box_width = "fixed:80"
    assert cli._scrollback_box_width() == 40


def test_fixed_32_on_wide_terminal(monkeypatch):
    import cli as climod
    monkeypatch.setattr(shutil, "get_terminal_size", _mock_term_size(200))
    cli = climod.HermesCLI.__new__(climod.HermesCLI)
    cli.response_box_width = "fixed:32"
    assert cli._scrollback_box_width() == 32


def test_fixed_31_falls_back_to_auto(monkeypatch):
    import cli as climod
    monkeypatch.setattr(shutil, "get_terminal_size", _mock_term_size(200))
    cli = climod.HermesCLI.__new__(climod.HermesCLI)
    cli.response_box_width = "fixed:31"
    assert cli._scrollback_box_width() == 200


def test_malformed_policy_falls_back_to_auto(monkeypatch):
    import cli as climod
    monkeypatch.setattr(shutil, "get_terminal_size", _mock_term_size(200))
    cli = climod.HermesCLI.__new__(climod.HermesCLI)
    cli.response_box_width = "lol"
    assert cli._scrollback_box_width() == 200


def test_case_insensitive_fixed_policy(monkeypatch):
    import cli as climod
    monkeypatch.setattr(shutil, "get_terminal_size", _mock_term_size(200))
    cli = climod.HermesCLI.__new__(climod.HermesCLI)
    cli.response_box_width = "Fixed:80"
    assert cli._scrollback_box_width() == 80


def test_scrollback_box_width_none_respects_mock(monkeypatch):
    import cli as climod
    monkeypatch.setattr(shutil, "get_terminal_size", _mock_term_size(120))
    cli = climod.HermesCLI.__new__(climod.HermesCLI)
    cli.response_box_width = "auto"
    assert cli._scrollback_box_width(None) == 120


# ── _terminal_width_for_streaming honors _active_box_width_cap ────────────
# The cap is set by HermesCLI.__init__ when a fixed-N policy is active.
# Markdown tables inside the clamped box must not exceed the box width.


def test_streaming_width_cap_smaller_than_terminal(monkeypatch):
    """Cap (80) < terminal (200) -> streaming budget shrinks to 80."""
    import cli as climod
    monkeypatch.setattr(shutil, "get_terminal_size", _mock_term_size(200))
    climod._active_box_width_cap = 80
    # 200 - 2 (pad/margin) = 198 raw; cap forces 80 - 2 = 78
    assert climod._terminal_width_for_streaming() == 78


def test_streaming_width_cap_larger_than_terminal(monkeypatch):
    """Cap (200) > terminal (80) -> cap is ignored, terminal drives."""
    import cli as climod
    monkeypatch.setattr(shutil, "get_terminal_size", _mock_term_size(80))
    climod._active_box_width_cap = 200
    # 80 - 2 = 78 (cap is larger, so it has no effect)
    assert climod._terminal_width_for_streaming() == 78


def test_streaming_width_cap_none_uses_terminal(monkeypatch):
    """Cap is None (auto policy) -> terminal drives."""
    import cli as climod
    monkeypatch.setattr(shutil, "get_terminal_size", _mock_term_size(150))
    climod._active_box_width_cap = None
    # 150 - 2 = 148
    assert climod._terminal_width_for_streaming() == 148