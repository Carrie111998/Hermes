import json
import math
import os
from pathlib import Path
from types import SimpleNamespace

import agent.session_events as session_events
from agent.session_events import (
    SessionEventRecorder,
    configure_agent_event_recorder,
    digest_trace_file,
    emit_agent_event,
    redact_trace_data,
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


def test_trace_redaction_covers_shareable_credentials_and_embedded_secrets():
    nsec = "nsec1" + "q" * 58
    pem = "-----BEGIN PRIVATE KEY-----\nABCDEF\n-----END PRIVATE KEY-----"
    api_key = "sk-" + "A" * 24

    redacted = redact_trace_data(
        {
            "private_key": "private-value",
            "wallet_private_key_hex": "f" * 64,
            "wallet_nsec": nsec,
            "mnemonic": "abandon " * 11 + "about",
            "seed_phrase": "alpha beta gamma delta",
            "content": f"before {nsec} after {api_key}",
            "result": pem,
        }
    )

    assert redacted["private_key"] == "[REDACTED]"
    assert redacted["wallet_private_key_hex"] == "[REDACTED]"
    assert redacted["wallet_nsec"] == "[REDACTED]"
    assert redacted["mnemonic"] == "[REDACTED]"
    assert redacted["seed_phrase"] == "[REDACTED]"
    assert nsec not in redacted["content"]
    assert api_key not in redacted["content"]
    assert "before" in redacted["content"]
    assert "after" in redacted["content"]
    assert pem not in redacted["result"]


def test_trace_redaction_covers_private_keys_inside_serialized_json_text():
    private_key_hex = "f" * 64
    serialized = json.dumps({"private_key_hex": private_key_hex})

    redacted = redact_trace_data({"result": serialized})

    assert private_key_hex not in redacted["result"]
    assert "[REDACTED]" in redacted["result"]


def test_recorder_reopens_at_next_sequence_number(tmp_path: Path):
    path = tmp_path / "session.jsonl"
    SessionEventRecorder(path, "session-1", clock=lambda: 1.0).append("turn/start", {}, turn=1)

    recorder = SessionEventRecorder(path, "session-1", clock=lambda: 2.0)
    recorder.append("turn/end", {}, turn=1)

    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [event["seq"] for event in events] == [1, 2]


def test_two_recorder_instances_allocate_unique_sequences(tmp_path: Path):
    path = tmp_path / "session.jsonl"
    first = SessionEventRecorder(path, "session-1", clock=lambda: 1.0)
    second = SessionEventRecorder(path, "session-1", clock=lambda: 2.0)

    first.append("turn/start", {}, turn=1)
    second.append("turn/end", {}, turn=1)

    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [event["seq"] for event in events] == [1, 2]


def test_interprocess_lock_contention_is_bounded(tmp_path: Path, monkeypatch):
    if os.name == "nt":
        import msvcrt

        monkeypatch.setattr(msvcrt, "locking", lambda *_args: (_ for _ in ()).throw(OSError("busy")))
    else:
        import fcntl

        monkeypatch.setattr(fcntl, "flock", lambda *_args: (_ for _ in ()).throw(OSError("busy")))

    try:
        with session_events._interprocess_path_lock(tmp_path / "session.jsonl", timeout=0):
            pass
    except TimeoutError:
        pass
    else:
        raise AssertionError("expected lock contention to time out")


def test_recorder_reopen_reuses_persistent_validation_checkpoint(tmp_path: Path, monkeypatch):
    path = tmp_path / "session.jsonl"
    events = [
        {
            "schema_version": 1,
            "seq": seq,
            "time": 1.0,
            "session_id": "session-1",
            "type": "step/start",
            "data": {},
        }
        for seq in range(1, 101)
    ]
    path.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n\n",
        encoding="utf-8",
    )
    real_loads = session_events.json.loads
    loads_calls = 0

    def counting_loads(value):
        nonlocal loads_calls
        loads_calls += 1
        return real_loads(value)

    monkeypatch.setattr(session_events.json, "loads", counting_loads)

    first = SessionEventRecorder(path, "session-1")
    first_loads_calls = loads_calls
    session_events._validated_sequences.clear()
    second = SessionEventRecorder(path, "session-1")

    assert first._seq == 100
    assert second._seq == 100
    assert first_loads_calls == 100
    assert loads_calls == first_loads_calls + 2


def test_recorder_reopen_ignores_wrong_type_validation_checkpoint(tmp_path: Path):
    path = tmp_path / "session.jsonl"
    first = SessionEventRecorder(path, "session-1")
    first.append("turn/start", {})
    first._checkpoint_path.write_text("[]", encoding="utf-8")
    session_events._validated_sequences.clear()

    reopened = SessionEventRecorder(path, "session-1")
    reopened.append("turn/end", {})

    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [event["seq"] for event in events] == [1, 2]


def test_recorder_reopen_does_not_trust_checkpoint_sequence_over_trace_tail(tmp_path: Path):
    path = tmp_path / "session.jsonl"
    first = SessionEventRecorder(path, "session-1")
    first.append("turn/start", {})
    checkpoint = json.loads(first._checkpoint_path.read_text(encoding="utf-8"))
    checkpoint["seq"] = 999
    first._checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
    session_events._validated_sequences.clear()

    reopened = SessionEventRecorder(path, "session-1")

    assert reopened._seq == 1


def test_append_revalidates_full_history_after_external_mutation(tmp_path: Path):
    path = tmp_path / "session.jsonl"
    recorder = SessionEventRecorder(path, "session-1")
    recorder.append("turn/start", {})
    recorder.append("turn/end", {})
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    events[0]["seq"] = 9
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

    try:
        recorder.append("step/start", {})
    except ValueError as exc:
        assert "non-monotonic" in str(exc)
    else:
        raise AssertionError("expected externally mutated history to fail append")


def test_historical_sequence_and_owner_types_are_strict(tmp_path: Path):
    path = tmp_path / "session.jsonl"
    invalid_events = [
        {"seq": True, "session_id": "session-1"},
        {"seq": "1", "session_id": "session-1"},
        {"seq": 1, "session_id": 123},
    ]

    for event in invalid_events:
        path.write_text(json.dumps(event) + "\n", encoding="utf-8")
        session_events._validated_sequences.clear()
        try:
            SessionEventRecorder(path, "session-1")
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected strict history validation for {event!r}")


def test_recorder_reopen_rejects_invalid_earlier_history(tmp_path: Path):
    path = tmp_path / "session.jsonl"
    first = {"seq": 1, "session_id": "session-1"}
    last = {"seq": 2, "session_id": "session-1"}
    path.write_text(f"{json.dumps(first)}\n{{invalid}}\n{json.dumps(last)}\n", encoding="utf-8")

    try:
        SessionEventRecorder(path, "session-1")
    except ValueError as exc:
        assert "line 2" in str(exc)
    else:
        raise AssertionError("expected invalid history to fail recorder construction")


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
    assert recorder.path.parent == tmp_path / "trajectories" / "events"
    assert recorder.path.name.startswith("session_unsafe~")
    assert recorder.path.suffix == ".jsonl"


def test_sanitized_session_ids_cannot_share_a_trace_path(tmp_path: Path):
    unsafe = SimpleNamespace(session_id="a/b", _session_event_recorder=None)
    literal = SimpleNamespace(session_id="a_b", _session_event_recorder=None)

    unsafe_recorder = configure_agent_event_recorder(unsafe, enabled=True, home=tmp_path)
    literal_recorder = configure_agent_event_recorder(literal, enabled=True, home=tmp_path)

    assert unsafe_recorder is not None
    assert literal_recorder is not None
    assert unsafe_recorder.path != literal_recorder.path
    assert literal_recorder.path.name == "a_b.jsonl"


def test_sanitized_session_id_resumes_matching_legacy_trace(tmp_path: Path):
    events_dir = tmp_path / "trajectories" / "events"
    legacy_path = events_dir / "a_b.jsonl"
    SessionEventRecorder(legacy_path, "a/b").append("turn/start", {})
    agent = SimpleNamespace(session_id="a/b", _session_event_recorder=None)

    recorder = configure_agent_event_recorder(agent, enabled=True, home=tmp_path)
    assert recorder is not None
    recorder.append("turn/end", {})

    assert recorder.path == legacy_path
    events = [json.loads(line) for line in legacy_path.read_text(encoding="utf-8").splitlines()]
    assert [event["seq"] for event in events] == [1, 2]


def test_safe_session_id_avoids_legacy_trace_owned_by_another_session(tmp_path: Path):
    events_dir = tmp_path / "trajectories" / "events"
    legacy_path = events_dir / "a_b.jsonl"
    SessionEventRecorder(legacy_path, "a/b").append("turn/start", {})
    agent = SimpleNamespace(session_id="a_b", _session_event_recorder=None)

    recorder = configure_agent_event_recorder(agent, enabled=True, home=tmp_path)

    assert recorder is not None
    assert recorder.path != legacy_path


def test_recorder_rejects_history_owned_by_another_session(tmp_path: Path):
    path = tmp_path / "session.jsonl"
    SessionEventRecorder(path, "session-a").append("turn/start", {})

    try:
        SessionEventRecorder(path, "session-b")
    except ValueError as exc:
        assert "session mismatch" in str(exc)
    else:
        raise AssertionError("expected mismatched history ownership to fail")


def test_case_variant_session_ids_cannot_collide_on_windows_filesystems(tmp_path: Path):
    upper = SimpleNamespace(session_id="ABC", _session_event_recorder=None)
    lower = SimpleNamespace(session_id="abc", _session_event_recorder=None)

    upper_recorder = configure_agent_event_recorder(upper, enabled=True, home=tmp_path)
    lower_recorder = configure_agent_event_recorder(lower, enabled=True, home=tmp_path)

    assert upper_recorder is not None
    assert lower_recorder is not None
    assert upper_recorder.path.name.lower() != lower_recorder.path.name.lower()


def test_configure_recorder_explicit_disable_detaches_existing_recorder(tmp_path: Path):
    agent = SimpleNamespace(session_id="session-1", _session_event_recorder=None)
    recorder = configure_agent_event_recorder(agent, enabled=True, home=tmp_path)

    assert recorder is not None
    assert configure_agent_event_recorder(agent, enabled=False, home=tmp_path) is None
    assert agent._session_event_recorder is None


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
