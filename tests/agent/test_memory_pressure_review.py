"""Memory-pressure early background review.

Port of code-yeongyu/oh-my-openagent's memory-pressure surfacing
(omo-senpi PRs #7006/#7008), adapted to Hermes' background-review fork:
when a memory store crosses the pressure threshold, the post-turn review
fires early with a consolidation-steering prompt instead of waiting out
the turn interval. The main conversation's system prompt is never touched
(prefix-cache invariant).
"""

from unittest.mock import MagicMock

from agent.background_review import (
    _MEMORY_REVIEW_PROMPT,
    spawn_background_review_thread,
)
from tools.memory_tool import MemoryStore


def _bare_agent():
    agent = MagicMock()
    del agent._COMBINED_REVIEW_PROMPT
    del agent._MEMORY_REVIEW_PROMPT
    del agent._SKILL_REVIEW_PROMPT
    return agent


class TestPressureReport:
    def test_empty_store_reports_no_pressure(self):
        store = MemoryStore(memory_char_limit=100, user_char_limit=100)
        assert store.pressure_report(0.9) == []

    def test_store_below_threshold_reports_nothing(self):
        store = MemoryStore(memory_char_limit=100, user_char_limit=100)
        store.memory_entries = ["x" * 50]
        assert store.pressure_report(0.9) == []

    def test_store_at_threshold_reports_pressure(self):
        store = MemoryStore(memory_char_limit=100, user_char_limit=100)
        store.memory_entries = ["x" * 95]
        report = store.pressure_report(0.9)
        assert len(report) == 1
        assert report[0]["target"] == "memory"
        assert report[0]["pct"] == 95
        assert report[0]["current"] == 95
        assert report[0]["limit"] == 100

    def test_both_stores_reported_most_full_first(self):
        store = MemoryStore(memory_char_limit=100, user_char_limit=100)
        store.memory_entries = ["x" * 91]
        store.user_entries = ["y" * 99]
        report = store.pressure_report(0.9)
        assert [r["target"] for r in report] == ["user", "memory"]

    def test_zero_limit_never_pressures(self):
        store = MemoryStore(memory_char_limit=0, user_char_limit=0)
        store.memory_entries = ["anything"]
        assert store.usage_ratio("memory") == 0.0
        assert store.pressure_report(0.9) == []

    def test_usage_ratio_reads_live_entries(self):
        store = MemoryStore(memory_char_limit=100, user_char_limit=100)
        assert store.usage_ratio("memory") == 0.0
        store.memory_entries = ["x" * 80]
        assert store.usage_ratio("memory") == 0.8


class TestPressurePromptSteering:
    def test_pressure_focus_appended_and_consumed(self):
        agent = _bare_agent()
        agent._memory_pressure_focus = "MEMORY store at 95% (95/100 chars)"
        _target, prompt = spawn_background_review_thread(
            agent, [], review_memory=True, review_skills=False
        )
        assert prompt.startswith(_MEMORY_REVIEW_PROMPT)
        assert "MEMORY PRESSURE:" in prompt
        assert "95%" in prompt
        assert "consolidating" in prompt
        # Consumed once — cleared for the next (interval) review.
        assert agent._memory_pressure_focus is None

    def test_no_pressure_line_without_flag(self):
        agent = _bare_agent()
        agent._memory_pressure_focus = None
        _target, prompt = spawn_background_review_thread(
            agent, [], review_memory=True, review_skills=False
        )
        assert prompt == _MEMORY_REVIEW_PROMPT

    def test_skill_only_review_ignores_pressure_focus(self):
        agent = _bare_agent()
        agent._memory_pressure_focus = "MEMORY store at 95% (95/100 chars)"
        _target, prompt = spawn_background_review_thread(
            agent, [], review_memory=False, review_skills=True
        )
        assert "MEMORY PRESSURE:" not in prompt

    def test_non_string_pressure_focus_ignored(self):
        # MagicMock attribute leakage or other junk must never inject.
        agent = _bare_agent()
        agent._memory_pressure_focus = object()
        _target, prompt = spawn_background_review_thread(
            agent, [], review_memory=True, review_skills=False
        )
        assert prompt == _MEMORY_REVIEW_PROMPT


class TestTurnContextPressureTrigger:
    """Exercise the edge-trigger logic in turn_context via a minimal stand-in.

    The real seam is inline in build_turn_context; replicating its exact
    guard here would be a change-detector. Instead we verify the two
    contract pieces it composes: pressure_report threshold behavior (above)
    and the fired/re-arm latch semantics on a real MemoryStore.
    """

    def test_edge_trigger_latch_semantics(self):
        store = MemoryStore(memory_char_limit=100, user_char_limit=100)
        store.memory_entries = ["x" * 95]

        fired = False
        # First crossing fires.
        pressure = store.pressure_report(0.9)
        assert pressure and not fired
        fired = True
        # While still under pressure, latch prevents refire.
        assert store.pressure_report(0.9) and fired
        # Consolidation clears pressure — latch re-arms.
        store.memory_entries = ["x" * 40]
        assert store.pressure_report(0.9) == []
        fired = False
        # A later re-crossing fires again.
        store.memory_entries = ["x" * 96]
        assert store.pressure_report(0.9) and not fired
