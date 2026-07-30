"""Integration test for the codex_app_server runtime path through AIAgent.

Verifies that:
  - api_mode='codex_app_server' is accepted on AIAgent construction
  - run_conversation() takes the early-return path and never enters the
    chat completions loop
  - Projected messages from a fake Codex session land in the messages list
  - tool_iterations from the codex session tick the skill nudge counter
  - Memory nudge counter ticks once per turn
  - The returned dict has the same shape as the chat_completions path
"""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import run_agent
from agent.transports.codex_app_server_session import CodexAppServerSession, TurnResult
from hermes_state import SessionDB


@pytest.fixture
def fake_session(monkeypatch):
    """Replace CodexAppServerSession with a stub that returns a fixed
    TurnResult, so we can drive AIAgent without spawning real codex."""

    def fake_run_turn(self, user_input: str, **kwargs):
        return TurnResult(
            final_text=f"echo: {user_input}",
            projected_messages=[
                {"role": "assistant", "content": None,
                 "tool_calls": [{"id": "exec_1", "type": "function",
                                 "function": {"name": "exec_command",
                                              "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "exec_1", "content": "ok"},
                {"role": "assistant", "content": f"echo: {user_input}"},
            ],
            tool_iterations=1,
            interrupted=False,
            error=None,
            turn_id="turn-stub-1",
            thread_id="thread-stub-1",
        )

    monkeypatch.setattr(CodexAppServerSession, "run_turn", fake_run_turn)
    monkeypatch.setattr(
        CodexAppServerSession, "ensure_started", lambda self: "thread-stub-1"
    )


def _make_codex_agent(**kwargs):
    """Construct an AIAgent in codex_app_server mode without contacting any
    real provider. We pass api_mode explicitly so the constructor takes the
    fast path for direct credentials."""
    owned_session_db = None
    if "session_db" not in kwargs:
        owned_session_db = SessionDB(Path(":memory:"))
        kwargs["session_db"] = owned_session_db
    agent = run_agent.AIAgent(
        api_key="stub",
        base_url="https://stub.invalid",
        provider="openai",
        api_mode="codex_app_server",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        **kwargs,
    )
    # Keep the in-memory database alive for the agent's whole test lifetime.
    agent._test_owned_session_db = owned_session_db
    return agent


class TestApiModeAccepted:
    def test_api_mode_is_codex_app_server(self):
        agent = _make_codex_agent()
        assert agent.api_mode == "codex_app_server"


class TestRunConversationCodexPath:
    def test_run_conversation_returns_codex_shape(self, fake_session):
        agent = _make_codex_agent()
        # No background review fork during tests
        with patch.object(agent, "_spawn_background_review", return_value=None):
            result = agent.run_conversation("hello there")
        assert result["final_response"] == "echo: hello there"
        assert result["completed"] is True
        assert result["partial"] is False
        assert result["error"] is None
        assert result["api_calls"] == 1
        assert result["codex_thread_id"] == "thread-stub-1"
        assert result["codex_turn_id"] == "turn-stub-1"

    @pytest.mark.parametrize(
        ("interrupted", "error"),
        [
            (False, "turn failed after start"),
            (True, None),
        ],
    )
    def test_started_turn_failure_still_counts_one_api_call(
        self, monkeypatch, interrupted, error
    ):
        def failed_turn(self, user_input: str, **kwargs):
            return TurnResult(
                error=error,
                interrupted=interrupted,
                turn_id="turn-started-1",
                thread_id="thread-started-1",
                should_retire=True,
            )

        monkeypatch.setattr(CodexAppServerSession, "run_turn", failed_turn)
        agent = _make_codex_agent()
        with patch.object(agent, "_spawn_background_review", return_value=None):
            result = agent.run_conversation("count this request")

        assert result["completed"] is False
        assert result["api_calls"] == 1
        assert agent.session_api_calls == 1

    def test_codex_thread_binding_resumes_after_agent_recreation(
        self, monkeypatch, tmp_path
    ):
        instances = []

        class RecordingSession:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.thread_id = (
                    kwargs.get("resume_thread_id") or "thread-persisted-1"
                )
                self.closed = False
                self.lease_acquired = False
                instances.append(self)

            def run_turn(self, user_input, **_kwargs):
                acquire = self.kwargs.get("lease_acquire")
                if acquire is not None and not self.lease_acquired:
                    assert acquire()
                    self.lease_acquired = True
                callback = self.kwargs.get("on_thread_ready")
                if callback is not None:
                    callback(self.thread_id)
                return TurnResult(
                    final_text=f"done:{user_input}",
                    projected_messages=[
                        {"role": "assistant", "content": f"done:{user_input}"}
                    ],
                    turn_id=f"turn-{len(instances)}",
                    thread_id=self.thread_id,
                )

            def close(self):
                self.closed = True
                if self.lease_acquired:
                    release = self.kwargs.get("lease_release")
                    assert release is not None and release()
                    self.lease_acquired = False

        monkeypatch.setattr(
            "agent.transports.codex_app_server_session.CodexAppServerSession",
            RecordingSession,
        )

        db = SessionDB(tmp_path / "state.db")
        sid = "hermes-session-1"
        db.create_session(session_id=sid, source="telegram", model="codex")
        try:
            first = _make_codex_agent(session_db=db, session_id=sid)
            with patch.object(first, "_spawn_background_review", return_value=None):
                first_result = first.run_conversation("first")
            first.release_clients()

            assert first_result["completed"] is True
            assert db.get_codex_thread_id(sid) == "thread-persisted-1"
            assert instances[0].kwargs["resume_thread_id"] is None
            assert instances[0].closed is True

            second = _make_codex_agent(session_db=db, session_id=sid)
            with patch.object(second, "_spawn_background_review", return_value=None):
                second_result = second.run_conversation("second")
            second.release_clients()

            assert second_result["completed"] is True
            assert instances[1].kwargs["resume_thread_id"] == "thread-persisted-1"
            assert db.get_codex_thread_id(sid) == "thread-persisted-1"
        finally:
            db.close()

    @pytest.mark.parametrize("failure_mode", ["binding_read", "missing_row"])
    def test_codex_thread_start_fails_closed_when_binding_state_is_unavailable(
        self, monkeypatch, tmp_path, failure_mode
    ):
        db = SessionDB(tmp_path / f"{failure_mode}.db")
        sid = f"hermes-{failure_mode}"
        if failure_mode == "binding_read":
            db.create_session(session_id=sid, source="telegram", model="codex")
            monkeypatch.setattr(
                db,
                "get_codex_thread_id",
                MagicMock(side_effect=RuntimeError("binding read failed")),
            )

        session_factory = MagicMock(
            side_effect=AssertionError(
                "CodexAppServerSession must not be constructed"
            )
        )
        monkeypatch.setattr(
            "agent.transports.codex_app_server_session.CodexAppServerSession",
            session_factory,
        )

        agent = _make_codex_agent(session_db=db, session_id=sid)
        # Exercise the exact cold-rebuild state: the AIAgent believes its
        # persistence row is already owned by the surrounding session store.
        # A missing row must be detected explicitly rather than interpreted as
        # an unbound session that is safe to attach to a fresh Codex thread.
        agent._session_db_created = True
        try:
            with patch.object(agent, "_spawn_background_review", return_value=None):
                result = agent.run_conversation("must not start")

            session_factory.assert_not_called()
            assert getattr(agent, "_codex_session", None) is None
            assert result["completed"] is False
            assert result["partial"] is True
            assert result["api_calls"] == 0
            assert "refused to start a new thread" in result["error"]
        finally:
            agent.release_clients()
            db.close()

    def test_codex_thread_start_fails_closed_when_session_creation_fails(
        self, monkeypatch, tmp_path
    ):
        db = SessionDB(tmp_path / "create_failure.db")
        sid = "hermes-create-failure"
        session_factory = MagicMock(
            side_effect=AssertionError(
                "CodexAppServerSession must not be constructed"
            )
        )
        monkeypatch.setattr(
            "agent.transports.codex_app_server_session.CodexAppServerSession",
            session_factory,
        )

        agent = _make_codex_agent(session_db=db, session_id=sid)
        monkeypatch.setattr(agent, "_ensure_db_session", MagicMock())
        try:
            with patch.object(agent, "_spawn_background_review", return_value=None):
                result = agent.run_conversation("must not start")

            agent._ensure_db_session.assert_called()
            session_factory.assert_not_called()
            assert getattr(agent, "_codex_session", None) is None
            assert result["completed"] is False
            assert result["partial"] is True
            assert result["api_calls"] == 0
            assert "refused to start a new thread" in result["error"]
        finally:
            agent.release_clients()
            db.close()

    def test_codex_turn_lease_contention_runs_no_app_server_rpc(
        self, monkeypatch, tmp_path
    ):
        db = SessionDB(tmp_path / "lease_contention.db")
        sid = "hermes-lease-contention"
        holder = str(uuid.uuid4())
        db.create_session(session_id=sid, source="telegram", model="codex")
        assert db.try_acquire_codex_turn_lease(sid, holder)

        client_factory = MagicMock(
            side_effect=AssertionError("Codex client must not be constructed")
        )
        monkeypatch.setattr(
            "agent.transports.codex_app_server_session.CodexAppServerClient",
            client_factory,
        )

        agent = _make_codex_agent(session_db=db, session_id=sid)
        try:
            with patch.object(agent, "_spawn_background_review", return_value=None):
                result = agent.run_conversation("must serialize")

            assert result["completed"] is False
            assert result["partial"] is True
            assert result["api_calls"] == 0
            assert "another Hermes/Codex client" in result["error"]
            client_factory.assert_not_called()
        finally:
            agent.release_clients()
            assert db.release_codex_turn_lease(sid, holder)
            db.close()

    @pytest.mark.parametrize("missing", ["session_db", "session_id"])
    def test_codex_thread_start_fails_closed_without_durable_session_identity(
        self, monkeypatch, missing
    ):
        session_factory = MagicMock(
            side_effect=AssertionError(
                "CodexAppServerSession must not be constructed"
            )
        )
        monkeypatch.setattr(
            "agent.transports.codex_app_server_session.CodexAppServerSession",
            session_factory,
        )

        agent = _make_codex_agent()
        if missing == "session_db":
            agent._session_db = None
        else:
            agent.session_id = None

        with patch.object(agent, "_spawn_background_review", return_value=None):
            result = agent.run_conversation("must not start")

        session_factory.assert_not_called()
        assert getattr(agent, "_codex_session", None) is None
        assert result["completed"] is False
        assert result["partial"] is True
        assert result["api_calls"] == 0
        assert "refused to start a new thread" in result["error"]

    def test_codex_app_server_token_usage_updates_session_accounting(self, monkeypatch):
        def fake_run_turn(self, user_input: str, **kwargs):
            return TurnResult(
                final_text="done",
                projected_messages=[{"role": "assistant", "content": "done"}],
                turn_id="turn-usage-1",
                thread_id="thread-usage-1",
                token_usage_last={
                    "totalTokens": 130,
                    "inputTokens": 80,
                    "cachedInputTokens": 20,
                    "outputTokens": 25,
                    "reasoningOutputTokens": 5,
                },
                model_context_window=200000,
            )

        monkeypatch.setattr(CodexAppServerSession, "run_turn", fake_run_turn)
        monkeypatch.setattr(
            CodexAppServerSession, "ensure_started", lambda self: "thread-usage-1"
        )
        agent = _make_codex_agent()
        with patch.object(agent, "_spawn_background_review", return_value=None):
            result = agent.run_conversation("hello")

        assert result["api_calls"] == 1
        assert result["prompt_tokens"] == 100
        assert result["completion_tokens"] == 25
        assert result["total_tokens"] == 130
        assert result["input_tokens"] == 80
        assert result["output_tokens"] == 25
        assert result["cache_read_tokens"] == 20
        assert result["cache_write_tokens"] == 0
        assert result["reasoning_tokens"] == 5
        assert result["last_prompt_tokens"] == 100

        assert agent.session_api_calls == 1
        assert agent.session_prompt_tokens == 100
        assert agent.session_completion_tokens == 25
        assert agent.session_total_tokens == 130
        assert agent.session_input_tokens == 80
        assert agent.session_output_tokens == 25
        assert agent.session_cache_read_tokens == 20
        assert agent.session_cache_write_tokens == 0
        assert agent.session_reasoning_tokens == 5
        assert agent.context_compressor.last_prompt_tokens == 100
        assert agent.context_compressor.last_completion_tokens == 25
        assert agent.context_compressor.last_total_tokens == 130
        assert agent.context_compressor.context_length == 200000

    def test_native_codex_compaction_updates_bookkeeping(self, monkeypatch):
        def fake_run_turn(self, user_input: str, **kwargs):
            return TurnResult(
                final_text="done",
                projected_messages=[{"role": "assistant", "content": "done"}],
                turn_id="turn-compact-1",
                thread_id="thread-compact-1",
                compacted=True,
                token_usage_last={
                    "totalTokens": 300_000,
                    "inputTokens": 300_000,
                    "cachedInputTokens": 0,
                    "outputTokens": 0,
                    "reasoningOutputTokens": 0,
                },
            )

        monkeypatch.setattr(CodexAppServerSession, "run_turn", fake_run_turn)
        monkeypatch.setattr(
            CodexAppServerSession, "ensure_started", lambda self: "thread-compact-1"
        )
        events = []
        agent = _make_codex_agent(event_callback=lambda name, payload: events.append((name, payload)))

        with patch.object(agent, "_spawn_background_review", return_value=None):
            result = agent.run_conversation("hello")

        assert result["completed"] is True
        assert agent.context_compressor.compression_count == 1
        # A compacted turn with real usage is judged against that same real
        # prompt count, exactly like a normal completed compression boundary.
        assert agent.context_compressor.last_prompt_tokens == 300_000
        assert agent.context_compressor.awaiting_real_usage_after_compression is False
        assert agent.context_compressor._ineffective_compression_count == 1
        assert events == [
            (
                "session:compress",
                {
                    "platform": "",
                    "session_id": agent.session_id,
                    "old_session_id": "",
                    "in_place": False,
                    "compression_count": 1,
                    "runtime": "codex_app_server",
                    "thread_id": "thread-compact-1",
                    "turn_id": "turn-compact-1",
                },
            )
        ]

    def test_native_codex_compaction_waits_through_empty_usage_for_real_prompt(
        self, monkeypatch
    ):
        """The app-server usage notification is a sibling of compaction.

        Codex may finish the compaction turn before it has a ``last`` usage
        bucket.  That empty update is not evidence the compaction worked; the
        next real low prompt must be the one that clears the verdict.
        """
        def fake_run_turn(self, user_input: str, **kwargs):
            return TurnResult(
                final_text="done",
                projected_messages=[{"role": "assistant", "content": "done"}],
                turn_id="turn-compact-empty-usage",
                thread_id="thread-compact-empty-usage",
                compacted=True,
                # A non-empty partial sibling payload is still not a prompt
                # measurement, so it must leave the latch armed just like an
                # entirely absent usage notification.
                token_usage_last={"totalTokens": 10, "outputTokens": 10},
            )

        monkeypatch.setattr(CodexAppServerSession, "run_turn", fake_run_turn)
        monkeypatch.setattr(
            CodexAppServerSession,
            "ensure_started",
            lambda self: "thread-compact-empty-usage",
        )
        agent = _make_codex_agent()
        with patch.object(agent, "_spawn_background_review", return_value=None):
            result = agent.run_conversation("hello")

        assert result["completed"] is True
        assert agent.context_compressor.awaiting_real_usage_after_compression is True
        assert agent.context_compressor._verify_compaction_cleared_threshold is True
        assert agent.context_compressor.last_prompt_tokens == -1

        from agent.codex_runtime import _record_codex_app_server_usage

        _record_codex_app_server_usage(
            agent,
            TurnResult(
                token_usage_last={
                    "totalTokens": 5_010,
                    "inputTokens": 5_000,
                    "cachedInputTokens": 0,
                    "outputTokens": 10,
                }
            ),
        )

        assert agent.context_compressor.last_prompt_tokens == 5_000
        assert agent.context_compressor.awaiting_real_usage_after_compression is False
        assert agent.context_compressor._verify_compaction_cleared_threshold is False

    def test_projected_messages_are_spliced(self, fake_session):
        agent = _make_codex_agent()
        with patch.object(agent, "_spawn_background_review", return_value=None):
            result = agent.run_conversation("hello")
        msgs = result["messages"]
        # User message + 3 projected (assistant tool_call + tool + assistant text)
        assert len(msgs) >= 4
        assert msgs[0]["role"] == "user"
        assert msgs[0]["content"] == "hello"
        # Last assistant message has the final text
        final = [m for m in msgs if m.get("role") == "assistant"
                 and m.get("content") == "echo: hello"]
        assert final, f"expected final assistant message in {msgs}"

    def test_projected_messages_are_synced_to_external_memory(self, fake_session):
        agent = _make_codex_agent()
        agent._memory_manager = MagicMock()
        agent._memory_manager.build_system_prompt.return_value = ""

        with patch.object(agent, "_spawn_background_review", return_value=None):
            result = agent.run_conversation("hello")

        agent._memory_manager.sync_all.assert_called_once()
        assert agent._memory_manager.sync_all.call_args.kwargs["messages"] == result["messages"]

    def test_nudge_counters_tick(self, fake_session):
        """The skill nudge counter must accumulate tool_iterations across
        turns. The memory nudge counter is gated on memory being configured
        (which we skip via skip_memory=True), so we don't assert on it here —
        a separate test below covers that path explicitly."""
        agent = _make_codex_agent()
        agent._iters_since_skill = 0
        agent._user_turn_count = 0
        with patch.object(agent, "_spawn_background_review", return_value=None):
            agent.run_conversation("first")
        assert agent._iters_since_skill == 1  # one tool_iteration in fake turn
        # _user_turn_count is incremented by run_conversation pre-loop, not
        # by the codex helper — confirms we delegate that to the standard flow.
        assert agent._user_turn_count == 1
        with patch.object(agent, "_spawn_background_review", return_value=None):
            agent.run_conversation("second")
        assert agent._iters_since_skill == 2
        assert agent._user_turn_count == 2

    def test_user_message_not_duplicated(self, fake_session):
        """Regression guard: the user message must appear exactly once in
        the messages list. The standard run_conversation pre-loop appends
        it, and the codex helper must NOT append again."""
        agent = _make_codex_agent()
        with patch.object(agent, "_spawn_background_review", return_value=None):
            result = agent.run_conversation("ping unique 12345")
        user_count = sum(
            1 for m in result["messages"]
            if m.get("role") == "user" and m.get("content") == "ping unique 12345"
        )
        assert user_count == 1, f"user message appeared {user_count}× in {result['messages']}"

    def test_background_review_NOT_invoked_below_threshold(self, fake_session):
        """A single turn shouldn't trigger background review — counters
        haven't reached the nudge interval (default 10)."""
        agent = _make_codex_agent()
        agent._memory_nudge_interval = 10
        agent._skill_nudge_interval = 10
        agent._iters_since_skill = 0
        with patch.object(agent, "_spawn_background_review",
                          return_value=None) as spawn:
            agent.run_conversation("ping")
        # Below threshold → review should NOT fire (was a real bug:
        # the helper was calling _spawn_background_review() with no
        # args after every turn, which would crash with TypeError).
        assert not spawn.called

    def test_background_review_skill_trigger_fires_above_threshold(
        self, monkeypatch
    ):
        """When tool iterations cross the skill nudge interval, the
        background review fires with review_skills=True and the right
        messages_snapshot signature."""
        from agent.transports.codex_app_server_session import (
            CodexAppServerSession, TurnResult,
        )
        # Make the fake session report 10 tool iterations in one turn
        # (matching the default skill threshold).
        def fake_run_turn(self, user_input: str, **kwargs):
            return TurnResult(
                final_text=f"echo: {user_input}",
                projected_messages=[
                    {"role": "assistant", "content": f"echo: {user_input}"},
                ],
                tool_iterations=10,
                turn_id="t1", thread_id="th1",
            )
        monkeypatch.setattr(CodexAppServerSession, "run_turn", fake_run_turn)
        monkeypatch.setattr(
            CodexAppServerSession, "ensure_started", lambda self: "th1"
        )

        agent = _make_codex_agent()
        agent._skill_nudge_interval = 10
        agent._iters_since_skill = 0
        # Make valid_tool_names include 'skill_manage' so the gate passes
        agent.valid_tool_names = set(getattr(agent, "valid_tool_names", set()))
        agent.valid_tool_names.add("skill_manage")

        with patch.object(agent, "_spawn_background_review",
                          return_value=None) as spawn:
            agent.run_conversation("do tool work")

        assert spawn.called, "skill threshold tripped but review didn't fire"
        # Verify the call signature matches what _spawn_background_review
        # actually expects — this is the regression guard for the original
        # bug where the codex path called it with no args at all.
        call = spawn.call_args
        assert "messages_snapshot" in call.kwargs
        assert isinstance(call.kwargs["messages_snapshot"], list)
        assert call.kwargs["review_skills"] is True
        # Counter should be reset after the review fires
        assert agent._iters_since_skill == 0

    def test_background_review_signature_never_breaks(self, fake_session):
        """Even when no trigger fires, the helper must never call
        _spawn_background_review with the wrong signature. Run a turn,
        then run another turn after manually tripping the skill counter
        and confirm the call shape is the kwargs-only form the function
        actually accepts."""
        agent = _make_codex_agent()
        agent._skill_nudge_interval = 1  # very low so any iter trips it
        agent._iters_since_skill = 0
        agent.valid_tool_names = set(getattr(agent, "valid_tool_names", set()))
        agent.valid_tool_names.add("skill_manage")

        with patch.object(agent, "_spawn_background_review",
                          return_value=None) as spawn:
            agent.run_conversation("first")
        # The fake session reports tool_iterations=1, which trips
        # _skill_nudge_interval=1. So review should fire.
        assert spawn.called
        # Critical invariant: positional args must be empty, all real
        # args must be kwargs (matching _spawn_background_review's
        # actual signature).
        call = spawn.call_args
        assert call.args == (), (
            f"expected no positional args, got {call.args!r} — "
            "would crash _spawn_background_review at runtime"
        )
        assert "messages_snapshot" in call.kwargs

    def test_chat_completions_loop_is_not_entered(self, fake_session):
        """The early-return must bypass the regular API call loop entirely.
        We confirm by patching the SDK call and asserting it's never invoked."""
        agent = _make_codex_agent()
        # The chat_completions loop calls self.client.chat.completions.create(...)
        # If our early-return works, that path is dead.
        with patch.object(agent, "client") as client_mock, patch.object(
            agent, "_spawn_background_review", return_value=None
        ):
            agent.run_conversation("hi")
        assert not client_mock.chat.completions.create.called

    def test_gateway_terminal_cwd_seeds_codex_thread_cwd(self, monkeypatch, tmp_path):
        """Gateway sessions set TERMINAL_CWD without stamping agent.session_cwd.
        Codex app-server must still start in that configured workspace instead
        of falling back to the Hermes daemon process cwd."""
        from agent.transports.codex_app_server_session import (
            CodexAppServerSession, TurnResult,
        )

        captured: dict[str, str] = {}

        def fake_init(self, **kwargs):
            captured["cwd"] = kwargs["cwd"]
            self._thread_id = "thread-stub-1"

        def fake_run_turn(self, user_input: str, **kwargs):
            return TurnResult(
                final_text="ok",
                projected_messages=[{"role": "assistant", "content": "ok"}],
                turn_id="turn-stub-1",
                thread_id="thread-stub-1",
            )

        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        monkeypatch.setattr(CodexAppServerSession, "__init__", fake_init)
        monkeypatch.setattr(CodexAppServerSession, "run_turn", fake_run_turn)

        agent = _make_codex_agent()
        assert not hasattr(agent, "session_cwd")
        with patch.object(agent, "_spawn_background_review", return_value=None):
            agent.run_conversation("hi")

        assert captured["cwd"] == str(tmp_path)

    def _capture_routing_agent(self, monkeypatch):
        """Build a codex agent with a CodexAppServerSession stub that captures
        the request_routing passed at construction time, so we can assert how
        the gateway-context approval routing was resolved."""
        captured: dict = {}

        def fake_init(self, **kwargs):
            captured.update(kwargs)
            self._thread_id = "thread-stub-1"

        def fake_run_turn(self, user_input: str, **kwargs):
            return TurnResult(
                final_text="ok",
                projected_messages=[{"role": "assistant", "content": "ok"}],
                turn_id="turn-stub-1",
                thread_id="thread-stub-1",
            )

        monkeypatch.setattr(CodexAppServerSession, "__init__", fake_init)
        monkeypatch.setattr(CodexAppServerSession, "run_turn", fake_run_turn)
        monkeypatch.setattr(
            CodexAppServerSession, "ensure_started", lambda self: "thread-stub-1"
        )
        return captured

    def test_approvals_mode_off_auto_approves_codex_server_requests(
        self, monkeypatch
    ):
        """When the user disables Hermes approvals, codex app-server approval
        requests should not fail closed just because no interactive callback is
        wired (the typical gateway path). Codex's own sandbox permission
        profile remains the filesystem boundary."""
        captured = self._capture_routing_agent(monkeypatch)
        with patch(
            "hermes_cli.config.load_config",
            return_value={"approvals": {"mode": "off"}},
        ):
            agent = _make_codex_agent()
            with patch.object(
                agent, "_spawn_background_review", return_value=None
            ):
                agent.run_conversation("write something")
        routing = captured["request_routing"]
        assert routing.auto_approve_exec is True
        assert routing.auto_approve_apply_patch is True

    def test_yaml_boolean_false_approval_mode_also_auto_approves(
        self, monkeypatch
    ):
        """YAML 1.1 parses unquoted `off` as False; match the normal approval
        subsystem's compatibility behavior for codex app-server routing too."""
        captured = self._capture_routing_agent(monkeypatch)
        with patch(
            "hermes_cli.config.load_config",
            return_value={"approvals": {"mode": False}},
        ):
            agent = _make_codex_agent()
            with patch.object(
                agent, "_spawn_background_review", return_value=None
            ):
                agent.run_conversation("write something")
        routing = captured["request_routing"]
        assert routing.auto_approve_exec is True
        assert routing.auto_approve_apply_patch is True

    def test_manual_approvals_keep_codex_server_requests_fail_closed(
        self, monkeypatch
    ):
        """Default (manual) approvals must preserve the fail-closed behavior —
        this fix is a no-op for users who haven't opted out."""
        captured = self._capture_routing_agent(monkeypatch)
        with patch(
            "hermes_cli.config.load_config",
            return_value={"approvals": {"mode": "manual"}},
        ):
            agent = _make_codex_agent()
            with patch.object(
                agent, "_spawn_background_review", return_value=None
            ):
                agent.run_conversation("write something")
        routing = captured["request_routing"]
        assert routing.auto_approve_exec is False
        assert routing.auto_approve_apply_patch is False

    def test_frozen_yolo_env_auto_approves_codex_server_requests(
        self, monkeypatch
    ):
        """--yolo / HERMES_YOLO_MODE (frozen into _YOLO_MODE_FROZEN at import
        time — a prompt-injection-safe process-scoped bypass) should flow
        through to codex app-server routing so gateway/cron contexts do not
        fail closed when the user launched with yolo mode."""
        import tools.approval as _approval

        captured = self._capture_routing_agent(monkeypatch)
        monkeypatch.setattr(_approval, "_YOLO_MODE_FROZEN", True)
        with patch(
            "hermes_cli.config.load_config",
            return_value={"approvals": {"mode": "manual"}},
        ):
            agent = _make_codex_agent()
            with patch.object(
                agent, "_spawn_background_review", return_value=None
            ):
                agent.run_conversation("write something")
        routing = captured["request_routing"]
        assert routing.auto_approve_exec is True
        assert routing.auto_approve_apply_patch is True

    def test_session_yolo_auto_approves_codex_server_requests(
        self, monkeypatch
    ):
        """The /yolo session toggle should be honored at Codex session creation
        time, independent of the startup-time approvals config."""
        captured = self._capture_routing_agent(monkeypatch)
        with patch(
            "hermes_cli.config.load_config",
            return_value={"approvals": {"mode": "manual"}},
        ):
            agent = _make_codex_agent()
            with patch(
                "tools.approval.is_approval_bypass_active_for_session",
                return_value=True,
            ), patch.object(
                agent, "_spawn_background_review", return_value=None
            ):
                agent.run_conversation("write something")
        routing = captured["request_routing"]
        assert routing.auto_approve_exec is True
        assert routing.auto_approve_apply_patch is True


class TestReviewForkApiModeDowngrade:
    """When the parent agent runs on codex_app_server, the background
    review fork must downgrade to codex_responses — otherwise the fork
    can't dispatch agent-loop tools (memory, skill_manage) which is the
    whole point of the review."""

    def test_codex_app_server_parent_downgrades_review_fork(self):
        """Live test against the real _spawn_background_review code path:
        verify the review_agent gets api_mode=codex_responses when the
        parent is codex_app_server."""
        from unittest.mock import MagicMock, patch as _patch
        agent = _make_codex_agent()
        # Pretend memory + skills are configured so the review fork
        # reaches the AIAgent constructor.
        agent._memory_store = MagicMock()
        agent._memory_enabled = True
        agent._user_profile_enabled = True
        # Mock _current_main_runtime to return the parent's codex_app_server
        # state so we can confirm the helper detects + downgrades it.
        agent._current_main_runtime = lambda: {
            "api_mode": "codex_app_server",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "api_key": "stub-token",
        }
        # Capture what AIAgent gets constructed with inside the helper.
        captured = {}

        def _capture_init(self, **kwargs):
            captured.update(kwargs)
            # Set bare attributes the rest of the spawn function reads
            # so it can finish without exploding.
            self.api_mode = kwargs.get("api_mode")
            self.provider = kwargs.get("provider")
            self.model = kwargs.get("model")
            self._memory_write_origin = None
            self._memory_write_context = None
            self._memory_store = None
            self._memory_enabled = False
            self._user_profile_enabled = False
            self._memory_nudge_interval = 0
            self._skill_nudge_interval = 0
            self.suppress_status_output = False
            self._session_messages = []

            def _no_op_run_conv(*a, **kw):
                return {"final_response": "", "messages": []}
            self.run_conversation = _no_op_run_conv

            def _no_op_close(*a, **kw):
                return None
            self.close = _no_op_close

        with _patch("run_agent.AIAgent.__init__", _capture_init):
            agent._spawn_background_review(
                messages_snapshot=[{"role": "user", "content": "x"}],
                review_memory=True,
                review_skills=False,
            )
            # Wait for the spawned thread to actually execute
            import time
            for _ in range(30):
                if "api_mode" in captured:
                    break
                time.sleep(0.1)

        assert captured.get("api_mode") == "codex_responses", (
            f"review fork should be downgraded to codex_responses when "
            f"parent is codex_app_server; got {captured.get('api_mode')!r}"
        )


class TestErrorHandling:
    def test_session_exception_returns_partial_with_error(self, monkeypatch):
        def boom_run_turn(self, user_input, **kwargs):
            raise RuntimeError("subprocess died")

        monkeypatch.setattr(CodexAppServerSession, "ensure_started",
                            lambda self: "t1")
        monkeypatch.setattr(CodexAppServerSession, "run_turn", boom_run_turn)

        agent = _make_codex_agent()
        with patch.object(agent, "_spawn_background_review", return_value=None):
            result = agent.run_conversation("hi")
        assert result["completed"] is False
        assert result["partial"] is True
        assert "subprocess died" in result["error"]
        assert "codex-runtime auto" in result["final_response"]

    def test_interrupted_turn_marked_partial(self, monkeypatch):
        def interrupted_turn(self, user_input, **kwargs):
            return TurnResult(
                final_text="",
                projected_messages=[],
                tool_iterations=0,
                interrupted=True,
                error="user interrupted",
                turn_id="t",
                thread_id="th",
            )
        monkeypatch.setattr(CodexAppServerSession, "ensure_started",
                            lambda self: "th")
        monkeypatch.setattr(CodexAppServerSession, "run_turn", interrupted_turn)

        agent = _make_codex_agent()
        with patch.object(agent, "_spawn_background_review", return_value=None):
            result = agent.run_conversation("hi")
        assert result["completed"] is False
        assert result["partial"] is True
        assert result["error"] == "user interrupted"


class TestSessionRetirementOnRunAgent:
    """run_agent.py side: when run_turn returns should_retire=True, the
    AIAgent must close + null _codex_session so the next turn respawns."""

    def test_should_retire_drops_session(self, monkeypatch):
        closes = {"count": 0}

        def fake_run_turn(self, user_input, **kwargs):
            return TurnResult(
                final_text="",
                projected_messages=[],
                tool_iterations=0,
                interrupted=True,
                error="turn timed out after 600.0s",
                turn_id="tu1",
                thread_id="th1",
                should_retire=True,
            )

        def fake_close(self):
            closes["count"] += 1

        monkeypatch.setattr(CodexAppServerSession, "ensure_started",
                            lambda self: "th1")
        monkeypatch.setattr(CodexAppServerSession, "run_turn", fake_run_turn)
        monkeypatch.setattr(CodexAppServerSession, "close", fake_close)

        agent = _make_codex_agent()
        with patch.object(agent, "_spawn_background_review", return_value=None):
            result = agent.run_conversation("hi")

        # The session was closed and cleared
        assert closes["count"] == 1
        assert getattr(agent, "_codex_session", "MISSING") is None
        # Partial result was still returned (caller still sees the error)
        assert result["partial"] is True
        assert result["error"] == "turn timed out after 600.0s"

    def test_normal_turn_keeps_session(self, fake_session):
        """fake_session fixture returns should_retire=False (default).
        The session must stay attached for the next turn to reuse."""
        agent = _make_codex_agent()
        with patch.object(agent, "_spawn_background_review", return_value=None):
            agent.run_conversation("hi")
        # Session was lazily created and still attached.
        assert getattr(agent, "_codex_session", None) is not None

    def test_exception_path_also_drops_session(self, monkeypatch):
        """Even if run_turn raises (not just sets should_retire), we must
        drop the session — a thrown exception is the strongest possible
        signal the process is dead."""
        closes = {"count": 0}

        def boom_run_turn(self, user_input, **kwargs):
            raise RuntimeError("codex segfaulted")

        def fake_close(self):
            closes["count"] += 1

        monkeypatch.setattr(CodexAppServerSession, "ensure_started",
                            lambda self: "th1")
        monkeypatch.setattr(CodexAppServerSession, "run_turn", boom_run_turn)
        monkeypatch.setattr(CodexAppServerSession, "close", fake_close)

        agent = _make_codex_agent()
        with patch.object(agent, "_spawn_background_review", return_value=None):
            result = agent.run_conversation("hi")

        assert closes["count"] == 1
        assert agent._codex_session is None
        assert result["completed"] is False
        assert "codex segfaulted" in result["error"]

    def test_release_clients_closes_codex_session_idempotently(self):
        agent = _make_codex_agent()
        codex_session = MagicMock()
        agent._codex_session = codex_session

        agent.release_clients()
        agent.release_clients()

        codex_session.close.assert_called_once_with()
        assert agent._codex_session is None

    def test_close_closes_codex_session_idempotently(self):
        agent = _make_codex_agent()
        codex_session = MagicMock()
        agent._codex_session = codex_session

        agent.close()
        agent.close()

        codex_session.close.assert_called_once_with()
        assert agent._codex_session is None


class TestCodexToolProgressBridge:
    """#38835 / #33200: Codex app-server item notifications must surface as
    Hermes tool-progress so gateways show verbose breadcrumbs on this route.
    The original item/started-only mapper was superseded by the full event
    bridge (make_codex_app_server_event_bridge); these tests pin the same
    mapping contract against the bridge helpers."""

    def test_mapper_command_execution(self):
        from agent.codex_runtime import (
            _codex_item_to_args,
            _codex_item_to_preview,
            _codex_item_to_tool_name,
        )
        item = {"type": "commandExecution", "command": "ls -la", "cwd": "/tmp"}
        assert _codex_item_to_tool_name(item) == "exec_command"
        assert _codex_item_to_preview(item) == "ls -la"
        assert _codex_item_to_args(item) == {"command": "ls -la", "cwd": "/tmp"}

    def test_mapper_file_change(self):
        from agent.codex_runtime import (
            _codex_item_to_preview,
            _codex_item_to_tool_name,
        )
        item = {
            "type": "fileChange",
            "changes": [{"path": "a.py"}, {"path": "b.py"}],
        }
        assert _codex_item_to_tool_name(item) == "apply_patch"
        assert _codex_item_to_preview(item) == "a.py, b.py"

    def test_mapper_mcp_and_dynamic_tool_calls(self):
        from agent.codex_runtime import (
            _codex_item_to_args,
            _codex_item_to_tool_name,
        )
        mcp = {"type": "mcpToolCall", "server": "fs", "tool": "read", "arguments": {"p": 1}}
        assert _codex_item_to_tool_name(mcp) == "mcp.fs.read"
        assert _codex_item_to_args(mcp) == {"p": 1}

        dyn = {"type": "dynamicToolCall", "tool": "web_search", "arguments": {"q": "x"}}
        assert _codex_item_to_tool_name(dyn) == "web_search"

    def test_bridge_ignores_non_tool_items_and_other_methods(self):
        from agent.codex_runtime import make_codex_app_server_event_bridge
        events = []
        agent = SimpleNamespace(
            tool_progress_callback=lambda *a, **kw: events.append(a),
            _fire_stream_delta=None,
            _fire_reasoning_delta=None,
            _emit_interim_assistant_message=None,
        )
        on_event = make_codex_app_server_event_bridge(agent)
        # agentMessage started items are not tool-shaped
        on_event({"method": "item/started", "params": {
            "item": {"type": "agentMessage", "text": "hi"}}})
        # malformed / empty notes
        on_event({"method": "item/completed", "params": {}})
        on_event({})
        assert events == []

    def test_session_wired_with_on_event_that_fires_tool_progress(self, monkeypatch):
        """The session is constructed with an on_event hook that, when fed an
        item/started note, calls the agent's tool_progress_callback."""
        captured_init = {}
        events = []

        def fake_init(self, **kwargs):
            captured_init.update(kwargs)
            # minimal attrs so the rest of run_turn stubs work
            self._client = None

        def fake_run_turn(self, user_input, **kwargs):
            # Exercise the wired on_event hook with a real item/started note.
            on_event = captured_init.get("on_event")
            if on_event:
                on_event({"method": "item/started", "params": {"item": {
                    "type": "commandExecution", "command": "pytest", "cwd": "/repo"}}})
            return TurnResult(final_text="done", projected_messages=[
                {"role": "assistant", "content": "done"}], turn_id="t1", thread_id="th1")

        monkeypatch.setattr(CodexAppServerSession, "__init__", fake_init)
        monkeypatch.setattr(CodexAppServerSession, "ensure_started", lambda self: "th1")
        monkeypatch.setattr(CodexAppServerSession, "run_turn", fake_run_turn)

        agent = _make_codex_agent()
        agent.tool_progress_callback = lambda kind, name, preview, args: events.append(
            (kind, name, preview))
        with patch.object(agent, "_spawn_background_review", return_value=None):
            agent.run_conversation("run the tests")

        assert "on_event" in captured_init and captured_init["on_event"] is not None
        assert ("tool.started", "exec_command", "pytest") in events
