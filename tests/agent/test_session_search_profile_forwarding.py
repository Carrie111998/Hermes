"""Regression tests: the session_search `profile` parameter must survive the
inline dispatch paths.

`session_search(profile="X")` is the documented way to read another profile's
session DB (resolving an ``@session:<profile>/<id>`` link). The tool schema
accepts the parameter and ``session_search()`` implements it, but the inline
agent-level dispatch sites in ``agent/agent_runtime_helpers.py`` (concurrent
path) and ``agent/tool_executor.py`` (sequential path) rebuilt the kwargs by
hand and silently dropped ``profile`` — a cross-profile query then ran against
the caller's own DB and returned confidently wrong results.
"""

import inspect
from unittest.mock import MagicMock

from agent import agent_runtime_helpers, tool_executor


class _FakeSessionDB:
    """Stand-in for the recall SessionDB; the dispatch only passes it through."""

    closed = 0

    def close(self):
        self.closed += 1


def _make_agent():
    return MagicMock(
        session_id="session-dispatch",
        _get_session_db_for_recall=lambda: _FakeSessionDB(),
    )


def _capture_session_search(monkeypatch):
    captured = {}

    def _fake_session_search(**kwargs):
        captured.update(kwargs)
        return '{"results": []}'

    monkeypatch.setattr(
        "tools.session_search_tool.session_search", _fake_session_search
    )
    return captured


_ARGS = {
    "query": "deploy notes",
    "session_id": "sess-other-profile",
    "profile": "work",
}


class TestInvokeToolForwardsProfile:
    def test_concurrent_dispatch_forwards_profile(self, monkeypatch):
        captured = _capture_session_search(monkeypatch)
        agent = _make_agent()

        agent_runtime_helpers.invoke_tool(
            agent,
            "session_search",
            dict(_ARGS),
            effective_task_id="",
            # Skip both middleware layers: this test exercises the plain
            # dispatch table, not the middleware pipeline.
            skip_tool_request_middleware=True,
            skip_tool_execution_middleware=True,
        )

        assert captured.get("profile") == "work", (
            "invoke_tool dropped the profile parameter — the cross-profile "
            "query ran against the caller's own session DB"
        )
        assert captured.get("session_id") == "sess-other-profile"

    def test_profile_absent_passes_none(self, monkeypatch):
        captured = _capture_session_search(monkeypatch)
        agent = _make_agent()

        agent_runtime_helpers.invoke_tool(
            agent,
            "session_search",
            {"query": "anything"},
            effective_task_id="",
            skip_tool_request_middleware=True,
            skip_tool_execution_middleware=True,
        )

        # No profile given: session_search() keeps its default behavior
        # (the caller's own DB).
        assert "profile" in captured
        assert captured["profile"] is None


class TestSequentialDispatchForwardsProfile:
    def test_sequential_dispatch_calls_session_search_with_profile(self, monkeypatch):
        """The sequential tool executor's session_search branch forwards profile.

        That branch lives inside a closure rebuilt per tool-call inside
        ``execute_tool_calls_sequential``; the cheapest reliable probe is the
        source contract: the hand-built kwargs must include ``profile=``.
        A behavioral test would need a full assistant-message + agent-loop
        scaffold disproportionate to a one-kwarg forwarding fix.
        """

        source = inspect.getsource(tool_executor.execute_tool_calls_sequential)
        start = source.index('elif function_name == "session_search":')
        end = source.index("elif", start + 10)
        branch = source[start:end]
        assert 'profile=next_args.get("profile")' in branch, (
            "the sequential session_search dispatch must forward the profile "
            "parameter alongside the other hand-listed kwargs"
        )
