"""Stamping the calling conversation's identity onto every MCP call.

An MCP server that must answer back into the conversation that called it needs to
know which conversation that was. That is transport context, not task input.
delegate-wave briefly made it an optional MODEL argument; a model omitted it, the
work ran to completion, and nobody was told. The model now never sees it.

Two things are guarded here. The key must match what the receiver looks for --
that one is load-bearing. And the identity is read on the turn's own thread
rather than on the shared MCP loop, which is a deliberate choice rather than a
requirement: context propagation makes both work today, and the test explains why
the safer one is kept anyway. Per-turn isolation itself is proved by running it,
in test_mcp_caller_identity_runtime.py.
"""

import ast
import inspect
import pathlib

import pytest

from tools.mcp_tool import CALLER_SESSION_META_KEY


def test_the_key_matches_the_receiver():
    """Pinned to a literal on both sides so they cannot drift apart.

    delegate-wave keys its watch registration off this exact string
    (src/mcp/server.js, CALLER_SESSION_META_KEY). Renaming one side alone returns
    the system to starting sessions nobody is watching -- silently, because a
    missing key is indistinguishable from a caller that never sent one.
    """
    assert CALLER_SESSION_META_KEY == "io.delegate-wave/hermes-session-id"


@pytest.fixture(scope="module")
def handler_node():
    """The synchronous `_handler` that MCP tool calls enter through.

    Parsed from the whole module rather than a text slice: an earlier version of
    this test cut a fixed number of characters out of the source and handed
    ast.parse an unclosed expression, so it failed for its own reasons rather
    than the code's.
    """
    import tools.mcp_tool as mcp_tool

    tree = ast.parse(pathlib.Path(inspect.getsourcefile(mcp_tool)).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "_handler":
            continue
        if any(
            isinstance(call, ast.Call) and getattr(call.func, "id", "") == "get_session_env"
            for call in ast.walk(node)
        ):
            return node
    raise AssertionError("no synchronous _handler reads the caller identity any more")


def test_the_identity_is_read_on_the_turns_own_thread(handler_node):
    """A DELIBERATE CONSTRAINT, NOT A CORRECTNESS REQUIREMENT. Measured.

    The obvious claim -- "reading this on the MCP loop would see the wrong
    conversation" -- is FALSE in this codebase, and asserting it would have been a
    test enforcing a rule nobody can justify. asyncio.run_coroutine_threadsafe
    propagates the calling thread's context, so a read inside the coroutine
    returns the right value: measured directly, two turns reading on the loop
    still came back ['AAA', 'BBB'].

    The synchronous read is kept anyway, and this test keeps it, because the
    correctness of the alternative rests on a subtle property of how the call is
    scheduled. Anyone replacing run_coroutine_threadsafe with something that does
    not copy context would silently address every answer to the wrong
    conversation -- and nothing else here would notice.

    So this guards a choice, not a law. Per-turn isolation itself is proved at
    runtime by tests/tools/test_mcp_caller_identity_runtime.py, which fails
    against a genuinely shared value.
    """
    for inner in ast.walk(handler_node):
        if not isinstance(inner, ast.AsyncFunctionDef):
            continue
        for call in ast.walk(inner):
            if isinstance(call, ast.Call) and getattr(call.func, "id", "") == "get_session_env":
                raise AssertionError(
                    f"the identity is read inside async {inner.name}(). It works today only "
                    "because run_coroutine_threadsafe copies the caller's context; keep the "
                    "read on the turn's own thread so correctness does not depend on that"
                )


def test_it_is_read_before_the_call_is_issued(handler_node):
    reads = [
        node.lineno for node in ast.walk(handler_node)
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "get_session_env"
    ]
    calls = [
        node.lineno for node in ast.walk(handler_node)
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "call_tool"
    ]
    assert reads and calls, "handler no longer both reads the identity and issues the call"
    assert min(reads) < min(calls), (
        "the identity is captured at or after the RPC; taking it first keeps the value "
        "independent of how the call is scheduled"
    )


def test_the_identity_actually_travels_with_the_call(handler_node):
    """The read is worthless if the value never leaves the handler."""
    for node in ast.walk(handler_node):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "call_tool":
            keywords = {kw.arg for kw in node.keywords}
            assert "meta" in keywords, (
                "call_tool no longer carries the caller identity, so the receiver "
                "has no way to know which conversation is waiting"
            )
            return
    raise AssertionError("no call_tool invocation found in the handler")
