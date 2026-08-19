import json
import math
from pathlib import Path
from types import SimpleNamespace

from agent.session_events import (
    SessionEventRecorder,
    configure_agent_event_recorder,
    digest_trace_file,
    emit_agent_event,
    resolve_hermes_trace_binary,
    summarize_trace_file,
    start_agent_turn_trace,
    start_agent_step_trace,
    verify_trace_file,
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


def test_trace_utility_helpers_call_hermes_trace_with_safe_argv(tmp_path: Path):
    trace = tmp_path / "session.jsonl"
    trace.write_text("", encoding="utf-8")
    calls = []

    def runner(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

    assert verify_trace_file(trace, binary="hermes-trace", runner=runner) is True
    assert calls == [
        (
            ["hermes-trace", "verify", str(trace)],
            {"check": False, "capture_output": True, "text": True, "timeout": 30.0},
        )
    ]


def test_resolve_hermes_trace_binary_prefers_repo_release(tmp_path: Path):
    binary_name = "hermes-trace.exe"
    release_binary = tmp_path / "crates" / "hermes-trace" / "target" / "release" / binary_name
    release_binary.parent.mkdir(parents=True)
    release_binary.write_text("", encoding="utf-8")
    start = tmp_path / "agent" / "session_events.py"

    assert resolve_hermes_trace_binary(start=start, binary_name=binary_name) == str(release_binary)


def test_resolve_hermes_trace_binary_uses_environment_override(tmp_path: Path):
    env_binary = tmp_path / "custom" / "hermes-trace"
    release_binary = tmp_path / "crates" / "hermes-trace" / "target" / "release" / "hermes-trace"
    release_binary.parent.mkdir(parents=True)
    release_binary.write_text("", encoding="utf-8")

    assert resolve_hermes_trace_binary(
        start=tmp_path,
        binary_name="hermes-trace",
        environ={"HERMES_TRACE_BINARY": str(env_binary)},
    ) == str(env_binary)


def test_resolve_hermes_trace_binary_explicit_binary_wins_over_environment(tmp_path: Path):
    explicit = tmp_path / "explicit" / "hermes-trace"
    env_binary = tmp_path / "env" / "hermes-trace"

    assert resolve_hermes_trace_binary(
        binary=explicit,
        environ={"HERMES_TRACE_BINARY": str(env_binary)},
    ) == str(explicit)


def test_resolve_hermes_trace_binary_falls_back_to_path_name(tmp_path: Path):
    assert (
        resolve_hermes_trace_binary(start=tmp_path, binary_name="hermes-trace")
        == "hermes-trace"
    )


def test_resolve_hermes_trace_binary_uses_explicit_path_lookup(tmp_path: Path):
    path_binary = tmp_path / "bin" / "hermes-trace"

    assert (
        resolve_hermes_trace_binary(
            start=tmp_path,
            binary_name="hermes-trace",
            path_lookup=lambda name: str(path_binary) if name == "hermes-trace" else None,
        )
        == str(path_binary)
    )


def test_trace_summary_and_digest_helpers_parse_cli_output(tmp_path: Path):
    trace = tmp_path / "session.jsonl"
    trace.write_text("", encoding="utf-8")

    def runner(args, **kwargs):
        if args[1] == "summary":
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    '{"turns":1,"steps":2,"tool_calls":3,"tool_errors":4,'
                    '"input_tokens":5,"output_tokens":6}\n'
                ),
                stderr="",
            )
        if args[1] == "digest":
            return SimpleNamespace(returncode=0, stdout="a" * 64 + "\n", stderr="")
        raise AssertionError(args)

    assert summarize_trace_file(trace, runner=runner) == {
        "turns": 1,
        "steps": 2,
        "tool_calls": 3,
        "tool_errors": 4,
        "input_tokens": 5,
        "output_tokens": 6,
    }
    assert digest_trace_file(trace, runner=runner) == "a" * 64


def test_summarize_trace_file_rejects_non_integer_summary_values(tmp_path: Path):
    trace = tmp_path / "session.jsonl"
    trace.write_text("", encoding="utf-8")

    def runner(args, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=(
                '{"turns":true,"steps":"2","tool_calls":3,"tool_errors":4,'
                '"input_tokens":5,"output_tokens":6}\n'
            ),
            stderr="",
        )

    try:
        summarize_trace_file(trace, runner=runner)
    except ValueError as exc:
        assert "turns" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_summarize_trace_file_rejects_negative_summary_values(tmp_path: Path):
    trace = tmp_path / "session.jsonl"
    trace.write_text("", encoding="utf-8")

    def runner(args, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=(
                '{"turns":1,"steps":2,"tool_calls":3,"tool_errors":4,'
                '"input_tokens":-1,"output_tokens":6}\n'
            ),
            stderr="",
        )

    try:
        summarize_trace_file(trace, runner=runner)
    except ValueError as exc:
        assert "input_tokens" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_summarize_trace_file_rejects_missing_summary_fields(tmp_path: Path):
    trace = tmp_path / "session.jsonl"
    trace.write_text("", encoding="utf-8")

    def runner(args, **kwargs):
        return SimpleNamespace(returncode=0, stdout='{"turns":1}\n', stderr="")

    try:
        summarize_trace_file(trace, runner=runner)
    except ValueError as exc:
        assert "steps" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_trace_utility_helpers_raise_on_cli_failure(tmp_path: Path):
    trace = tmp_path / "session.jsonl"
    trace.write_text("", encoding="utf-8")

    def runner(args, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="bad trace\n")

    try:
        verify_trace_file(trace, runner=runner)
    except RuntimeError as exc:
        assert "bad trace" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def test_trace_utility_helpers_reject_non_positive_timeout_before_subprocess(tmp_path: Path):
    trace = tmp_path / "session.jsonl"
    trace.write_text("", encoding="utf-8")

    def runner(args, **kwargs):
        raise AssertionError("runner should not be called")

    try:
        verify_trace_file(trace, timeout=0, runner=runner)
    except ValueError as exc:
        assert "timeout" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_trace_utility_helpers_reject_non_finite_timeout_before_subprocess(tmp_path: Path):
    trace = tmp_path / "session.jsonl"
    trace.write_text("", encoding="utf-8")

    def runner(args, **kwargs):
        raise AssertionError("runner should not be called")

    for timeout in (math.nan, math.inf, "nan", "inf"):
        try:
            verify_trace_file(trace, timeout=timeout, runner=runner)
        except ValueError as exc:
            assert "timeout" in str(exc)
        else:
            raise AssertionError(f"expected ValueError for timeout={timeout!r}")
