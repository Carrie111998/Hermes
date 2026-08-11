"""Regression tests: achievement stats must never regress after compaction.

Context compaction rewrites a session's messages (fewer survive) and bumps
``last_active``, which invalidates the scan checkpoint's fingerprint and forces
re-analysis of the smaller transcript. Before the monotonic guard, the fresh
(smaller) stats replaced the cached ones, lifetime aggregates dropped, and
earned badges visibly rolled back — including, fittingly, ``rollback_wizard``.

Two guards under test:

* ``_merge_stats_monotonic`` — per-session metrics take the element-wise max
  with the cached scan, so counts only ever go up.
* the unlock floor in ``_compute_from_scan`` — a badge recorded in
  ``state.json`` stays displayed as unlocked (at >= its recorded first tier)
  even if the live aggregate loses its backing.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Dict

PLUGIN_MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "hermes-achievements"
    / "dashboard"
    / "plugin_api.py"
)


def load_plugin():
    spec = importlib.util.spec_from_file_location("achievements_monotonic_test_module", PLUGIN_MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_merge_keeps_higher_cached_counts_after_compaction_shrink():
    pa = load_plugin()
    cached = {"tool_calls": 400, "git_events": 12, "model_names": ["qwen"], "title": "before"}
    fresh = {"tool_calls": 37, "git_events": 0, "model_names": ["claude"], "title": "after"}
    merged = pa._merge_stats_monotonic(cached, fresh)
    assert merged["tool_calls"] == 400
    assert merged["git_events"] == 12
    assert sorted(merged["model_names"]) == ["claude", "qwen"]
    # session metadata is not a metric — the fresh value wins
    assert merged["title"] == "after"


def test_merge_lets_genuine_growth_through():
    pa = load_plugin()
    merged = pa._merge_stats_monotonic({"tool_calls": 10}, {"tool_calls": 22, "new_metric": 5})
    assert merged["tool_calls"] == 22
    assert merged["new_metric"] == 5


def test_rescan_after_compaction_does_not_shrink_aggregate(monkeypatch, tmp_path):
    """End-to-end through scan_sessions with a fake SessionDB: analyze once,
    compact (shrink messages + bump last_active), rescan — totals must hold."""
    pa = load_plugin()
    monkeypatch.setattr(pa, "get_hermes_home", lambda: tmp_path)
    (tmp_path / "plugins" / "hermes-achievements").mkdir(parents=True)

    def make_msgs(n: int):
        return [
            {"role": "assistant", "content": "ran a tool", "tool_calls": [{"function": {"name": "terminal"}}]}
            for _ in range(n)
        ]

    class FakeDB:
        def __init__(self, messages, last_active):
            self._messages = messages
            self._last_active = last_active

        def list_sessions_rich(self, **_kw):
            return [{"id": "s1", "title": "t", "started_at": 1.0, "last_active": self._last_active, "model": "m"}]

        def get_messages(self, _sid):
            return self._messages

        def close(self):
            pass

    class FakeState:
        SessionDB = None  # replaced per-scan below

    # first scan: 40 tool calls
    fake_module = type(sys)("hermes_state")
    fake_module.SessionDB = lambda: FakeDB(make_msgs(40), last_active=100.0)
    monkeypatch.setitem(sys.modules, "hermes_state", fake_module)
    first = pa.scan_sessions()
    first_calls = first["sessions"][0]["tool_call_count"]
    assert first_calls >= 40

    # compaction: only 3 messages survive, last_active bumps -> fingerprint change
    fake_module.SessionDB = lambda: FakeDB(make_msgs(3), last_active=200.0)
    second = pa.scan_sessions()
    second_calls = second["sessions"][0]["tool_call_count"]
    assert second_calls == first_calls, (
        f"per-session tool_calls regressed after compaction: {first_calls} -> {second_calls}"
    )


def test_unlock_floor_survives_lost_aggregate(monkeypatch, tmp_path):
    pa = load_plugin()
    monkeypatch.setattr(pa, "get_hermes_home", lambda: tmp_path)
    state_dir = tmp_path / "plugins" / "hermes-achievements"
    state_dir.mkdir(parents=True)
    # pick a real tiered achievement from the catalog
    definition = next(d for d in pa.ACHIEVEMENTS if d.get("threshold_metric") and d.get("tiers"))
    metric = definition["threshold_metric"]
    first_tier = definition["tiers"][0]["name"]
    pa.save_state({"unlocks": {definition["id"]: {"unlocked_at": 1, "first_tier": first_tier, "evidence": None}}})

    computed = pa._compute_from_scan({"aggregate": {metric: 0}, "sessions": []})
    flat: Dict[str, Any] = {}
    for section in computed.values():
        if isinstance(section, list):
            for item in section:
                if isinstance(item, dict) and item.get("id") == definition["id"]:
                    flat = item
    assert flat, "achievement missing from computed payload"
    assert flat["unlocked"] is True, "recorded unlock rolled back when aggregate lost its backing"
    assert flat.get("tier") == first_tier
