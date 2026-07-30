"""Integration coverage for the Codex -> hermes-tools policy boundary."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from agent.request_phase import activate_turn_policy, clear_turn_policy
from agent.transports.codex_app_server_session import CodexAppServerSession
from agent.transports.turn_policy_channel import (
    CodexTurnPolicyChannel,
    POLICY_DB_ENV,
    POLICY_ID_ENV,
    POLICY_KEY_ENV,
    POLICY_REQUIRED_ENV,
    dispatch_with_turn_policy,
)

QUOTE_ANALYSIS_REQUEST = (
    "analyze existing quote skills/process and work downward."
)
REPO_ROOT = Path(__file__).resolve().parents[3]
POLICY_SUBPROCESS_TIMEOUT_SECONDS = 180


@pytest.fixture(autouse=True)
def _reset_policy():
    clear_turn_policy()
    yield
    clear_turn_policy()


def _run_policy_subprocess(
    channel: CodexTurnPolicyChannel,
    *,
    names_and_sizes: list[tuple[str, int]],
) -> list[dict]:
    script = """
import json
import model_tools
from tools import skills_tool
from agent.transports.turn_policy_channel import dispatch_with_turn_policy

sizes = json.loads(__import__("os").environ["TEST_SKILL_SIZES"])

def fake_skill_view(name, file_path=None, task_id=None):
    return json.dumps({
        "success": True,
        "name": name,
        "content": "x" * int(sizes[name]),
        "linked_files": {},
    })

skills_tool.skill_view = fake_skill_view
for name in sizes:
    raw = dispatch_with_turn_policy(
        "skill_view",
        {"name": name},
        model_tools.handle_function_call,
    )
    payload = json.loads(raw)
    print("POLICY_RESULT=" + json.dumps({
        "success": payload.get("success"),
        "has_content": "content" in payload,
        "content_chars": len(payload.get("content", "")),
        "error": payload.get("error", ""),
        "budget": payload.get("payload_budget", {}),
    }, sort_keys=True))
"""
    env = os.environ.copy()
    env.update(channel.environment)
    env["HERMES_QUIET"] = "1"
    env["TEST_SKILL_SIZES"] = json.dumps(dict(names_and_sizes))
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=POLICY_SUBPROCESS_TIMEOUT_SECONDS,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return [
        json.loads(line.split("=", 1)[1])
        for line in completed.stdout.splitlines()
        if line.startswith("POLICY_RESULT=")
    ]


def _run_terminal_subprocess(
    channel: CodexTurnPolicyChannel,
    *,
    target: Path,
    workdir: Path,
) -> dict:
    script = """
import json
import os
import subprocess
import sys
import model_tools
from agent.transports.turn_policy_channel import dispatch_with_turn_policy

target = os.environ["TEST_MUTATION_TARGET"]
workdir = os.environ["TEST_MUTATION_WORKDIR"]
python_code = (
    "from pathlib import Path; "
    f"Path({target!r}).write_text('changed', encoding='utf-8')"
)
command = subprocess.list2cmdline([sys.executable, "-c", python_code])
raw = dispatch_with_turn_policy(
    "terminal",
    {"command": command, "workdir": workdir},
    model_tools.handle_function_call,
)
print("POLICY_RESULT=" + raw)
"""
    env = os.environ.copy()
    env.update(channel.environment)
    env["HERMES_QUIET"] = "1"
    env["TEST_MUTATION_TARGET"] = str(target)
    env["TEST_MUTATION_WORKDIR"] = str(workdir)
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=POLICY_SUBPROCESS_TIMEOUT_SECONDS,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    line = next(
        value
        for value in completed.stdout.splitlines()
        if value.startswith("POLICY_RESULT=")
    )
    return json.loads(line.split("=", 1)[1])


def test_actual_mcp_subprocess_blocks_exact_incident_skill_packets(tmp_path):
    """The 103k/119k quote fan-out cannot bypass the parent turn budget."""

    policy = activate_turn_policy(QUOTE_ANALYSIS_REQUEST, cwd=tmp_path)
    policy.loaded_root_skills.extend(
        ["terrain-quote-workflows", "terrain-communications"]
    )
    channel = CodexTurnPolicyChannel(db_path=tmp_path / "policy.sqlite3")
    try:
        channel.publish(
            policy,
            fallback_phase="investigation",
            fallback_request_text=QUOTE_ANALYSIS_REQUEST,
            fallback_workspace=str(tmp_path),
        )

        results = _run_policy_subprocess(
            channel,
            names_and_sizes=[
                ("terrain-quote-workflows", 103_000),
                ("terrain-communications", 119_000),
            ],
        )

        assert len(results) == 2
        assert all(result["success"] is False for result in results)
        assert all(result["has_content"] is False for result in results)
        assert all(
            result["budget"]["reason"] == "per_result_limit"
            for result in results
        )
        assert channel.read_state()["skill_payload_chars"] == 0
    finally:
        channel.close()


def test_actual_mcp_subprocess_cannot_escape_phase_with_terminal(
    tmp_path,
):
    outside = tmp_path / "outside"
    outside.mkdir()
    repo = tmp_path / "protected-repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", str(repo)],
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    target = repo / "quote.py"
    target.write_text("original\n", encoding="utf-8")
    policy = activate_turn_policy(
        QUOTE_ANALYSIS_REQUEST,
        cwd=outside,
    )
    channel = CodexTurnPolicyChannel(db_path=tmp_path / "policy.sqlite3")
    try:
        channel.publish(
            policy,
            fallback_phase="investigation",
            fallback_request_text=QUOTE_ANALYSIS_REQUEST,
            fallback_workspace=str(outside),
        )

        result = _run_terminal_subprocess(
            channel,
            target=target,
            workdir=outside,
        )

        assert "error" in result
        assert "explicit implementation instruction" in result["error"]
        assert target.read_text(encoding="utf-8") == "original\n"
    finally:
        channel.close()


def test_codex_session_passes_exact_parent_policy_to_child_environment(tmp_path):
    activate_turn_policy(QUOTE_ANALYSIS_REQUEST, cwd=tmp_path)
    captured: dict[str, object] = {}

    class FakeClient:
        def initialize(self, **_kwargs):
            return {}

        def request(self, method, _params=None, **_kwargs):
            assert method == "thread/start"
            return {"thread": {"id": "thread-policy-test"}}

        def close(self):
            return None

    def factory(**kwargs):
        captured.update(kwargs)
        return FakeClient()

    session = CodexAppServerSession(
        cwd=str(tmp_path),
        request_phase="investigation",
        enforce_read_only=True,
        client_factory=factory,
    )
    try:
        session.ensure_started()

        child_env = captured["env"]
        assert isinstance(child_env, dict)
        assert set(channel_key for channel_key in child_env) == {
            POLICY_DB_ENV,
            POLICY_ID_ENV,
            POLICY_KEY_ENV,
        }
        assert "extra_args" not in captured
        assert session._turn_policy_channel is not None
        state = session._turn_policy_channel.read_state()
        assert state["phase"] == "investigation"
        assert state["request_text"] == QUOTE_ANALYSIS_REQUEST
        assert state["workspace"] == str(tmp_path)
        assert state["skill_payload_chars"] == 0
    finally:
        session.close()


def test_actual_mcp_subprocess_shares_cumulative_budget_atomically(tmp_path):
    policy = activate_turn_policy("Review these three skills.", cwd=tmp_path)
    policy.loaded_root_skills.extend(["skill-a", "skill-b", "skill-c"])
    channel = CodexTurnPolicyChannel(db_path=tmp_path / "policy.sqlite3")
    try:
        channel.publish(
            policy,
            fallback_phase="investigation",
            fallback_request_text="Review these three skills.",
            fallback_workspace=str(tmp_path),
        )

        results = _run_policy_subprocess(
            channel,
            names_and_sizes=[
                ("skill-a", 23_000),
                ("skill-b", 23_000),
                ("skill-c", 23_000),
            ],
        )

        assert [result["success"] for result in results] == [True, True, False]
        assert [result["content_chars"] for result in results] == [
            23_000,
            23_000,
            0,
        ]
        assert results[2]["budget"]["reason"] == "turn_limit"
        assert channel.read_state()["skill_payload_chars"] == 46_000
    finally:
        channel.close()


def test_skill_view_fails_closed_without_policy_channel(monkeypatch):
    monkeypatch.delenv(POLICY_DB_ENV, raising=False)
    monkeypatch.delenv(POLICY_ID_ENV, raising=False)
    monkeypatch.delenv(POLICY_KEY_ENV, raising=False)
    calls = []

    result = json.loads(
        dispatch_with_turn_policy(
            "skill_view",
            {"name": "anything"},
            lambda name, args: calls.append((name, args)) or "{}",
        )
    )

    assert result["success"] is False
    assert "environment is missing" in result["error"]
    assert calls == []


def test_required_child_fails_closed_for_non_skill_effect_without_channel(
    monkeypatch,
):
    monkeypatch.delenv(POLICY_DB_ENV, raising=False)
    monkeypatch.delenv(POLICY_ID_ENV, raising=False)
    monkeypatch.delenv(POLICY_KEY_ENV, raising=False)
    monkeypatch.setenv(POLICY_REQUIRED_ENV, "1")
    calls = []

    result = json.loads(
        dispatch_with_turn_policy(
            "terminal",
            {"command": "python -c \"print('effect')\""},
            lambda name, args: calls.append((name, args)) or "{}",
        )
    )

    assert "error" in result
    assert "environment is missing" in result["error"]
    assert calls == []


def test_tampered_policy_row_fails_closed_without_dispatch(
    tmp_path,
    monkeypatch,
):
    policy = activate_turn_policy("Review one skill.", cwd=tmp_path)
    channel = CodexTurnPolicyChannel(db_path=tmp_path / "policy.sqlite3")
    try:
        channel.publish(
            policy,
            fallback_phase="investigation",
            fallback_request_text="Review one skill.",
            fallback_workspace=str(tmp_path),
        )
        with sqlite3.connect(channel.db_path) as connection:
            connection.execute(
                """
                UPDATE turn_policy SET state_json = ?
                WHERE policy_id = ?
                """,
                ('{"phase":"implementation"}', channel.policy_id),
            )
        for key, value in channel.environment.items():
            monkeypatch.setenv(key, value)
        calls = []

        result = json.loads(
            dispatch_with_turn_policy(
                "skill_view",
                {"name": "anything"},
                lambda name, args: calls.append((name, args)) or "{}",
            )
        )

        assert result["success"] is False
        assert "signature is invalid" in result["error"]
        assert calls == []
    finally:
        channel.close()
