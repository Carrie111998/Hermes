"""The stdio child-watcher is built once per call, then raced or closed.

The fast-fail machinery races the RPC against a coroutine that polls child
liveness. The type-test for that coroutine used to CALL it, discard the result,
and call it again for the real race -- leaking one un-awaited coroutine per
stdio tool call and emitting RuntimeWarning on a live MCP path.

A NOTE ON HOW THESE ARE WRITTEN, because the first version of this file was
vacuous and passed against the bug it was meant to catch:

  - `warnings.simplefilter("error")` does NOT catch it. The un-awaited warning is
    emitted when the orphaned coroutine is GARBAGE COLLECTED, long after the
    block exits. The warning must be recorded and gc forced.
  - Counting from inside the coroutine BODY does not catch it either. A
    discarded coroutine never runs, so its body never increments anything. The
    count has to happen in the factory that builds it.

Both mistakes made the test agree with the code instead of checking it.
"""

import asyncio
import gc
import warnings

import pytest

import tools.mcp_tool as mcp_tool


class _Result:
    isError = False
    content = []
    structuredContent = None


class _Session:
    def __init__(self):
        self.calls = 0

    async def call_tool(self, name, arguments=None, meta=None, **kwargs):
        self.calls += 1
        return _Result()


def _server(monkeypatch, *, watcher):
    mcp_tool._ensure_mcp_loop()
    server = type("_Server", (), {})()
    server.session = _Session()
    server.name = "watchdog_probe"
    server._rpc_lock = asyncio.Lock()
    server._stdio_child_pids = [1]
    server._is_http = lambda: False
    server._stdio_children_dead = lambda: False
    server._watch_stdio_children = watcher
    server._pending_call_context = None
    monkeypatch.setattr(
        mcp_tool, "_get_connected_server_for_call", lambda name: server, raising=False
    )
    return server


def _counting_watcher():
    """Returns (factory, constructed) where `constructed` counts CONSTRUCTION.

    Incremented in the factory, not in the coroutine body: the whole defect is a
    coroutine that is created and never run.
    """
    constructed = []

    async def _body():
        await asyncio.sleep(3600)  # never wins the race

    def factory():
        constructed.append(1)
        return _body()

    return factory, constructed


def test_the_watcher_is_constructed_exactly_once(monkeypatch):
    """THE FALSIFICATION. Restore the double call and this reports 2 != 1."""
    factory, constructed = _counting_watcher()
    server = _server(monkeypatch, watcher=factory)
    handler = mcp_tool._make_tool_handler("watchdog_probe", "some_tool", 30.0)

    handler({})

    assert len(constructed) == 1, (
        f"the child-watcher coroutine was built {len(constructed)} times; every "
        "extra one is created, never awaited, and leaked on a live MCP path"
    )
    assert server.session.calls == 1, "and the RPC still has to happen"


def test_no_un_awaited_coroutine_survives_the_call(monkeypatch):
    """The user-visible symptom: RuntimeWarning on every stdio tool call.

    Recorded rather than raised, and gc forced, because this warning is emitted
    from the collector -- which is precisely why turning warnings into errors
    silently failed to catch the bug.
    """
    factory, _ = _counting_watcher()
    _server(monkeypatch, watcher=factory)
    handler = mcp_tool._make_tool_handler("watchdog_probe", "some_tool", 30.0)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        handler({})
        gc.collect()

    leaked = [
        w for w in caught
        if issubclass(w.category, RuntimeWarning) and "never awaited" in str(w.message)
    ]
    assert not leaked, f"leaked un-awaited coroutine(s): {[str(w.message) for w in leaked]}"


def test_a_server_without_a_watcher_still_calls(monkeypatch):
    """The no-watcher path (stubs, http servers) keeps pre-#81995 semantics."""
    server = _server(monkeypatch, watcher=None)
    handler = mcp_tool._make_tool_handler("watchdog_probe", "some_tool", 30.0)

    handler({})

    assert server.session.calls == 1
