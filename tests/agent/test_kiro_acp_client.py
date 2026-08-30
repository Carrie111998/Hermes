"""Kiro ACP transport + thin client. Unit tests stay green without kiro-cli."""

from __future__ import annotations

import io
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
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


def test_prompt_is_a_transcript_not_an_xml_tool_bridge():
    """Codex-parity: no XML contract, no Hermes schema dump in the prompt."""
    tools = [
        {"type": "function", "function": {"name": "memory", "description": "d", "parameters": {}}},
        {"type": "function", "function": {"name": "terminal", "description": "d", "parameters": {}}},
    ]
    prompt = format_messages_as_prompt(
        [{"role": "user", "content": "hi"}],
        model="claude-opus-5",
        tools=tools,
    )
    assert "<tool_call>" not in prompt
    assert "inference backend" not in prompt.lower()
    assert '"name": "memory"' not in prompt
    assert "User:\nhi" in prompt


def test_prompt_keeps_prior_hermes_tool_calls_and_named_results():
    """Empty-content assistant(tool_calls) must not vanish from the next Kiro prompt."""
    prompt = format_messages_as_prompt(
        [
            {"role": "user", "content": "status of ubnt1"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "terminal",
                            "arguments": '{"command": "hostname"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "name": "terminal",
                "content": "box",
            },
        ],
        model="claude-opus-5",
    )
    assert "<tool_call>" not in prompt
    assert "Hermes tools requested: terminal" in prompt
    assert "terminal (call_1) result:" in prompt
    assert "box" in prompt


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


def test_kiro_fs_bridge_is_disabled(tmp_path):
    target = tmp_path / "gated.txt"
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
            def _emit_prompt_tail() -> None:
                if getattr(self, "tool_updates", None):
                    for update in self.tool_updates:
                        self.stdout.push(
                            {
                                "jsonrpc": "2.0",
                                "method": "session/update",
                                "params": {"update": update},
                            }
                        )
                if getattr(self, "permission_requests", None):
                    for req in self.permission_requests:
                        self.stdout.push(
                            {
                                "jsonrpc": "2.0",
                                "id": 9000 + self._sessions,
                                "method": "session/request_permission",
                                "params": req,
                            }
                        )
                if getattr(self, "usage", None):
                    usage_result = {"stopReason": "end_turn", "usage": self.usage}
                else:
                    usage_result = {"stopReason": "end_turn"}
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
                    {"jsonrpc": "2.0", "id": message_id, "result": usage_result}
                )

            prefix = getattr(self, "reply_prefix", "")
            if prefix:
                self.stdout.push(
                    {
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "update": {
                                "sessionUpdate": "agent_message_chunk",
                                "content": {"text": prefix},
                            }
                        },
                    }
                )
            gap = float(getattr(self, "mid_prompt_gap_s", 0) or 0)
            if gap > 0:
                threading.Thread(
                    target=lambda: (time.sleep(gap), _emit_prompt_tail()),
                    daemon=True,
                ).start()
            else:
                _emit_prompt_tail()
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


def test_kiro_client_denies_native_permissions_like_copilot(tmp_path):
    """Hermes executes tools; Kiro must not run its own execute/read/write."""
    client = KiroACPClient(command="kiro-cli", args=["acp"], acp_cwd=str(tmp_path))
    process = SimpleNamespace(stdin=io.StringIO())
    handled = client._transport.handle_server_message(
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
    assert payload["result"]["outcome"]["outcome"] == "cancelled"


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



def test_resolve_kiro_args_never_adds_trust_all_tools():
    args = __import__("agent.kiro_acp_client", fromlist=["resolve_kiro_args"]).resolve_kiro_args(
        model="claude-opus-5"
    )
    assert "--trust-all-tools" not in args
    assert args[:1] == ["acp"]


def _tool_calls_from_stream(chunks):
    for chunk in chunks:
        if getattr(chunk, "choices", None) and chunk.choices[0].delta.tool_calls:
            return list(chunk.choices[0].delta.tool_calls)
    return []


def test_stream_execute_stub_then_command_is_one_ready_terminal(tmp_path):
    """First ACP execute has no command; later update with same id must dispatch once."""
    process = _FakeACPProcess(pid=440, reply="listing")
    process.tool_updates = [
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "tc-ls",
            "kind": "execute",
            "status": "in_progress",
            "title": "ls",
            "rawInput": {"command": None},
        },
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "tc-ls",
            "kind": "execute",
            "status": "in_progress",
            "rawInput": {"command": "ls -la"},
        },
    ]
    spawner = _FakeACPSpawner(process)
    client = KiroACPClient(command="kiro-cli", args=["acp"], acp_cwd=str(tmp_path))
    with patch("agent.acp_stdio_transport.subprocess.Popen", spawner):
        chunks = list(
            client.chat.completions.create(
                model="claude-opus-5",
                messages=[{"role": "user", "content": "hi"}],
                stream=True,
                timeout=5,
            )
        )
    calls = _tool_calls_from_stream(chunks)
    assert len(calls) == 1
    assert calls[0].function.name == "terminal"
    assert json.loads(calls[0].function.arguments)["command"] == "ls -la"
    assert "session/cancel" in process.methods()


def test_stream_stub_and_real_ids_dispatch_only_real_terminal(tmp_path):
    """Kiro often emits a command-less execute plus a later real toolCallId."""
    process = _FakeACPProcess(pid=441, reply="listing")
    process.tool_updates = [
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "tc-stub",
            "kind": "execute",
            "status": "in_progress",
            "title": "preparing terminal",
            "rawInput": {},
        },
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "tc-real",
            "kind": "execute",
            "status": "in_progress",
            "rawInput": {"command": "pwd"},
        },
    ]
    spawner = _FakeACPSpawner(process)
    client = KiroACPClient(command="kiro-cli", args=["acp"], acp_cwd=str(tmp_path))
    with patch("agent.acp_stdio_transport.subprocess.Popen", spawner):
        completion = client.chat.completions.create(
            model="claude-opus-5",
            messages=[{"role": "user", "content": "hi"}],
            timeout=5,
        )
    calls = completion.choices[0].message.tool_calls
    assert len(calls) == 1
    assert calls[0].id == "tc-real"
    assert json.loads(calls[0].function.arguments)["command"] == "pwd"


def test_prompt_after_empty_terminal_keeps_assistant_and_tool_result():
    """Next Kiro prompt must still carry the failed terminal, not look like a new user turn."""
    prompt = format_messages_as_prompt(
        [
            {"role": "user", "content": "check mlir-gym"},
            {
                "role": "assistant",
                "content": "I'll check the worktrees.",
                "tool_calls": [
                    {
                        "id": "call_empty",
                        "type": "function",
                        "function": {"name": "terminal", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_empty",
                "name": "terminal",
                "content": "Invalid command: expected string, got NoneType",
            },
        ],
        model="claude-opus-5",
    )
    assert "User:\ncheck mlir-gym" in prompt
    assert "I'll check the worktrees." in prompt
    assert "Hermes tools requested: terminal" in prompt
    assert "terminal (call_empty) result:" in prompt
    assert "expected string, got NoneType" in prompt
    assert "<tool_call>" not in prompt


def test_intercept_refuses_unready_terminal_and_drops_stub_id(tmp_path):
    from agent.acp_tool_bridge import build_hermes_tool_call

    client = KiroACPClient(command="kiro-cli", args=["acp"], acp_cwd=str(tmp_path))
    bucket: list = []
    stub = {
        "id": "tc-stub",
        "name": "terminal",
        "args": {"command": None},
        "status": "in_progress",
    }
    assert client._intercept_tool(stub, bucket) is None
    assert bucket == []

    same = {
        "id": "tc-ls",
        "name": "terminal",
        "args": {},
        "status": "in_progress",
    }
    assert client._intercept_tool(same, bucket) is None
    ready = {
        "id": "tc-ls",
        "name": "terminal",
        "args": {"command": "ls -la"},
        "status": "in_progress",
    }
    assert client._intercept_tool(ready, bucket) is not None
    assert len(bucket) == 1
    assert json.loads(bucket[0].function.arguments)["command"] == "ls -la"

    bucket[:] = [
        build_hermes_tool_call(call_id="tc-leftover", name="terminal", arguments="{}")
    ]
    other = {
        "id": "tc-real",
        "name": "terminal",
        "args": {"command": "pwd"},
        "status": "in_progress",
    }
    assert client._intercept_tool(other, bucket) is not None
    assert [item.id for item in bucket] == ["tc-real"]


def test_stream_search_kind_with_shell_command_is_hermes_terminal(tmp_path):
    """kind=search + rawInput.command=pipeline must dispatch terminal, not search_files."""
    process = _FakeACPProcess(pid=442, reply="listing")
    process.tool_updates = [
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "tc-ps",
            "kind": "search",
            "status": "in_progress",
            "title": "grep",
            "rawInput": {"command": "ps aux | grep foo"},
        }
    ]
    spawner = _FakeACPSpawner(process)
    client = KiroACPClient(command="kiro-cli", args=["acp"], acp_cwd=str(tmp_path))
    with patch("agent.acp_stdio_transport.subprocess.Popen", spawner):
        chunks = list(
            client.chat.completions.create(
                model="claude-opus-5",
                messages=[{"role": "user", "content": "hi"}],
                stream=True,
                timeout=5,
            )
        )
    calls = _tool_calls_from_stream(chunks)
    assert len(calls) == 1
    assert calls[0].function.name == "terminal"
    assert json.loads(calls[0].function.arguments)["command"] == "ps aux | grep foo"
    assert "session/cancel" in process.methods()


def test_stream_native_execute_becomes_hermes_terminal_tool_call(tmp_path):
    """Codex-parity: Kiro execute intent is a Hermes `terminal` tool_call."""
    process = _FakeACPProcess(pid=401, reply="I will list files")
    process.tool_updates = [
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "tc-ls",
            "kind": "execute",
            "status": "in_progress",
            "title": "ls",
            "rawInput": {"command": "ls"},
        },
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "tc-ls",
            "status": "cancelled",
            "rawOutput": "denied",
        },
    ]
    process.usage = {"inputTokens": 111, "outputTokens": 9}
    spawner = _FakeACPSpawner(process)
    client = KiroACPClient(command="kiro-cli", args=["acp"], acp_cwd=str(tmp_path))
    with patch("agent.acp_stdio_transport.subprocess.Popen", spawner):
        stream = client.chat.completions.create(
            model="claude-opus-5",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
            timeout=5,
        )
        chunks = list(stream)
    tool_chunks = [
        c for c in chunks
        if c.choices and c.choices[0].delta.tool_calls
    ]
    assert tool_chunks, "Kiro execute must become a Hermes-executable tool_call"
    names = [tc.function.name for tc in tool_chunks[0].choices[0].delta.tool_calls]
    assert names == ["terminal"]
    args = json.loads(tool_chunks[0].choices[0].delta.tool_calls[0].function.arguments)
    assert args["command"] == "ls"
    assert tool_chunks[0].choices[0].finish_reason == "tool_calls"
    usage_chunks = [c for c in chunks if not c.choices]
    assert usage_chunks
    assert usage_chunks[-1].usage.prompt_tokens == 111
    assert usage_chunks[-1].usage.completion_tokens == 9
    assert "session/cancel" in process.methods()


def test_stream_without_vendor_usage_does_not_stamp_zeros(tmp_path):
    process = _FakeACPProcess(pid=402, reply="done")
    process.tool_updates = [
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "tc-ls",
            "kind": "execute",
            "status": "in_progress",
            "title": "ls",
            "rawInput": {"command": "ls"},
        },
    ]
    spawner = _FakeACPSpawner(process)
    client = KiroACPClient(command="kiro-cli", args=["acp"], acp_cwd=str(tmp_path))
    with patch("agent.acp_stdio_transport.subprocess.Popen", spawner):
        chunks = list(
            client.chat.completions.create(
                model="claude-opus-5",
                messages=[{"role": "user", "content": "hi"}],
                stream=True,
                timeout=5,
            )
        )
    usage_chunks = [c for c in chunks if getattr(c, "usage", None) is not None]
    assert usage_chunks == []


def _stream_would_mark_truncated(chunks) -> bool:
    """Mirror chat_completion_helpers text-only drop: no finish_reason + text."""
    finish_reason = None
    has_content = False
    has_tools = False
    for chunk in chunks:
        if not getattr(chunk, "choices", None):
            continue
        delta = chunk.choices[0].delta
        if getattr(delta, "content", None):
            has_content = True
        if getattr(delta, "tool_calls", None):
            has_tools = True
        reason = getattr(chunk.choices[0], "finish_reason", None)
        if reason:
            finish_reason = reason
    return finish_reason is None and has_content and not has_tools


def test_xml_in_assistant_text_is_not_executed_as_a_tool(tmp_path):
    xml = (
        '<tool_call>{"id": "call_1", "type": "function", '
        '"function": {"name": "terminal", "arguments": "{\\"command\\": \\"pwd\\"}"}}'
        "</tool_call>"
    )
    process = _FakeACPProcess(pid=500, reply=xml)
    spawner = _FakeACPSpawner(process)
    client = KiroACPClient(command="kiro-cli", args=["acp"], acp_cwd=str(tmp_path))
    with patch("agent.acp_stdio_transport.subprocess.Popen", spawner):
        completion = client.chat.completions.create(
            model="claude-opus-5",
            messages=[{"role": "user", "content": "hi"}],
            timeout=5,
        )
    assert not completion.choices[0].message.tool_calls
    assert completion.choices[0].finish_reason == "stop"


def test_stream_does_not_paint_tool_progress_as_reasoning(tmp_path):
    process = _FakeACPProcess(pid=412, reply="Checking.")
    process.tool_updates = [
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "tc-pwd",
            "kind": "execute",
            "status": "in_progress",
            "rawInput": {"command": "pwd"},
        }
    ]
    spawner = _FakeACPSpawner(process)
    client = KiroACPClient(command="kiro-cli", args=["acp"], acp_cwd=str(tmp_path))
    with patch("agent.acp_stdio_transport.subprocess.Popen", spawner):
        stream = client.chat.completions.create(
            model="claude-opus-5",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
            timeout=5,
        )
        chunks = list(stream)
    reasoning = "".join(
        str(c.choices[0].delta.reasoning or "")
        for c in chunks
        if getattr(c, "choices", None)
    )
    assert "💻" not in reasoning
    assert "session/cancel" in process.methods()


def test_stream_text_only_yields_finish_reason_stop(tmp_path):
    """Live text must end with finish_reason=stop, not a silent iterator end."""
    process = _FakeACPProcess(pid=410, reply="Hi. What would you like to work on?")
    spawner = _FakeACPSpawner(process)
    client = KiroACPClient(command="kiro-cli", args=["acp"], acp_cwd=str(tmp_path))
    with patch("agent.acp_stdio_transport.subprocess.Popen", spawner):
        stream = client.chat.completions.create(
            model="claude-opus-5",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
            timeout=5,
        )
        chunks = list(stream)
    finish_reasons = [
        c.choices[0].finish_reason
        for c in chunks
        if getattr(c, "choices", None) and c.choices[0].finish_reason
    ]
    assert finish_reasons[-1] == "stop"
    assert not _stream_would_mark_truncated(chunks)
    text = "".join(
        str(c.choices[0].delta.content or "")
        for c in chunks
        if getattr(c, "choices", None)
    )
    assert "What would you like" in text


def test_stream_survives_quiet_gap_then_finishes_stop(tmp_path):
    """A 20s quiet think after the first update is not EOF / truncated.

    session/prompt stays in flight across the gap, then a final message
    plus the RPC result must yield finish_reason=stop.
    """
    process = _FakeACPProcess(pid=411, reply="final message")
    process.reply_prefix = "partial "
    process.mid_prompt_gap_s = 20
    spawner = _FakeACPSpawner(process)
    client = KiroACPClient(command="kiro-cli", args=["acp"], acp_cwd=str(tmp_path))
    with patch("agent.acp_stdio_transport.subprocess.Popen", spawner):
        stream = client.chat.completions.create(
            model="claude-opus-5",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
            timeout=60,
        )
        chunks = list(stream)
    text = "".join(
        str(c.choices[0].delta.content or "")
        for c in chunks
        if getattr(c, "choices", None)
    )
    assert "partial" in text
    assert "final message" in text
    finish_reasons = [
        c.choices[0].finish_reason
        for c in chunks
        if getattr(c, "choices", None) and c.choices[0].finish_reason
    ]
    assert finish_reasons[-1] == "stop"
    assert not _stream_would_mark_truncated(chunks)
    keepalives = [
        c for c in chunks
        if not getattr(c, "choices", None) and getattr(c, "usage", None) is None
    ]
    assert keepalives, "quiet gap must emit keep-alives, not close the stream"


def test_permission_toolcall_without_session_update_becomes_hermes_tool_call(tmp_path):
    """Kiro often only names the tool on request_permission, not session/update."""
    process = _FakeACPProcess(pid=403, reply="need a command")
    process.permission_requests = [
        {
            "options": [{"optionId": "allow_once"}],
            "toolCall": {
                "toolCallId": "tc-perm",
                "kind": "execute",
                "title": "pwd",
                "rawInput": {"command": "pwd"},
            },
        }
    ]
    spawner = _FakeACPSpawner(process)
    client = KiroACPClient(command="kiro-cli", args=["acp"], acp_cwd=str(tmp_path))
    with patch("agent.acp_stdio_transport.subprocess.Popen", spawner):
        completion = client.chat.completions.create(
            model="claude-opus-5",
            messages=[{"role": "user", "content": "hi"}],
            timeout=5,
        )
    calls = completion.choices[0].message.tool_calls
    assert calls
    assert calls[0].function.name == "terminal"
    assert json.loads(calls[0].function.arguments)["command"] == "pwd"


def test_nonstream_native_read_becomes_hermes_read_file_tool_call(tmp_path):
    process = _FakeACPProcess(pid=402, reply="reading")
    process.tool_updates = [
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "tc-rd",
            "kind": "read",
            "status": "in_progress",
            "locations": [{"path": "/tmp/foo.py"}],
        },
    ]
    spawner = _FakeACPSpawner(process)
    client = KiroACPClient(command="kiro-cli", args=["acp"], acp_cwd=str(tmp_path))
    with patch("agent.acp_stdio_transport.subprocess.Popen", spawner):
        completion = client.chat.completions.create(
            model="claude-opus-5",
            messages=[{"role": "user", "content": "hi"}],
            timeout=5,
        )
    calls = completion.choices[0].message.tool_calls
    assert calls
    assert calls[0].function.name == "read_file"
    assert json.loads(calls[0].function.arguments)["path"] == "/tmp/foo.py"
    assert completion.choices[0].finish_reason == "tool_calls"


def test_permission_allowed_helper_still_picks_allow_once_when_asked():
    from agent.acp_stdio_transport import permission_allowed, permission_denied

    assert permission_allowed(1, [{"optionId": "allow_once"}])["result"]["outcome"]["optionId"] == "allow_once"
    assert permission_denied(2)["result"]["outcome"]["outcome"] == "cancelled"


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
