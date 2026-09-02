"""Security and parity tests for read-only MCP tools in execute_code."""

import base64
import json
import socket
import threading
from unittest.mock import patch

from tools.code_execution_tool import (
    _rpc_poll_loop,
    _rpc_server_loop,
    _sandbox_mcp_tools,
    build_execute_code_schema,
    generate_hermes_tools_module,
)
from tools import mcp_tool


READ_TOOL = "mcp__linear__search_issues"
WRITE_TOOL = "mcp__linear__create_issue"


def test_discovery_view_requires_exact_read_only_hint_and_registration():
    provenance = {
        READ_TOOL: "linear",
        WRITE_TOOL: "linear",
        "mcp__linear__list_resources": "linear",
    }
    read_only = {
        READ_TOOL: True,
        WRITE_TOOL: False,
        "mcp__linear__list_resources": False,
    }
    with patch.dict(mcp_tool._mcp_tool_server_names, provenance, clear=True), patch.dict(
        mcp_tool._mcp_tool_read_only, read_only, clear=True
    ):
        assert mcp_tool.get_read_only_mcp_tools() == {READ_TOOL: "linear"}


def test_exposure_is_off_by_default_and_can_select_server_or_tool():
    discovered = {
        READ_TOOL: "linear",
        "mcp__notion__query": "notion",
    }
    available = set(discovered) | {WRITE_TOOL}
    with patch(
        "tools.mcp_tool.get_read_only_mcp_tools", return_value=discovered
    ):
        assert _sandbox_mcp_tools(available, {}) == frozenset()
        assert _sandbox_mcp_tools(
            available, {"expose_mcp_tools": True}
        ) == frozenset(discovered)
        assert _sandbox_mcp_tools(
            available, {"expose_mcp_tools": ["linear"]}
        ) == frozenset({READ_TOOL})
        assert _sandbox_mcp_tools(
            available, {"expose_mcp_tools": ["mcp__notion__query"]}
        ) == frozenset({"mcp__notion__query"})


def test_stub_and_schema_exposure_stay_compact_and_name_only():
    names = {f"mcp__server__read_{index}" for index in range(30)}
    src = generate_hermes_tools_module(
        ["terminal", *names], mcp_tools=list(names)
    )
    compile(src, "hermes_tools.py", "exec")
    for name in names:
        assert f"def {name}(**kwargs):" in src

    base = build_execute_code_schema({"terminal"}, mode="strict")["description"]
    expanded = build_execute_code_schema(
        {"terminal"}, mode="strict", enabled_mcp_tools=names
    )["description"]
    for name in names:
        assert expanded.count(f"{name}(**kwargs)") == 1
    assert "same keyword args as their model-visible schemas" in expanded
    # Schema growth is only the names/signature markers plus one compact label;
    # no repeated MCP descriptions or parameter schemas ride inside it.
    assert len(expanded) - len(base) <= sum(len(name) + len("(**kwargs), ") for name in names) + 160


class _OneShotListener:
    def __init__(self, conn):
        self.conn = conn
        self.served = False

    def settimeout(self, _timeout):
        pass

    def accept(self):
        if self.served:
            raise socket.timeout()
        self.served = True
        return self.conn, ("peer", 0)


def test_local_rpc_refuses_unexposed_raw_mcp_and_enforces_sub_budget():
    server, client = socket.socketpair()
    stop = threading.Event()
    total_counter = [0]
    mcp_counter = [0]
    log = []

    def dispatch(tool_name, args):
        return json.dumps({"tool": tool_name, "args": args})

    thread = threading.Thread(
        target=_rpc_server_loop,
        args=(
            _OneShotListener(server), "task", log, total_counter, 50,
            frozenset({READ_TOOL}), stop, "token",
        ),
        kwargs={
            "dispatch": dispatch,
            "mcp_tools": frozenset({READ_TOOL}),
            "mcp_tool_call_counter": mcp_counter,
            "max_mcp_tool_calls": 1,
        },
        daemon=True,
    )
    with patch(
        "tools.code_execution_tool._sandbox_mcp_tools",
        return_value=frozenset({READ_TOOL}),
    ):
        thread.start()
        requests = [
            {"tool": WRITE_TOOL, "args": {}, "token": "token"},
            {"tool": READ_TOOL, "args": {"q": "one"}, "token": "token"},
            {"tool": READ_TOOL, "args": {"q": "two"}, "token": "token"},
        ]
        client.sendall(
            "".join(json.dumps(request) + "\n" for request in requests).encode()
        )
        stream = client.makefile("r", encoding="utf-8")
        responses = [json.loads(stream.readline()) for _ in requests]
        stream.close()

    stop.set()
    client.close()
    server.close()
    thread.join(timeout=5)
    assert "not available" in responses[0]["error"]
    assert responses[1]["tool"] == READ_TOOL
    assert "MCP tool call limit reached (1)" in responses[2]["error"]
    assert total_counter == [1]
    assert mcp_counter == [1]
    assert log[0]["source"] == "mcp"


def test_remote_rpc_uses_same_raw_allowlist_policy():
    stop = threading.Event()

    class FakeRemoteEnv:
        def __init__(self):
            self.response = ""
            self.listed = False

        def execute(self, command, cwd=None, timeout=None):
            if command.startswith("ls -1"):
                if self.listed:
                    return {"output": ""}
                self.listed = True
                return {"output": "/rpc/req_000001\n"}
            if command == "cat /rpc/req_000001":
                return {
                    "output": json.dumps(
                        {"tool": WRITE_TOOL, "args": {}, "seq": 1, "token": "token"}
                    )
                }
            if command.startswith("echo '"):
                encoded = command.split("'", 2)[1]
                self.response = base64.b64decode(encoded).decode("utf-8")
                stop.set()
            return {"output": ""}

    env = FakeRemoteEnv()
    _rpc_poll_loop(
        env,
        "/rpc",
        "task",
        [],
        [0],
        50,
        frozenset({READ_TOOL}),
        stop,
        "token",
        mcp_tools=frozenset({READ_TOOL}),
        mcp_tool_call_counter=[0],
        max_mcp_tool_calls=10,
    )
    assert "not available" in json.loads(env.response)["error"]
