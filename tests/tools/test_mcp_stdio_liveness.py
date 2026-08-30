"""Whether a stdio MCP server is alive, answered correctly.

The check exists to fail a call fast when the subprocess behind it has died,
instead of waiting out a 300-second tool timeout on a transport nobody will
answer. Its two branches were transposed, so it reported the opposite: a HEALTHY
subprocess was declared dead and every tools/call failed instantly.

That is worse than the problem it was written to solve, and it is invisible from
the outside -- the server starts, discovery lists its tools, and then every call
to those tools fails with "subprocess has exited" while the process is running
and answering the identical calls by hand.
"""

import types

import pytest

from tools.mcp_tool import MCPServerTask


class _Server:
    """The two attributes the check reads, without building a real MCPServer."""

    def __init__(self, pids):
        self._stdio_child_pids = pids

    def _is_http(self):
        return False

    _stdio_children_dead = MCPServerTask._stdio_children_dead


def test_a_running_child_is_not_reported_dead(monkeypatch):
    """The bug, stated directly.

    Before the fix this returned True for a live process, so a working MCP
    server could never be called.
    """
    monkeypatch.setattr("psutil.pid_exists", lambda pid: True)
    assert _Server([4242])._stdio_children_dead() is False


def test_children_that_have_all_exited_are_reported_dead(monkeypatch):
    """The case the check was written for still works."""
    monkeypatch.setattr("psutil.pid_exists", lambda pid: False)
    assert _Server([4242, 4243])._stdio_children_dead() is True


def test_one_survivor_keeps_the_transport_alive(monkeypatch):
    """A server is only dead when NOTHING it spawned is left.

    Reporting dead while a child still runs would fast-fail calls that a live
    process was about to answer -- the same failure as the original bug, reached
    from the other direction.
    """
    alive = {4243}
    monkeypatch.setattr("psutil.pid_exists", lambda pid: pid in alive)
    assert _Server([4242, 4243])._stdio_children_dead() is False


def test_unknown_liveness_never_fails_a_call(monkeypatch):
    """No captured pids means no evidence, and no evidence must not fail a call.

    Fast-failing is an optimisation. Guessing at it costs a working tool call.
    """
    monkeypatch.setattr("psutil.pid_exists", lambda pid: False)
    assert _Server([])._stdio_children_dead() is False
    assert _Server(None)._stdio_children_dead() is False


def test_the_dead_branch_is_reachable():
    """The unreachable `return False` under the old `return True` was the tell.

    A branch nobody can reach is not a guard; it is a comment that looks like
    code. This asserts the function has no statement after a return in the same
    block.
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(MCPServerTask._stdio_children_dead)))
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list):
            continue
        for index, statement in enumerate(body[:-1]):
            assert not isinstance(statement, ast.Return), (
                "there is a statement after a return in the same block -- the shape "
                "the transposed branches left behind"
            )
