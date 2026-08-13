"""Tests for continuable-children v1 (delegation durable identity).

Covers the SAFE SUBSET of the continuable-children feature:
  1. Durable ID — children spawned with the ``continuable`` opt-in expose
     ``child_session_id`` (the child's persisted session id) and
     ``subagent_id`` on every result entry and on the background dispatch
     payload. Default (non-continuable) delegations stay byte-identical.
  2. Settlement notice — the async completion event carries the child ids
     and the re-injection block renders a 'Child <id> finished ...' line
     keyed by the durable id (single and batch shapes).
  3. Report v1 — a subagent session (source='subagent', parent_session_id
     set) is re-readable by id via session_search's READ shape.

All tests run with mocks — no LLM calls. Deferred (send_message to child,
interrupt, cold resume) are intentionally NOT covered here.
"""

import json
import queue
import time
from unittest.mock import MagicMock, patch

import pytest

from tools.delegate_tool import (
    DELEGATE_TASK_SCHEMA,
    _attach_continuable_ids,
    _continuable_child_ids,
    _run_single_child,
    delegate_task,
)
from tools.process_registry import format_process_notification


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_parent():
    parent = MagicMock()
    parent.base_url = "https://openrouter.ai/api/v1"
    parent.api_key = "***"
    parent.provider = "openrouter"
    parent.api_mode = "chat_completions"
    parent.model = "anthropic/claude-sonnet-4"
    parent.platform = "cli"
    parent.providers_allowed = None
    parent.providers_ignored = None
    parent.providers_order = None
    parent.provider_sort = None
    parent._session_db = None
    parent._delegate_depth = 0
    parent._active_children = []
    parent._active_children_lock = MagicMock()
    parent._print_fn = None
    parent.tool_progress_callback = None
    parent.thinking_callback = None
    return parent


def _mock_child(*, continuable=True, session_id="child-sess-1", subagent_id="sa-0-ab12cd34"):
    child = MagicMock()
    child.model = "claude-sonnet-4-6"
    child.session_prompt_tokens = 100
    child.session_completion_tokens = 50
    child._credential_pool = None
    child._continuable = continuable
    child.session_id = session_id
    child._subagent_id = subagent_id
    child._delegate_role = "leaf"
    child.run_conversation.return_value = {
        "final_response": "done",
        "completed": True,
        "interrupted": False,
        "api_calls": 1,
        "messages": [],
    }
    return child


def _ok_entry(task_index=0, **extra):
    entry = {
        "task_index": task_index,
        "status": "completed",
        "summary": "done",
        "api_calls": 1,
        "duration_seconds": 0.1,
        "model": "m",
        "exit_reason": "completed",
        "tokens": {"input": 0, "output": 0},
        "tool_trace": [],
    }
    entry.update(extra)
    return entry


# ---------------------------------------------------------------------------
# Schema / opt-in surface
# ---------------------------------------------------------------------------

class TestSchema:
    def test_continuable_param_exposed_but_optional(self):
        props = DELEGATE_TASK_SCHEMA["parameters"]["properties"]
        assert "continuable" in props
        assert props["continuable"]["type"] == "boolean"
        # Not required — the default path must not change.
        assert "continuable" not in DELEGATE_TASK_SCHEMA["parameters"]["required"]

    def test_existing_params_untouched(self):
        props = DELEGATE_TASK_SCHEMA["parameters"]["properties"]
        for name in ("goal", "context", "tasks", "role", "output_schema", "background"):
            assert name in props


# ---------------------------------------------------------------------------
# _continuable_child_ids / _attach_continuable_ids
# ---------------------------------------------------------------------------

class TestContinuableIds:
    def test_default_child_returns_none(self):
        child = MagicMock()  # MagicMock auto-creates attrs — must stay None
        assert _continuable_child_ids(child) is None

    def test_explicit_false_returns_none(self):
        child = _mock_child(continuable=False)
        assert _continuable_child_ids(child) is None

    def test_continuable_returns_durable_session_id(self):
        child = _mock_child(continuable=True, session_id="sess-xyz", subagent_id="sa-9-deadbeef")
        ids = _continuable_child_ids(child)
        assert ids == {"child_session_id": "sess-xyz", "subagent_id": "sa-9-deadbeef"}

    def test_continuable_without_subagent_id_still_has_session_id(self):
        child = _mock_child(continuable=True, session_id="sess-abc")
        child._subagent_id = None
        assert _continuable_child_ids(child) == {"child_session_id": "sess-abc"}

    def test_attach_is_noop_on_default_child(self):
        entry = _ok_entry()
        _attach_continuable_ids(entry, MagicMock())
        assert "child_session_id" not in entry
        assert "subagent_id" not in entry

    def test_attach_folds_ids_into_entry(self):
        entry = _ok_entry()
        _attach_continuable_ids(entry, _mock_child(continuable=True))
        assert entry["child_session_id"] == "child-sess-1"
        assert entry["subagent_id"] == "sa-0-ab12cd34"


# ---------------------------------------------------------------------------
# _run_single_child entry shape (success / crash / timeout)
# ---------------------------------------------------------------------------

class TestRunSingleChildIds:
    def test_default_path_has_no_id_fields(self):
        child = _mock_child(continuable=False)
        result = _run_single_child(0, "goal", child, _mock_parent())
        assert result["status"] == "completed"
        assert "child_session_id" not in result
        assert "subagent_id" not in result

    def test_continuable_success_entry_carries_ids(self):
        child = _mock_child(continuable=True)
        result = _run_single_child(0, "goal", child, _mock_parent())
        assert result["status"] == "completed"
        assert result["child_session_id"] == "child-sess-1"
        assert result["subagent_id"] == "sa-0-ab12cd34"
        # The summary itself is untouched — ids are additive.
        assert result["summary"] == "done"

    def test_continuable_crash_entry_carries_ids(self):
        child = _mock_child(continuable=True)
        child.run_conversation.side_effect = RuntimeError("boom")
        result = _run_single_child(1, "goal", child, _mock_parent())
        assert result["status"] == "error"
        assert result["child_session_id"] == "child-sess-1"
        assert result["subagent_id"] == "sa-0-ab12cd34"

    def test_continuable_timeout_entry_carries_ids(self):
        from concurrent.futures import TimeoutError as FuturesTimeoutError

        class _FakeFuture:
            def result(self, timeout=None):
                raise FuturesTimeoutError("timed out")

        class _FakeExecutor:
            def __init__(self, *a, **kw):
                pass

            def submit(self, fn, *a, **kw):
                return _FakeFuture()

            def shutdown(self, wait=True):
                pass

        child = _mock_child(continuable=True)
        with (
            patch("tools.daemon_pool.DaemonThreadPoolExecutor", _FakeExecutor),
            patch(
                "tools.delegate_tool._dump_subagent_timeout_diagnostic",
                return_value=None,
            ),
        ):
            result = _run_single_child(0, "goal", child, _mock_parent())
        assert result["status"] == "timeout"
        assert result["child_session_id"] == "child-sess-1"
        assert result["subagent_id"] == "sa-0-ab12cd34"


# ---------------------------------------------------------------------------
# delegate_task end-to-end (mock AIAgent) — sync + background dispatch
# ---------------------------------------------------------------------------

class TestDelegateTaskContinuable:
    def test_sync_single_continuable_result_exposes_ids(self):
        with patch("run_agent.AIAgent") as MockAgent:
            MockAgent.return_value = _mock_child(
                continuable=True, session_id="sess-e2e", subagent_id="sa-0-e2e"
            )
            result = json.loads(
                delegate_task(goal="Do it", parent_agent=_mock_parent(), continuable=True)
            )
        entry = result["results"][0]
        assert entry["child_session_id"] == "sess-e2e"
        # subagent_id is generated by _build_child_agent (sa-<task>-<hex>);
        # the durable id is what the parent keys on.
        assert entry["subagent_id"].startswith("sa-0-")

    def test_sync_single_default_result_has_no_ids(self):
        with patch("run_agent.AIAgent") as MockAgent:
            MockAgent.return_value = _mock_child(continuable=False)
            result = json.loads(
                delegate_task(goal="Do it", parent_agent=_mock_parent())
            )
        entry = result["results"][0]
        assert "child_session_id" not in entry
        assert "subagent_id" not in entry

    def test_background_dispatch_payload_names_children_when_continuable(self):
        child = _mock_child(continuable=True, session_id="sess-bg", subagent_id="sa-0-bg")
        with (
            patch("run_agent.AIAgent", return_value=child),
            patch(
                "tools.async_delegation.dispatch_async_delegation_batch",
                return_value={"status": "dispatched", "delegation_id": "deleg_test_1"},
            ) as mock_dispatch,
            patch("gateway.session_context.async_delivery_supported", return_value=True),
            patch("tools.approval.get_current_session_key", return_value="test-session"),
        ):
            result = json.loads(
                delegate_task(
                    goal="Background task",
                    parent_agent=_mock_parent(),
                    continuable=True,
                    background=True,
                )
            )
        assert result["status"] == "dispatched"
        children = result["children"]
        assert len(children) == 1
        assert children[0]["task_index"] == 0
        assert children[0]["goal"] == "Background task"
        assert children[0]["child_session_id"] == "sess-bg"
        assert children[0]["subagent_id"].startswith("sa-0-")
        mock_dispatch.assert_called_once()

    def test_background_dispatch_payload_omits_children_by_default(self):
        child = _mock_child(continuable=False, session_id="sess-bg")
        with (
            patch("run_agent.AIAgent", return_value=child),
            patch(
                "tools.async_delegation.dispatch_async_delegation_batch",
                return_value={"status": "dispatched", "delegation_id": "deleg_test_1"},
            ),
            patch("gateway.session_context.async_delivery_supported", return_value=True),
            patch("tools.approval.get_current_session_key", return_value="test-session"),
        ):
            result = json.loads(
                delegate_task(
                    goal="Background task",
                    parent_agent=_mock_parent(),
                    background=True,
                )
            )
        assert result["status"] == "dispatched"
        assert "children" not in result


# ---------------------------------------------------------------------------
# Settlement notice — async completion event + re-injection block
# ---------------------------------------------------------------------------

def _drain_one(timeout=5.0):
    from tools.process_registry import process_registry

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_registry.completion_queue.empty():
            return process_registry.completion_queue.get_nowait()
        time.sleep(0.02)
    return None


@pytest.fixture(autouse=True)
def _clean_delegation_state():
    from tools import async_delegation as ad
    from tools.process_registry import process_registry

    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()
    yield
    deadline = time.monotonic() + 2.0
    while ad.active_count() and time.monotonic() < deadline:
        time.sleep(0.02)
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()


class TestSettlementNotice:
    def test_completion_event_carries_child_ids(self):
        from tools import async_delegation as ad

        def runner():
            return {
                "status": "completed",
                "summary": "the result",
                "api_calls": 1,
                "duration_seconds": 0.1,
                "model": "m",
                "child_session_id": "sess-child-9",
                "subagent_id": "sa-0-99999999",
            }

        res = ad.dispatch_async_delegation(
            goal="g", context=None, toolsets=None, role="leaf", model="m",
            session_key="", runner=runner, max_async_children=3,
        )
        assert res["status"] == "dispatched"
        evt = _drain_one()
        assert evt is not None
        assert evt["child_session_id"] == "sess-child-9"
        assert evt["subagent_id"] == "sa-0-99999999"

    def test_completion_event_without_ids_stays_clean(self):
        from tools import async_delegation as ad

        def runner():
            return {
                "status": "completed",
                "summary": "plain",
                "api_calls": 1,
                "duration_seconds": 0.1,
                "model": "m",
            }

        ad.dispatch_async_delegation(
            goal="g", context=None, toolsets=None, role="leaf", model="m",
            session_key="", runner=runner, max_async_children=3,
        )
        evt = _drain_one()
        assert evt is not None
        assert "child_session_id" not in evt
        assert "subagent_id" not in evt

    def test_single_block_renders_settlement_line_by_id(self):
        text = format_process_notification(
            {
                "type": "async_delegation",
                "delegation_id": "deleg_1",
                "goal": "original goal",
                "status": "completed",
                "summary": "answer",
                "api_calls": 2,
                "duration_seconds": 1.0,
                "dispatched_at": time.time(),
                "completed_at": time.time(),
                "child_session_id": "sess-child-1",
                "subagent_id": "sa-0-aaaa1111",
            }
        )
        assert text is not None
        assert "Child sess-child-1 finished (status=completed)." in text
        assert "(subagent_id=sa-0-aaaa1111)" in text

    def test_single_block_error_status_renders_failure_line(self):
        text = format_process_notification(
            {
                "type": "async_delegation",
                "delegation_id": "deleg_2",
                "goal": "g",
                "status": "error",
                "error": "boom",
                "api_calls": 0,
                "duration_seconds": 1.0,
                "dispatched_at": time.time(),
                "completed_at": time.time(),
                "child_session_id": "sess-child-2",
            }
        )
        assert text is not None
        assert "Child sess-child-2 failed before it finished (status=error)." in text

    def test_single_block_without_ids_has_no_settlement_line(self):
        text = format_process_notification(
            {
                "type": "async_delegation",
                "delegation_id": "deleg_3",
                "goal": "g",
                "status": "completed",
                "summary": "answer",
                "api_calls": 1,
                "duration_seconds": 1.0,
                "dispatched_at": time.time(),
                "completed_at": time.time(),
            }
        )
        assert text is not None
        assert "Child " not in text

    def test_batch_block_renders_per_task_settlement_lines(self):
        now = time.time()
        text = format_process_notification(
            {
                "type": "async_delegation",
                "delegation_id": "deleg_batch",
                "is_batch": True,
                "goals": ["task A", "task B"],
                "status": "completed",
                "results": [
                    _ok_entry(
                        task_index=0,
                        status="completed",
                        summary="A done",
                        child_session_id="sess-child-a",
                        subagent_id="sa-0-aaaa",
                    ),
                    _ok_entry(
                        task_index=1,
                        status="failed",
                        summary=None,
                        error="went wrong",
                        child_session_id="sess-child-b",
                        subagent_id="sa-1-bbbb",
                    ),
                ],
                "total_duration_seconds": 2.0,
                "dispatched_at": now,
                "completed_at": now,
            }
        )
        assert text is not None
        assert "Child sess-child-a finished (status=completed)." in text
        assert "Child sess-child-b failed before it finished (status=failed)." in text

    def test_batch_block_without_ids_has_no_settlement_lines(self):
        now = time.time()
        text = format_process_notification(
            {
                "type": "async_delegation",
                "delegation_id": "deleg_batch_plain",
                "is_batch": True,
                "goals": ["task A"],
                "status": "completed",
                "results": [_ok_entry(task_index=0, status="completed", summary="A done")],
                "total_duration_seconds": 1.0,
                "dispatched_at": now,
                "completed_at": now,
            }
        )
        assert text is not None
        assert "Child " not in text


# ---------------------------------------------------------------------------
# Report v1 — child session re-readable by id via session_search READ
# ---------------------------------------------------------------------------

class TestReportBySessionId:
    def test_subagent_session_readable_by_id(self, tmp_path):
        """A subagent session (source='subagent', parent_session_id set) is
        re-readable via session_search's READ shape — this is the REPORT v1
        path: the durable child_session_id names a persisted transcript."""
        from hermes_state import SessionDB
        from tools.session_search_tool import _read_session

        db = SessionDB(tmp_path / "state.db")
        db.create_session("parent-sess-1", source="cli")
        db.create_session(
            "child-sess-report",
            source="subagent",
            parent_session_id="parent-sess-1",
        )
        db.append_message("child-sess-report", role="user", content="delegated goal")
        db.append_message(
            "child-sess-report", role="assistant", content="final answer of the child"
        )
        db._conn.commit()

        payload = json.loads(_read_session(db, "child-sess-report"))
        assert payload["success"] is True
        assert payload["session_id"] == "child-sess-report"
        assert payload["session_meta"]["source"] == "subagent"
        contents = [m["content"] for m in payload["messages"]]
        assert "final answer of the child" in contents
        db.close()

    def test_unknown_session_id_errors(self, tmp_path):
        from hermes_state import SessionDB
        from tools.session_search_tool import _read_session

        db = SessionDB(tmp_path / "state.db")
        payload = json.loads(_read_session(db, "no-such-session"))
        assert payload.get("success") is False
        db.close()
