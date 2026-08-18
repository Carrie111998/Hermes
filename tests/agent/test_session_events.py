import json
from pathlib import Path
from types import SimpleNamespace

from agent.session_events import (
    SessionEventRecorder,
    configure_agent_event_recorder,
    emit_agent_event,
    start_agent_turn_trace,
    start_agent_step_trace,
)


def test_recorder_appends_monotonic_versioned_events(tmp_path: Path):
    path = tmp_path / "session.jsonl"
    recorder = SessionEventRecorder(path, "session-1", clock=lambda: 123.5)

    recorder.append("turn/start", {"message": "hello"}, turn=1)
    recorder.append("step/start", {}, turn=1, step=1)

    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [event["seq"] for event in events] == [1, 2]
    assert events[0] == {
        "schema_version": 1,
        "seq": 1,
        "time": 123.5,
        "session_id": "session-1",
        "type": "turn/start",
        "turn": 1,
        "data": {"message": "hello"},
    }
    assert events[1]["step"] == 1


def test_recorder_redacts_secrets_before_writing(tmp_path: Path):
    path = tmp_path / "session.jsonl"
    recorder = SessionEventRecorder(path, "session-1", clock=lambda: 1.0)

    recorder.append(
        "request/header",
        {
            "provider": "local",
            "api_key": "secret-value",
            "headers": {"Authorization": "Bearer secret", "x-safe": "ok"},
        },
        turn=1,
        step=1,
    )

    event = json.loads(path.read_text(encoding="utf-8"))
    assert event["data"]["api_key"] == "[REDACTED]"
    assert event["data"]["headers"]["Authorization"] == "[REDACTED]"
    assert event["data"]["headers"]["x-safe"] == "ok"


def test_recorder_reopens_at_next_sequence_number(tmp_path: Path):
    path = tmp_path / "session.jsonl"
    SessionEventRecorder(path, "session-1", clock=lambda: 1.0).append("turn/start", {}, turn=1)

    recorder = SessionEventRecorder(path, "session-1", clock=lambda: 2.0)
    recorder.append("turn/end", {}, turn=1)

    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [event["seq"] for event in events] == [1, 2]


def test_emit_agent_event_uses_injected_recorder_and_never_raises(tmp_path: Path):
    path = tmp_path / "session.jsonl"
    recorder = SessionEventRecorder(path, "session-1", clock=lambda: 3.0)
    agent = SimpleNamespace(
        _session_event_recorder=recorder,
        _trajectory_turn_number=2,
        _trajectory_step_number=4,
    )

    assert emit_agent_event(agent, "assistant/message", {"text": "done"}) is True

    event = json.loads(path.read_text(encoding="utf-8"))
    assert event["turn"] == 2
    assert event["step"] == 4

    agent._session_event_recorder = object()
    assert emit_agent_event(agent, "turn/end", {}) is False
    assert agent._session_event_recorder is None


def test_configure_recorder_is_opt_in_and_profile_scoped(tmp_path: Path):
    agent = SimpleNamespace(session_id="session/unsafe", _session_event_recorder=None)

    assert configure_agent_event_recorder(agent, enabled=False, home=tmp_path) is None

    recorder = configure_agent_event_recorder(agent, enabled=True, home=tmp_path)
    assert recorder is agent._session_event_recorder
    assert recorder.path == tmp_path / "trajectories" / "events" / "session_unsafe.jsonl"


def test_turn_and_step_helpers_attach_stable_coordinates(tmp_path: Path):
    agent = SimpleNamespace(session_id="session-1", platform="desktop", provider="local")

    start_agent_turn_trace(
        agent,
        turn_id="turn-a",
        user_message="hello",
        enabled=True,
        home=tmp_path,
    )
    start_agent_step_trace(agent, step=1, previous_tools=[{"name": "read_file"}])

    events = [
        json.loads(line)
        for line in (tmp_path / "trajectories" / "events" / "session-1.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [(event["type"], event["turn"], event.get("step")) for event in events] == [
        ("turn/start", 1, None),
        ("step/start", 1, 1),
    ]
    assert events[0]["data"]["turn_id"] == "turn-a"
    assert events[1]["data"]["previous_tools"] == [{"name": "read_file"}]
