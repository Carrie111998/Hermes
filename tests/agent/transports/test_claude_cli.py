from __future__ import annotations

import io
import json
import subprocess
import threading
from types import SimpleNamespace

import pytest

from agent.transports.claude_cli import (
    CLAUDE_CLI_FORBIDDEN_FLAGS,
    ClaudeCLIBusyError,
    ClaudeCLIClient,
    ClaudeCLIError,
    ClaudeCLISession,
    ClaudeCLITransport,
    build_claude_cli_argv,
    build_claude_cli_env,
    check_claude_cli_available,
    parse_claude_cli_help,
    parse_claude_cli_version,
)


def _line(event):
    if isinstance(event, bytes):
        return event + b"\n"
    return json.dumps(event).encode() + b"\n"


def _turn(session_id, text, *, include_init=False):
    events = []
    if include_init:
        events.append(
            {"type": "system", "subtype": "init", "session_id": session_id, "tools": []}
        )
    events.extend(
        [
            {"type": "user", "session_id": session_id},
            {
                "type": "stream_event",
                "session_id": session_id,
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": text},
                },
            },
            {
                "type": "result",
                "session_id": session_id,
                "is_error": False,
                "result": text,
                "usage": {"input_tokens": 2, "output_tokens": 1},
            },
        ]
    )
    return events


class FakeProcess:
    next_pid = 4000

    def __init__(self, events):
        type(self).next_pid += 1
        self.pid = type(self).next_pid
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO(b"".join(_line(event) for event in events))
        self.stderr = io.BytesIO()
        self.returncode = None
        self.terminate_calls = 0
        self.kill_calls = 0

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        del timeout
        if self.stdin.closed:
            self.returncode = 0
            return 0
        raise subprocess.TimeoutExpired("claude", 0)

    def terminate(self):
        self.terminate_calls += 1
        self.returncode = 0

    def kill(self):
        self.kill_calls += 1
        self.returncode = -9


def test_fixed_argv_is_exact_shell_safe_and_stage0_only():
    argv = build_claude_cli_argv("claude-opus-5")
    assert argv == [
        "claude",
        "-p",
        "--verbose",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--include-partial-messages",
        "--replay-user-messages",
        "--model",
        "claude-opus-5",
        "--tools",
        "",
        "--strict-mcp-config",
    ]
    assert not CLAUDE_CLI_FORBIDDEN_FLAGS.intersection(argv)
    with pytest.raises(ValueError, match="only model"):
        build_claude_cli_argv("claude-sonnet-5")


def test_env_strips_request_and_cli_backend_shaping_but_preserves_cli_oauth():
    env = build_claude_cli_env(
        {
            "HOME": "home-marker",
            "USERPROFILE": "profile-marker",
            "PATH": "path-marker",
            "ANTHROPIC_API_KEY": "secret-marker",
            "ANTHROPIC_AUTH_TOKEN": "secret-marker",
            "ANTHROPIC_BASE_URL": "endpoint-marker",
            "ANTHROPIC_MODEL": "model-marker",
            "ANTHROPIC_CUSTOM_SHAPING": "shape-marker",
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "CLAUDE_CODE_USE_VERTEX": "1",
            "CLAUDE_CODE_OAUTH_TOKEN": "oauth-marker",
        }
    )
    assert env["HOME"] == "home-marker"
    assert env["USERPROFILE"] == "profile-marker"
    assert env["PATH"] == "path-marker"
    assert not any(key.startswith("ANTHROPIC_") for key in env)
    assert "CLAUDE_CODE_USE_BEDROCK" not in env
    assert "CLAUDE_CODE_USE_VERTEX" not in env
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-marker"


def test_version_and_help_capability_parsers_fail_closed():
    assert parse_claude_cli_version("2.1.221 (Claude Code)") == (2, 1, 221)
    assert parse_claude_cli_version("unknown") is None
    help_text = " ".join(
        [
            "--input-format stream-json",
            "--output-format stream-json",
            "--include-partial-messages",
            "--replay-user-messages",
            "--model",
            "--tools",
            "--verbose",
            "--strict-mcp-config",
        ]
    )
    assert parse_claude_cli_help(help_text).stage0_ready is True
    assert parse_claude_cli_help("--input-format stream-json").stage0_ready is False


def test_availability_gate_checks_version_and_help_shell_free(monkeypatch):
    calls = []
    help_text = " ".join(
        [
            "--input-format stream-json",
            "--output-format stream-json",
            "--include-partial-messages",
            "--replay-user-messages",
            "--model",
            "--tools",
            "--verbose",
            "--strict-mcp-config",
        ]
    )

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        stdout = "2.1.221 (Claude Code)" if argv[-1] == "--version" else help_text
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr("agent.transports.claude_cli.subprocess.run", run)
    assert check_claude_cli_available() == (True, "2.1.221")
    assert [call[0] for call in calls] == [
        ["claude", "--version"],
        ["claude", "--help"],
    ]
    assert all(call[1]["shell"] is False for call in calls)


def test_real_spawn_path_fails_closed_when_capability_gate_fails(monkeypatch):
    monkeypatch.setattr(
        "agent.transports.claude_cli.check_claude_cli_available",
        lambda: (False, "capability unavailable"),
    )
    client = ClaudeCLIClient(env={"HOME": "x"})
    with pytest.raises(ClaudeCLIError, match="capability unavailable"):
        client.start()
    assert client.pid is None


def test_two_turns_use_same_process_and_session_with_ordered_deltas():
    process = FakeProcess(
        _turn("session-one", "A", include_init=True) + _turn("session-one", "B")
    )
    spawns = []

    def popen(argv, **kwargs):
        spawns.append((argv, kwargs))
        return process

    client = ClaudeCLIClient(popen_factory=popen, env={"HOME": "x"})
    deltas = []
    first = client.run_turn("first", on_delta=deltas.append)
    first_pid = client.pid
    assert client.is_alive()
    second = client.run_turn("second", on_delta=deltas.append)
    assert client.pid == first_pid
    assert first.session_id == second.session_id == "session-one"
    assert first.generation == second.generation == 1
    assert first.final_text == "A"
    assert second.final_text == "B"
    assert deltas == ["A", "B"]
    assert len(spawns) == 1
    assert spawns[0][1]["shell"] is False
    client.close()
    client.close()
    assert process.returncode == 0


def test_completed_session_refuses_silent_respawn_after_process_exit():
    first_process = FakeProcess(_turn("session-one", "first", include_init=True))
    replacement_process = FakeProcess(
        _turn("session-two", "replacement", include_init=True)
    )
    processes = [first_process, replacement_process]
    spawns = []

    def popen(argv, **kwargs):
        process = processes[len(spawns)]
        spawns.append((argv, kwargs))
        return process

    client = ClaudeCLIClient(popen_factory=popen, env={"HOME": "x"})
    assert client.run_turn("first").final_text == "first"
    first_process.returncode = 1

    with pytest.raises(ClaudeCLIError, match="refusing to respawn.*context"):
        client.run_turn("second")

    assert len(spawns) == 1
    assert client.completed_turns == 1
    client.close()


@pytest.mark.parametrize(
    ("replacement_events", "match"),
    [
        (_turn("respawned", "unexpected", include_init=False), "Stage-0 initialization"),
        (
            [
                {
                    "type": "system",
                    "subtype": "init",
                    "session_id": "respawned",
                    "tools": ["Read"],
                },
                {"type": "user", "session_id": "respawned"},
                {
                    "type": "result",
                    "session_id": "respawned",
                    "result": "unexpected",
                    "is_error": False,
                },
            ],
            "zero-tool",
        ),
    ],
)
def test_retry_generation_requires_fresh_zero_tool_initialization(
    replacement_events, match
):
    first_process = FakeProcess([])
    replacement_process = FakeProcess(replacement_events)
    processes = [first_process, replacement_process]
    spawns = []

    def popen(*args, **kwargs):
        del args, kwargs
        process = processes[len(spawns)]
        spawns.append(process)
        return process

    session = ClaudeCLISession(popen_factory=popen, env={"HOME": "x"})

    with pytest.raises(ClaudeCLIError, match=match):
        session.run_turn("input")

    assert spawns == processes
    assert session.client is not None
    assert session.client.generation == 2
    session.close()


@pytest.mark.parametrize(
    "events,match",
    [
        ([b"not-json"], "malformed"),
        (
            [
                {"type": "system", "subtype": "init", "session_id": "one", "tools": []},
                {"type": "user", "session_id": "one"},
                {"type": "result", "session_id": "two", "result": "x"},
            ],
            "correlation",
        ),
        (
            [
                {"type": "system", "subtype": "init", "session_id": "one", "tools": ["Read"]},
            ],
            "zero-tool",
        ),
    ],
)
def test_protocol_and_capability_failures_are_closed(events, match):
    client = ClaudeCLIClient(
        popen_factory=lambda *a, **k: FakeProcess(events), env={"HOME": "x"}
    )
    with pytest.raises(ClaudeCLIError, match=match):
        client.run_turn("input")
    client.close()


def test_pre_output_eof_retries_once_but_post_output_does_not():
    processes = [
        FakeProcess([]),
        FakeProcess(_turn("stable", "ok", include_init=True)),
    ]
    calls = []

    def factory(*args, **kwargs):
        del args, kwargs
        calls.append(1)
        return processes[len(calls) - 1]

    session = ClaudeCLISession(popen_factory=factory, env={"HOME": "x"})
    retried = session.run_turn("input")
    assert retried.final_text == "ok"
    assert retried.generation == 2
    assert len(calls) == 2
    session.close()

    partial = _turn("partial", "seen", include_init=True)[:-1]
    calls.clear()
    session = ClaudeCLISession(
        popen_factory=lambda *a, **k: (calls.append(1) or FakeProcess(partial)),
        env={"HOME": "x"},
    )
    with pytest.raises(ClaudeCLIError) as exc:
        session.run_turn("input")
    assert exc.value.visible_output is True
    assert len(calls) == 1


def test_busy_rejection_preserves_active_client_process_and_stdin():
    process = FakeProcess([])
    client = ClaudeCLIClient(popen_factory=lambda *a, **k: process, env={"HOME": "x"})
    client._proc = process
    client._generation = 1
    session = ClaudeCLISession(env={"HOME": "x"})
    session._client = client
    client._turn_lock.acquire()
    try:
        with pytest.raises(ClaudeCLIBusyError):
            session.run_turn("concurrent")

        assert session.client is client
        assert client._proc is process
        assert client.is_alive()
        assert process.stdin.closed is False
    finally:
        client._turn_lock.release()
        session.close()


def test_interrupt_during_pre_output_failure_never_respawns_or_resends(monkeypatch):
    entered = threading.Event()
    interrupted = threading.Event()
    instances = []

    class BlockingClient:
        def __init__(self, **kwargs):
            del kwargs
            self.completed_turns = 0
            self._cancelled = False
            self.calls = []
            instances.append(self)

        def run_turn(self, text, **kwargs):
            del kwargs
            self.calls.append(text)
            entered.set()
            assert interrupted.wait(timeout=1.0)
            raise ClaudeCLIError(
                "synthetic pipe closure", transient=True, category="pipe"
            )

        def request_interrupt(self):
            self._cancelled = True
            interrupted.set()

        def close(self):
            return None

    monkeypatch.setattr("agent.transports.claude_cli.ClaudeCLIClient", BlockingClient)
    session = ClaudeCLISession(env={"HOME": "x"})
    errors = []

    def run():
        try:
            session.run_turn("once")
        except ClaudeCLIError as exc:
            errors.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    assert entered.wait(timeout=1.0)
    session.request_interrupt()
    worker.join(timeout=1.0)

    assert not worker.is_alive()
    assert len(instances) == 1
    assert instances[0].calls == ["once"]
    assert len(errors) == 1


def test_close_racing_reader_thread_start_does_not_raise(monkeypatch):
    process = FakeProcess([])
    real_thread = threading.Thread
    second_reader_constructing = threading.Event()
    allow_second_reader = threading.Event()
    reader_count = 0

    def gated_reader_thread(*args, **kwargs):
        nonlocal reader_count
        reader_count += 1
        if reader_count == 2:
            second_reader_constructing.set()
            assert allow_second_reader.wait(timeout=1.0)
        return real_thread(*args, **kwargs)

    monkeypatch.setattr("agent.transports.claude_cli.threading.Thread", gated_reader_thread)
    client = ClaudeCLIClient(popen_factory=lambda *a, **k: process, env={"HOME": "x"})
    errors = []

    def call(method):
        try:
            method()
        except Exception as exc:  # pragma: no branch - asserted below
            errors.append(exc)

    starter = real_thread(target=call, args=(client.start,))
    starter.start()
    assert second_reader_constructing.wait(timeout=1.0)
    closer = real_thread(target=call, args=(client.close,))
    closer.start()
    allow_second_reader.set()
    starter.join(timeout=1.0)
    closer.join(timeout=1.0)

    assert not starter.is_alive()
    assert not closer.is_alive()
    assert errors == []
    assert process.returncode == 0


def test_first_event_timeout_and_interrupt_close_are_bounded(monkeypatch):
    process = FakeProcess([])
    client = ClaudeCLIClient(env={"HOME": "x"})
    client._proc = process
    client._generation = 1
    monkeypatch.setattr(client, "start", lambda: None)
    with pytest.raises(ClaudeCLIError, match="idle"):
        client.run_turn("input", first_event_timeout=0.01, total_timeout=0.1)
    client.request_interrupt()
    client.close()
    assert process.returncode == 0


def test_transport_rejects_tools_keys_urls_and_non_text():
    transport = ClaudeCLITransport()
    with pytest.raises(ValueError, match="tools"):
        transport.build_kwargs(
            "claude-opus-5", [{"role": "user", "content": "x"}], tools=[{"x": 1}]
        )
    with pytest.raises(ValueError, match="api_key"):
        transport.build_kwargs(
            "claude-opus-5",
            [{"role": "user", "content": "x"}],
            api_key="not-allowed",
        )
    with pytest.raises(ValueError, match="text-only"):
        transport.build_kwargs(
            "claude-opus-5", [{"role": "user", "content": [{"type": "image"}]}]
        )
