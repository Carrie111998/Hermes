"""Regression tests for subagent timeout diagnostic dump (issue #14726).

When delegate_task's child subagent times out without having made any API
call, a structured diagnostic file is written under
``~/.hermes/logs/subagent-timeout-<sid>-<ts>.log``. This gives users a
concrete artifact to inspect (worker thread stack, system prompt size,
tool schema bytes, credential pool state, etc.) instead of the previous
opaque "subagent timed out" error.

These tests pin:
- the diagnostic writer's output format and content
- the timeout branch in _run_single_child only dumps when api_calls == 0
- the error message surfaces the diagnostic path
- api_calls > 0 timeouts do NOT write a zero-call dump and return a bounded,
  explicitly incomplete checkpoint without guessing at a root cause
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


class _StubChild:
    """Minimal stand-in for an AIAgent subagent."""
    def __init__(
        self,
        *,
        api_call_count: int = 0,
        hang_seconds: float = 5.0,
        subagent_id: str = "sa-0-stubabc",
        tool_schema=None,
    ):
        self._subagent_id = subagent_id
        self._delegate_depth = 1
        self._delegate_role = "leaf"
        self.model = "test/model"
        self.provider = "testprov"
        self.api_mode = "chat_completions"
        self.base_url = "https://example.test/v1"
        self.max_iterations = 30
        self.quiet_mode = True
        self.skip_memory = True
        self.skip_context_files = True
        self.platform = "cli"
        self.ephemeral_system_prompt = "sys prompt"
        self.enabled_toolsets = ["web", "terminal"]
        self.valid_tool_names = {"web_search", "terminal"}
        self.tools = tool_schema if tool_schema is not None else [
            {"name": "web_search", "description": "search"},
            {"name": "terminal", "description": "shell"},
        ]
        self._api_call_count = api_call_count
        self._hang = threading.Event()
        self._hang_seconds = hang_seconds

    def get_activity_summary(self):
        return {
            "api_call_count": self._api_call_count,
            "max_iterations": self.max_iterations,
            "current_tool": None,
            "seconds_since_activity": 60,
        }

    def run_conversation(self, user_message, task_id=None, stream_callback=None):
        self._hang.wait(self._hang_seconds)
        return {"final_response": "", "completed": False, "api_calls": self._api_call_count}

    def interrupt(self):
        self._hang.set()


class _ProgressChild(_StubChild):
    """Timed-out child with durable in-memory tool-call progress."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._live_transcript_path = "/tmp/deleg-test/task-0.log"
        self._session_messages = [
            {"role": "user", "content": "research"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "function": {
                            "name": "web_search",
                            "arguments": '{"query":"one"}',
                        },
                    },
                    {
                        "id": "call-2",
                        "function": {
                            "name": "web_extract",
                            "arguments": '{"urls":["https://example.test"]}',
                        },
                    },
                    {
                        "id": "malformed-call",
                        "function": "not-a-dict",
                    },
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "content": "untrusted result content must not be copied",
            },
            {
                "role": "tool",
                "tool_call_id": "call-2",
                "content": "another raw result that must stay out of the checkpoint",
            },
        ]


class _ChildRaisedTimeout(_StubChild):
    """Child failure that happens to use Python's TimeoutError type."""

    def run_conversation(self, user_message, task_id=None, stream_callback=None):
        raise TimeoutError("child-internal timeout")


class _ImmediateChild(_StubChild):
    """Successful child used to verify the default no-timeout contract."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.steer_calls = 0

    def run_conversation(self, user_message, task_id=None, stream_callback=None):
        return {
            "final_response": "Immediate completion",
            "completed": True,
            "api_calls": 1,
            "messages": [
                {"role": "assistant", "content": "Immediate completion"}
            ],
        }

    def steer(self, message):
        self.steer_calls += 1
        return True


class _DeadlineAwareChild(_StubChild):
    """Child that can turn an approaching deadline into a final response."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._finalize = threading.Event()
        self.steer_message = None

    def steer(self, message):
        self.steer_message = message
        self._finalize.set()
        return True

    def run_conversation(self, user_message, task_id=None, stream_callback=None):
        if self._finalize.wait(self._hang_seconds):
            self._api_call_count += 1
            return {
                "final_response": "Deadline-safe partial findings",
                "completed": True,
                "api_calls": self._api_call_count,
                "messages": [
                    {"role": "user", "content": user_message},
                    {
                        "role": "assistant",
                        "content": "Deadline-safe partial findings",
                    },
                ],
            }
        return {
            "final_response": "",
            "completed": False,
            "api_calls": self._api_call_count,
        }


class _BlockingSteerChild(_StubChild):
    """Child whose steer implementation blocks until the test releases it."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.steer_started = threading.Event()
        self.release_steer = threading.Event()

    def steer(self, message):
        self.steer_started.set()
        self.release_steer.wait(10.0)
        return True


# ── _dump_subagent_timeout_diagnostic ──────────────────────────────────

class TestDumpSubagentTimeoutDiagnostic:

    def test_writes_log_with_expected_sections(self, hermes_home):
        from tools.delegate_tool import _dump_subagent_timeout_diagnostic
        child = _StubChild(subagent_id="sa-7-abc123")

        worker = threading.Thread(
            target=lambda: child.run_conversation("test"),
            daemon=True,
        )
        worker.start()
        time.sleep(0.1)
        try:
            path = _dump_subagent_timeout_diagnostic(
                child=child,
                task_index=7,
                timeout_seconds=300.0,
                duration_seconds=300.01,
                worker_thread=worker,
                goal="Research something long",
            )
        finally:
            child.interrupt()
            worker.join(timeout=2.0)

        assert path is not None
        p = Path(path)
        assert p.is_file()
        # File lives under HERMES_HOME/logs/
        assert p.parent == hermes_home / "logs"
        assert p.name.startswith("subagent-timeout-sa-7-abc123-")
        assert p.suffix == ".log"

        content = p.read_text()
        # Header references the issue for future grep-ability
        assert "issue #14726" in content
        # Timeout facts
        assert "task_index:        7" in content
        assert "subagent_id:       sa-7-abc123" in content
        assert "configured_timeout: 300.0s" in content
        assert "actual_duration:   300.01s" in content
        # Goal
        assert "Research something long" in content
        # Child config
        assert "model: 'test/model'" in content
        assert "provider: 'testprov'" in content
        assert "base_url: 'https://example.test/v1'" in content
        assert "max_iterations: 30" in content
        # Toolsets
        assert "enabled_toolsets:  ['web', 'terminal']" in content
        assert "loaded tool count: 2" in content
        # Prompt / schema sizes
        assert "system_prompt_bytes:" in content
        assert "tool_schema_count: 2" in content
        assert "tool_schema_bytes:" in content
        # Activity summary
        assert "api_call_count: 0" in content
        # Worker stack
        assert "Worker thread stack at timeout" in content
        # The thread is parked inside _hang.wait → cond.wait → waiter.acquire
        assert "acquire" in content or "wait" in content

    def test_truncates_very_long_goal(self, hermes_home):
        from tools.delegate_tool import _dump_subagent_timeout_diagnostic
        child = _StubChild()
        huge_goal = "x" * 5000

        path = _dump_subagent_timeout_diagnostic(
            child=child,
            task_index=0,
            timeout_seconds=300.0,
            duration_seconds=300.0,
            worker_thread=None,
            goal=huge_goal,
        )
        child.interrupt()

        content = Path(path).read_text()
        assert "[truncated]" in content
        # Goal section trimmed to 1000 chars + suffix
        goal_block = content.split("## Goal", 1)[1].split("## Child config", 1)[0]
        assert len(goal_block) < 1200

    def test_missing_worker_thread_is_handled(self, hermes_home):
        from tools.delegate_tool import _dump_subagent_timeout_diagnostic
        child = _StubChild()
        path = _dump_subagent_timeout_diagnostic(
            child=child,
            task_index=0,
            timeout_seconds=300.0,
            duration_seconds=300.0,
            worker_thread=None,
            goal="x",
        )
        child.interrupt()
        content = Path(path).read_text()
        assert "<no worker thread handle>" in content

    def test_exited_worker_thread_is_handled(self, hermes_home):
        from tools.delegate_tool import _dump_subagent_timeout_diagnostic
        child = _StubChild()
        # A thread that has already finished
        t = threading.Thread(target=lambda: None)
        t.start()
        t.join()
        assert not t.is_alive()
        path = _dump_subagent_timeout_diagnostic(
            child=child,
            task_index=0,
            timeout_seconds=300.0,
            duration_seconds=300.0,
            worker_thread=t,
            goal="x",
        )
        child.interrupt()
        content = Path(path).read_text()
        assert "<worker thread already exited>" in content

    def test_returns_none_on_unwritable_logs_dir(self, tmp_path, monkeypatch):
        # Point HERMES_HOME at an unwritable path so logs/ can't be created
        # (simulates permission-denied). Helper must not raise.
        from tools.delegate_tool import _dump_subagent_timeout_diagnostic
        bogus = tmp_path / "does-not-exist" / ".hermes"
        monkeypatch.setenv("HERMES_HOME", str(bogus))
        child = _StubChild()

        # Make the logs dir itself unwritable by creating it as a FILE
        # so mkdir(exist_ok=True) → NotADirectoryError and we fall through.
        bogus.parent.mkdir(parents=True, exist_ok=True)
        bogus.mkdir()
        (bogus / "logs").write_text("not a dir")
        result = _dump_subagent_timeout_diagnostic(
            child=child,
            task_index=0,
            timeout_seconds=300.0,
            duration_seconds=300.0,
            worker_thread=None,
            goal="x",
        )
        child.interrupt()
        # Either None (mkdir failed) or a real path; must never raise.
        # We assert no exception propagates — the return value is advisory.
        assert result is None or Path(result).exists()


# ── _run_single_child timeout branch wiring ───────────────────────────

class TestRunSingleChildTimeoutDump:
    """The timeout branch in _run_single_child must emit the diagnostic
    dump when api_calls == 0, and must NOT emit it when api_calls > 0."""

    def _invoke_with_short_timeout(
        self, child, monkeypatch, timeout: float | None = 0.3
    ):
        """Run _run_single_child with a tiny timeout to force the timeout branch."""
        from tools import delegate_tool
        # Bypass the production 30s config floor so the unit test stays fast.
        monkeypatch.setattr(delegate_tool, "_get_child_timeout", lambda: timeout)

        parent = MagicMock()
        parent._touch_activity = MagicMock()
        parent._current_task_id = None
        return delegate_tool._run_single_child(
            task_index=0,
            goal="test goal",
            child=child,
            parent_agent=parent,
        )

    def test_zero_api_calls_writes_dump_and_surfaces_path(self, hermes_home, monkeypatch):
        child = _StubChild(api_call_count=0, hang_seconds=10.0)
        result = self._invoke_with_short_timeout(child, monkeypatch)

        assert result["status"] == "timeout"
        assert result["api_calls"] == 0
        assert result["diagnostic_path"] is not None
        dump_path = Path(result["diagnostic_path"])
        assert dump_path.is_file()
        assert dump_path.parent == hermes_home / "logs"

        # Error message surfaces the path and the "no API call" phrasing
        assert "without making any API call" in result["error"]
        assert "Diagnostic:" in result["error"]
        assert str(dump_path) in result["error"]

    def test_nonzero_api_calls_skips_dump_and_reports_hard_deadline(
        self, hermes_home, monkeypatch
    ):
        child = _StubChild(api_call_count=5, hang_seconds=10.0)
        result = self._invoke_with_short_timeout(child, monkeypatch)

        assert result["status"] == "timeout"
        assert result["api_calls"] == 5
        # No zero-call diagnostic file should be written for a child that made
        # progress. The message must report the configured deadline without
        # inventing a slow-call/network root cause.
        assert result.get("diagnostic_path") is None
        assert "configured hard deadline expired" in result["error"]
        assert "stuck on a slow API call" not in result["error"]
        # And no subagent-timeout-* file should exist under logs/
        logs_dir = hermes_home / "logs"
        if logs_dir.is_dir():
            dumps = list(logs_dir.glob("subagent-timeout-*.log"))
            assert dumps == []

    # ── explicit timeout metadata (#51690, salvaged from PR #60378) ────

    def test_timeout_result_carries_structured_metadata(self, hermes_home, monkeypatch):
        """Parents must be able to distinguish a child_timeout_seconds kill
        from other failures without parsing the error string."""
        child = _StubChild(api_call_count=0, hang_seconds=10.0)
        result = self._invoke_with_short_timeout(child, monkeypatch)

        assert result["status"] == "timeout"
        assert result["timeout_seconds"] == 0.3
        assert result["timed_out_after_seconds"] == result["duration_seconds"]
        assert result["timeout_phase"] == "before_first_llm_call"

    def test_timeout_phase_after_llm_calls(self, hermes_home, monkeypatch):
        child = _StubChild(api_call_count=5, hang_seconds=10.0)
        result = self._invoke_with_short_timeout(child, monkeypatch)

        assert result["timeout_phase"] == "after_llm_calls"
        assert result["timeout_seconds"] == 0.3

    def test_non_timeout_error_has_null_timeout_metadata(self, hermes_home, monkeypatch):
        """The metadata fields are timeout-specific — a child that raises
        must report them as None so consumers can key on presence."""
        from tools import delegate_tool
        monkeypatch.setattr(delegate_tool, "_get_child_timeout", lambda: 30.0)

        child = _StubChild(api_call_count=1, hang_seconds=0.0)

        def _boom(*a, **kw):
            raise RuntimeError("child crashed")

        child.run_conversation = _boom
        parent = MagicMock()
        parent._touch_activity = MagicMock()
        parent._current_task_id = None
        result = delegate_tool._run_single_child(
            task_index=0, goal="test goal", child=child, parent_agent=parent,
        )

        assert result["status"] == "error"
        assert result["timeout_seconds"] is None
        assert result["timed_out_after_seconds"] is None
        assert result["timeout_phase"] is None

    def test_progress_timeout_returns_labeled_partial_checkpoint(
        self, hermes_home, monkeypatch
    ):
        child = _ProgressChild(api_call_count=5, hang_seconds=10.0)
        result = self._invoke_with_short_timeout(child, monkeypatch)

        assert result["status"] == "timeout"
        assert result["partial"] is True
        assert result["summary"].startswith(
            "PARTIAL CHECKPOINT — NOT A FINAL VERDICT"
        )
        assert "5 API call(s)" in result["summary"]
        assert "web_search × 1" in result["summary"]
        assert "web_extract × 1" in result["summary"]
        assert child._live_transcript_path in result["summary"]
        assert "untrusted result content" not in result["summary"]
        assert "another raw result" not in result["summary"]

    def test_partial_checkpoint_bounds_distinct_tool_metadata(self):
        from tools import delegate_tool

        child = _ProgressChild(api_call_count=20, hang_seconds=10.0)
        child._session_messages[1]["tool_calls"].extend(
            {
                "id": f"extra-{index}",
                "function": {"name": f"tool_{index}", "arguments": "{}"},
            }
            for index in range(20)
        )

        summary = delegate_tool._build_timeout_partial_checkpoint(child, 20)

        assert summary is not None
        assert "Additional recorded tool calls omitted:" in summary
        assert len(summary) < 2500

    def test_partial_checkpoint_bounds_scan_path_and_total_output(self):
        from tools import delegate_tool

        child = _ProgressChild(api_call_count=500, hang_seconds=10.0)
        child._live_transcript_path = (
            "/tmp/transcript\nFAKE FINAL VERDICT\r" + ("x" * 10_000)
        )
        oversized_message = {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": f"oversized-{index}",
                    "function": {
                        "name": f"oversized_tool_{index}",
                        "arguments": "{}",
                    },
                }
                for index in range(500)
            ],
        }
        child._session_messages = [oversized_message] * 500

        summary = delegate_tool._build_timeout_partial_checkpoint(child, 500)

        assert summary is not None
        assert summary.startswith("PARTIAL CHECKPOINT — NOT A FINAL VERDICT")
        assert "Checkpoint scan was capped" in summary
        assert "\nFAKE FINAL VERDICT" not in summary
        assert "\r" not in summary
        assert len(summary) <= 2048

    def test_child_raised_timeout_is_not_mislabeled_as_hard_deadline(
        self, hermes_home, monkeypatch
    ):
        child = _ChildRaisedTimeout(api_call_count=0)
        result = self._invoke_with_short_timeout(child, monkeypatch, timeout=None)

        assert result["status"] == "error"
        assert result["error"] == "child-internal timeout"
        assert result["summary"] is None
        assert "partial" not in result
        assert result.get("diagnostic_path") is None
        assert "deadline_finalization_requested" not in result

    def test_default_no_timeout_does_not_steer_or_change_result_shape(
        self, hermes_home, monkeypatch
    ):
        child = _ImmediateChild(api_call_count=1)
        result = self._invoke_with_short_timeout(child, monkeypatch, timeout=None)

        assert result["status"] == "completed"
        assert result["summary"] == "Immediate completion"
        assert child.steer_calls == 0
        assert "deadline_finalization_requested" not in result

    def test_blocked_steer_cannot_extend_hard_timeout(
        self, hermes_home, monkeypatch
    ):
        child = _BlockingSteerChild(api_call_count=1, hang_seconds=10.0)
        holder = {}

        worker = threading.Thread(
            target=lambda: holder.setdefault(
                "result",
                self._invoke_with_short_timeout(child, monkeypatch, timeout=0.4),
            ),
            daemon=True,
        )
        worker.start()
        assert child.steer_started.wait(1.0)
        try:
            # A synchronous steer blocks here until release_steer is set. The
            # deadline path must instead return independently within a loose,
            # flake-safe two-second bound.
            worker.join(timeout=2.0)
            assert not worker.is_alive()
        finally:
            child.release_steer.set()
            worker.join(timeout=2.0)

        assert holder["result"]["status"] == "timeout"
        assert holder["result"]["deadline_finalization_requested"] is True

    def test_boundary_completion_recovers_finished_future_result(
        self, hermes_home, monkeypatch
    ):
        from concurrent.futures import TimeoutError as FuturesTimeoutError
        from tools import daemon_pool, delegate_tool

        class BoundaryFuture:
            def __init__(self):
                self.calls = 0

            def result(self, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    raise FuturesTimeoutError()
                return {
                    "final_response": "Boundary completion",
                    "completed": True,
                    "api_calls": 1,
                    "messages": [
                        {"role": "assistant", "content": "Boundary completion"}
                    ],
                }

            def done(self):
                return True

        class BoundaryExecutor:
            def __init__(self, *args, **kwargs):
                self.future = BoundaryFuture()

            def submit(self, *args, **kwargs):
                return self.future

            def shutdown(self, wait=False, cancel_futures=False):
                return None

        monkeypatch.setattr(delegate_tool, "_get_child_timeout", lambda: 0.4)
        monkeypatch.setattr(
            daemon_pool, "DaemonThreadPoolExecutor", BoundaryExecutor
        )

        parent = MagicMock()
        parent._touch_activity = MagicMock()
        parent._interrupt_requested = False
        parent.session_id = "parent-test"
        parent._active_children = []

        result = delegate_tool._run_single_child(
            task_index=0,
            goal="boundary race",
            child=_StubChild(api_call_count=1),
            parent_agent=parent,
        )

        assert result["status"] == "completed"
        assert result["summary"] == "Boundary completion"

    def test_soft_deadline_requests_final_summary_before_hard_timeout(
        self, hermes_home, monkeypatch
    ):
        child = _DeadlineAwareChild(api_call_count=3, hang_seconds=2.0)
        result = self._invoke_with_short_timeout(child, monkeypatch, timeout=0.4)

        assert result["status"] == "completed"
        assert result["summary"] == "Deadline-safe partial findings"
        assert child.steer_message is not None
        assert "deadline" in child.steer_message.lower()
        assert "do not call more tools" in child.steer_message.lower()
