"""Behavior tests for MCPServerTask._stdio_children_dead (#81995 fast-fail).

Contract: returns True only when EVERY tracked stdio child is dead;
returns False while at least one tracked child is alive.
"""
import os
import sys

from tools.mcp_tool import MCPServerTask


def _task_with_pids(monkeypatch, pids):
    task = MCPServerTask("test-server")
    monkeypatch.setattr(task, "_stdio_child_pids", set(pids), raising=False)
    return task


def test_returns_false_when_child_alive(monkeypatch):
    """A live PID must yield False — this was inverted, failing every stdio
    MCP tool call on hosts where psutil is installed (all Windows installs)."""
    live_pid = os.getpid()  # our own interpreter is definitely running
    task = _task_with_pids(monkeypatch, [live_pid])
    assert task._stdio_children_dead() is False


def test_returns_true_when_all_children_dead(monkeypatch):
    task = _task_with_pids(monkeypatch, [-1])  # PID -1 never exists
    assert task._stdio_children_dead() is True


def test_returns_false_when_any_child_alive(monkeypatch):
    dead, alive = -1, os.getpid()
    task = _task_with_pids(monkeypatch, [dead, alive])
    assert task._stdio_children_dead() is False


def test_returns_false_without_tracked_pids(monkeypatch):
    """No captured PIDs → unknown → must NOT fail fast."""
    task = _task_with_pids(monkeypatch, [])
    assert task._stdio_children_dead() is False
