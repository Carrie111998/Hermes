"""Stamping the calling conversation's identity onto every MCP call.

An MCP server that must answer back into the conversation that called it needs to
know which conversation that was. That is transport context, not task input.
delegate-wave briefly made it an optional MODEL argument; a model omitted it, the
work ran to completion, and nobody was told. The model now never sees it.

Two properties are load-bearing and neither is visible from reading the call
site: the identity must be read on the TURN's own thread rather than on the
shared MCP background loop, and the key must match what the receiver looks for.
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
    """WHY THE PLACEMENT MATTERS, ASSERTED STRUCTURALLY.

    The MCP session runs on a long-lived background loop shared by every
    conversation, and it does not inherit this turn's ContextVars -- the exact
    problem gateway/session_context.py exists to solve. Reading the id there
    would yield whichever conversation last touched the loop, or nothing at all.
    """
    for inner in ast.walk(handler_node):
        if not isinstance(inner, ast.AsyncFunctionDef):
            continue
        for call in ast.walk(inner):
            if isinstance(call, ast.Call) and getattr(call.func, "id", "") == "get_session_env":
                raise AssertionError(
                    f"the identity is read inside async {inner.name}(), which runs on the "
                    "shared MCP loop and cannot see this turn's session"
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
        "the identity is captured at or after the RPC; it must be taken before "
        "anything is scheduled onto the MCP loop"
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
