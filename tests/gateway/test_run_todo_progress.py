"""Tests for Telegram todo checklist rendering in gateway progress messages.

When the todo tool completes during a Telegram session, the gateway should
render a compact editable checklist in the progress message bubble. Subsequent todo
calls edit the same message in-place.
"""

import json
import queue
import logging


def _make_todo_result(todos):
    """Build the JSON string that the todo tool returns for a given item list."""
    pending = sum(1 for i in todos if i.get("status", "pending") == "pending")
    in_progress = sum(1 for i in todos if i.get("status") == "in_progress")
    completed = sum(1 for i in todos if i.get("status") == "completed")
    cancelled = sum(1 for i in todos if i.get("status") == "cancelled")
    return json.dumps({
        "todos": todos,
        "summary": {
            "total": len(todos),
            "pending": pending,
            "in_progress": in_progress,
            "completed": completed,
            "cancelled": cancelled,
        },
    }, ensure_ascii=False)


class TestRenderTodoChecklist:
    """Unit tests for the _render_todo_checklist helper in gateway/run.py."""

    def _render(self, result_str):
        from gateway.run import _render_todo_checklist
        return _render_todo_checklist(result_str)

    def test_renders_checklist_from_todo_result(self):
        """Should render a full checklist with all four status types."""
        result = _make_todo_result([
            {"id": "1", "content": "Set up CI/CD", "status": "pending"},
            {"id": "2", "content": "Write tests", "status": "in_progress"},
            {"id": "3", "content": "Deploy", "status": "completed"},
            {"id": "4", "content": "Cancel this", "status": "cancelled"},
        ])
        checklist = self._render(result)
        assert "📋 Task List" in checklist
        assert "[ ] Set up CI/CD (1)" in checklist
        assert "[>] Write tests (2)" in checklist
        assert "[x] Deploy (3)" in checklist
        assert "[-] Cancel this (4)" in checklist

    def test_empty_todos_returns_empty_string(self):
        """Empty todo list should return empty string (not crash)."""
        result = _make_todo_result([])
        assert self._render(result) == ""

    def test_invalid_json_returns_empty_string(self):
        """Invalid JSON result should return empty string (fail closed)."""
        assert self._render("not valid json") == ""
        assert self._render("") == ""
        assert self._render(None) == ""

    def test_truncates_long_content(self):
        """Items longer than 80 chars should be truncated."""
        long_content = "A" * 200
        result = _make_todo_result([
            {"id": "1", "content": long_content, "status": "pending"},
        ])
        checklist = self._render(result)
        assert len(long_content) > 80
        assert "A" * 80 in checklist
        assert "A" * 81 not in checklist

    def test_default_status_is_pending(self):
        """Items without a status should render as pending."""
        result = _make_todo_result([
            {"id": "1", "content": "No status", "status": ""},
        ])
        checklist = self._render(result)
        assert "[ ] No status" in checklist


class TestDispatchTodoProgress:
    """Adapter-level integration tests for the _dispatch_todo_progress function.

    Tests the actual dispatch path used by progress_callback — not a copy of
    the logic — by importing and exercising the module-level function directly.
    """

    def _dispatch(self, event_type, tool_name, result_str):
        """Call the real dispatch function and return (consumed, queue_items)."""
        from gateway.run import _dispatch_todo_progress

        q = queue.Queue()
        log = logging.getLogger("test-dispatch")
        consumed = _dispatch_todo_progress(q, event_type, tool_name, result_str, log)
        items = []
        while not q.empty():
            items.append(q.get_nowait())
        return consumed, items

    def test_dispatch_todo_completed_pushes_to_queue(self):
        """A tool.completed todo event should render a checklist and push it."""
        result = _make_todo_result([
            {"id": "t1", "content": "Update docs", "status": "pending"},
            {"id": "t2", "content": "Run suite", "status": "completed"},
        ])
        consumed, items = self._dispatch("tool.completed", "todo", result)
        assert consumed is True
        assert len(items) == 1
        assert "📋 Task List" in items[0]

    def test_dispatch_returns_false_for_non_todo_completed(self):
        """Non-todo completed events should not be consumed."""
        result = _make_todo_result([{"id": "1", "content": "Test", "status": "pending"}])
        consumed, items = self._dispatch("tool.completed", "search_files", result)
        assert consumed is False
        assert items == []

    def test_dispatch_returns_false_for_non_completed(self):
        """Events other than tool.completed should not be consumed."""
        result = _make_todo_result([{"id": "1", "content": "Test", "status": "pending"}])
        consumed, items = self._dispatch("tool.started", "todo", result)
        assert consumed is False
        assert items == []

    def test_dispatch_empty_result_pushes_nothing(self):
        """Empty/missing result should not push anything but still be consumed."""
        consumed, items = self._dispatch("tool.completed", "todo", "")
        assert consumed is True
        assert items == []

    def test_dispatch_invalid_result_pushes_nothing(self):
        """Invalid result should not push anything but still be consumed."""
        consumed, items = self._dispatch("tool.completed", "todo", "{{{bad json")
        assert consumed is True
        assert items == []

    def test_dispatch_empty_todos_pushes_nothing(self):
        """Result with empty todos should not push anything but still be consumed."""
        result = _make_todo_result([])
        consumed, items = self._dispatch("tool.completed", "todo", result)
        assert consumed is True
        assert items == []

    def test_dispatch_logger_error_does_not_raise(self):
        """A logger exception inside dispatch should not propagate to the caller."""
        from gateway.run import _dispatch_todo_progress

        result = _make_todo_result([{"id": "1", "content": "Test", "status": "pending"}])
        q = queue.Queue()
        # A logger that raises on debug calls
        class _BrokenLogger:
            def debug(self, msg, **kwargs):
                raise RuntimeError("intentional test failure")
        # Should not raise — the except blocks it
        consumed = _dispatch_todo_progress(q, "tool.completed", "todo", result, _BrokenLogger())  # noqa: F841
        consumed = _dispatch_todo_progress(q, "tool.completed", "todo", result, _BrokenLogger())

    def test_dispatch_none_result_handled_gracefully(self):
        """None result should not crash — treated as empty."""
        consumed, items = self._dispatch("tool.completed", "todo", None)
        assert consumed is True
        assert items == []