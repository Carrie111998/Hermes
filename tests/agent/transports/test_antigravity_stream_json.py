"""Contract tests for the Antigravity stream-json Hermes adapter.

These tests intentionally exercise the wire projection without launching agy.
The live probe already established the observed event shapes; this suite pins
what Hermes is allowed to claim after receiving them.
"""

from __future__ import annotations

import json

from agent.transports.antigravity_stream_json import (
    AntigravityStreamJsonSession,
    build_agy_stream_command,
    parse_stream_events,
)


def _line(payload: dict) -> str:
    return json.dumps(payload)


def test_tool_lifecycle_projects_into_hermes_messages() -> None:
    result = parse_stream_events(
        [
            _line({"event": "init", "conversation_id": "conv-1", "init": {"tools": ["run_command"]}}),
            _line(
                {
                    "event": "step_update",
                    "step_update": {
                        "step_index": 3,
                        "state": "ACTIVE",
                        "step_type": "tool",
                        "tool_name": "run_command",
                        "tool_info": {"parameters": {"CommandLine": "printf AGY_TOOL_PROBE"}},
                    },
                }
            ),
            _line(
                {
                    "event": "step_update",
                    "step_update": {
                        "step_index": 3,
                        "state": "DONE",
                        "step_type": "tool",
                        "tool_name": "run_command",
                        "tool_info": {
                            "parameters": {"CommandLine": "printf AGY_TOOL_PROBE"},
                            "output": "AGY_TOOL_PROBE",
                        },
                    },
                }
            ),
            _line(
                {
                    "event": "result",
                    "result": {
                        "conversation_id": "conv-1",
                        "status": "SUCCESS",
                        "response": "AGY_TOOL_PROBE",
                    },
                }
            ),
        ]
    )

    assert result.completed is True
    assert result.conversation_id == "conv-1"
    assert result.tool_iterations == 1
    assert len(result.projected_messages) == 2
    assert result.projected_messages[0]["role"] == "assistant"
    assert result.projected_messages[0]["tool_calls"][0]["function"]["name"] == "run_command"
    assert result.projected_messages[1]["role"] == "tool"
    assert result.projected_messages[1]["content"] == "AGY_TOOL_PROBE"


def test_checkpoint_is_evidence_not_a_success_claim_without_terminal_result() -> None:
    result = parse_stream_events(
        [
            _line(
                {
                    "event": "step_update",
                    "step_update": {
                        "step_index": 4,
                        "state": "DONE",
                        "step_type": "checkpoint",
                        "duration_seconds": 0.5,
                    },
                }
            )
        ]
    )

    assert result.checkpoint_events == [
        {"step_index": 4, "state": "DONE", "step_type": "checkpoint", "duration_seconds": 0.5}
    ]
    assert result.completed is False
    assert result.error == "Antigravity stream ended without a terminal result"


def test_failed_terminal_result_is_not_reported_as_completed() -> None:
    result = parse_stream_events(
        [
            _line(
                {
                    "event": "result",
                    "result": {
                        "conversation_id": "conv-2",
                        "status": "ERROR",
                        "response": "I could not complete the task",
                    },
                }
            )
        ]
    )

    assert result.completed is False
    assert result.final_text == "I could not complete the task"
    assert "status=ERROR" in result.error


def test_command_options_precede_prompt_and_resume_conversation() -> None:
    command = build_agy_stream_command(
        "Continue the task",
        conversation_id="conv-1",
        model="flash",
        print_timeout="180s",
    )

    assert command[:2] == ["agy", "--output-format"]
    assert command.index("--conversation") < command.index("-p")
    assert command[-2:] == ["-p", "Continue the task"]
    assert "--conversation" in command
    assert "conv-1" in command


class _FakeProcess:
    def __init__(self, stdout: str, stderr: str = "", returncode: int = 0):
        self.stdout_text = stdout
        self.stderr_text = stderr
        self.returncode = returncode
        self.killed = False

    def communicate(self, timeout=None):
        return self.stdout_text, self.stderr_text

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return self.returncode


def test_session_runs_stream_and_reuses_conversation(monkeypatch) -> None:
    outputs = [
        "\n".join(
            [
                _line({"event": "init", "conversation_id": "conv-1", "init": {}}),
                _line({"event": "result", "result": {"conversation_id": "conv-1", "status": "SUCCESS", "response": "one"}}),
            ]
        ),
        "\n".join(
            [
                _line({"event": "result", "result": {"conversation_id": "conv-1", "status": "SUCCESS", "response": "two"}}),
            ]
        ),
    ]
    commands = []

    def fake_popen(command, **kwargs):
        commands.append(command)
        return _FakeProcess(outputs.pop(0))

    session = AntigravityStreamJsonSession(
        agy_bin="agy-test",
        process_factory=fake_popen,
    )
    first = session.run_turn("hello", system_prompt="Hermes system")
    second = session.run_turn("follow-up")

    assert first.completed is True
    assert first.final_text == "one"
    assert second.completed is True
    assert second.final_text == "two"
    assert "Hermes system" in commands[0][-1]
    assert "--conversation" not in commands[0]
    assert commands[1][commands[1].index("--conversation") + 1] == "conv-1"


def test_session_nonzero_exit_is_fail_closed() -> None:
    session = AntigravityStreamJsonSession(
        agy_bin="agy-test",
        process_factory=lambda command, **kwargs: _FakeProcess(
            _line({"event": "result", "result": {"status": "SUCCESS", "response": "fake"}}),
            stderr="network failed",
            returncode=7,
        ),
    )

    result = session.run_turn("hello")

    assert result.completed is False
    assert "exited with code 7" in result.error
    assert "network failed" in result.error
