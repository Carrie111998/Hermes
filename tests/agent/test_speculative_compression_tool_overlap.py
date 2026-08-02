from __future__ import annotations

from types import SimpleNamespace

from agent.speculative_compression import (
    SpeculativeCompressionSettings,
    schedule_tool_wait_candidate,
)
from agent.turn_context import _try_install_speculative_candidate


class Compressor:
    context_length = 1_000
    max_tokens = 100

    def _protect_head_size(self, messages):
        return 0

    def _find_tail_cut_by_tokens(self, messages, _head_end):
        return len(messages) - 1

    def _align_boundary_forward(self, _messages, cut):
        return cut

    def should_compress(self, _tokens):
        return True


class Manager:
    def __init__(self, candidate=None):
        self.candidate = candidate
        self.calls = []

    def maybe_start(self, session_id, snapshot, factory, **kwargs):
        self.calls.append((session_id, snapshot, factory, kwargs))
        return "started"

    def take_matching_candidate(self, session_id, messages, **kwargs):
        self.calls.append((session_id, messages, kwargs))
        return self.candidate


def _agent(manager):
    return SimpleNamespace(
        speculative_compression_enabled=True,
        speculative_compression_settings=SpeculativeCompressionSettings(
            enabled=True, hard_wait_seconds=0.01
        ),
        _speculative_compression_manager=manager,
        context_compressor=Compressor(),
        api_mode="chat_completions",
        session_id="session-1",
    )


def test_tool_wait_schedules_when_soft_pressure_is_reached(monkeypatch):
    manager = Manager()
    agent = _agent(manager)
    monkeypatch.setattr(
        "agent.speculative_compression.is_builtin_compression_eligible",
        lambda **_kwargs: True,
    )
    result = schedule_tool_wait_candidate(
        agent,
        [
            {"role": "user", "content": "old"},
            {"role": "user", "content": "tail"},
        ],
        800,
    )
    assert result == "started"
    assert len(manager.calls) == 1


def test_hard_pressure_without_candidate_requests_synchronous_fallback(monkeypatch):
    manager = Manager()
    agent = _agent(manager)
    monkeypatch.setattr(
        "agent.speculative_compression.is_builtin_compression_eligible",
        lambda **_kwargs: True,
    )
    messages = [{"role": "user", "content": "current"}]
    result = _try_install_speculative_candidate(agent, messages, "sys", 900, "task-1")
    assert result == (messages, None, False, True)
    assert manager.calls[-1][2]["wait_seconds"] == 0.01


def test_candidate_commit_rejection_falls_back_without_installing(monkeypatch):
    class RejectedCandidate:
        def is_expired(self, _max_age):
            return False

    manager = Manager(RejectedCandidate())
    agent = _agent(manager)
    agent._compress_context = lambda *args, **kwargs: (
        setattr(agent, "_speculative_install_status", "rejected") or (args[0], "sys")
    )
    monkeypatch.setattr(
        "agent.speculative_compression.is_builtin_compression_eligible",
        lambda **_kwargs: True,
    )
    messages = [{"role": "user", "content": "current"}]
    result = _try_install_speculative_candidate(agent, messages, "sys", 900, "task-1")
    assert result == (messages, None, False, True)


def test_anti_thrash_ineffective_still_installs_ready_candidate(monkeypatch):
    """A ready candidate must install even when the anti-thrash breaker is
    active — only the forced synchronous fallback is suppressed."""

    class ReadyCandidate:
        def is_expired(self, _max_age):
            return False

    manager = Manager(ReadyCandidate())
    agent = _agent(manager)
    agent.context_compressor.should_compress_info = lambda _tokens: (
        False,
        "ineffective",
    )
    agent._compress_context = lambda *args, **kwargs: (
        setattr(agent, "_speculative_install_status", "installed")
        or (["compressed"], "sys")
    )
    monkeypatch.setattr(
        "agent.speculative_compression.is_builtin_compression_eligible",
        lambda **_kwargs: True,
    )
    messages = [{"role": "user", "content": "current"}]
    result = _try_install_speculative_candidate(agent, messages, "sys", 900, "task-1")
    assert result == (["compressed"], "sys", True, False)
    assert len(manager.calls) >= 1  # take_matching_candidate was queried


def test_anti_thrash_ineffective_without_candidate_suppresses_sync_fallback(
    monkeypatch,
):
    """No candidate + ineffective breaker: do not force synchronous
    compression (the freeze/thrash loop #40803/#11529 guard)."""

    manager = Manager()
    agent = _agent(manager)
    agent.context_compressor.should_compress_info = lambda _tokens: (
        False,
        "ineffective",
    )
    monkeypatch.setattr(
        "agent.speculative_compression.is_builtin_compression_eligible",
        lambda **_kwargs: True,
    )
    messages = [{"role": "user", "content": "current"}]
    result = _try_install_speculative_candidate(agent, messages, "sys", 900, "task-1")
    assert result == (messages, None, False, False)


def test_schedule_gate_skips_during_cooldown(monkeypatch):
    manager = Manager()
    agent = _agent(manager)
    agent.context_compressor.get_active_compression_failure_cooldown = (
        lambda: {"remaining_seconds": 30}
    )
    monkeypatch.setattr(
        "agent.speculative_compression.is_builtin_compression_eligible",
        lambda **_kwargs: True,
    )
    result = schedule_tool_wait_candidate(
        agent,
        [{"role": "user", "content": "old"}, {"role": "user", "content": "tail"}],
        800,
    )
    assert result == "blocked_cooldown"
    assert manager.calls == []


def test_schedule_gate_skips_after_ineffective_passes(monkeypatch):
    manager = Manager()
    agent = _agent(manager)
    agent.context_compressor.should_compress_info = lambda _tokens: (
        False,
        "ineffective",
    )
    monkeypatch.setattr(
        "agent.speculative_compression.is_builtin_compression_eligible",
        lambda **_kwargs: True,
    )
    result = schedule_tool_wait_candidate(
        agent,
        [{"role": "user", "content": "old"}, {"role": "user", "content": "tail"}],
        800,
    )
    assert result == "blocked_ineffective"
    assert manager.calls == []
