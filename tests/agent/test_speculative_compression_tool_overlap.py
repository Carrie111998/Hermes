from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import agent.conversation_loop as conversation_loop
import agent.speculative_compression as speculative
from agent.speculative_compression import (
    SpeculativeCandidate,
    SpeculativeCompressionManager,
    SpeculativeCompressionSettings,
    configure_speculative_compression,
    fingerprint_messages,
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
        self.restored = []

    def maybe_start(self, session_id, snapshot, factory, **kwargs):
        self.calls.append((session_id, snapshot, factory, kwargs))
        return "started"

    def take_matching_candidate(self, session_id, messages, **kwargs):
        self.calls.append((session_id, messages, kwargs))
        return self.candidate

    def restore_candidate(self, session_id, candidate):
        self.restored.append((session_id, candidate))


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


def test_disabled_speculation_skips_request_token_estimation(monkeypatch):
    agent = _agent(Manager())
    agent.speculative_compression_enabled = False
    agent.tools = []

    def fail_estimate(*_args, **_kwargs):
        raise AssertionError("disabled speculation must not estimate tokens")

    monkeypatch.setattr(
        conversation_loop, "estimate_request_tokens_rough", fail_estimate
    )

    assert (
        conversation_loop._schedule_speculative_tool_wait(
            agent, [{"role": "user", "content": "text"}], "system"
        )
        == "disabled"
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


def test_tool_wait_epoch_fence_drops_snapshot_before_manager_start(monkeypatch):
    manager = Manager()
    agent = _agent(manager)
    monkeypatch.setattr(
        "agent.speculative_compression.is_builtin_compression_eligible",
        lambda **_kwargs: True,
    )
    messages = [
        {"role": "user", "content": "old"},
        {"role": "user", "content": "tail"},
    ]
    capture_started = threading.Event()
    release_capture = threading.Event()
    real_capture = speculative.capture_snapshot

    def blocked_capture(*args, **kwargs):
        capture_started.set()
        agent.speculative_compression_enabled = False
        agent._speculative_epoch = 1
        assert release_capture.wait(2)
        return real_capture(*args, **kwargs)

    monkeypatch.setattr(speculative, "capture_snapshot", blocked_capture)
    result = []

    thread = threading.Thread(
        target=lambda: result.append(
            schedule_tool_wait_candidate(agent, messages, 800)
        )
    )
    thread.start()
    assert capture_started.wait(2)
    release_capture.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert result == ["blocked_off"]
    assert manager.calls == []


def test_off_waits_for_scheduler_already_at_manager_start(monkeypatch):
    """Off must serialize with the final scheduler start, not merely snapshot capture."""
    manager = Manager()
    agent = _agent(manager)
    monkeypatch.setattr(
        "agent.speculative_compression.is_builtin_compression_eligible",
        lambda **_kwargs: True,
    )
    start_entered = threading.Event()
    release_start = threading.Event()
    off_returned = threading.Event()

    def blocked_start(*args, **kwargs):
        start_entered.set()
        assert release_start.wait(2)
        return "started"

    manager.maybe_start = blocked_start
    scheduled = []
    scheduler = threading.Thread(
        target=lambda: scheduled.append(
            schedule_tool_wait_candidate(
                agent,
                [
                    {"role": "user", "content": "old"},
                    {"role": "user", "content": "tail"},
                ],
                800,
            )
        )
    )
    scheduler.start()
    assert start_entered.wait(2)

    off_thread = threading.Thread(
        target=lambda: (
            configure_speculative_compression(agent, False), off_returned.set()
        )
    )
    off_thread.start()
    assert not off_returned.wait(0.1)

    release_start.set()
    scheduler.join(timeout=2)
    off_thread.join(timeout=2)

    assert not scheduler.is_alive()
    assert not off_thread.is_alive()
    assert scheduled == ["started"]
    assert off_returned.is_set()
    assert agent.speculative_compression_enabled is False


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


def test_claimed_epoch_is_forwarded_and_cleared_around_install(monkeypatch):
    class ReadyCandidate:
        def is_expired(self, _max_age):
            return False

    manager = Manager(ReadyCandidate())
    agent = _agent(manager)
    claimed_epochs = []

    def install(*args, **kwargs):
        claimed_epochs.append(getattr(agent, "_speculative_epoch_claimed", None))
        agent._speculative_install_status = "installed"
        return ["compressed"], "sys"

    agent._compress_context = install
    monkeypatch.setattr(
        "agent.speculative_compression.is_builtin_compression_eligible",
        lambda **_kwargs: True,
    )
    messages = [{"role": "user", "content": "current"}]

    result = _try_install_speculative_candidate(agent, messages, "sys", 900, "task-1")

    assert result == (["compressed"], "sys", True, True)
    assert claimed_epochs == [0]
    assert agent._speculative_epoch_claimed is None


def test_candidate_taken_before_off_is_dropped_without_restoring(monkeypatch):
    class ReadyCandidate:
        def is_expired(self, _max_age):
            return False

    manager = Manager(ReadyCandidate())
    agent = _agent(manager)

    def take_and_disable(*args, **kwargs):
        candidate = Manager.take_matching_candidate(manager, *args, **kwargs)
        agent.speculative_compression_enabled = False
        return candidate

    manager.take_matching_candidate = take_and_disable
    agent._compress_context = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("disabled candidate must not reach the install path")
    )
    monkeypatch.setattr(
        "agent.speculative_compression.is_builtin_compression_eligible",
        lambda **_kwargs: True,
    )
    messages = [{"role": "user", "content": "current"}]

    result = _try_install_speculative_candidate(agent, messages, "sys", 900, "task-1")

    assert result == (messages, None, False, True)
    assert manager.restored == []
    assert agent._speculative_install_status != "installed"


def test_off_cancels_claimed_candidate_without_parking_it(monkeypatch):
    manager = SpeculativeCompressionManager(max_workers=1)
    messages = [
        {"role": "user", "content": "old"},
        {"role": "user", "content": "current"},
    ]
    candidate = SpeculativeCandidate(
        session_id="session-1",
        source_fingerprint=fingerprint_messages(messages[:1]),
        boundary_fingerprint=fingerprint_messages(messages[1:]),
        cut_index=1,
        compress_start=0,
        original_count=len(messages),
        compressed_prefix=({"role": "user", "content": "summary"},),
        created_at=time.monotonic(),
    )
    manager.restore_candidate("session-1", candidate)
    restore = MagicMock(wraps=manager.restore_candidate)
    manager.restore_candidate = restore
    agent = _agent(manager)
    agent.compression_enabled = True

    real_take = manager.take_matching_candidate

    def take_and_disable(*args, **kwargs):
        claimed = real_take(*args, **kwargs)
        configure_speculative_compression(agent, False)
        return claimed

    manager.take_matching_candidate = take_and_disable
    monkeypatch.setattr(
        "agent.speculative_compression.is_builtin_compression_eligible",
        lambda **_kwargs: True,
    )
    try:
        result = _try_install_speculative_candidate(
            agent, messages, "sys", 900, "task-1"
        )

        assert result == (messages, None, False, True)
        assert "session-1" not in manager._entries
        assert manager.take_matching_candidate("session-1", messages) is None
        restore.assert_not_called()
    finally:
        manager.shutdown()


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
    agent.context_compressor._automatic_compression_blocked = lambda: True
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


def test_schedule_gate_ignores_below_normal_threshold_when_unblocked(monkeypatch):
    """The breaker gate must not mistake 'below normal threshold' for a
    block — scheduling happens above the speculative soft trigger, which can
    sit below a raised compression.threshold."""
    manager = Manager()
    agent = _agent(manager)
    # should_compress_info would report no block below the normal threshold;
    # the breaker itself says unblocked, so scheduling proceeds.
    agent.context_compressor.should_compress_info = lambda _tokens: (
        False,
        None,
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
    assert result == "started"
    assert len(manager.calls) == 1
