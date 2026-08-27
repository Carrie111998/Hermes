import os
from tools.mcp_tool import MCPServerTask


class MockTask(MCPServerTask):
    def _is_http(self):
        return False


def test_stdio_children_dead_empty():
    task = MockTask.__new__(MockTask)
    task._stdio_child_pids = []
    assert task._stdio_children_dead() is False


def test_stdio_children_dead_alive_pid():
    task = MockTask.__new__(MockTask)
    task._stdio_child_pids = [os.getpid()]
    assert task._stdio_children_dead() is False


def test_stdio_children_dead_dead_pid():
    task = MockTask.__new__(MockTask)
    task._stdio_child_pids = [9999999]
    assert task._stdio_children_dead() is True
