"""Two conversations, one shared MCP session, correct identity on each call.

The structural tests assert WHERE the caller id is read. This one asserts what
that placement is for, by running it: two turns bound to different sessions enter
the same handler concurrently, and each call must carry its own conversation.

This is the property an environment variable could never provide. The stdio
server is spawned once and outlives every conversation, so anything baked into
its process would address all of them to whichever chat started it. The read has
to happen on the turn's own thread, where the ContextVar is bound -- not on the
long-lived MCP loop, which belongs to nobody.
"""

import threading

import pytest

import tools.mcp_tool as mcp_tool
from gateway.session_context import set_session_vars


class _RecordingSession:
    """Stands in for the live MCP ClientSession, capturing what each call carried."""

    def __init__(self):
        self.calls = []
        self._lock = threading.Lock()

    async def call_tool(self, name, arguments=None, meta=None, **kwargs):
        # A real call crosses a thread boundary; sleeping here widens the window
        # in which two turns can interleave and clobber a shared value.
        import asyncio

        await asyncio.sleep(0.05)
        with self._lock:
            self.calls.append({"name": name, "meta": dict(meta or {})})

        class _Result:
            isError = False
            content = []
            structuredContent = None

        return _Result()


@pytest.fixture
def wired(monkeypatch):
    """A handler wired to a recording session, for a server that opted in.

    The real background MCP loop is started, because it IS the boundary under
    test: the handler runs on the calling thread and the RPC runs over there, and
    the whole question is whether the identity survives that hop.
    """
    mcp_tool._ensure_mcp_loop()
    session = _RecordingSession()

    server = type("_Server", (), {})()
    server.session = session
    server.name = "delegate_wave"
    server._rpc_lock = __import__("asyncio").Lock()
    server._stdio_child_pids = None
    server._is_http = lambda: False
    server._stdio_children_dead = lambda: False
    server._watch_stdio_children = None
    server._pending_call_context = None

    monkeypatch.setitem(mcp_tool._server_wants_caller_session, "delegate_wave", True)
    # The handler resolves its server through _get_connected_server_for_call; a
    # guess at the wrong name silently produced a handler that never called
    # anything, which the test then reported as a missing call rather than a
    # missing stub.
    monkeypatch.setattr(
        mcp_tool, "_get_connected_server_for_call", lambda name: server, raising=False
    )
    return server, session


def _drive(handler, session_id, results, index):
    """One turn: bind its session, call the tool, record what came back."""
    set_session_vars(session_id=session_id)
    try:
        results[index] = handler({"project_id": "p", "intent": "x"})
    except Exception as exc:  # captured so a failure names the turn
        results[index] = f"ERROR: {exc}"


def test_two_concurrent_turns_do_not_swap_identities(wired, monkeypatch):
    """THE FALSIFICATION THAT MATTERS.

    Reverting the synchronous capture -- reading the id on the MCP loop instead --
    makes both calls carry the same value, or neither carry one, because that loop
    has no turn bound to it.
    """
    server, session = wired
    handler = mcp_tool._make_tool_handler("delegate_wave", "session_start", 30.0)

    results = [None, None]
    turns = [
        threading.Thread(target=_drive, args=(handler, "session_AAA", results, 0)),
        threading.Thread(target=_drive, args=(handler, "session_BBB", results, 1)),
    ]
    for thread in turns:
        thread.start()
    for thread in turns:
        thread.join(timeout=60)

    carried = sorted(
        call["meta"].get(mcp_tool.CALLER_SESSION_META_KEY) for call in session.calls
    )
    assert len(session.calls) == 2, f"both turns must have called: {results}"
    assert carried == ["session_AAA", "session_BBB"], (
        f"identities were swapped, shared or lost: {carried} (results={results})"
    )


def test_a_server_that_did_not_ask_receives_nothing(wired, monkeypatch):
    """Default off. A stable handle on somebody's conversation is not broadcast.

    The first version attached it to every call on every server, which made one
    integration's transport contract a disclosure to every third party the user
    had configured.
    """
    server, session = wired
    monkeypatch.setitem(mcp_tool._server_wants_caller_session, "delegate_wave", False)
    handler = mcp_tool._make_tool_handler("delegate_wave", "session_start", 30.0)

    set_session_vars(session_id="session_PRIVATE")
    handler({"project_id": "p", "intent": "x"})

    assert session.calls, "the call still has to happen"
    for call in session.calls:
        assert not call["meta"].get(mcp_tool.CALLER_SESSION_META_KEY), (
            "a server that did not opt in received the caller's conversation id"
        )
