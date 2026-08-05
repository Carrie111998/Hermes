"""Regression tests for the s2-w1a extraction (w1a blind implementer).

Covers the PURE methods/functions moved verbatim out of ``cli.py``:

  * ``hermes_cli/status_bar_mixin.py`` (cluster c18 — status-bar helpers)
  * ``hermes_cli/skill_command_helpers.py`` (cluster c7 — skill slash-commands)

plus the API-preservation guarantees of the extraction:

  * every moved name is still importable from ``cli`` (re-exported, so
    ``from cli import ...`` keeps working unchanged);
  * ``HermesCLI`` still resolves the moved methods through ``StatusBarMixin``
    via the MRO;
  * the ``_skill_commands`` registry global now lives in the helper module,
    and ``_reload_skills`` writes it there.
"""
import time

import pytest

from cli import HermesCLI  # noqa: F401  (import-time sanity: module loads)
from cli import (
    _looks_like_slash_command,
    _parse_skills_argument,
)
from hermes_cli.skill_command_helpers import (
    _skill_commands,
    _skill_bundles,
    get_skill_commands,
)
from hermes_cli.status_bar_mixin import StatusBarMixin


class TestSlashCommandDetection:
    def test_commands_are_detected(self):
        assert _looks_like_slash_command("/help") is True
        assert _looks_like_slash_command("/model gpt-4") is True
        assert _looks_like_slash_command("/q") is True
        assert _looks_like_slash_command("/skills a,b") is True

    def test_paths_are_not_commands(self):
        assert _looks_like_slash_command("") is False
        assert _looks_like_slash_command("hello") is False
        assert _looks_like_slash_command("/Users/ironin/file.md:45-46 can you fix this?") is False
        assert _looks_like_slash_command("/tmp/a/b") is False


class TestParseSkillsArgument:
    def test_normalization_and_dedup(self):
        assert _parse_skills_argument(None) == []
        assert _parse_skills_argument("") == []
        assert _parse_skills_argument("a,b") == ["a", "b"]
        assert _parse_skills_argument(["a", " b ", "a"]) == ["a", "b"]
        assert _parse_skills_argument(("x", None, "y")) == ["x", "y"]
        assert _parse_skills_argument(3) == ["3"]


class TestSkillCommandRegistry:
    def test_registry_globals_live_in_helper_module(self):
        # cli.py re-exports the functions; the cache state must be the
        # helper module's globals, not a cli.py copy.
        import hermes_cli.skill_command_helpers as sch

        assert sch.get_skill_commands is get_skill_commands
        assert _skill_commands is None  # pristine import-time state
        assert _skill_bundles is None

    def test_reload_sync_writes_helper_module_global(self):
        # Mirrors what cli._reload_skills does after rescanning: the write
        # must land where get_skill_commands() reads.
        import hermes_cli.skill_command_helpers as sch

        old = sch._skill_commands
        try:
            sch._skill_commands = {"__s2_w1a_test__": {"commands": {}}}
            assert get_skill_commands() == {"__s2_w1a_test__": {"commands": {}}}
        finally:
            sch._skill_commands = old


class TestStatusBarPureHelpers:
    @staticmethod
    def _bare():
        return StatusBarMixin.__new__(StatusBarMixin)

    def test_status_bar_context_style(self):
        sb = self._bare()
        assert sb._status_bar_context_style(None) == "class:status-bar-dim"
        assert sb._status_bar_context_style(30) == "class:status-bar-good"
        assert sb._status_bar_context_style(60) == "class:status-bar-warn"
        assert sb._status_bar_context_style(85) == "class:status-bar-bad"
        assert sb._status_bar_context_style(99) == "class:status-bar-critical"

    def test_battery_status_style(self):
        assert StatusBarMixin._battery_status_style("good") == "class:status-bar-good"
        assert StatusBarMixin._battery_status_style("warn") == "class:status-bar-warn"
        assert StatusBarMixin._battery_status_style("unknown") == "class:status-bar-dim"

    def test_compression_count_style(self):
        assert StatusBarMixin._compression_count_style(0) == "class:status-bar-dim"
        assert StatusBarMixin._compression_count_style(7) == "class:status-bar-warn"
        assert StatusBarMixin._compression_count_style(12) == "class:status-bar-bad"

    def test_build_context_bar(self):
        sb = self._bare()
        bar = sb._build_context_bar(50, width=10)
        assert bar.startswith("[") and bar.endswith("]")
        assert bar.count("\u2588") == 5 and bar.count("\u2591") == 5
        assert sb._build_context_bar(None, width=4) == "[\u2591\u2591\u2591\u2591]"

    def test_format_prompt_elapsed_frozen(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        assert StatusBarMixin._format_prompt_elapsed(1000.0, 0.0) == "\u23f2 0s"
        assert StatusBarMixin._format_prompt_elapsed(None, 0.0) == "\u23f2 0s"
        assert StatusBarMixin._format_prompt_elapsed(970.0, 0.0) == "\u23f2 30s"
        # live turn: emoji flips to the stopwatch-with-running-indicator
        assert StatusBarMixin._format_prompt_elapsed(940.0, 0.0, live=True) == "\u23f1 1m"

    def test_format_idle_since(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        assert StatusBarMixin._format_idle_since(None, turn_live=False) == ""
        assert StatusBarMixin._format_idle_since(900.0, turn_live=True) == ""
        # 100s -> format_duration_compact -> "2m"
        assert StatusBarMixin._format_idle_since(900.0, turn_live=False) == "\u2713 2m"

    def test_trim_status_bar_text(self):
        text = "a" * 200
        trimmed = StatusBarMixin._trim_status_bar_text(text, 20)
        assert len(trimmed) <= 20 + 3
        assert trimmed.endswith("...")
        assert StatusBarMixin._trim_status_bar_text("short", 20) == "short"
        assert StatusBarMixin._trim_status_bar_text("", 20) == ""

    def test_status_bar_display_width(self):
        # ASCII width equals len(); wide glyphs count double via prompt_toolkit.
        assert StatusBarMixin._status_bar_display_width("abc") == 3
        assert StatusBarMixin._status_bar_display_width("") == 0

    def test_scrollback_box_width(self):
        assert StatusBarMixin._scrollback_box_width(None) >= 32
        assert StatusBarMixin._scrollback_box_width(10) == 32
        assert StatusBarMixin._scrollback_box_width(100) == 100

    def test_use_minimal_tui_chrome(self):
        sb = self._bare()
        # Explicit width avoids environment dependence (prompt_toolkit may
        # register a DummyApplication whose get_size() reports 80 cols).
        assert sb._use_minimal_tui_chrome(width=40) is True
        assert sb._use_minimal_tui_chrome(width=63) is True
        assert sb._use_minimal_tui_chrome(width=64) is False
        assert sb._use_minimal_tui_chrome(width=120) is False

    def test_tui_input_rule_height(self):
        sb = self._bare()
        assert sb._tui_input_rule_height("top") == 1
        with pytest.raises(ValueError):
            sb._tui_input_rule_height("middle")

    def test_agent_spacer_height(self):
        sb = self._bare()
        assert sb._agent_spacer_height() == 0  # not running on bare object

    def test_status_bar_goal_segment(self):
        assert StatusBarMixin._status_bar_goal_segment({}) == ""
        assert (
            StatusBarMixin._status_bar_goal_segment(
                {"goal_active": True, "goal_turns_used": 3, "goal_max_turns": 20}
            )
            == "\u2299 goal 3/20"
        )
        assert (
            StatusBarMixin._status_bar_goal_segment(
                {"goal_active": True, "goal_turns_used": 0, "goal_max_turns": 0}
            )
            == "\u2299 goal"
        )


class TestMroResolution:
    def test_hermescli_inherits_status_bar_mixin(self):
        cli_obj = HermesCLI.__new__(HermesCLI)
        assert isinstance(cli_obj, StatusBarMixin)
        # MRO lookup resolves to the mixin's verbatim implementations.
        assert HermesCLI._status_bar_context_style.__qualname__.startswith("StatusBarMixin.")
        assert HermesCLI._format_prompt_elapsed.__qualname__.startswith("StatusBarMixin.")
        assert HermesCLI._trim_status_bar_text.__qualname__.startswith("StatusBarMixin.")
        assert HermesCLI._get_status_bar_fragments.__qualname__.startswith("StatusBarMixin.")
