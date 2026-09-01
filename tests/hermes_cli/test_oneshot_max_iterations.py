"""Regression tests: -z (oneshot) must honour the same turn-limit resolution
chain as the interactive CLI (#99957).

``hermes_cli/oneshot.py:_run_agent`` used to build AIAgent without ever
passing ``max_iterations``, so ``agent.max_turns`` / the legacy root-level
``max_turns`` / ``HERMES_MAX_ITERATIONS`` were all silently discarded for
every ``-z`` run and the agent fell back to its constructor default
(unbounded iterations on current main).
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

from hermes_cli.oneshot import _resolve_oneshot_max_iterations, _run_agent


class TestResolveOneshotMaxIterations:
    def test_config_agent_max_turns_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("HERMES_MAX_ITERATIONS", "4")
        assert _resolve_oneshot_max_iterations({"agent": {"max_turns": 150}}) == 150

    def test_env_used_when_config_missing(self, monkeypatch):
        monkeypatch.setenv("HERMES_MAX_ITERATIONS", "4")
        assert _resolve_oneshot_max_iterations({}) == 4

    def test_env_used_when_config_explicitly_none(self, monkeypatch):
        # cli.py's chain skips a config value of None the same way — env is
        # the next stop, not the unlimited default.
        monkeypatch.setenv("HERMES_MAX_ITERATIONS", "4")
        assert _resolve_oneshot_max_iterations({"agent": {"max_turns": None}}) == 4

    def test_unlimited_when_nothing_set(self, monkeypatch):
        monkeypatch.delenv("HERMES_MAX_ITERATIONS", raising=False)
        assert _resolve_oneshot_max_iterations({}) == sys.maxsize

    def test_unlimited_spellings_normalise(self, monkeypatch):
        # resolve_turn_limit is the single normalization point: "none" etc.
        # are first-class spellings of unlimited, not parse errors.
        monkeypatch.delenv("HERMES_MAX_ITERATIONS", raising=False)
        assert _resolve_oneshot_max_iterations({"agent": {"max_turns": "none"}}) == sys.maxsize

    def test_numeric_string_config(self, monkeypatch):
        monkeypatch.delenv("HERMES_MAX_ITERATIONS", raising=False)
        assert _resolve_oneshot_max_iterations({"agent": {"max_turns": "150"}}) == 150


def test_run_agent_passes_resolved_max_iterations(monkeypatch):
    """_run_agent must forward the resolved turn limit into AIAgent (#99957)."""
    captured: dict = {}

    class _FakeAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self._session_messages = None

        def run_conversation(self, prompt):
            return {"final_response": "ok"}

        def shutdown_memory_provider(self, *args, **kwargs):
            pass

        def close(self):
            pass

    monkeypatch.delenv("HERMES_INFERENCE_MODEL", raising=False)
    monkeypatch.delenv("HERMES_MAX_ITERATIONS", raising=False)
    monkeypatch.setattr(
        "hermes_cli.config.load_config", lambda: {"agent": {"max_turns": 150}}
    )
    monkeypatch.setattr("hermes_cli.oneshot.get_fallback_chain", lambda cfg: [])
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider", lambda **kw: {}
    )
    monkeypatch.setattr(
        "hermes_cli.mcp_startup.ensure_mcp_discovery_before_agent_build",
        lambda **kw: None,
    )
    monkeypatch.setattr(
        "hermes_cli.oneshot._create_session_db_for_oneshot", lambda: MagicMock()
    )
    monkeypatch.setattr("run_agent.AIAgent", _FakeAgent)

    text, result = _run_agent("hi", toolsets=["standard"])

    assert captured["max_iterations"] == 150
    assert text == "ok"


def test_run_agent_env_only_feeds_through(monkeypatch):
    """No config value: the env var must still reach AIAgent — the issue's
    exact reproduction (HERMES_MAX_ITERATIONS=4 hermes -z ...)."""
    captured: dict = {}

    class _FakeAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self._session_messages = None

        def run_conversation(self, prompt):
            return {"final_response": "ok"}

        def shutdown_memory_provider(self, *args, **kwargs):
            pass

        def close(self):
            pass

    monkeypatch.delenv("HERMES_INFERENCE_MODEL", raising=False)
    monkeypatch.setenv("HERMES_MAX_ITERATIONS", "4")
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
    monkeypatch.setattr("hermes_cli.oneshot.get_fallback_chain", lambda cfg: [])
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider", lambda **kw: {}
    )
    monkeypatch.setattr(
        "hermes_cli.mcp_startup.ensure_mcp_discovery_before_agent_build",
        lambda **kw: None,
    )
    monkeypatch.setattr(
        "hermes_cli.oneshot._create_session_db_for_oneshot", lambda: MagicMock()
    )
    monkeypatch.setattr("run_agent.AIAgent", _FakeAgent)

    text, result = _run_agent("hi", toolsets=["standard"])

    assert captured["max_iterations"] == 4
