from types import SimpleNamespace

from agent.transports import claude_cli


class FakeSession:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.calls = []

    def run_turn(self, text, on_delta=None):
        self.calls.append(text)
        if on_delta:
            on_delta("one")
            on_delta("two")
        return claude_cli.ClaudeCLITurnResult(
            final_text="onetwo",
            usage={"input_tokens": 3, "output_tokens": 2},
            session_id="opaque",
            generation=1,
        )

    def close(self):
        return None


def _agent(**overrides):
    values = {
        "model": "claude-opus-5",
        "provider": "claude-cli",
        "base_url": "",
        "session_cwd": ".",
        "_claude_cli_session": None,
        "_session_db": None,
        "_fire_stream_delta": lambda text: None,
        "_sync_external_memory_for_turn": lambda **kwargs: None,
        "_iters_since_skill": 0,
        "_interrupt_requested": False,
        "_interrupt_message": None,
    }
    values.update(overrides)
    agent = SimpleNamespace(**values)

    def clear_interrupt():
        agent._interrupt_requested = False
        agent._interrupt_message = None

    agent.clear_interrupt = clear_interrupt
    return agent


def test_special_turn_streams_persists_and_preserves_role_alternation(monkeypatch):
    monkeypatch.setattr(claude_cli, "ClaudeCLISession", FakeSession)
    deltas = []
    persisted = []
    agent = SimpleNamespace(
        model="claude-opus-5",
        provider="claude-cli",
        base_url="",
        session_cwd=".",
        _claude_cli_session=None,
        _session_db=object(),
        _fire_stream_delta=deltas.append,
        _flush_messages_to_session_db=lambda messages: persisted.append(list(messages)),
        _sync_external_memory_for_turn=lambda **kwargs: None,
        _iters_since_skill=0,
    )
    messages = [{"role": "user", "content": "input"}]
    result = claude_cli.run_claude_cli_turn(
        agent,
        user_message="input",
        original_user_message="input",
        messages=messages,
        effective_task_id="task",
    )
    assert deltas == ["one", "two"]
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[-1]["content"] == "onetwo"
    assert persisted and persisted[-1] == messages
    assert result["completed"] is True
    assert result["prompt_tokens"] == 3
    assert result["completion_tokens"] == 2
    assert agent._last_turn_usage["total_tokens"] == 5


def test_interrupted_turn_result_is_propagated_and_skips_memory(monkeypatch):
    memory_calls = []

    class InterruptedSession(FakeSession):
        def run_turn(self, text, on_delta=None):
            del text, on_delta
            return claude_cli.ClaudeCLITurnResult(
                final_text="partial",
                generation=1,
                interrupted=True,
            )

    monkeypatch.setattr(claude_cli, "ClaudeCLISession", InterruptedSession)
    agent = _agent(
        _interrupt_requested=True,
        _interrupt_message="stop now",
        _sync_external_memory_for_turn=lambda **kwargs: memory_calls.append(kwargs),
    )
    messages = [{"role": "user", "content": "input"}]

    result = claude_cli.run_claude_cli_turn(
        agent,
        user_message="input",
        original_user_message="input",
        messages=messages,
        effective_task_id="task",
    )

    assert result["completed"] is False
    assert result["partial"] is True
    assert result["interrupted"] is True
    assert result["interrupt_message"] == "stop now"
    assert agent._interrupt_requested is False
    assert memory_calls == []


def test_successful_non_interrupted_turn_clears_late_interrupt(monkeypatch):
    monkeypatch.setattr(claude_cli, "ClaudeCLISession", FakeSession)
    agent = _agent(_interrupt_requested=True, _interrupt_message="late")

    result = claude_cli.run_claude_cli_turn(
        agent,
        user_message="input",
        original_user_message="input",
        messages=[{"role": "user", "content": "input"}],
        effective_task_id="task",
    )

    assert result["completed"] is True
    assert result["interrupted"] is False
    assert agent._interrupt_requested is False


def test_busy_rejection_does_not_retire_runtime_session():
    class BusySession:
        close_calls = 0

        def run_turn(self, text, on_delta=None):
            del text, on_delta
            raise claude_cli.ClaudeCLIBusyError("active")

        def close(self):
            self.close_calls += 1

    session = BusySession()
    agent = _agent(_claude_cli_session=session)
    result = claude_cli.run_claude_cli_turn(
        agent,
        user_message="input",
        original_user_message="input",
        messages=[{"role": "user", "content": "input"}],
        effective_task_id="task",
    )

    assert result["completed"] is False
    assert session.close_calls == 0
    assert agent._claude_cli_session is session
