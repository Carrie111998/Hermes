"""Regression for steer OOB persistence (#fix/steer-oob-persist).

Covers the four gaps named in the task:

1) 单批多工具时 marker 拼到最后一条 tool 尾部且落盘后可被查询
2) 空批 (num_tool_msgs==0) 时 steer 回退不丢失，下次有 tool 时仍能投递
3) 连续 steer 合并 (agent.steer 多次调用) 不丢
4) marker 不污染 api_content 的独立校验

All cases use the real persistence path (SessionDB + _flush_messages_to_session_db)
so they break if the marker is split, dropped, or stored in the wrong column.
"""

from __future__ import annotations

import copy
import threading
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.prompt_builder import STEER_MARKER_CLOSE, STEER_MARKER_OPEN, format_steer_marker
from agent.tool_dispatch_helpers import make_tool_result_message
from hermes_state import SessionDB
from run_agent import AIAgent


# ---------------------------------------------------------------------------
# helpers — mirror tests/run_agent/test_steer.py:: _bare_agent
# ---------------------------------------------------------------------------

def _bare_agent() -> AIAgent:
    agent = object.__new__(AIAgent)
    agent._pending_steer = None
    agent._pending_steer_lock = threading.Lock()
    agent._pending_redirect = None
    agent._pending_redirect_lock = threading.Lock()
    agent._model_request_active = threading.Event()
    agent._executing_tools = False
    agent._execution_thread_id = None
    agent._interrupt_thread_signal_pending = False
    agent._interrupt_requested = False
    agent._interrupt_message = None
    agent._active_children = []
    agent._active_children_lock = threading.Lock()
    agent._tool_worker_threads = None
    agent._tool_worker_threads_lock = None
    agent._current_streamed_assistant_text = ""
    agent._stream_needs_break = False
    agent._strip_think_blocks = lambda content: content
    agent.quiet_mode = True
    agent.api_mode = "chat_completions"
    return agent


def _make_agent_with_db(tmp_path: Path, session_id: str = "steer-persist-sess") -> tuple[AIAgent, SessionDB, Path]:
    """Real SessionDB-backed agent for persistence assertions."""
    hermes_home = Path(tempfile.mkdtemp(prefix="hermes-steer-persist-"))
    (hermes_home / "logs").mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "state.db"
    # Patch the minimal surfaces AIAgent.__init__ touches so we can construct
    # without network / model metadata.
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
        patch("run_agent._hermes_home", hermes_home),
        patch("agent.model_metadata.fetch_model_metadata", return_value={}),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    # Wire a real SessionDB
    db = SessionDB(db_path=db_path)
    db.create_session(session_id=session_id, source="cli", model="test/model")
    agent._session_db = db
    agent._session_db_created = True
    agent.session_id = session_id
    agent._last_flushed_db_idx = 0
    agent._flushed_db_message_ids = set()
    agent._flushed_db_message_session_id = None
    agent._persist_disabled = False
    agent._db_flush_scan_prefix = None
    return agent, db, db_path


def _durable(db_path: Path, session_id: str) -> list[dict]:
    db = SessionDB(db_path=db_path)
    try:
        return db.get_messages_as_conversation(session_id)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 1) 单批多工具：marker 拼到最后一条 tool 尾部且落盘后可被查询
# ---------------------------------------------------------------------------

class TestSteerMultiToolTailPersistence:
    def test_marker_appended_to_last_tool_only_in_memory(self):
        agent = _bare_agent()
        agent.steer("please also check auth.log")
        messages = [
            {"role": "user", "content": "what's in /var/log?"},
            {"role": "assistant", "tool_calls": [{"id": "a"}, {"id": "b"}, {"id": "c"}]},
            {"role": "tool", "content": "A", "tool_call_id": "a"},
            {"role": "tool", "content": "B", "tool_call_id": "b"},
            {"role": "tool", "content": "C", "tool_call_id": "c"},
        ]
        agent._apply_pending_steer_to_tool_results(messages, num_tool_msgs=3)
        assert messages[2]["content"] == "A"
        assert messages[3]["content"] == "B"
        assert "C" in messages[4]["content"]
        assert STEER_MARKER_OPEN in messages[4]["content"]
        assert STEER_MARKER_CLOSE in messages[4]["content"]
        assert "please also check auth.log" in messages[4]["content"]
        # marker is suffix — not prepended
        assert messages[4]["content"].endswith(format_steer_marker("please also check auth.log"))
        assert agent._pending_steer is None

    def test_multi_tool_steer_is_durable_and_queryable(self, tmp_path):
        agent, db, db_path = _make_agent_with_db(tmp_path, session_id="steer-multi-tail")
        try:
            # Seed a persisted user turn so flush has a history prefix
            messages: list[dict] = [
                {"role": "user", "content": "do work"},
            ]
            agent._flush_messages_to_session_db(messages, conversation_history=[])
            # Simulate assistant tool_calls + results (what run_conversation does)
            messages.append({"role": "assistant", "tool_calls": [{"id": "a", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}, {"id": "b", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}]})
            messages.append({"role": "tool", "content": "output A", "tool_call_id": "a"})
            messages.append({"role": "tool", "content": "output B", "tool_call_id": "b"})
            agent.steer("also check migrations")
            agent._apply_pending_steer_to_tool_results(messages, num_tool_msgs=2)
            # Persist the injected tail
            ok = agent._flush_messages_to_session_db(messages, conversation_history=[])
            assert ok is True
            durable = _durable(db_path, agent.session_id)
            # durable must contain the marker on the LAST tool, not the first
            tool_rows = [m for m in durable if m.get("role") == "tool"]
            assert len(tool_rows) == 2
            assert tool_rows[0]["content"] == "output A"
            assert STEER_MARKER_OPEN not in tool_rows[0]["content"]
            assert STEER_MARKER_OPEN in tool_rows[1]["content"]
            assert "also check migrations" in tool_rows[1]["content"]
            # Direct DB queryability — content LIKE '%OUT-OF-BAND%'
            with db._read_ctx() as conn:
                row = conn.execute(
                    "SELECT content FROM messages WHERE session_id=? AND role='tool' AND content LIKE '%OUT-OF-BAND%'",
                    (agent.session_id,),
                ).fetchone()
            assert row is not None
            assert "also check migrations" in row["content"]
        finally:
            db.close()

    def test_multimodal_last_tool_appended_as_block_and_persisted(self, tmp_path):
        agent, db, db_path = _make_agent_with_db(tmp_path, session_id="steer-multi-modal")
        try:
            messages: list[dict] = [{"role": "user", "content": "hi"}]
            agent._flush_messages_to_session_db(messages, conversation_history=[])
            messages.append({"role": "assistant", "tool_calls": [{"id": "a"}, {"id": "b"}]})
            messages.append({"role": "tool", "content": [{"type": "text", "text": "first"}], "tool_call_id": "a"})
            messages.append({"role": "tool", "content": [{"type": "text", "text": "second"}], "tool_call_id": "b"})
            agent.steer("extra note")
            agent._apply_pending_steer_to_tool_results(messages, num_tool_msgs=2)
            # In-memory: last tool is list with appended marker block
            assert isinstance(messages[-1]["content"], list)
            assert messages[-1]["content"][-1]["type"] == "text"
            assert "extra note" in messages[-1]["content"][-1]["text"]
            assert messages[-2]["content"] == [{"type": "text", "text": "first"}]
            # Persist — multimodal tool content is stored as joined text
            ok = agent._flush_messages_to_session_db(messages, conversation_history=[])
            assert ok is True
            durable = _durable(db_path, agent.session_id)
            tool_rows = [m for m in durable if m.get("role") == "tool"]
            assert STEER_MARKER_OPEN not in tool_rows[0]["content"]
            assert "extra note" in tool_rows[1]["content"]
        finally:
            db.close()


# ---------------------------------------------------------------------------
# 2) 空批 (num_tool_msgs==0) 回退不丢失，下次有 tool 时仍能投递
# ---------------------------------------------------------------------------

class TestSteerEmptyBatchFallback:
    def test_empty_batch_keeps_pending_intact(self):
        agent = _bare_agent()
        agent.steer("deferred note")
        messages = [{"role": "user", "content": "hello"}]
        agent._apply_pending_steer_to_tool_results(messages, num_tool_msgs=0)
        assert agent._pending_steer == "deferred note"
        assert STEER_MARKER_OPEN not in str(messages)

    def test_empty_batch_delivered_on_next_tool_batch(self):
        agent = _bare_agent()
        agent.steer("deferred note")
        # First batch is empty — steer stays pending
        agent._apply_pending_steer_to_tool_results([{"role": "user", "content": "hi"}], num_tool_msgs=0)
        assert agent._pending_steer == "deferred note"
        # Next batch has a real tool result — marker lands there
        messages = [
            {"role": "assistant", "tool_calls": [{"id": "t1"}]},
            {"role": "tool", "content": "tool output", "tool_call_id": "t1"},
        ]
        agent._apply_pending_steer_to_tool_results(messages, num_tool_msgs=1)
        assert STEER_MARKER_OPEN in messages[-1]["content"]
        assert "deferred note" in messages[-1]["content"]
        assert agent._pending_steer is None

    def test_empty_batch_persisted_steer_survives_until_next_flush(self, tmp_path):
        agent, db, db_path = _make_agent_with_db(tmp_path, session_id="steer-empty-batch")
        try:
            messages: list[dict] = [{"role": "user", "content": "start"}]
            agent._flush_messages_to_session_db(messages, conversation_history=[])
            agent.steer("will arrive next batch")
            # Simulate a turn that produced no tool messages (e.g. model returned
            # text immediately). The drain must NOT consume the steer.
            agent._apply_pending_steer_to_tool_results(messages, num_tool_msgs=0)
            assert agent._pending_steer == "will arrive next batch"
            # Next turn produces a tool — steer finally lands and persists
            messages.append({"role": "assistant", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}]})
            messages.append({"role": "tool", "content": "tool out", "tool_call_id": "c1"})
            agent._apply_pending_steer_to_tool_results(messages, num_tool_msgs=1)
            ok = agent._flush_messages_to_session_db(messages, conversation_history=[])
            assert ok is True
            durable = _durable(db_path, agent.session_id)
            tool_rows = [m for m in durable if m.get("role") == "tool"]
            assert any("will arrive next batch" in (m.get("content") or "") for m in tool_rows)
        finally:
            db.close()

    def test_no_tool_in_tail_restashes_and_delivers_next_batch(self):
        """num_tool_msgs>0 but tail has no role==tool (all skipped by interrupt)."""
        agent = _bare_agent()
        agent.steer("skipped batch note")
        messages = [
            {"role": "assistant", "tool_calls": [{"id": "x"}]},
            # No tool role in the tail slice — e.g. all calls cancelled
            {"role": "assistant", "content": "placeholder"},
        ]
        agent._apply_pending_steer_to_tool_results(messages, num_tool_msgs=1)
        # Must have been put back
        assert agent._pending_steer == "skipped batch note"
        # Next real tool batch delivers it
        messages2 = [{"role": "tool", "content": "real output", "tool_call_id": "y"}]
        agent._apply_pending_steer_to_tool_results(messages2, num_tool_msgs=1)
        assert "skipped batch note" in messages2[0]["content"]
        assert agent._pending_steer is None

    def test_conversation_loop_pre_api_drain_restash_semantics(self):
        """Mirrors conversation_loop.py pre-API drain: no tool -> restash with merge."""
        agent = _bare_agent()
        agent.steer("first")
        # Pre-API drain path (conversation_loop ~2042): drains then scans for tool
        pre = agent._drain_pending_steer()
        assert pre == "first"
        messages: list[dict] = [{"role": "user", "content": "hello"}]
        injected = False
        for sm in reversed(messages):
            if isinstance(sm, dict) and sm.get("role") == "tool":
                injected = True
                break
        assert not injected
        # No tool found — must restash (with merge if something raced in)
        _lock = getattr(agent, "_pending_steer_lock", None)
        if _lock is not None:
            with _lock:
                if agent._pending_steer:
                    agent._pending_steer = agent._pending_steer + "\n" + pre
                else:
                    agent._pending_steer = pre
        else:
            existing = getattr(agent, "_pending_steer", None)
            agent._pending_steer = (existing + "\n" + pre) if existing else pre
        assert agent._pending_steer == "first"
        # A concurrent steer arriving before the next flush merges
        agent.steer("second")
        assert agent._pending_steer == "first\nsecond"


# ---------------------------------------------------------------------------
# 3) 连续 steer 合并 (agent.steer 多次调用) 不丢
# ---------------------------------------------------------------------------

class TestSteerContinuousMerge:
    def test_sequential_steer_calls_concatenate_with_newline(self):
        agent = _bare_agent()
        assert agent.steer("first") is True
        assert agent.steer("second") is True
        assert agent.steer("third") is True
        assert agent._pending_steer == "first\nsecond\nthird"
        messages = [{"role": "tool", "content": "out", "tool_call_id": "1"}]
        agent._apply_pending_steer_to_tool_results(messages, num_tool_msgs=1)
        content = messages[0]["content"]
        assert "first" in content and "second" in content and "third" in content
        assert content.count(STEER_MARKER_OPEN) == 1  # single marker wrapping all
        assert agent._pending_steer is None

    def test_concurrent_steer_calls_preserve_all_text(self):
        agent = _bare_agent()
        N = 100
        threads = [threading.Thread(target=lambda i=i: agent.steer(f"note-{i}")) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        text = agent._drain_pending_steer()
        assert text is not None
        lines = text.split("\n")
        assert len(lines) == N
        assert set(lines) == {f"note-{i}" for i in range(N)}

    def test_merged_steer_persisted_as_single_marker(self, tmp_path):
        agent, db, db_path = _make_agent_with_db(tmp_path, session_id="steer-merge-persist")
        try:
            messages: list[dict] = [{"role": "user", "content": "start"}]
            agent._flush_messages_to_session_db(messages, conversation_history=[])
            messages.append({"role": "assistant", "tool_calls": [{"id": "c1"}]})
            messages.append({"role": "tool", "content": "tool output", "tool_call_id": "c1"})
            agent.steer("alpha")
            agent.steer("beta")
            agent.steer("gamma")
            assert agent._pending_steer == "alpha\nbeta\ngamma"
            agent._apply_pending_steer_to_tool_results(messages, num_tool_msgs=1)
            ok = agent._flush_messages_to_session_db(messages, conversation_history=[])
            assert ok is True
            durable = _durable(db_path, agent.session_id)
            tool_rows = [m for m in durable if m.get("role") == "tool"]
            assert len(tool_rows) == 1
            assert "alpha" in tool_rows[0]["content"]
            assert "beta" in tool_rows[0]["content"]
            assert "gamma" in tool_rows[0]["content"]
            # Single OOB wrapper, not three
            assert tool_rows[0]["content"].count(STEER_MARKER_OPEN) == 1
            assert tool_rows[0]["content"].count(STEER_MARKER_CLOSE) == 1
        finally:
            db.close()

    def test_restash_merges_with_existing_pending(self):
        """Drain-then-restash path must merge with a concurrent steer that raced in."""
        agent = _bare_agent()
        agent.steer("original")
        # Simulate conversation_loop pre-API drain: drain, find no tool, then
        # a concurrent steer lands before restash
        pre = agent._drain_pending_steer()
        assert pre == "original"
        assert agent._pending_steer is None
        agent.steer("concurrent")
        assert agent._pending_steer == "concurrent"
        # Restash the drained text — must merge, not overwrite
        _lock = getattr(agent, "_pending_steer_lock", None)
        if _lock is not None:
            with _lock:
                if agent._pending_steer:
                    agent._pending_steer = agent._pending_steer + "\n" + pre
                else:
                    agent._pending_steer = pre
        # Order is concurrent first, then restashed pre (matches real code: existing + drained)
        assert agent._pending_steer == "concurrent\noriginal"
        messages = [{"role": "tool", "content": "out", "tool_call_id": "1"}]
        agent._apply_pending_steer_to_tool_results(messages, 1)
        assert "concurrent" in messages[0]["content"]
        assert "original" in messages[0]["content"]


# ---------------------------------------------------------------------------
# 4) marker 不污染 api_content 的独立校验
# ---------------------------------------------------------------------------

class TestSteerApiContentIsolation:
    def test_marker_does_not_create_api_content(self):
        agent = _bare_agent()
        agent.steer("check auth")
        messages = [{"role": "tool", "content": "tool output", "tool_call_id": "1"}]
        agent._apply_pending_steer_to_tool_results(messages, num_tool_msgs=1)
        assert STEER_MARKER_OPEN in messages[0]["content"]
        # api_content must not be created or contain the marker
        assert "api_content" not in messages[0] or messages[0].get("api_content") is None or STEER_MARKER_OPEN not in str(messages[0].get("api_content") or "")

    def test_marker_does_not_pollute_existing_api_content(self):
        agent = _bare_agent()
        agent.steer("note")
        messages = [
            {"role": "tool", "content": "tool output", "tool_call_id": "1", "api_content": "original sidecar"},
        ]
        agent._apply_pending_steer_to_tool_results(messages, num_tool_msgs=1)
        assert STEER_MARKER_OPEN in messages[0]["content"]
        assert messages[0]["api_content"] == "original sidecar"
        assert STEER_MARKER_OPEN not in messages[0]["api_content"]

    def test_persisted_tool_has_no_api_content_marker(self, tmp_path):
        agent, db, db_path = _make_agent_with_db(tmp_path, session_id="steer-api-content")
        try:
            messages: list[dict] = [{"role": "user", "content": "start"}]
            agent._flush_messages_to_session_db(messages, conversation_history=[])
            messages.append({"role": "assistant", "tool_calls": [{"id": "c1"}]})
            messages.append({"role": "tool", "content": "tool output", "tool_call_id": "c1"})
            agent.steer("isolated note")
            agent._apply_pending_steer_to_tool_results(messages, num_tool_msgs=1)
            ok = agent._flush_messages_to_session_db(messages, conversation_history=[])
            assert ok is True
            # Durable row: content has marker, api_content column is NULL / absent
            durable = _durable(db_path, agent.session_id)
            tool_rows = [m for m in durable if m.get("role") == "tool"]
            assert len(tool_rows) == 1
            assert STEER_MARKER_OPEN in tool_rows[0]["content"]
            assert "isolated note" in tool_rows[0]["content"]
            assert "api_content" not in tool_rows[0] or tool_rows[0].get("api_content") is None or STEER_MARKER_OPEN not in str(tool_rows[0].get("api_content") or "")
            # Raw DB check: api_content column must be NULL for the tool row
            with db._read_ctx() as conn:
                row = conn.execute(
                    "SELECT content, api_content FROM messages WHERE session_id=? AND role='tool' ORDER BY id DESC LIMIT 1",
                    (agent.session_id,),
                ).fetchone()
            assert row is not None
            assert STEER_MARKER_OPEN in row["content"]
            assert row["api_content"] is None
        finally:
            db.close()

    def test_api_messages_projection_substitutes_api_content_not_marker(self, tmp_path):
        """The api_messages build pops api_content and does NOT inject marker there."""
        from agent.turn_context import compose_user_api_content

        agent, db, db_path = _make_agent_with_db(tmp_path, session_id="steer-api-projection")
        try:
            # Simulate a user turn with plugin injection that creates api_content
            messages: list[dict] = [{"role": "user", "content": "hello"}]
            # Stamp api_content as the turn prologue would
            injected = compose_user_api_content("hello", "", "PLUGIN-CTX")
            # compose returns None when plugin ctx empty; use explicit api_content
            messages[0]["api_content"] = "hello\n\nPLUGIN-CTX"
            agent._flush_messages_to_session_db(messages, conversation_history=[])
            messages.append({"role": "assistant", "tool_calls": [{"id": "c1"}]})
            messages.append({"role": "tool", "content": "tool output", "tool_call_id": "c1"})
            agent.steer("steer note")
            agent._apply_pending_steer_to_tool_results(messages, num_tool_msgs=1)
            agent._flush_messages_to_session_db(messages, conversation_history=[])
            durable = _durable(db_path, agent.session_id)
            # Durable user row keeps api_content as sidecar, tool row has marker in content
            user_rows = [m for m in durable if m.get("role") == "user"]
            tool_rows = [m for m in durable if m.get("role") == "tool"]
            assert user_rows[0].get("api_content") == "hello\n\nPLUGIN-CTX"
            assert STEER_MARKER_OPEN not in user_rows[0].get("api_content", "")
            assert STEER_MARKER_OPEN in tool_rows[0]["content"]
            assert "api_content" not in tool_rows[0]
        finally:
            db.close()

    def test_turn_finalizer_pending_steer_leftover_is_returned_not_lost(self):
        """When a turn ends with no tool batch to drain into, leftover is returned."""
        from agent.turn_finalizer import finalize_turn

        agent = _bare_agent()
        # Minimal stub that finalize_turn needs beyond _drain_pending_steer
        agent.max_iterations = 10
        agent.iteration_budget = SimpleNamespace(remaining=5, used=1, max_total=10)
        agent.context_compressor = SimpleNamespace(last_prompt_tokens=0)
        agent.model = "test/model"
        agent.provider = "test"
        agent.base_url = ""
        agent.session_id = "sess-finalizer"
        agent.quiet_mode = True
        agent.platform = "cli"
        agent._interrupt_requested = False
        agent._interrupt_message = None
        agent._tool_guardrail_halt_decision = None
        agent._response_was_previewed = False
        agent._skill_nudge_interval = 0
        agent._iters_since_skill = 0
        for attr in (
            "session_input_tokens", "session_output_tokens", "session_cache_read_tokens",
            "session_cache_write_tokens", "session_reasoning_tokens", "session_prompt_tokens",
            "session_completion_tokens", "session_total_tokens", "session_estimated_cost_usd",
        ):
            setattr(agent, attr, 0)
        agent.session_cost_status = "ok"
        agent.session_cost_source = "stub"
        agent._save_trajectory = lambda *a, **k: None
        agent._cleanup_task_resources = lambda *a, **k: None
        agent._drop_trailing_empty_response_scaffolding = lambda *a, **k: None
        agent._persist_session = lambda *a, **k: None
        agent._emit_status = lambda *a, **k: None
        agent._safe_print = lambda *a, **k: None
        agent._handle_max_iterations = lambda messages, n: "summary"
        agent._file_mutation_verifier_enabled = lambda: False
        agent._turn_completion_explainer_enabled = lambda: False
        agent.clear_interrupt = lambda: None
        agent._sync_external_memory_for_turn = lambda **k: None
        # Steer that will be pending at finalization (no tool batch to consume it)
        agent.steer("leftover note")
        with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
            result = finalize_turn(
                agent,
                final_response="Done.",
                api_call_count=1,
                interrupted=False,
                failed=False,
                messages=[{"role": "user", "content": "hi"}, {"role": "assistant", "content": "Done."}],
                conversation_history=[],
                effective_task_id="t1",
                turn_id="turn1",
                user_message="hi",
                original_user_message="hi",
                _should_review_memory=False,
                _turn_exit_reason="text_response(final)",
            )
        assert result.get("pending_steer") == "leftover note"
        # And it was drained
        assert agent._pending_steer is None
