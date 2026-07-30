from __future__ import annotations

import json
import os
import sys
from pathlib import Path

os.environ.setdefault("LOCALAPPDATA", os.environ.get("TEMP", r"C:\Windows\Temp"))

import pytest

from agent.claude_cli_client import ClaudeCLIClient
from agent.claude_cli_process import (
    ClaudeCLIProcessRunner,
    ClaudeCLIStaleSessionError,
)
from hermes_state import SessionDB


FIXTURE = Path(__file__).parents[1] / "fixtures" / "fake_claude_cli.py"
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "echo",
            "description": "Return text",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
                "additionalProperties": False,
            },
        },
    }
]
FIRST_MESSAGES = [
    {"role": "system", "content": "Hermes system"},
    {"role": "user", "content": "one"},
]


def make_client(tmp_path, monkeypatch, *, mode="success"):
    log_path = tmp_path / "calls.json"
    monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log_path))
    db = SessionDB(tmp_path / "state.db")
    db.create_session("hermes-1", "cli")
    runner = ClaudeCLIProcessRunner(
        executable=sys.executable,
        executable_args=[str(FIXTURE)],
        timeout_seconds=5,
    )
    client = ClaudeCLIClient(
        model="opus",
        session_db=db,
        session_id="hermes-1",
        runner=runner,
    )
    return client, db, log_path


def read_log(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_first_completion_bootstraps_and_persists_attachment(
    tmp_path, monkeypatch
):
    client, db, log_path = make_client(tmp_path, monkeypatch)

    response = client.chat.completions.create(
        model="opus",
        messages=FIRST_MESSAGES,
        tools=TOOLS,
        stream=True,
    )

    assert response.choices[0].message.content == "ok"
    call = read_log(log_path)[0]
    assert "--session-id" in call["argv"]
    assert json.loads(call["prompt"])["frame"] == "bootstrap"
    attachment = db.get_provider_attachment("hermes-1", "claude-cli")
    assert attachment["provider_session_id"] == call["argv"][
        call["argv"].index("--session-id") + 1
    ]
    assert attachment["model_reported"] == "claude-opus-5"


def test_compatible_second_completion_resumes_with_delta(tmp_path, monkeypatch):
    client, _, log_path = make_client(tmp_path, monkeypatch)
    client.chat.completions.create(
        model="opus", messages=FIRST_MESSAGES, tools=TOOLS
    )

    client.chat.completions.create(
        model="opus",
        messages=[
            *FIRST_MESSAGES,
            {"role": "assistant", "content": "one"},
            {"role": "user", "content": "two"},
        ],
        tools=TOOLS,
    )

    calls = read_log(log_path)
    first_session = calls[0]["argv"][calls[0]["argv"].index("--session-id") + 1]
    assert calls[1]["argv"][calls[1]["argv"].index("--resume") + 1] == first_session
    prompt = json.loads(calls[1]["prompt"])
    assert prompt["frame"] == "delta"
    assert prompt["messages"] == [{"role": "user", "content": "two"}]


def test_changed_tool_fingerprint_starts_fresh_session(tmp_path, monkeypatch):
    client, _, log_path = make_client(tmp_path, monkeypatch)
    client.chat.completions.create(
        model="opus", messages=FIRST_MESSAGES, tools=TOOLS
    )

    client.chat.completions.create(
        model="opus", messages=FIRST_MESSAGES, tools=[], stream=True
    )

    calls = read_log(log_path)
    assert "--session-id" in calls[1]["argv"]
    assert "--resume" not in calls[1]["argv"]
    assert json.loads(calls[1]["prompt"])["frame"] == "bootstrap"


def test_rewritten_or_compressed_history_starts_fresh_session(
    tmp_path, monkeypatch
):
    client, _, log_path = make_client(tmp_path, monkeypatch)
    client.chat.completions.create(
        model="opus", messages=FIRST_MESSAGES, tools=TOOLS
    )

    client.chat.completions.create(
        model="opus",
        messages=[
            {"role": "system", "content": "Hermes system"},
            {"role": "user", "content": "compressed replacement"},
        ],
        tools=TOOLS,
    )

    calls = read_log(log_path)
    assert "--session-id" in calls[1]["argv"]
    assert "--resume" not in calls[1]["argv"]
    assert json.loads(calls[1]["prompt"])["frame"] == "bootstrap"


def test_stale_resume_retries_once_with_fresh_bootstrap(tmp_path, monkeypatch):
    client, db, log_path = make_client(tmp_path, monkeypatch)
    client.chat.completions.create(
        model="opus", messages=FIRST_MESSAGES, tools=TOOLS
    )
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "stale-on-resume")

    response = client.chat.completions.create(
        model="opus",
        messages=[*FIRST_MESSAGES, {"role": "user", "content": "two"}],
        tools=TOOLS,
    )

    assert response.choices[0].message.content == "ok"
    calls = read_log(log_path)
    assert "--resume" in calls[1]["argv"]
    assert "--session-id" in calls[2]["argv"]
    assert json.loads(calls[2]["prompt"])["frame"] == "bootstrap"
    assert db.get_provider_attachment("hermes-1", "claude-cli")[
        "provider_session_id"
    ] == calls[2]["argv"][calls[2]["argv"].index("--session-id") + 1]


def test_second_stale_failure_propagates(tmp_path, monkeypatch):
    client, _, _ = make_client(tmp_path, monkeypatch, mode="stale-session")

    with pytest.raises(ClaudeCLIStaleSessionError):
        client.chat.completions.create(
            model="opus", messages=FIRST_MESSAGES, tools=TOOLS
        )


def test_close_cancels_runner_and_marks_client_closed(tmp_path, monkeypatch):
    client, _, _ = make_client(tmp_path, monkeypatch)

    client.close()

    assert client.is_closed() is True
