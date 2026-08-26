"""Tests for MCPServerTask._stdio_children_dead (#81995 fast-fail regression).

The fast-fail guard treats a ``True`` return as "all stdio children have
exited" and rejects the tool call before sending it. A regression
(``786f37071``, the psutil.pid_exists conversion) inverted the liveness
branch: a *live* child returned ``True``, so every tool call to a healthy
stdio MCP server failed instantly with "MCP stdio subprocess ... has exited".
"""

import psutil
import pytest

from tools.mcp_tool import MCPServerTask


@pytest.fixture
def task():
    t = MCPServerTask("test-server")
    t._config = {}  # no "url" → stdio transport
    return t


def test_empty_pids_returns_false(task):
    task._stdio_child_pids = set()
    assert task._stdio_children_dead() is False


def test_http_transport_returns_false(task):
    task._config = {"url": "https://example.com/mcp"}
    task._stdio_child_pids = {999999}
    assert task._stdio_children_dead() is False


def test_live_child_returns_false(task, monkeypatch):
    monkeypatch.setattr(psutil, "pid_exists", lambda pid: pid == 12345)
    task._stdio_child_pids = {12345}
    assert task._stdio_children_dead() is False


def test_all_children_dead_returns_true(task, monkeypatch):
    monkeypatch.setattr(psutil, "pid_exists", lambda pid: False)
    task._stdio_child_pids = {12345, 67890}
    assert task._stdio_children_dead() is True


def test_mixed_alive_and_dead_returns_false(task, monkeypatch):
    monkeypatch.setattr(psutil, "pid_exists", lambda pid: pid == 12345)
    task._stdio_child_pids = {12345, 67890}
    assert task._stdio_children_dead() is False
