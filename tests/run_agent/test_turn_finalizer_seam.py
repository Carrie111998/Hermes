"""Seam tests for ``agent.turn_finalizer.persist_completed_text_turn``.

Slice CL-R5-1: the final-message append + best-effort SessionDB flush/warning
block was extracted from ``run_conversation`` into the lightweight helper
``persist_completed_text_turn``. These tests pin the behavioral seam:

1. append-before-flush ordering,
2. flush-failure warning-only (best-effort, never raises),
3. unchanged message/history identity (same list objects, same contents),
4. caller-owned control flow (helper never raises / never returns a value),
5. import/patch transparency (module-level ``run_conversation`` identity and
   the ``agent.conversation_loop.run_conversation`` patch target are intact),
6. logger/traceback semantics (warning text, logger name, ``exc_info=True``).

No source-reading: this file never calls ``inspect.getsource``, ``ast.parse``,
``read_text``, ``open``, ``subprocess``, or ``git``.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from agent.turn_finalizer import persist_completed_text_turn


def _make_agent(*, session_id="sess-1", flush_side_effect=None):
    agent = MagicMock()
    agent.session_id = session_id
    if flush_side_effect is not None:
        agent._flush_messages_to_session_db.side_effect = flush_side_effect
    return agent


class TestAppendBeforeFlushOrdering:
    def test_final_msg_is_appended_before_flush_is_called(self):
        """The reply must be part of the transcript before the flush runs."""
        agent = _make_agent()
        messages = [{"role": "user", "content": "hi"}]
        history = [{"role": "user", "content": "older"}]
        final_msg = {"role": "assistant", "content": "answer"}

        seen_at_flush = {}

        def _record(messages_arg, history_arg):
            seen_at_flush["messages"] = list(messages_arg)
            seen_at_flush["history"] = history_arg

        agent._flush_messages_to_session_db.side_effect = _record

        persist_completed_text_turn(
            agent=agent,
            messages=messages,
            conversation_history=history,
            final_msg=final_msg,
        )

        assert seen_at_flush["messages"][-1] is final_msg, (
            "The flush must observe final_msg already appended as the "
            "transcript tail."
        )
        assert seen_at_flush["messages"] == [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "answer"},
        ]

    def test_flush_receives_exact_messages_and_history_arguments(self):
        """The flush call must forward the caller's containers unchanged."""
        agent = _make_agent()
        messages = [{"role": "user", "content": "q"}]
        history = [{"role": "assistant", "content": "prior"}]
        final_msg = {"role": "assistant", "content": "a"}

        persist_completed_text_turn(
            agent=agent,
            messages=messages,
            conversation_history=history,
            final_msg=final_msg,
        )

        agent._flush_messages_to_session_db.assert_called_once_with(
            messages, history
        )


class TestFlushFailureWarningOnly:
    def test_flush_failure_is_swallowed_and_warns(self, caplog):
        """A failed flush must not raise; it logs a warning and returns None."""
        agent = _make_agent(
            session_id="sess-9",
            flush_side_effect=RuntimeError("database is locked"),
        )
        messages = []
        history = []

        with caplog.at_level(logging.WARNING, logger="agent.conversation_loop"):
            result = persist_completed_text_turn(
                agent=agent,
                messages=messages,
                conversation_history=history,
                final_msg={"role": "assistant", "content": "a"},
            )

        assert result is None, "The helper must return None (no sentinel)."
        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert record.name == "agent.conversation_loop"
        assert record.levelno == logging.WARNING
        assert record.exc_info is not None, (
            "The warning must carry traceback info (exc_info=True)."
        )
        assert record.exc_info[0] is RuntimeError
        assert "final text-turn flush failed (session=sess-9)" in record.getMessage()
        assert "not yet durable; relying on finalize_turn retry" in record.getMessage()

    def test_flush_failure_still_appends_final_msg(self):
        """Even on flush failure the reply stays in the transcript."""
        agent = _make_agent(flush_side_effect=ValueError("boom"))
        messages = []
        final_msg = {"role": "assistant", "content": "kept"}

        with patch("agent.conversation_loop.logger.warning"):
            persist_completed_text_turn(
                agent=agent,
                messages=messages,
                conversation_history=[],
                final_msg=final_msg,
            )

        assert messages == [final_msg]

    def test_missing_session_id_falls_back_to_none(self, caplog):
        """getattr(agent, 'session_id', None) or 'none' must render 'none'."""
        agent = _make_agent(flush_side_effect=RuntimeError("x"))
        del agent.session_id

        with caplog.at_level(logging.WARNING, logger="agent.conversation_loop"):
            persist_completed_text_turn(
                agent=agent,
                messages=[],
                conversation_history=[],
                final_msg={"role": "assistant", "content": "a"},
            )

        assert "session=none" in caplog.records[0].getMessage()


class TestMessageHistoryIdentity:
    def test_messages_and_history_objects_are_the_callers(self):
        """The helper must mutate the caller's lists in place, not copies."""
        agent = _make_agent()
        messages = [{"role": "user", "content": "q"}]
        history = [{"role": "user", "content": "old"}]
        final_msg = {"role": "assistant", "content": "a"}

        persist_completed_text_turn(
            agent=agent,
            messages=messages,
            conversation_history=history,
            final_msg=final_msg,
        )

        assert messages == [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "a"},
        ]
        assert history == [{"role": "user", "content": "old"}]
        # The flush saw the very same list objects.
        call = agent._flush_messages_to_session_db.call_args
        assert call.args[0] is messages
        assert call.args[1] is history


class _StubAgent:
    """Real class (no MagicMock attribute auto-creation) for control-flow tests."""

    def __init__(self):
        self.session_id = "sess-1"
        self.quiet_mode = False
        self._safe_print_called = False

    def _flush_messages_to_session_db(self, messages, conversation_history):
        return True

    def _safe_print(self, *args, **kwargs):
        self._safe_print_called = True


class TestCallerOwnedControlFlow:
    def test_helper_never_raises_on_success_or_failure(self):
        """No exception may escape the helper in either path."""
        ok_agent = _make_agent()
        fail_agent = _make_agent(flush_side_effect=RuntimeError("locked"))

        for agent in (ok_agent, fail_agent):
            with patch("agent.conversation_loop.logger.warning"):
                result = persist_completed_text_turn(
                    agent=agent,
                    messages=[],
                    conversation_history=[],
                    final_msg={"role": "assistant", "content": "a"},
                )
            assert result is None

    def test_helper_does_not_touch_exit_reason_or_completion_output(self):
        """Caller-owned state (exit reason, quiet-mode print) is untouched."""
        agent = _StubAgent()

        persist_completed_text_turn(
            agent=agent,
            messages=[],
            conversation_history=[],
            final_msg={"role": "assistant", "content": "a"},
        )

        # The helper must not set _turn_exit_reason, print completion output,
        # or call _safe_print.
        assert not hasattr(agent, "_turn_exit_reason")
        assert agent._safe_print_called is False


class TestImportPatchTransparency:
    def test_module_level_run_conversation_identity_is_preserved(self):
        """The public callable must still be the module-level function."""
        import agent.conversation_loop as cl

        assert callable(cl.run_conversation)
        assert cl.run_conversation.__module__ == "agent.conversation_loop"

    def test_patch_target_agent_conversation_loop_run_conversation_resolves(self):
        """Existing monkeypatch targets must keep resolving to the callable."""
        import agent.conversation_loop as cl

        with patch("agent.conversation_loop.run_conversation") as mocked:
            assert cl.run_conversation is mocked

    def test_helper_import_is_lightweight_and_cycle_free(self):
        """Importing the helper must not import conversation_loop eagerly."""
        import agent.turn_finalizer as tf

        # Cycle safety: the helper module's top-level namespace must not hold
        # a reference to the conversation_loop module. Its only
        # conversation_loop reference is the lazy logger import inside the
        # function body.
        conversation_loop_refs = [
            name
            for name, value in vars(tf).items()
            if getattr(value, "__name__", None) == "conversation_loop"
        ]
        assert conversation_loop_refs == []


class TestLoggerTracebackSemantics:
    def test_warning_uses_conversation_loop_logger_identity(self, caplog):
        """The warning must be emitted on the agent.conversation_loop logger."""
        agent = _make_agent(flush_side_effect=RuntimeError("locked"))

        with caplog.at_level(logging.WARNING, logger="agent.conversation_loop"):
            persist_completed_text_turn(
                agent=agent,
                messages=[],
                conversation_history=[],
                final_msg={"role": "assistant", "content": "a"},
            )

        assert caplog.records[0].name == "agent.conversation_loop"

    def test_warning_text_is_byte_identical_to_original(self, caplog):
        """The warning message must not have been reworded."""
        agent = _make_agent(
            session_id=None, flush_side_effect=RuntimeError("locked")
        )

        with caplog.at_level(logging.WARNING, logger="agent.conversation_loop"):
            persist_completed_text_turn(
                agent=agent,
                messages=[],
                conversation_history=[],
                final_msg={"role": "assistant", "content": "a"},
            )

        assert caplog.records[0].getMessage() == (
            "final text-turn flush failed (session=none) — reply is "
            "not yet durable; relying on finalize_turn retry"
        )

    def test_exc_info_true_carries_active_exception(self, caplog):
        """exc_info=True must attach the live exception to the record."""
        agent = _make_agent(flush_side_effect=RuntimeError("db locked"))

        with caplog.at_level(logging.WARNING, logger="agent.conversation_loop"):
            persist_completed_text_turn(
                agent=agent,
                messages=[],
                conversation_history=[],
                final_msg={"role": "assistant", "content": "a"},
            )

        record = caplog.records[0]
        assert record.exc_info is not None
        assert record.exc_info[0] is RuntimeError
        assert record.exc_info[1].args == ("db locked",)
