"""Tests for AIAgent.steer() — mid-run user message injection.

/steer lets the user add a note to the agent's next tool result without
interrupting the current tool call. The agent sees the note inline with
tool output on its next iteration, preserving message-role alternation
and prompt-cache integrity.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from agent.prompt_builder import STEER_MARKER_OPEN, format_steer_marker
from run_agent import AIAgent


def _bare_agent() -> AIAgent:
    """Build an AIAgent without running __init__, then install the steer
    state manually — matches the existing object.__new__ stub pattern
    used elsewhere in the test suite.
    """
    agent = object.__new__(AIAgent)
    agent._pending_steer = None
    agent._pending_steer_count = 0
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

    agent._steer_run_generation = 1
    agent._steer_checkpoint_open = True
    return agent


class TestSteerAcceptance:
    def test_accepts_non_empty_text(self):
        agent = _bare_agent()
        assert agent.steer("go ahead and check the logs") is True
        assert agent._pending_steer == "go ahead and check the logs"

    def test_rejects_empty_string(self):
        agent = _bare_agent()
        assert agent.steer("") is False
        assert agent._pending_steer is None

    def test_rejects_whitespace_only(self):
        agent = _bare_agent()
        assert agent.steer("   \n\t  ") is False
        assert agent._pending_steer is None

    def test_rejects_none(self):
        agent = _bare_agent()
        assert agent.steer(None) is False  # type: ignore[arg-type]
        assert agent._pending_steer is None

    def test_strips_surrounding_whitespace(self):
        agent = _bare_agent()
        assert agent.steer("  hello world  \n") is True
        assert agent._pending_steer == "hello world"

    def test_concatenates_multiple_steers_with_newlines(self):
        agent = _bare_agent()
        agent.steer("first note")
        agent.steer("second note")
        agent.steer("third note")
        assert agent._pending_steer == "first note\nsecond note\nthird note"

    def test_rejects_ninth_pending_steer_without_losing_the_first_eight(self):
        agent = _bare_agent()
        assert all(agent.steer(f"note-{index}") for index in range(8))

        assert agent.steer("note-8") is False
        assert agent._pending_steer.splitlines() == [f"note-{index}" for index in range(8)]

    def test_rejects_oversized_item_for_lossless_queue_fallback(self):
        agent = _bare_agent()

        assert agent.steer("x" * 4_001) is False
        assert agent._pending_steer is None

    def test_rejects_after_checkpoint_is_closed(self):
        agent = _bare_agent()
        generation = agent._steer_run_generation

        assert agent._close_steer_checkpoint(generation) is None
        assert agent.steer("too late", run_generation=generation) is False
        assert agent._pending_steer is None

    def test_cached_agent_rejects_stale_generation_after_next_run_opens(self):
        agent = _bare_agent()
        generation_n = agent._steer_run_generation
        agent._close_steer_checkpoint(generation_n)

        generation_n_plus_one = agent._open_steer_checkpoint()

        assert generation_n_plus_one > generation_n
        assert agent.steer("stale result", run_generation=generation_n) is False
        assert agent._pending_steer is None
        assert agent.steer("current result", run_generation=generation_n_plus_one) is True

    def test_checkpoint_close_race_never_returns_false_receipt(self):
        """A concurrent close either owns the text or makes steer reject it."""
        for _ in range(100):
            agent = _bare_agent()
            generation = agent._steer_run_generation
            barrier = threading.Barrier(3)

            def close_checkpoint():
                barrier.wait()
                return agent._close_steer_checkpoint(generation)

            def steer_at_boundary():
                barrier.wait()
                return agent.steer("boundary update", run_generation=generation)

            with ThreadPoolExecutor(max_workers=2) as executor:
                close_future = executor.submit(close_checkpoint)
                steer_future = executor.submit(steer_at_boundary)
                barrier.wait()
                leftover = close_future.result(timeout=2)
                accepted = steer_future.result(timeout=2)

            assert (accepted, leftover) in {
                (False, None),
                (True, "boundary update"),
            }
            assert agent._pending_steer is None

    def test_run_wrapper_closes_checkpoint_and_returns_late_steer(self, monkeypatch):
        agent = _bare_agent()
        agent._steer_run_generation = 0
        agent._steer_checkpoint_open = False
        agent.session_id = None
        agent._session_db = None

        def fake_run_conversation(live_agent, *_args, **_kwargs):
            generation = live_agent._steer_run_generation
            assert live_agent.steer("accepted before close", run_generation=generation)
            return {"final_response": "done"}

        monkeypatch.setattr(
            "agent.conversation_loop.run_conversation",
            fake_run_conversation,
        )

        result = agent.run_conversation("prompt")

        assert result["pending_steer"] == "accepted before close"
        assert agent.steer("after close") is False

    def test_consumption_ack_waits_for_injection_and_successful_turn(self, monkeypatch):
        agent = _bare_agent()
        agent._steer_run_generation = 0
        agent._steer_checkpoint_open = False
        agent.session_id = None
        agent._session_db = None
        consumed = []

        def fake_run_conversation(live_agent, *_args, **_kwargs):
            generation = live_agent._steer_run_generation
            assert live_agent.steer(
                "durable correction",
                run_generation=generation,
                on_consumed=lambda: consumed.append("committed"),
            )
            assert consumed == []
            messages = [{"role": "tool", "content": "result", "tool_call_id": "1"}]
            live_agent._apply_pending_steer_to_tool_results(messages, 1)
            live_agent._mark_injected_steer_receipts_requested(
                {"messages": messages}
            )
            assert "durable correction" in messages[0]["content"]
            assert consumed == []
            return {
                "final_response": "done",
                "messages": messages,
                "completed": True,
                "receipt_terminal_success": True,
            }

        monkeypatch.setattr(
            "agent.conversation_loop.run_conversation",
            fake_run_conversation,
        )

        agent.run_conversation("prompt")

        assert consumed == ["committed"]

    def test_clean_turn_without_marker_in_final_request_returns_unconsumed(
        self, monkeypatch
    ):
        agent = _bare_agent()
        agent._steer_run_generation = 0
        agent._steer_checkpoint_open = False
        agent.session_id = None
        agent._session_db = None
        callbacks = []

        def fake_run_conversation(live_agent, *_args, **_kwargs):
            assert live_agent.steer(
                "durable correction",
                run_generation=live_agent._steer_run_generation,
                on_consumed=lambda: callbacks.append("consumed"),
                on_unconsumed=lambda: callbacks.append("unconsumed"),
                on_uncertain=lambda: callbacks.append("uncertain"),
            )
            injected_messages = [
                {"role": "tool", "content": "result", "tool_call_id": "1"}
            ]
            live_agent._apply_pending_steer_to_tool_results(injected_messages, 1)
            # Simulate middleware replacing the provider payload after injection.
            assert (
                live_agent._mark_injected_steer_receipts_requested(
                    {
                        "messages": [
                            {
                                "role": "tool",
                                "content": "result without marker",
                                "tool_call_id": "1",
                            }
                        ]
                    }
                )
                == 0
            )
            return {
                "final_response": "done",
                "messages": injected_messages,
                "completed": True,
                "receipt_terminal_success": True,
            }

        monkeypatch.setattr(
            "agent.conversation_loop.run_conversation",
            fake_run_conversation,
        )

        agent.run_conversation("prompt")

        assert callbacks == ["unconsumed"]

    def test_multiple_receipts_in_one_marker_are_all_linked_to_provider_request(
        self, monkeypatch
    ):
        agent = _bare_agent()
        agent._steer_run_generation = 0
        agent._steer_checkpoint_open = False
        agent.session_id = None
        agent._session_db = None
        callbacks = []

        def fake_run_conversation(live_agent, *_args, **_kwargs):
            generation = live_agent._steer_run_generation
            for text in ("first correction", "second correction"):
                assert live_agent.steer(
                    text,
                    run_generation=generation,
                    on_consumed=lambda text=text: callbacks.append(
                        f"consumed:{text}"
                    ),
                    on_unconsumed=lambda text=text: callbacks.append(
                        f"unconsumed:{text}"
                    ),
                    on_uncertain=lambda text=text: callbacks.append(
                        f"uncertain:{text}"
                    ),
                )
            messages = [
                {"role": "tool", "content": "result", "tool_call_id": "1"}
            ]
            live_agent._apply_pending_steer_to_tool_results(messages, 1)
            assert (
                live_agent._mark_injected_steer_receipts_requested(
                    {"messages": messages}
                )
                == 2
            )
            return {
                "final_response": "done",
                "messages": messages,
                "completed": True,
                "receipt_terminal_success": True,
            }

        monkeypatch.setattr(
            "agent.conversation_loop.run_conversation",
            fake_run_conversation,
        )

        agent.run_conversation("prompt")

        assert callbacks == [
            "consumed:first correction",
            "consumed:second correction",
        ]

    def test_duplicate_markers_link_only_one_envelope_per_provider_occurrence(
        self, monkeypatch
    ):
        agent = _bare_agent()
        agent._steer_run_generation = 0
        agent._steer_checkpoint_open = False
        agent.session_id = None
        agent._session_db = None
        callbacks = []

        def fake_run_conversation(live_agent, *_args, **_kwargs):
            generation = live_agent._steer_run_generation
            injected_messages = []
            for label in ("first", "second"):
                assert live_agent.steer(
                    "duplicate correction",
                    run_generation=generation,
                    on_consumed=lambda label=label: callbacks.append(
                        f"consumed:{label}"
                    ),
                    on_unconsumed=lambda label=label: callbacks.append(
                        f"unconsumed:{label}"
                    ),
                )
                message = {
                    "role": "tool",
                    "content": f"result-{label}",
                    "tool_call_id": label,
                }
                live_agent._apply_pending_steer_to_tool_results([message], 1)
                injected_messages.append(message)

            # Middleware retained only the first of two byte-identical markers.
            # One provider occurrence must own exactly one receipt envelope.
            assert (
                live_agent._mark_injected_steer_receipts_requested(
                    {"messages": injected_messages[:1]}
                )
                == 1
            )
            return {
                "final_response": "done",
                "messages": injected_messages,
                "completed": True,
                "receipt_terminal_success": True,
            }

        monkeypatch.setattr(
            "agent.conversation_loop.run_conversation",
            fake_run_conversation,
        )

        agent.run_conversation("prompt")

        assert callbacks == ["consumed:first", "unconsumed:second"]

    def test_consumption_ack_rejects_completed_turn_with_cleanup_errors(self, monkeypatch):
        agent = _bare_agent()
        agent._steer_run_generation = 0
        agent._steer_checkpoint_open = False
        agent.session_id = None
        agent._session_db = None
        consumed = []

        def fake_run_conversation(live_agent, *_args, **_kwargs):
            assert live_agent.steer(
                "durable correction",
                run_generation=live_agent._steer_run_generation,
                on_consumed=lambda: consumed.append("committed"),
            )
            messages = [{"role": "tool", "content": "result", "tool_call_id": "1"}]
            live_agent._apply_pending_steer_to_tool_results(messages, 1)
            live_agent._mark_injected_steer_receipts_requested(
                {"messages": messages}
            )
            return {
                "final_response": "done",
                "messages": messages,
                "completed": True,
                "receipt_terminal_success": True,
                "cleanup_errors": ["session persistence failed"],
            }

        monkeypatch.setattr(
            "agent.conversation_loop.run_conversation",
            fake_run_conversation,
        )

        agent.run_conversation("prompt")

        assert consumed == []

    def test_consumption_ack_io_error_is_not_ignored_by_turn_finalizer(
        self, monkeypatch
    ):
        from cli import SmartCliDurableDisposition

        agent = _bare_agent()
        agent._steer_run_generation = 0
        agent._steer_checkpoint_open = False
        agent.session_id = None
        agent._session_db = None

        def fake_run_conversation(live_agent, *_args, **_kwargs):
            assert live_agent.steer(
                "durable correction",
                run_generation=live_agent._steer_run_generation,
                on_consumed=lambda: SmartCliDurableDisposition.IO_ERROR,
            )
            messages = [{"role": "tool", "content": "result", "tool_call_id": "1"}]
            live_agent._apply_pending_steer_to_tool_results(messages, 1)
            live_agent._mark_injected_steer_receipts_requested(
                {"messages": messages}
            )
            return {
                "final_response": "done",
                "messages": messages,
                "completed": True,
                "receipt_terminal_success": True,
            }

        monkeypatch.setattr(
            "agent.conversation_loop.run_conversation",
            fake_run_conversation,
        )

        result = agent.run_conversation("prompt")

        assert result["cleanup_errors"] == ["steer receipt finalization failed"]

    def test_injected_receipt_reports_uncertain_on_dirty_terminal_result(self, monkeypatch):
        agent = _bare_agent()
        agent._steer_run_generation = 0
        agent._steer_checkpoint_open = False
        agent.session_id = None
        agent._session_db = None
        consumed = []
        unconsumed = []
        uncertain = []

        def fake_run_conversation(live_agent, *_args, **_kwargs):
            assert live_agent.steer(
                "durable correction",
                run_generation=live_agent._steer_run_generation,
                on_consumed=lambda: consumed.append(True),
                on_unconsumed=lambda: unconsumed.append(True),
                on_uncertain=lambda: uncertain.append(True),
            )
            messages = [{"role": "tool", "content": "result", "tool_call_id": "1"}]
            live_agent._apply_pending_steer_to_tool_results(messages, 1)
            live_agent._mark_injected_steer_receipts_requested(
                {"messages": messages}
            )
            return {
                "final_response": "partial",
                "messages": messages,
                "completed": False,
                "partial": True,
            }

        monkeypatch.setattr(
            "agent.conversation_loop.run_conversation",
            fake_run_conversation,
        )

        agent.run_conversation("prompt")

        assert consumed == []
        assert unconsumed == []
        assert uncertain == [True]

    def test_late_generation_finalizer_cannot_terminalize_next_generation_receipt(
        self, monkeypatch
    ):
        agent = _bare_agent()
        agent._steer_run_generation = 0
        agent._steer_checkpoint_open = False
        agent.session_id = None
        agent._session_db = None
        agent._execution_thread_id = None
        agent._interrupt_thread_signal_pending = False
        agent._tool_worker_threads = None
        agent._tool_worker_threads_lock = None
        agent._active_children_lock = threading.Lock()
        agent._active_children = set()
        agent.quiet_mode = True
        generation_n_injected = threading.Event()
        generation_n_plus_one_injected = threading.Event()
        release_generation_n = threading.Event()
        release_generation_n_plus_one = threading.Event()
        callbacks = []
        failures = []

        def fake_run_conversation(live_agent, user_message, *_args, **_kwargs):
            generation = live_agent._steer_run_generation
            if user_message == "generation-n":
                assert live_agent.steer(
                    "payload-n",
                    run_generation=generation,
                    on_consumed=lambda: callbacks.append("n-consumed"),
                    on_unconsumed=lambda: callbacks.append("n-unconsumed"),
                    on_uncertain=lambda: callbacks.append("n-uncertain"),
                )
                messages = [
                    {"role": "tool", "content": "result-n", "tool_call_id": "n"}
                ]
                live_agent._apply_pending_steer_to_tool_results(messages, 1)
                live_agent._mark_injected_steer_receipts_requested(
                    {"messages": messages}
                )
                generation_n_injected.set()
                assert release_generation_n.wait(timeout=5)
                return {
                    "final_response": "interrupted",
                    "messages": messages,
                    "completed": False,
                    "interrupted": True,
                }

            assert user_message == "generation-n-plus-one"
            assert live_agent.steer(
                "payload-n-plus-one",
                run_generation=generation,
                on_consumed=lambda: callbacks.append("n+1-consumed"),
                on_unconsumed=lambda: callbacks.append("n+1-unconsumed"),
                on_uncertain=lambda: callbacks.append("n+1-uncertain"),
            )
            messages = [
                {"role": "tool", "content": "result-n+1", "tool_call_id": "n+1"}
            ]
            live_agent._apply_pending_steer_to_tool_results(messages, 1)
            live_agent._mark_injected_steer_receipts_requested(
                {"messages": messages}
            )
            generation_n_plus_one_injected.set()
            assert release_generation_n_plus_one.wait(timeout=5)
            return {
                "final_response": "done",
                "messages": messages,
                "completed": True,
                "receipt_terminal_success": True,
            }

        monkeypatch.setattr(
            "agent.conversation_loop.run_conversation",
            fake_run_conversation,
        )

        def run_turn(message):
            try:
                agent.run_conversation(message)
            except BaseException as exc:  # pragma: no cover - surfaced below
                failures.append(exc)

        generation_n_thread = threading.Thread(
            target=run_turn,
            args=("generation-n",),
        )
        generation_n_thread.start()
        assert generation_n_injected.wait(timeout=5)
        agent.interrupt("replace the active turn")

        generation_n_plus_one_thread = threading.Thread(
            target=run_turn,
            args=("generation-n-plus-one",),
        )
        generation_n_plus_one_thread.start()
        assert generation_n_plus_one_injected.wait(timeout=5)

        release_generation_n.set()
        generation_n_thread.join(timeout=5)
        assert not generation_n_thread.is_alive()
        release_generation_n_plus_one.set()
        generation_n_plus_one_thread.join(timeout=5)
        assert not generation_n_plus_one_thread.is_alive()

        assert failures == []
        assert callbacks == ["n-uncertain", "n+1-consumed"]

    def test_unconsumed_callback_owns_steer_when_turn_closes_without_tool(self, monkeypatch):
        agent = _bare_agent()
        agent._steer_run_generation = 0
        agent._steer_checkpoint_open = False
        agent.session_id = None
        agent._session_db = None
        consumed = []
        unconsumed = []

        def fake_run_conversation(live_agent, *_args, **_kwargs):
            assert live_agent.steer(
                "late durable correction",
                run_generation=live_agent._steer_run_generation,
                on_consumed=lambda: consumed.append(True),
                on_unconsumed=lambda: unconsumed.append(True),
            )
            return {
                "final_response": "done",
                "messages": [],
                "completed": True,
                "receipt_terminal_success": True,
            }

        monkeypatch.setattr(
            "agent.conversation_loop.run_conversation",
            fake_run_conversation,
        )

        result = agent.run_conversation("prompt")

        assert consumed == []
        assert unconsumed == [True]
        assert "pending_steer" not in result

    def test_unconsumed_io_error_is_not_ignored_when_checkpoint_closes(
        self, monkeypatch
    ):
        from agent.agent_runtime_helpers import SmartCliDurableDisposition

        agent = _bare_agent()
        agent._steer_run_generation = 0
        agent._steer_checkpoint_open = False
        agent.session_id = None
        agent._session_db = None

        def fake_run_conversation(live_agent, *_args, **_kwargs):
            assert live_agent.steer(
                "late durable correction",
                run_generation=live_agent._steer_run_generation,
                on_unconsumed=lambda: SmartCliDurableDisposition.IO_ERROR,
            )
            return {
                "final_response": "done",
                "messages": [],
                "completed": True,
                "receipt_terminal_success": True,
            }

        monkeypatch.setattr(
            "agent.conversation_loop.run_conversation",
            fake_run_conversation,
        )

        result = agent.run_conversation("prompt")

        assert result["cleanup_errors"] == ["steer receipt finalization failed"]


class TestSteerDrain:
    def test_drain_returns_and_clears(self):
        agent = _bare_agent()
        agent.steer("hello")
        assert agent._drain_pending_steer() == "hello"
        assert agent._pending_steer is None

    def test_drain_on_empty_returns_none(self):
        agent = _bare_agent()
        assert agent._drain_pending_steer() is None


class TestActiveTurnRedirect:
    def test_rejects_when_no_turn_is_active(self):
        agent = _bare_agent()
        assert agent.redirect("change course") is False
        assert agent._pending_redirect is None

    def test_cancels_only_an_active_model_request(self):
        agent = _bare_agent()
        agent._model_request_active.set()

        assert agent.redirect("use Postgres") is True
        assert agent._pending_redirect == "use Postgres"
        assert agent._interrupt_requested is True
        assert agent._interrupt_message is None

    def test_multiple_redirects_preserve_message_boundaries(self):
        agent = _bare_agent()
        agent._model_request_active.set()

        assert agent.redirect("first correction") is True
        assert agent.redirect("second correction") is True
        assert agent._pending_redirect == (
            "first correction\n\n"
            "[Additional user correction]\n"
            "second correction"
        )

    def test_hard_interrupt_wins_over_new_redirect(self):
        agent = _bare_agent()
        agent._model_request_active.set()
        agent._interrupt_requested = True

        assert agent.redirect("too late") is False
        assert agent._pending_redirect is None

    def test_reasoning_deltas_are_display_only(self):
        """Streamed reasoning must never accumulate into replayable transcript
        state — an assistant checkpoint that inlines chain-of-thought trips
        Anthropic's output classifier and permanently bricks the session
        (deterministic empty-response storms on every replay)."""
        agent = _bare_agent()
        seen = []
        agent.reasoning_callback = seen.append

        agent._fire_reasoning_delta("visible provider thinking")

        # Displayed to the surface, but never checkpointed anywhere.
        assert seen == ["visible provider thinking"]
        assert not getattr(agent, "_current_streamed_reasoning_text", "")

    def test_response_completion_before_redirect_lock_rejects_correction(self):
        agent = _bare_agent()
        agent._model_request_active.set()
        started = threading.Event()
        outcome = {}

        def redirect():
            started.set()
            outcome["accepted"] = agent.redirect("late correction")

        with agent._pending_redirect_lock:
            worker = threading.Thread(target=redirect)
            worker.start()
            assert started.wait(timeout=1)
            # Mirrors conversation_loop clearing the request-active marker
            # under this same lock before redirect can commit its slot.
            agent._model_request_active.clear()
        worker.join(timeout=1)

        assert outcome["accepted"] is False
        assert agent._pending_redirect is None

    def test_hard_stop_wins_concurrent_redirect(self):
        agent = _bare_agent()
        agent._model_request_active.set()
        start = threading.Barrier(3)
        outcome = {}

        def redirect():
            start.wait()
            outcome["redirect"] = agent.redirect("change course")

        def hard_stop():
            start.wait()
            agent.interrupt("stop requested")

        redirect_thread = threading.Thread(target=redirect)
        stop_thread = threading.Thread(target=hard_stop)
        redirect_thread.start()
        stop_thread.start()
        start.wait()
        redirect_thread.join(timeout=1)
        stop_thread.join(timeout=1)

        assert redirect_thread.is_alive() is False
        assert stop_thread.is_alive() is False
        assert agent._interrupt_requested is True
        assert agent._interrupt_message == "stop requested"
        assert agent._pending_redirect is None

    def test_codex_app_server_hard_stop_reaches_native_session(self):
        agent = _bare_agent()
        calls = []
        agent.api_mode = "codex_app_server"
        agent._codex_session = type(
            "_CodexSession",
            (),
            {"request_interrupt": lambda self: calls.append("interrupt")},
        )()

        agent.interrupt()

        assert calls == ["interrupt"]


    def test_redirect_during_tool_execution_uses_safe_steer_boundary(self):
        agent = _bare_agent()
        agent._executing_tools = True

        assert agent.redirect("also check migrations") is True
        assert agent._pending_redirect is None
        assert agent._pending_steer == "also check migrations"
        assert agent._interrupt_requested is False

class TestActiveTurnRedirectCheckpoint:
    def test_assistant_tail_puts_correction_last(self):
        from agent.conversation_loop import _apply_active_turn_redirect

        agent = _bare_agent()
        agent._current_streamed_assistant_text = "Visible draft."
        messages = [
            {"role": "user", "content": "start"},
            {"role": "assistant", "content": "committed assistant item"},
        ]

        _apply_active_turn_redirect(agent, messages, "Use Postgres instead.")

        assert [m["role"] for m in messages] == ["user", "assistant", "user"]
        assert messages[-1]["role"] == "user"
        assert messages[-1]["content"] == "Use Postgres instead."
        assert sum(1 for m in messages if m["role"] == "assistant") == 1
        # Scaffolding is provider-replay text, carried in the sidecar so the
        # model still sees the interrupted context — never in the transcript.
        replayed = messages[-1]["api_content"]
        assert "Visible draft." in replayed
        assert "Context from the interrupted assistant response" in replayed
        assert replayed.endswith("Use Postgres instead.")

    def test_scaffolding_never_lands_in_transcript_content(self):
        """The checkpoint machinery is for the MODEL, not the transcript.

        Persisting ``[This response was interrupted by a user correction.]``
        into ``content`` painted raw scaffolding as an assistant bubble on
        every reload. It must ride in ``api_content`` (replayed to the
        provider) while ``content`` stays clean, or be marked
        ``display_kind="hidden"`` when there is no clean form at all.
        """
        from agent.conversation_loop import _apply_active_turn_redirect

        scaffolding = (
            "[This response was interrupted by a user correction.]",
            "Visible response before the interruption:",
            "[Context from the interrupted assistant response]",
        )

        for tail_role in ("tool", "assistant"):
            for streamed in ("Partial reply on screen.", ""):
                agent = _bare_agent()
                agent._current_streamed_assistant_text = streamed
                messages = [{"role": "user", "content": "start"}]
                if tail_role == "assistant":
                    messages.append({"role": "assistant", "content": "committed"})
                else:
                    messages.append(
                        {"role": "assistant", "tool_calls": [{"id": "a"}]}
                    )
                    messages.append(
                        {"role": "tool", "content": "out", "tool_call_id": "a"}
                    )

                _apply_active_turn_redirect(agent, messages, "New direction.")

                for msg in messages:
                    if msg.get("display_kind") == "hidden":
                        continue  # dropped by every transcript surface
                    content = str(msg.get("content", ""))
                    for marker in scaffolding:
                        assert marker not in content, (
                            f"scaffolding leaked into visible content "
                            f"(tail={tail_role}, streamed={bool(streamed)}): {content!r}"
                        )

                # The user's correction is always shown verbatim.
                assert messages[-1]["content"] == "New direction."
                # ...and the model still receives the interrupted context.
                replayed = "".join(
                    str(m.get("api_content") or m.get("content", "")) for m in messages
                )
                assert "[This response was interrupted by a user correction.]" in replayed
                if streamed:
                    assert streamed in replayed

    def test_checkpoint_never_replays_chain_of_thought(self):
        """Raw CoT serialized into checkpoint content reads to Anthropic's
        output classifier as reasoning-injection; because the checkpoint is
        persisted and replayed on every later call, one redirect during a
        thinking phase permanently bricked sessions with deterministic
        empty-response storms (July 2026). Reasoning must never appear in
        replayable content — in either the assistant-checkpoint or the
        merged-user-correction shape."""
        from agent.conversation_loop import _apply_active_turn_redirect

        for tail_role in ("user", "assistant"):
            agent = _bare_agent()
            # Simulate a surface having displayed reasoning this turn.
            agent._current_streamed_reasoning_text = "SECRET chain of thought."
            agent._current_streamed_assistant_text = "Visible draft."
            messages = [{"role": "user", "content": "start"}]
            if tail_role == "assistant":
                messages.append({"role": "assistant", "content": "committed"})

            _apply_active_turn_redirect(agent, messages, "Change course.")

            # Check BOTH the transcript content and the replayed sidecar —
            # the sidecar is what actually reaches the provider.
            serialized = "".join(
                str(m.get("content", "")) + str(m.get("api_content") or "")
                for m in messages
            )
            assert "SECRET chain of thought." not in serialized
            assert "Reasoning shown before the interruption" not in serialized
            assert "Visible draft." in serialized

    def test_checkpoint_omits_reasoning_label_when_nothing_visible(self):
        from agent.conversation_loop import _apply_active_turn_redirect

        agent = _bare_agent()
        agent._current_streamed_reasoning_text = "thinking only, no text yet"
        messages = [{"role": "user", "content": "start"}]

        _apply_active_turn_redirect(agent, messages, "New direction.")

        checkpoint_row = messages[-2]
        # Nothing was on screen, so the row exists only for the model: hidden
        # from every transcript surface, scaffolding replayed via the sidecar.
        assert checkpoint_row["display_kind"] == "hidden"
        assert (
            checkpoint_row["api_content"]
            == "[This response was interrupted by a user correction.]"
        )
        assert messages[-1]["content"] == "New direction."


class TestSteerInjection:
    def test_appends_to_last_tool_result(self):
        agent = _bare_agent()
        agent.steer("please also check auth.log")
        messages = [
            {"role": "user", "content": "what's in /var/log?"},
            {"role": "assistant", "tool_calls": [{"id": "a"}, {"id": "b"}]},
            {"role": "tool", "content": "ls output A", "tool_call_id": "a"},
            {"role": "tool", "content": "ls output B", "tool_call_id": "b"},
        ]
        agent._apply_pending_steer_to_tool_results(messages, num_tool_msgs=2)
        # The LAST tool result is modified; earlier ones are untouched.
        assert messages[2]["content"] == "ls output A"
        assert "ls output B" in messages[3]["content"]
        assert STEER_MARKER_OPEN in messages[3]["content"]
        assert "please also check auth.log" in messages[3]["content"]
        # And pending_steer is consumed.
        assert agent._pending_steer is None

    def test_no_op_when_no_steer_pending(self):
        agent = _bare_agent()
        messages = [
            {"role": "assistant", "tool_calls": [{"id": "a"}]},
            {"role": "tool", "content": "output", "tool_call_id": "a"},
        ]
        agent._apply_pending_steer_to_tool_results(messages, num_tool_msgs=1)
        assert messages[-1]["content"] == "output"  # unchanged

    def test_no_op_when_num_tool_msgs_zero(self):
        agent = _bare_agent()
        agent.steer("steer")
        messages = [{"role": "user", "content": "hi"}]
        agent._apply_pending_steer_to_tool_results(messages, num_tool_msgs=0)
        # Steer should remain pending (nothing to drain into)
        assert agent._pending_steer == "steer"

    def test_marker_labels_text_as_out_of_band_user_message(self):
        """The injection marker must attribute the appended text to the user
        via the explicit out-of-band marker (which the system prompt tells the
        model to trust) — otherwise the model reads it as untrusted tool output
        and refuses it as suspected prompt injection.  Cache-safe: it only
        rewrites existing tool content, never the message-role sequence.
        """
        agent = _bare_agent()
        agent.steer("stop after next step")
        messages = [{"role": "tool", "content": "x", "tool_call_id": "1"}]
        agent._apply_pending_steer_to_tool_results(messages, num_tool_msgs=1)
        content = messages[-1]["content"]
        assert STEER_MARKER_OPEN in content
        assert "stop after next step" in content

    def test_multimodal_content_list_preserved(self):
        """Anthropic-style list content should be preserved, with the steer
        appended as a text block."""
        agent = _bare_agent()
        agent.steer("extra note")
        original_blocks = [{"type": "text", "text": "existing output"}]
        messages = [
            {"role": "tool", "content": list(original_blocks), "tool_call_id": "1"}
        ]
        agent._apply_pending_steer_to_tool_results(messages, num_tool_msgs=1)
        new_content = messages[-1]["content"]
        assert isinstance(new_content, list)
        assert len(new_content) == 2
        assert new_content[0] == {"type": "text", "text": "existing output"}
        assert new_content[1]["type"] == "text"
        assert "extra note" in new_content[1]["text"]

    def test_restashed_when_no_tool_result_in_batch(self):
        """If the 'batch' contains no tool-role messages (e.g. all skipped
        after an interrupt), the steer should be put back into the pending
        slot so the caller's fallback path can deliver it."""
        agent = _bare_agent()
        agent.steer("ping")
        messages = [
            {"role": "user", "content": "x"},
            {"role": "assistant", "content": "y"},
        ]
        # Claim there were N tool msgs, but the tail has none — simulates
        # the interrupt-cancelled case.
        agent._apply_pending_steer_to_tool_results(messages, num_tool_msgs=2)
        # Messages untouched
        assert messages[-1]["content"] == "y"
        # And the steer is back in pending so the fallback can grab it
        assert agent._pending_steer == "ping"

    def test_interrupt_during_no_tool_restore_keeps_payload_and_receipt_atomic(self):
        agent = _bare_agent()
        agent._execution_thread_id = None
        agent._interrupt_thread_signal_pending = False
        agent._tool_worker_threads = None
        agent._tool_worker_threads_lock = None
        agent._active_children_lock = threading.Lock()
        agent._active_children = set()
        agent.quiet_mode = True
        callbacks = []

        assert agent.steer(
            "durable correction",
            on_consumed=lambda: callbacks.append("consumed"),
            on_unconsumed=lambda: callbacks.append("unconsumed"),
            on_uncertain=lambda: callbacks.append("uncertain"),
        ) is True

        class InterruptOnSplitVector:
            """Interrupt exactly when text exists without its drained receipt."""

            def __init__(self):
                self._lock = threading.RLock()
                self.fired = False

            def __enter__(self):
                self._lock.acquire()
                return self

            def __exit__(self, exc_type, exc, tb):
                self._lock.release()
                split_vector = (
                    bool(getattr(agent, "_pending_steer", None))
                    and not getattr(agent, "_pending_steer_receipts", [])
                    and bool(getattr(agent, "_steer_drained_receipts", []))
                )
                if split_vector and not self.fired:
                    self.fired = True
                    agent.interrupt("replace the active turn")
                return False

        split_lock = InterruptOnSplitVector()
        agent._pending_steer_lock = split_lock
        messages = [
            {"role": "user", "content": "original"},
            {"role": "assistant", "content": "tool batch was skipped"},
        ]

        agent._apply_pending_steer_to_tool_results(messages, num_tool_msgs=2)
        if not split_lock.fired:
            # A correct implementation never exposes the split vector. Exercise
            # interrupt against the still-indivisible pending envelope instead.
            agent.interrupt("replace the active turn")

        assert "durable correction" not in str(messages)
        assert callbacks == ["unconsumed"]
        assert not getattr(agent, "_injected_steer_receipts", [])

    def test_interrupt_does_not_ignore_typed_unconsumed_failure(self):
        from agent.agent_runtime_helpers import SmartCliDurableDisposition

        agent = _bare_agent()
        agent._execution_thread_id = None
        agent._interrupt_thread_signal_pending = False
        agent._tool_worker_threads = None
        agent._tool_worker_threads_lock = None
        agent._active_children_lock = threading.Lock()
        agent._active_children = set()
        agent._steer_receipt_callback_failed = False
        agent.quiet_mode = True

        assert agent.steer(
            "durable correction",
            on_unconsumed=lambda: SmartCliDurableDisposition.IO_ERROR,
        ) is True

        agent.interrupt("replace the active turn")

        assert agent._steer_receipt_callback_failed is True

    def test_interrupt_cannot_observe_receipt_detached_before_tool_injection(
        self, monkeypatch
    ):
        agent = _bare_agent()
        agent._steer_run_generation = 0
        agent._steer_checkpoint_open = False
        agent.session_id = None
        agent._session_db = None
        agent._execution_thread_id = None
        agent._interrupt_thread_signal_pending = False
        agent._tool_worker_threads = None
        agent._tool_worker_threads_lock = None
        agent._active_children_lock = threading.Lock()
        agent._active_children = set()
        agent.quiet_mode = True
        callbacks = []
        detached_observed = []

        def fake_run_conversation(live_agent, *_args, **_kwargs):
            assert live_agent.steer(
                "durable correction",
                run_generation=live_agent._steer_run_generation,
                on_consumed=lambda: callbacks.append("consumed"),
                on_unconsumed=lambda: callbacks.append("unconsumed"),
                on_uncertain=lambda: callbacks.append("uncertain"),
            )
            messages = [
                {"role": "tool", "content": "result", "tool_call_id": "1"}
            ]

            class InterruptOnDetachedEnvelope:
                def __init__(self):
                    self._lock = threading.RLock()
                    self.fired = False

                def __enter__(self):
                    self._lock.acquire()
                    return self

                def __exit__(self, exc_type, exc, tb):
                    self._lock.release()
                    injected = getattr(
                        live_agent,
                        "_injected_steer_receipts_by_generation",
                        {},
                    )
                    detached = (
                        not getattr(live_agent, "_pending_steer", None)
                        and bool(getattr(live_agent, "_steer_drained_receipts", []))
                        and not any(injected.values())
                        and "durable correction" not in str(messages)
                    )
                    if detached and not self.fired:
                        self.fired = True
                        detached_observed.append(True)
                        live_agent.interrupt("replace the active turn")
                    return False

            split_lock = InterruptOnDetachedEnvelope()
            live_agent._pending_steer_lock = split_lock
            live_agent._apply_pending_steer_to_tool_results(messages, 1)
            live_agent._mark_injected_steer_receipts_requested(
                {"messages": messages}
            )
            if not split_lock.fired:
                live_agent.interrupt("replace the active turn")
            assert "durable correction" in messages[0]["content"]
            return {
                "final_response": "interrupted",
                "messages": messages,
                "completed": False,
                "interrupted": True,
            }

        monkeypatch.setattr(
            "agent.conversation_loop.run_conversation",
            fake_run_conversation,
        )

        agent.run_conversation("prompt")

        assert detached_observed == []
        assert callbacks == ["uncertain"]


class TestSteerThreadSafety:
    def test_concurrent_steer_calls_admit_exactly_the_bounded_capacity(self):
        agent = _bare_agent()
        N = 200

        accepted: list[tuple[int, bool]] = []
        accepted_lock = threading.Lock()

        def worker(idx: int) -> None:
            result = agent.steer(f"note-{idx}")
            with accepted_lock:
                accepted.append((idx, result))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        text = agent._drain_pending_steer()
        assert text is not None
        # The mailbox is bounded atomically; rejected callers can safely queue.
        lines = text.split("\n")
        accepted_indexes = {idx for idx, result in accepted if result}
        assert len(lines) == 8
        assert len(accepted_indexes) == 8
        assert set(lines) == {f"note-{idx}" for idx in accepted_indexes}


class TestSteerClearedOnInterrupt:
    def test_clear_interrupt_drops_pending_steer(self):
        """An explicit user cancellation owns the terminal disposition."""
        agent = _bare_agent()
        # Minimal surface needed by clear_interrupt()
        agent._interrupt_requested = True
        agent._interrupt_message = None
        agent._interrupt_thread_signal_pending = False
        agent._execution_thread_id = None
        agent._tool_worker_threads = None
        agent._tool_worker_threads_lock = None

        agent.steer("will be dropped")
        assert agent._pending_steer == "will be dropped"

        agent.clear_interrupt()
        assert agent._pending_steer is None

    def test_system_timeout_transfers_accepted_steer_to_recovery_mailbox(self):
        agent = _bare_agent()
        agent._execution_thread_id = None
        agent._interrupt_thread_signal_pending = False
        agent._tool_worker_threads = None
        agent._tool_worker_threads_lock = None
        agent._active_children_lock = threading.Lock()
        agent._active_children = set()
        agent.quiet_mode = True

        assert agent.steer("preserve after timeout") is True

        agent.interrupt("Execution timed out (inactivity)")

        assert agent._pending_steer is None
        assert agent.get_steer_generation() is None
        assert agent.take_failed_turn_pending_steer() == "preserve after timeout"
        assert agent.take_failed_turn_pending_steer() is None

    def test_interrupt_resets_mailbox_receipts_before_the_next_generation(self):
        agent = _bare_agent()
        agent._execution_thread_id = None
        agent._interrupt_thread_signal_pending = False
        agent._tool_worker_threads = None
        agent._tool_worker_threads_lock = None
        agent._active_children_lock = threading.Lock()
        agent._active_children = set()
        agent.quiet_mode = True
        returned = []

        assert agent.steer(
            "generation-n payload",
            on_consumed=lambda: returned.append("wrongly consumed"),
            on_unconsumed=lambda: returned.append("returned"),
        ) is True

        agent.interrupt("replace the active turn")

        assert returned == ["returned"]
        assert agent._pending_steer is None
        assert agent._pending_steer_count == 0
        assert agent._pending_steer_receipts == []

        next_generation = agent._open_steer_checkpoint()
        assert agent.steer(
            "generation-n-plus-one payload",
            run_generation=next_generation,
        ) is True
        messages = [{"role": "tool", "content": "result", "tool_call_id": "1"}]
        agent._apply_pending_steer_to_tool_results(messages, num_tool_msgs=1)

        assert "generation-n payload" not in messages[0]["content"]
        assert "generation-n-plus-one payload" in messages[0]["content"]
        assert getattr(agent, "_injected_steer_receipts", []) == [
            ("generation-n-plus-one payload", None, None, None)
        ]


class TestPreApiCallSteerDrain:
    """Test that steers arriving during an API call are drained before the
    next API call — not deferred until the next tool batch.  This is the
    fix for the scenario where /steer sent during model thinking only lands
    after the agent is completely done."""

    def test_pre_api_drain_injects_into_last_tool_result(self):
        """If a steer is pending when the main loop starts building
        api_messages, it should be injected into the last tool result
        in the messages list."""
        agent = _bare_agent()
        # Simulate messages after a tool batch completed
        messages = [
            {"role": "user", "content": "do something"},
            {"role": "assistant", "content": "ok", "tool_calls": [
                {"id": "tc1", "function": {"name": "terminal", "arguments": "{}"}}
            ]},
            {"role": "tool", "content": "output here", "tool_call_id": "tc1"},
        ]
        # Steer arrives during API call (set after tool execution)
        agent.steer("focus on error handling")
        # Simulate what the pre-API-call drain does:
        _pre_api_steer = agent._drain_pending_steer()
        assert _pre_api_steer == "focus on error handling"
        # Inject into last tool msg (mirrors the new code in run_conversation)
        for _si in range(len(messages) - 1, -1, -1):
            if messages[_si].get("role") == "tool":
                messages[_si]["content"] += format_steer_marker(_pre_api_steer)
                break
        assert STEER_MARKER_OPEN in messages[-1]["content"]
        assert "focus on error handling" in messages[-1]["content"]
        assert agent._pending_steer is None

    def test_pre_api_drain_restashes_when_no_tool_message(self):
        """If there are no tool results yet (first iteration), the steer
        should be put back into _pending_steer for the post-tool drain."""
        agent = _bare_agent()
        messages = [
            {"role": "user", "content": "hello"},
        ]
        agent.steer("early steer")
        _pre_api_steer = agent._drain_pending_steer()
        assert _pre_api_steer == "early steer"
        # No tool message found — put it back
        found = False
        for _si in range(len(messages) - 1, -1, -1):
            if messages[_si].get("role") == "tool":
                found = True
                break
        assert not found
        # Restash
        agent._pending_steer = _pre_api_steer
        assert agent._pending_steer == "early steer"

    def test_pre_api_drain_finds_tool_msg_past_assistant(self):
        """The pre-API drain should scan backwards past a non-tool message
        (e.g., if an assistant message was somehow appended after tools)
        and still find the tool result."""
        agent = _bare_agent()
        messages = [
            {"role": "user", "content": "do something"},
            {"role": "assistant", "content": "let me check", "tool_calls": [
                {"id": "tc1", "function": {"name": "web_search", "arguments": "{}"}}
            ]},
            {"role": "tool", "content": "search results", "tool_call_id": "tc1"},
        ]
        agent.steer("change approach")
        _pre_api_steer = agent._drain_pending_steer()
        assert _pre_api_steer is not None
        for _si in range(len(messages) - 1, -1, -1):
            if messages[_si].get("role") == "tool":
                messages[_si]["content"] += format_steer_marker(_pre_api_steer)
                break
        assert "change approach" in messages[2]["content"]


class TestSteerMarkerContract:
    def test_system_prompt_note_describes_the_real_marker(self):
        """The system-prompt note tells the model which marker to trust; it
        must reference the exact open/close the injector emits, or the model
        trusts a marker that never appears (and vice-versa)."""
        from agent.prompt_builder import STEER_CHANNEL_NOTE, STEER_MARKER_CLOSE

        emitted = format_steer_marker("hi")
        assert STEER_MARKER_OPEN in emitted and STEER_MARKER_CLOSE in emitted
        assert STEER_MARKER_OPEN in STEER_CHANNEL_NOTE and STEER_MARKER_CLOSE in STEER_CHANNEL_NOTE

    def test_marker_no_longer_uses_the_distrusted_label(self):
        """Regression: the bare 'User guidance:' line read as tool content and
        got refused as injection — it must not come back."""
        assert "User guidance:" not in format_steer_marker("hi")


class TestSteerCommandRegistry:
    def test_steer_in_command_registry(self):
        """The /steer slash command must be registered so it reaches all
        platforms (CLI, gateway, TUI autocomplete, Telegram/Slack menus).
        """
        from hermes_cli.commands import resolve_command

        cmd = resolve_command("steer")
        assert cmd is not None
        assert cmd.name == "steer"
        assert cmd.category == "Session"
        assert cmd.args_hint == "<prompt>"

    def test_steer_in_bypass_set(self):
        """When the agent is running, /steer MUST bypass the Level-1
        base-adapter queue so it reaches the gateway runner's /steer
        handler. Otherwise it would be queued as user text and only
        delivered at turn end — defeating the whole point.
        """
        from hermes_cli.commands import ACTIVE_SESSION_BYPASS_COMMANDS, should_bypass_active_session

        assert "steer" in ACTIVE_SESSION_BYPASS_COMMANDS
        assert should_bypass_active_session("steer") is True

    def test_callback_failure_log_redacts_private_exception_details(self, caplog):
        caplog.set_level("DEBUG", logger="run_agent")
        agent = _bare_agent()
        generation = agent._steer_run_generation
        private_error = "private customer path /accounts/acme/secret.json"

        def fail_unconsumed_callback():
            raise RuntimeError(private_error)

        assert agent.steer(
            "durable correction",
            run_generation=generation,
            on_unconsumed=fail_unconsumed_callback,
        ) is True

        assert agent._close_steer_checkpoint(generation) is None
        assert private_error not in caplog.text


def test_steer_delivery_log_contains_metadata_not_payload(caplog):
    caplog.set_level("INFO", logger="agent.agent_runtime_helpers")
    agent = _bare_agent()
    private_payload = "private steer /customers/acme/secret.json session-123"
    assert agent.steer(private_payload) is True
    messages = [{"role": "tool", "content": "result", "tool_call_id": "1"}]

    agent._apply_pending_steer_to_tool_results(messages, num_tool_msgs=1)

    assert private_payload in messages[0]["content"]
    assert private_payload not in caplog.text
    assert "steer_chars=" in caplog.text


def test_interrupt_diagnostics_hide_payload_exception_text_and_traceback(caplog, capsys):
    caplog.set_level("DEBUG", logger="run_agent")
    agent = _bare_agent()
    agent._execution_thread_id = None
    agent._interrupt_thread_signal_pending = False
    agent._tool_worker_threads = None
    agent._tool_worker_threads_lock = None
    agent._active_children_lock = threading.Lock()
    agent.quiet_mode = False
    private_message = "private interrupt /customers/acme/secret.json session-123"
    private_abort_error = "abort leaked /srv/private/customer.sock"
    private_child_error = "child leaked /srv/private/child.sock"

    class PrivateAbortFailure(RuntimeError):
        pass

    class PrivateChildFailure(RuntimeError):
        pass

    def fail_abort(_reason):
        raise PrivateAbortFailure(private_abort_error)

    class Child:
        def interrupt(self, _message):
            raise PrivateChildFailure(private_child_error)

    agent._active_request_abort = fail_abort
    agent._active_children = [Child()]

    agent.interrupt(private_message)

    output = capsys.readouterr().out
    diagnostic = caplog.text + output
    assert agent._interrupt_message == private_message
    assert private_message not in diagnostic
    assert private_abort_error not in diagnostic
    assert private_child_error not in diagnostic
    assert "Traceback" not in diagnostic
    assert "PrivateAbortFailure" in caplog.text
    assert "PrivateChildFailure" in caplog.text


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])