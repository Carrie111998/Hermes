from __future__ import annotations

from types import SimpleNamespace

import agent.speculative_compression as speculative
from agent.speculative_compression import (
    configure_speculative_compression,
    schedule_tool_wait_candidate,
    speculative_compression_status,
)


class Compressor:
    context_length = 1_000
    max_tokens = 100

    def _protect_head_size(self, _messages):
        return 0

    def _find_tail_cut_by_tokens(self, messages, _head_end):
        return len(messages) - 1

    def _align_boundary_forward(self, _messages, cut):
        return cut


class Manager:
    def __init__(self):
        self.started = []
        self.invalidated = []

    def maybe_start(self, session_id, snapshot, factory, **kwargs):
        self.started.append((session_id, snapshot, factory, kwargs))
        return "started"

    def invalidate_session(self, session_id):
        self.invalidated.append(session_id)


def test_runtime_switch_wires_scheduling_and_invalidates(monkeypatch):
    manager = Manager()
    agent = SimpleNamespace(
        compression_enabled=True,
        speculative_compression_enabled=False,
        context_compressor=Compressor(),
        api_mode="chat_completions",
        session_id="session-1",
    )
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"compression": {"speculative": {"enabled": False}}},
    )
    monkeypatch.setattr(speculative, "get_default_manager", lambda: manager)
    monkeypatch.setattr(
        speculative,
        "is_builtin_compression_eligible",
        lambda **_kwargs: True,
    )

    assert configure_speculative_compression(agent, True) is True
    assert agent.speculative_compression_enabled is True
    assert agent.speculative_compression_settings.enabled is True
    assert agent._speculative_compression_manager is manager
    assert "enabled=True" in speculative_compression_status(agent)
    assert "eligible=True" in speculative_compression_status(agent)
    assert "api_mode=chat_completions" in speculative_compression_status(agent)
    assert "context_engine=Compressor" in speculative_compression_status(agent)

    messages = [
        {"role": "user", "content": "old"},
        {"role": "user", "content": "current"},
    ]
    assert schedule_tool_wait_candidate(agent, messages, 800) == "started"
    assert len(manager.started) == 1

    assert configure_speculative_compression(agent, False) is False
    assert agent.speculative_compression_enabled is False
    assert manager.invalidated == ["session-1"]
    assert schedule_tool_wait_candidate(agent, messages, 800) == "disabled"
    assert len(manager.started) == 1
