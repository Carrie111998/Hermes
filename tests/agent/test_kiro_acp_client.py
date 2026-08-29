"""Kiro ACP transport + thin client. Unit tests stay green without kiro-cli."""

from __future__ import annotations

import io
import json
import os
import queue
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.acp_stdio_transport import (
    AcpStdioTransport,
    is_acp_base_url,
    permission_allowed,
    permission_denied,
)
from agent.kiro_acp_client import (
    KIRO_ACP_TOOL_ALLOWLIST,
    KiroACPClient,
    build_acp_client,
    format_messages_as_prompt,
    should_use_acp_client,
)


def test_factory_keys_on_acp_scheme_not_copilot_only():
    assert is_acp_base_url("acp://kiro")
    assert should_use_acp_client(provider="kiro-acp", base_url="https://example.com")
    assert should_use_acp_client(provider="openai-codex", base_url="acp://kiro")
    kiro = build_acp_client(provider="kiro-acp", base_url="acp://kiro", command="missing-kiro")
    assert isinstance(kiro, KiroACPClient)
    copilot = build_acp_client(provider="copilot-acp", base_url="acp://copilot", command="copilot")
    from agent.copilot_acp_client import CopilotACPClient

    assert isinstance(copilot, CopilotACPClient)
    assert not should_use_acp_client(provider="openai-codex", base_url="acp://somevendor")
    try:
        build_acp_client(provider="openai-codex", base_url="acp://somevendor")
    except ValueError as exc:
        assert "Unknown ACP vendor" in str(exc)
    else:
        raise AssertionError("unknown acp:// host must refuse, not build CopilotACPClient")


def test_prompt_forwards_only_agent_level_tools():
    tools = [
        {"type": "function", "function": {"name": "memory", "description": "d", "parameters": {}}},
        {"type": "function", "function": {"name": "read_file", "description": "d", "parameters": {}}},
        {"type": "function", "function": {"name": "todo", "description": "d", "parameters": {}}},
    ]
    prompt = format_messages_as_prompt(
        [{"role": "user", "content": "hi"}],
        model="claude-opus-5",
        tools=tools,
        allowlist=KIRO_ACP_TOOL_ALLOWLIST,
    )
    assert '"name": "memory"' in prompt
    assert '"name": "todo"' in prompt
    assert '"name": "read_file"' not in prompt


def test_permission_allowed_picks_allow_once():
    reply = permission_allowed(7, [{"optionId": "reject"}, {"optionId": "allow_once"}])
    assert reply["result"]["outcome"]["optionId"] == "allow_once"
    denied = permission_denied(8)
    assert denied["result"]["outcome"]["outcome"] == "cancelled"
    session_wide = permission_allowed(9, [{"optionId": "allow"}, {"optionId": "approve"}])
    assert session_wide["result"]["outcome"]["outcome"] == "cancelled"
    mixed = permission_allowed(10, [{"optionId": "allow"}, {"optionId": "allow_once"}])
    assert mixed["result"]["outcome"]["optionId"] == "allow_once"


def test_missing_binary_raises_clear_error(tmp_path):
    client = KiroACPClient(
        command="definitely-not-kiro-cli-xyz",
        args=["acp", "--model", "claude-opus-5"],
        acp_cwd=str(tmp_path),
    )
    with pytest.raises(RuntimeError, match="Could not start Kiro ACP command"):
        client.chat.completions.create(
            model="claude-opus-5",
            messages=[{"role": "user", "content": "hi"}],
            timeout=1,
        )


def test_write_approval_required_is_still_enforced(tmp_path):
    target = tmp_path / "gated.txt"
    transport = AcpStdioTransport(
        command="true",
        args=[],
        cwd=str(tmp_path),
        permission_handler=permission_allowed,
    )
    process = SimpleNamespace(stdin=io.StringIO())
    with patch(
        "agent.acp_stdio_transport.is_write_approval_required",
        return_value=True,
    ):
        handled = transport.handle_server_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "fs/write_text_file",
                "params": {"path": str(target), "content": "nope"},
            },
            process=process,
            cwd=str(tmp_path),
            text_parts=[],
            reasoning_parts=[],
        )
    assert handled
    payload = json.loads(process.stdin.getvalue())
    assert "error" in payload
    assert "interactive approval" in payload["error"]["message"]
    assert not target.exists()


class _FakeStream:
    def __init__(self) -> None:
        self._q: queue.Queue[str | None] = queue.Queue()
        self.closed = False

    def push(self, payload: dict) -> None:
        self._q.put(json.dumps(payload) + "\n")

    def eof(self) -> None:
        self._q.put(None)

    def close(self) -> None:
        self.closed = True
        self.eof()

    def __iter__(self):
        return self

    def __next__(self) -> str:
        item = self._q.get()
        if item is None:
            raise StopIteration
        return item


class _FakeStdin:
    def __init__(self, process: "_FakeACPProcess") -> None:
        self._process = process
        self.closed = False

    def write(self, data: str) -> None:
        if self.closed:
            raise ValueError("I/O operation on closed file.")
        self._process._handle(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class _FakeACPProcess:
    def __init__(self, pid: int, *, reply: str = "ok") -> None:
        self.pid = pid
        self.reply = reply
        self.stdout = _FakeStream()
        self.stderr = _FakeStream()
        self.stdin = _FakeStdin(self)
        self.requests: list[dict] = []
        self._returncode: int | None = None
        self._sessions = 0

    def _handle(self, data: str) -> None:
        message = json.loads(data)
        self.requests.append(message)
        if self._returncode is not None:
            raise BrokenPipeError("process is gone")
        method = message.get("method")
        message_id = message.get("id")
        if method == "initialize":
            self.stdout.push({"jsonrpc": "2.0", "id": message_id, "result": {"protocolVersion": 1}})
        elif method == "session/new":
            self._sessions += 1
            self.stdout.push(
                {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "result": {"sessionId": f"sess-{self.pid}-{self._sessions}"},
                }
            )
        elif method == "session/prompt":
            self.stdout.push(
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"text": self.reply},
                        }
                    },
                }
            )
            self.stdout.push(
                {"jsonrpc": "2.0", "id": message_id, "result": {"stopReason": "end_turn"}}
            )
        elif method == "session/request_permission":
            pass

    def methods(self) -> list[str]:
        return [str(request.get("method")) for request in self.requests]

    def prompts(self) -> list[dict]:
        return [r for r in self.requests if r.get("method") == "session/prompt"]

    def die(self, code: int = 1) -> None:
        self._returncode = code
        self.stdout.eof()
        self.stderr.eof()

    def poll(self) -> int | None:
        return self._returncode

    def wait(self, timeout: float | None = None) -> int:
        if self._returncode is None:
            self.die(0)
        return self._returncode or 0

    def terminate(self) -> None:
        self.die(-15)

    def kill(self) -> None:
        self.die(-9)


class _FakeACPSpawner:
    def __init__(self, *processes: _FakeACPProcess) -> None:
        self._processes = list(processes)
        self.calls: list[dict] = []
        self.spawned: list[_FakeACPProcess] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append({"cmd": cmd, "kwargs": kwargs})
        process = self._processes.pop(0)
        self.spawned.append(process)
        return process


def test_two_completions_reuse_one_process_and_open_two_sessions(tmp_path):
    process = _FakeACPProcess(pid=101, reply="answer")
    spawner = _FakeACPSpawner(process)
    client = KiroACPClient(command="kiro-cli", args=["acp"], acp_cwd=str(tmp_path))

    with patch("agent.acp_stdio_transport.subprocess.Popen", spawner):
        first = client.chat.completions.create(
            model="claude-opus-5",
            messages=[{"role": "user", "content": "first"}],
            timeout=5,
        )
        second = client.chat.completions.create(
            model="claude-opus-5",
            messages=[{"role": "user", "content": "second"}],
            timeout=5,
        )

    assert first.choices[0].message.content == "answer"
    assert second.choices[0].message.content == "answer"
    assert len(spawner.calls) == 1
    assert process.methods() == [
        "initialize",
        "session/new",
        "session/prompt",
        "session/new",
        "session/prompt",
    ]
    assert len({p["params"]["sessionId"] for p in process.prompts()}) == 2
    assert not client.is_closed
    env = spawner.calls[0]["kwargs"]["env"]
    assert "OPENAI_API_KEY" not in env or not env.get("OPENAI_API_KEY")


def test_permission_handler_allows_on_kiro_transport(tmp_path):
    transport = AcpStdioTransport(
        command="true",
        args=[],
        cwd=str(tmp_path),
        permission_handler=permission_allowed,
    )
    process = SimpleNamespace(stdin=io.StringIO())
    handled = transport.handle_server_message(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "session/request_permission",
            "params": {"options": [{"optionId": "allow"}, {"optionId": "allow_once"}]},
        },
        process=process,
        cwd=str(tmp_path),
        text_parts=[],
        reasoning_parts=[],
    )
    assert handled
    payload = json.loads(process.stdin.getvalue())
    assert payload["result"]["outcome"]["optionId"] == "allow_once"


def test_subprocess_does_not_inherit_hermes_api_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-should-not-leak")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-leak")
    process = _FakeACPProcess(pid=202)
    spawner = _FakeACPSpawner(process)
    client = KiroACPClient(command="kiro-cli", args=["acp"], acp_cwd=str(tmp_path))
    with patch("agent.acp_stdio_transport.subprocess.Popen", spawner):
        client.chat.completions.create(
            model="claude-opus-5",
            messages=[{"role": "user", "content": "hi"}],
            timeout=5,
        )
    env = spawner.calls[0]["kwargs"]["env"]
    assert env.get("OPENAI_API_KEY") in (None, "")
    assert env.get("ANTHROPIC_API_KEY") in (None, "")


def test_cwd_comes_from_resolve_agent_cwd(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setenv("TERMINAL_CWD", str(workspace))
    monkeypatch.chdir(elsewhere)
    client = KiroACPClient(command="kiro-cli", args=["acp"])
    assert client._acp_cwd == str(workspace.resolve())


def test_unknown_notification_without_id_is_not_minus_32601(tmp_path):
    transport = AcpStdioTransport(
        command="true",
        args=[],
        cwd=str(tmp_path),
        permission_handler=permission_allowed,
    )
    process = SimpleNamespace(stdin=io.StringIO())
    handled = transport.handle_server_message(
        {"jsonrpc": "2.0", "method": "kiro/progress", "params": {"phase": "thinking"}},
        process=process,
        cwd=str(tmp_path),
        text_parts=[],
        reasoning_parts=[],
    )
    assert handled
    assert process.stdin.getvalue() == ""
    handled = transport.handle_server_message(
        {"jsonrpc": "2.0", "id": 11, "method": "session/unknown_request"},
        process=process,
        cwd=str(tmp_path),
        text_parts=[],
        reasoning_parts=[],
    )
    assert handled
    payload = json.loads(process.stdin.getvalue())
    assert payload["error"]["code"] == -32601


def test_create_respawns_when_model_slug_changes(tmp_path):
    first = _FakeACPProcess(pid=301, reply="opus")
    second = _FakeACPProcess(pid=302, reply="sonnet")
    spawner = _FakeACPSpawner(first, second)
    client = KiroACPClient(
        command="kiro-cli",
        args=["acp", "--model", "claude-opus-5"],
        acp_cwd=str(tmp_path),
        model="claude-opus-5",
    )
    with patch("agent.acp_stdio_transport.subprocess.Popen", spawner):
        one = client.chat.completions.create(
            model="claude-opus-5",
            messages=[{"role": "user", "content": "first"}],
            timeout=5,
        )
        two = client.chat.completions.create(
            model="claude-sonnet-5",
            messages=[{"role": "user", "content": "second"}],
            timeout=5,
        )
    assert one.choices[0].message.content == "opus"
    assert two.choices[0].message.content == "sonnet"
    assert len(spawner.calls) == 2
    assert spawner.calls[0]["cmd"][1:] == ["acp", "--model", "claude-opus-5"]
    assert spawner.calls[1]["cmd"][1:] == ["acp", "--model", "claude-sonnet-5"]
    assert client._active_process is second
    assert client._active_process is not first


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group cleanup")
def test_close_kills_real_grandchild_process_group(tmp_path):
    """Prove killpg reaches a real forked grandchild, not a mocked os.killpg."""
    script = tmp_path / "fake_acp.py"
    grandchild_pid_file = tmp_path / "grandchild.pid"
    script.write_text(
        "\n".join(
            [
                "import json, os, sys, time",
                "pid = os.fork()",
                "if pid == 0:",
                "    time.sleep(30)",
                "    os._exit(0)",
                f"open({str(grandchild_pid_file)!r}, 'w').write(str(pid))",
                "for line in sys.stdin:",
                "    msg = json.loads(line)",
                "    mid = msg.get('id')",
                "    method = msg.get('method')",
                "    if method == 'initialize':",
                "        print(json.dumps({'jsonrpc':'2.0','id':mid,'result':{'protocolVersion':1}}), flush=True)",
                "    elif method == 'session/new':",
                "        print(json.dumps({'jsonrpc':'2.0','id':mid,'result':{'sessionId':'s1'}}), flush=True)",
                "    elif method == 'session/prompt':",
                "        print(json.dumps({'jsonrpc':'2.0','method':'session/update','params':{'update':{'sessionUpdate':'agent_message_chunk','content':{'text':'ok'}}}}), flush=True)",
                "        print(json.dumps({'jsonrpc':'2.0','id':mid,'result':{'stopReason':'end_turn'}}), flush=True)",
            ]
        )
        + "\n"
    )
    client = KiroACPClient(
        command=sys.executable,
        args=[str(script)],
        acp_cwd=str(tmp_path),
    )
    completion = client.chat.completions.create(
        model="claude-opus-5",
        messages=[{"role": "user", "content": "hi"}],
        timeout=5,
    )
    assert completion.choices[0].message.content == "ok"
    launcher = client._active_process
    assert launcher is not None
    deadline = time.monotonic() + 3
    while not grandchild_pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    grandchild_pid = int(grandchild_pid_file.read_text())
    os.kill(grandchild_pid, 0)  # still alive before close
    client.close()
    time.sleep(0.2)
    with pytest.raises(OSError):
        os.kill(grandchild_pid, 0)
    assert launcher.poll() is not None


_KIRO_AVAILABLE = shutil.which("kiro-cli") is not None


@pytest.mark.skipif(not _KIRO_AVAILABLE, reason="kiro-cli not installed")
def test_live_kiro_acp_opus5_marker(tmp_path):
    client = KiroACPClient(
        command="kiro-cli",
        args=["acp", "--model", "claude-opus-5"],
        acp_cwd=str(tmp_path),
        model="claude-opus-5",
    )
    try:
        first = client.chat.completions.create(
            model="claude-opus-5",
            messages=[{"role": "user", "content": "Reply with exactly KIRO_OPUS_5_OK and nothing else."}],
            timeout=180,
        )
        second = client.chat.completions.create(
            model="claude-opus-5",
            messages=[
                {"role": "user", "content": "Reply with exactly KIRO_OPUS_5_OK and nothing else."},
                {"role": "assistant", "content": first.choices[0].message.content},
                {"role": "user", "content": "Reply again with exactly KIRO_OPUS_5_OK and nothing else."},
            ],
            timeout=180,
        )
        combined = (
            (first.choices[0].message.content or "")
            + "\n"
            + (second.choices[0].message.content or "")
        )
        assert "KIRO_OPUS_5_OK" in combined, combined
        assert first.model == "claude-opus-5"
        assert second.model == "claude-opus-5"
    finally:
        client.close()


@pytest.mark.skipif(not _KIRO_AVAILABLE, reason="kiro-cli not installed")
def test_live_kiro_acp_two_turns_one_pid_and_no_orphans(tmp_path):
    before_spawn = _kiro_chat_pids()
    client = KiroACPClient(
        command="kiro-cli",
        args=["acp", "--model", "claude-opus-5"],
        acp_cwd=str(tmp_path),
        model="claude-opus-5",
    )
    try:
        first = client.chat.completions.create(
            model="claude-opus-5",
            messages=[{"role": "user", "content": "Reply with exactly KIRO_TURN_1 and nothing else."}],
            timeout=180,
        )
        launcher = client._active_process
        assert launcher is not None
        pid = launcher.pid
        assert pid
        first_text = first.choices[0].message.content or ""
        assert "KIRO_TURN_1" in first_text, first_text
        workers = _kiro_chat_pids() - before_spawn
        second = client.chat.completions.create(
            model="claude-opus-5",
            messages=[
                {"role": "user", "content": "Reply with exactly KIRO_TURN_1 and nothing else."},
                {"role": "assistant", "content": first_text},
                {"role": "user", "content": "Reply with exactly KIRO_TURN_2 and nothing else."},
            ],
            timeout=180,
        )
        assert client._active_process is not None
        assert client._active_process.pid == pid
        second_text = second.choices[0].message.content or ""
        assert "KIRO_TURN_2" in second_text, second_text
        workers |= _kiro_chat_pids() - before_spawn
    finally:
        client.close()
        time.sleep(0.5)
    after_close = _kiro_chat_pids()
    leaked = (workers & after_close) | (after_close - before_spawn)
    assert not leaked, f"orphaned kiro-cli-chat pids: {leaked}"


def _kiro_chat_pids() -> set[int]:
    try:
        out = subprocess.check_output(["pgrep", "-f", "kiro-cli-chat"], text=True)
    except subprocess.CalledProcessError:
        return set()
    return {int(line) for line in out.splitlines() if line.strip().isdigit()}
