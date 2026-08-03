"""Tests for agent.title_generator — auto-generated session titles."""

from unittest.mock import MagicMock, patch


from agent.title_generator import (
    generate_title,
    auto_title_session,
    maybe_auto_title,
    _title_language,
)
from agent.context_compressor import SUMMARY_PREFIX
from agent.message_content import (
    EXACT_INTERNAL_USER_REQUESTS,
    MAX_ITERATIONS_SUMMARY_REQUEST,
    build_tool_call_stream_continuation_request,
)
from hermes_state import SessionDB
from tools.todo_tool import TODO_INJECTION_HEADER


class TestGenerateTitle:
    """Unit tests for generate_title()."""




    def test_title_language_reads_config(self):
        cfg = {"auxiliary": {"title_generation": {"language": "  French "}}}

        with patch("hermes_cli.config.load_config", return_value=cfg), patch("hermes_cli.config.load_config_readonly", return_value=cfg):
            assert _title_language() == "French"
        with patch("hermes_cli.config.load_config", return_value={}), patch("hermes_cli.config.load_config_readonly", return_value={}):
            assert _title_language() == ""
        with patch("hermes_cli.config.load_config", side_effect=RuntimeError("bad config")), \
         patch("hermes_cli.config.load_config_readonly", side_effect=RuntimeError("bad config")):
            assert _title_language() == ""

    def test_default_timeout_delegates_to_auxiliary_config(self):
        captured_kwargs = {}

        def mock_call_llm(**kwargs):
            captured_kwargs.update(kwargs)
            resp = MagicMock()
            resp.choices = [MagicMock()]
            resp.choices[0].message.content = "Configured Timeout"
            return resp

        with patch("agent.title_generator.call_llm", side_effect=mock_call_llm):
            assert generate_title("question", "answer") == "Configured Timeout"

        assert captured_kwargs["task"] == "title_generation"
        assert captured_kwargs["timeout"] is None



    def test_strips_think_blocks(self):
        """Reasoning-model output wrapped in <think>...</think> must not
        leak into the session title."""
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = (
            "<think>The user wants a title. I'll summarize the topic "
            "concisely.</think>Debugging Python Import Errors"
        )

        with patch("agent.title_generator.call_llm", return_value=mock_response):
            title = generate_title("help me fix this import", "Sure...")
            assert title == "Debugging Python Import Errors"
            assert "<think>" not in title
            assert "summarize" not in title

    def test_strips_unterminated_think_block(self):
        """An unterminated <think> block (no close tag) must still be
        stripped so the leaked reasoning doesn't become the title."""
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = (
            "<think>Let me reason about a good title for this session"
        )

        with patch("agent.title_generator.call_llm", return_value=mock_response):
            title = generate_title("hello", "hi there")
            # Everything from the unterminated open tag onward is stripped,
            # leaving nothing → None.
            assert title is None


    def test_truncates_long_titles(self):
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "A" * 100

        with patch("agent.title_generator.call_llm", return_value=mock_response):
            title = generate_title("question", "answer")
            assert len(title) == 80
            assert title.endswith("...")



    def test_invokes_failure_callback_on_exception(self):
        """failure_callback must fire so the user sees a warning (issue #15775)."""
        captured = []

        def _cb(task, exc):
            captured.append((task, exc))

        exc = RuntimeError("openrouter 402: credits exhausted")
        with patch("agent.title_generator.call_llm", side_effect=exc):
            result = generate_title("question", "answer", failure_callback=_cb)

        assert result is None
        assert len(captured) == 1
        assert captured[0][0] == "title generation"
        assert captured[0][1] is exc











class TestAutoTitleSession:
    """Tests for auto_title_session() — the sync worker function."""




    def test_does_not_overwrite_title_set_immediately_before_conditional_write(
        self, tmp_path
    ):
        db = SessionDB(tmp_path / "state.db")
        db.create_session(session_id="sess-1", source="cli")
        seen = []

        def generate_after_manual_title(*_args, **_kwargs):
            db.set_session_title("sess-1", "Manual Title")
            return "Auto Title"

        with patch(
            "agent.title_generator.generate_title",
            side_effect=generate_after_manual_title,
        ):
            auto_title_session(
                db,
                "sess-1",
                "hi",
                "hello",
                title_callback=seen.append,
            )

        assert db.get_session_title("sess-1") == "Manual Title"
        assert seen == []

    def test_invokes_title_callback_after_setting_title(self):
        db = MagicMock()
        db.get_session_title.return_value = None
        db.set_auto_title_if_empty.return_value = True
        seen = []
        with patch("agent.title_generator.generate_title", return_value="Readable Session"):
            auto_title_session(
                db,
                "sess-1",
                "hello",
                "hi there",
                title_callback=seen.append,
            )
        db.set_auto_title_if_empty.assert_called_once_with("sess-1", "Readable Session")
        assert seen == ["Readable Session"]



    def test_body_exception_routed_to_failure_callback(self):
        db = MagicMock()
        db.get_session_title.return_value = None
        seen = []

        boom = ImportError("stale module")
        with patch("agent.title_generator._auto_title_session", side_effect=boom):
            auto_title_session(
                db,
                "sess-1",
                "hi",
                "hello",
                failure_callback=lambda task, exc: seen.append((task, exc)),
            )
        assert seen == [("title generation", boom)]



class TestRealUserMessageClassification:
    """Shared classifier coverage for the title gate and compression."""

    def test_compaction_metadata_and_projected_content_are_internal(self):
        from collections import UserDict

        from agent.context_compressor import (
            COMPRESSED_SUMMARY_METADATA_KEY,
            LEGACY_SUMMARY_PREFIX,
            SUMMARY_PREFIX,
            _HISTORICAL_SUMMARY_PREFIXES,
        )
        from agent.conversation_compression import is_real_user_message

        marked = {
            "role": "user",
            "content": "summary body",
            COMPRESSED_SUMMARY_METADATA_KEY: True,
        }
        marked_mapping = UserDict(marked)
        projected = {
            "role": "user",
            "content": f"{SUMMARY_PREFIX}\nsummary body",
        }
        legacy = {
            "role": "user",
            "content": f"{LEGACY_SUMMARY_PREFIX}\nsummary body",
        }
        historical = {
            "role": "user",
            "content": f"{_HISTORICAL_SUMMARY_PREFIXES[0]}\nsummary body",
        }
        structured = {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": f"{SUMMARY_PREFIX}\nsummary body",
                }
            ],
        }

        for message in (
            marked,
            marked_mapping,
            projected,
            legacy,
            historical,
            structured,
        ):
            assert not is_real_user_message(message)

    def test_todo_and_compression_continuation_rows_are_internal(self):
        from collections import UserDict

        from agent.context_compressor import (
            COMPRESSION_CONTINUATION_USER_CONTENT,
            _LEGACY_COMPRESSION_CONTINUATION_USER_CONTENT,
        )
        from agent.conversation_compression import is_real_user_message
        from tools.todo_tool import TODO_INJECTION_HEADER

        messages = [
            {"role": "user", "content": TODO_INJECTION_HEADER},
            {
                "role": "user",
                "content": f"{TODO_INJECTION_HEADER}\n- [>] Continue",
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"{TODO_INJECTION_HEADER}\n- [>] Continue",
                    }
                ],
            },
            {
                "role": "user",
                "content": COMPRESSION_CONTINUATION_USER_CONTENT,
            },
            {
                "role": "user",
                "content": _LEGACY_COMPRESSION_CONTINUATION_USER_CONTENT,
            },
            UserDict(
                {
                    "role": "user",
                    "content": f"{TODO_INJECTION_HEADER}\n- [>] Continue",
                }
            ),
        ]

        assert all(not is_real_user_message(message) for message in messages)

    def test_exact_runtime_requests_are_internal(self):
        from agent.conversation_compression import is_real_user_message

        for request in EXACT_INTERNAL_USER_REQUESTS:
            assert not is_real_user_message({"role": "user", "content": request})
            assert not is_real_user_message(
                {
                    "role": "user",
                    "content": [{"type": "text", "text": request}],
                }
            )

    def test_dynamic_tool_continuation_is_matched_as_a_complete_wire_message(self):
        from agent.conversation_compression import is_real_user_message

        request = build_tool_call_stream_continuation_request(
            ["write_file", "patch"]
        )

        assert not is_real_user_message({"role": "user", "content": request})
        assert is_real_user_message(
            {"role": "user", "content": f"Please explain this message:\n{request}"}
        )
        assert is_real_user_message(
            {"role": "user", "content": f"{request}\nWhy did Hermes send it?"}
        )

    def test_existing_synthetic_flags_are_internal(self):
        from agent.conversation_compression import (
            _SYNTHETIC_USER_FLAGS,
            is_real_user_message,
        )

        for flag in _SYNTHETIC_USER_FLAGS:
            message = {"role": "user", "content": "runtime row", flag: True}
            assert not is_real_user_message(message), flag
        assert "_kanban_stop_synthetic" in _SYNTHETIC_USER_FLAGS

    def test_runtime_requests_inside_ordinary_user_content_remain_real(self):
        from agent.context_compressor import SUMMARY_PREFIX
        from agent.conversation_compression import is_real_user_message
        from tools.todo_tool import TODO_INJECTION_HEADER

        contents = [
            *(f'Why did Hermes say: "{request}"?' for request in EXACT_INTERNAL_USER_REQUESTS),
            f"Please explain this marker: {TODO_INJECTION_HEADER}",
            f"Please analyze this header:\n{SUMMARY_PREFIX}",
        ]

        assert all(
            is_real_user_message({"role": "user", "content": content})
            for content in contents
        )

    def test_media_and_other_structured_user_inputs_are_real(self):
        from agent.conversation_compression import is_real_user_message

        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://example.test/cat.png"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "input_audio", "input_audio": {"data": "AA=="}}
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "document", "document": {"name": "notes.pdf"}}
                ],
            },
        ]

        assert all(is_real_user_message(message) for message in messages)

    def test_media_with_merged_todo_text_remains_real(self):
        from agent.conversation_compression import is_real_user_message

        for media_part in (
            {
                "type": "image_url",
                "image_url": {"url": "https://example.test/cat.png"},
            },
            {"type": "input_audio", "input_audio": {"data": "AA=="}},
        ):
            message = {
                "role": "user",
                "content": [
                    media_part,
                    {
                        "type": "text",
                        "text": f"{TODO_INJECTION_HEADER}\n- [>] Finish the task",
                    },
                ],
            }
            assert is_real_user_message(message)

    def test_display_bookkeeping_rows_are_internal(self):
        from agent.conversation_compression import is_real_user_message

        for display_kind in (
            "model_switch",
            "auto_continue",
            "async_delegation_complete",
            "hidden",
        ):
            assert not is_real_user_message(
                {
                    "role": "user",
                    "content": "timeline bookkeeping",
                    "display_kind": display_kind,
                }
            )
        assert is_real_user_message(
            {"role": "user", "content": "timeline bookkeeping", "display_kind": ""}
        )

    def test_async_delegation_wire_messages_are_internal(self):
        from agent.conversation_compression import is_real_user_message
        from tools.process_registry import format_process_notification

        single = format_process_notification(
            {
                "type": "async_delegation",
                "delegation_id": "deleg-1",
                "goal": "Inspect the title gate",
                "status": "completed",
                "summary": "The gate counted synthetic rows.",
            }
        )
        batch = format_process_notification(
            {
                "type": "async_delegation",
                "delegation_id": "deleg-batch",
                "is_batch": True,
                "goals": ["Inspect compression"],
                "results": [
                    {
                        "task_index": 0,
                        "status": "completed",
                        "summary": "Compression audited.",
                    }
                ],
            }
        )

        assert isinstance(single, str)
        assert isinstance(batch, str)
        assert not is_real_user_message({"role": "user", "content": single})
        assert not is_real_user_message({"role": "user", "content": batch})

    def test_background_process_wire_messages_are_internal(self):
        from agent.conversation_compression import is_real_user_message
        from tools.process_registry import format_process_notification

        multiline_command = "python - <<'PY'\nprint('ok')\nPY"
        notifications = [
            format_process_notification(
                {
                    "type": "completion",
                    "session_id": "proc-1",
                    "command": "tests",
                    "exit_code": 0,
                    "output": "passed",
                }
            ),
            format_process_notification(
                {
                    "type": "watch_match",
                    "session_id": "proc-2",
                    "command": "tests",
                    "pattern": "FAILED",
                    "output": "FAILED test_title",
                }
            ),
            format_process_notification(
                {
                    "type": "completion",
                    "session_id": "proc-3",
                    "command": multiline_command,
                    "exit_code": 0,
                    "output": "ok",
                }
            ),
            format_process_notification(
                {
                    "type": "watch_match",
                    "session_id": "proc-4",
                    "command": multiline_command,
                    "pattern": "ok",
                    "output": "ok",
                }
            ),
        ]

        assert all(isinstance(item, str) for item in notifications)
        assert all(
            not is_real_user_message({"role": "user", "content": item})
            for item in notifications
        )
        watch_discussion = f"{notifications[-1][:-1]}\nWhy did Hermes send this?"
        assert is_real_user_message(
            {"role": "user", "content": watch_discussion}
        )

    def test_blank_malformed_and_non_user_rows_are_not_genuine(self):
        from types import SimpleNamespace

        from agent.conversation_compression import is_real_user_message

        assert not is_real_user_message(None)
        assert not is_real_user_message({"role": "assistant", "content": "hello"})
        assert not is_real_user_message({"role": "user", "content": "  "})
        assert not is_real_user_message({"role": "user", "content": []})
        assert not is_real_user_message({"role": "user", "content": {}})
        assert not is_real_user_message(
            {"role": "user", "content": {"type": "text", "text": ""}}
        )
        assert not is_real_user_message(
            {
                "role": "user",
                "content": SimpleNamespace(type="output_text", text=""),
            }
        )


class TestMaybeAutoTitle:
    """Tests for maybe_auto_title() — the fire-and-forget entry point."""

    def test_fires_when_first_exchange_contains_internal_user_scaffolding(self):
        db = MagicMock()
        history = [
            {"role": "user", "content": "Investigate the Telegram title bug"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "terminal", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": "done"},
            {"role": "user", "content": f"{SUMMARY_PREFIX}\nCompacted work so far"},
            {
                "role": "user",
                "content": f"{TODO_INJECTION_HEADER}\n- Finish the regression test",
            },
            {"role": "assistant", "content": "The issue is fixed."},
        ]

        with patch("agent.title_generator.threading.Thread") as thread_cls:
            maybe_auto_title(
                db,
                "sess-1",
                "Investigate the Telegram title bug",
                "The issue is fixed.",
                history,
            )

        thread_cls.assert_called_once()
        thread_cls.return_value.start.assert_called_once_with()

    def test_skips_if_not_first_exchange(self):
        """Should not fire for conversations with more than 2 user messages."""
        db = MagicMock()
        history = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "response 1"},
            {"role": "user", "content": "second"},
            {"role": "assistant", "content": "response 2"},
            {"role": "user", "content": "third"},
            {"role": "assistant", "content": "response 3"},
        ]

        with patch("agent.title_generator.threading.Thread") as thread_cls:
            maybe_auto_title(db, "sess-1", "third", "response 3", history)

        thread_cls.assert_not_called()

    def test_fires_on_first_exchange(self):
        """Should fire a background thread for the first exchange."""
        db = MagicMock()
        db.get_session_title.return_value = None
        history = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]

        with patch("agent.title_generator.auto_title_session") as mock_auto:
            import threading
            called = threading.Event()
            mock_auto.side_effect = lambda *a, **k: called.set()
            maybe_auto_title(db, "sess-1", "hello", "hi there", history)
            # Event-based wait: sleep-sync flaked when the daemon thread
            # wasn't scheduled within the fixed nap on a loaded runner.
            assert called.wait(timeout=10), "auto_title thread never ran"
            mock_auto.assert_called_once_with(
                db,
                "sess-1",
                "hello",
                "hi there",
                failure_callback=None,
                main_runtime=None,
                title_callback=None,
                runtime_validator=None,
            )

    def test_fires_with_max_iteration_scaffolding(self):
        db = MagicMock()
        history = [
            {"role": "user", "content": "Investigate the Telegram rename bug"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call-1"}],
            },
            {
                "role": "user",
                "content": f"{SUMMARY_PREFIX}\nEarlier turns were compacted.",
            },
            {"role": "assistant", "content": "Continuing the investigation."},
            {
                "role": "user",
                "content": f"{TODO_INJECTION_HEADER}\n- [>] Find the root cause",
            },
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call-2"}],
            },
            {"role": "user", "content": MAX_ITERATIONS_SUMMARY_REQUEST},
            {"role": "assistant", "content": "The title gate counted scaffolding."},
        ]

        with patch("agent.title_generator.auto_title_session") as mock_auto:
            import threading

            called = threading.Event()
            mock_auto.side_effect = lambda *args, **kwargs: called.set()
            maybe_auto_title(
                db,
                "sess-1",
                "Investigate the Telegram rename bug",
                "The title gate counted scaffolding.",
                history,
            )
            assert called.wait(timeout=10), "auto_title thread never ran"

        mock_auto.assert_called_once()

    def test_repeated_continuation_nudges_do_not_consume_the_allowance(self):
        db = MagicMock()
        continuation_requests = sorted(
            EXACT_INTERNAL_USER_REQUESTS - {MAX_ITERATIONS_SUMMARY_REQUEST}
        )
        history = [{"role": "user", "content": "Fix the title gate"}]
        for index, request in enumerate(continuation_requests):
            history.extend(
                [
                    {"role": "assistant", "content": f"retry {index}"},
                    {"role": "user", "content": request},
                    {"role": "assistant", "content": f"retry {index} again"},
                    {"role": "user", "content": request},
                ]
            )
        history.append({"role": "assistant", "content": "Fixed."})

        with patch("agent.title_generator.auto_title_session") as mock_auto:
            import threading

            called = threading.Event()
            mock_auto.side_effect = lambda *_args, **_kwargs: called.set()
            maybe_auto_title(
                db,
                "sess-1",
                "Fix the title gate",
                "Fixed.",
                history,
            )
            assert called.wait(timeout=10), "auto_title thread never ran"

        mock_auto.assert_called_once()

    def test_fires_with_two_real_user_turns_plus_internal_scaffolding(self):
        from agent.context_compressor import (
            COMPRESSION_CONTINUATION_USER_CONTENT,
            SUMMARY_PREFIX,
        )

        db = MagicMock()
        history = [
            {"role": "user", "content": "first request"},
            {"role": "assistant", "content": "first response"},
            {"role": "user", "content": f"{SUMMARY_PREFIX}\nsummary body"},
            {
                "role": "user",
                "content": (
                    "[Your active task list was preserved across context compression]\n"
                    "- [>] Continue"
                ),
            },
            {"role": "user", "content": "second request"},
            {"role": "assistant", "content": "working"},
            {"role": "user", "content": COMPRESSION_CONTINUATION_USER_CONTENT},
            {"role": "user", "content": MAX_ITERATIONS_SUMMARY_REQUEST},
            {"role": "assistant", "content": "final response"},
        ]

        with patch("agent.title_generator.auto_title_session") as mock_auto:
            import threading

            called = threading.Event()
            mock_auto.side_effect = lambda *args, **kwargs: called.set()
            maybe_auto_title(
                db,
                "sess-1",
                "second request",
                "final response",
                history,
            )
            assert called.wait(timeout=10), "auto_title thread never ran"

        mock_auto.assert_called_once()

    def test_skips_with_three_repeated_real_turns_plus_internal_scaffolding(self):
        from agent.context_compressor import SUMMARY_PREFIX

        db = MagicMock()
        history = [
            {"role": "user", "content": "same request"},
            {"role": "assistant", "content": "first response"},
            {"role": "user", "content": f"{SUMMARY_PREFIX}\nsummary body"},
            {"role": "user", "content": "same request"},
            {"role": "assistant", "content": "second response"},
            {
                "role": "user",
                "content": (
                    "[Your active task list was preserved across context compression]\n"
                    "- [>] Continue"
                ),
            },
            {"role": "user", "content": "same request"},
            {"role": "assistant", "content": "third response"},
        ]

        with (
            patch("agent.title_generator.threading.Thread") as mock_thread,
            patch("agent.title_generator.auto_title_session") as mock_auto,
        ):
            maybe_auto_title(
                db,
                "sess-1",
                "same request",
                "third response",
                history,
            )

        mock_thread.assert_not_called()
        mock_auto.assert_not_called()

    def test_internal_only_history_never_starts_a_title_thread(self):
        from tools.process_registry import format_process_notification

        delegation = format_process_notification(
            {
                "type": "async_delegation",
                "delegation_id": "deleg-1",
                "goal": "Inspect the title gate",
                "status": "completed",
                "summary": "Done.",
            }
        )
        history = [
            {"role": "user", "content": MAX_ITERATIONS_SUMMARY_REQUEST},
            {"role": "assistant", "content": "summary"},
            {
                "role": "user",
                "content": "model changed",
                "display_kind": "model_switch",
            },
            {"role": "user", "content": delegation},
            {"role": "assistant", "content": "notification handled"},
        ]

        with patch("agent.title_generator.threading.Thread") as thread_cls:
            maybe_auto_title(
                MagicMock(),
                "sess-internal",
                MAX_ITERATIONS_SUMMARY_REQUEST,
                "notification handled",
                history,
            )

        thread_cls.assert_not_called()


class TestAutoTitleDuplicateHandling:
    """Duplicate auto-title handling and not-found hardening (#50537)."""

    def test_dedupes_duplicate_title_via_lineage(self):
        db = MagicMock()
        db.get_session_title.return_value = None
        # Atomic write path: collision raises ValueError, retry persists.
        db.set_auto_title_if_empty.side_effect = [ValueError("in use"), True]
        db.get_next_title_in_lineage.return_value = "Debugging Import Error #2"
        with patch(
            "agent.title_generator.generate_title",
            return_value="Debugging Import Error",
        ):
            seen = []
            auto_title_session(db, "sess-1", "hi", "hello", title_callback=seen.append)
        db.get_next_title_in_lineage.assert_called_once_with("Debugging Import Error")
        assert db.set_auto_title_if_empty.call_args_list[-1][0] == (
            "sess-1",
            "Debugging Import Error #2",
        )
        # callback fires with the actually-persisted (deduped) title
        assert seen == ["Debugging Import Error #2"]



    def test_manual_title_race_skips_without_callback(self):
        # Atomic predicate fails (manual /title landed while generation was in
        # flight) -> nothing persisted, no callback fired.
        from agent.title_generator import _persist_session_title
        db = MagicMock()
        db.set_auto_title_if_empty.return_value = False
        assert _persist_session_title(db, "sess-1", "Some Title") is None
        db.set_session_title.assert_not_called()



class TestRuntimeValidator:
    """runtime_validator gating (#19027): a stale background title request
    must not fire when the session's model/provider changed after spawn."""



    def test_broken_validator_fails_open(self):
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "Resilient Title"

        def _bad_validator():
            raise RuntimeError("validator gone")

        with patch("agent.title_generator.call_llm", return_value=mock_response) as mock_llm:
            title = generate_title(
                "question", "answer",
                runtime_validator=_bad_validator,
            )
            assert title == "Resilient Title"
            mock_llm.assert_called_once()

    def test_forwards_runtime_validator_to_worker(self):
        db = MagicMock()
        db.get_session_title.return_value = None
        history = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]

        def _v():
            return True

        with patch("agent.title_generator.auto_title_session") as mock_auto:
            import threading
            called = threading.Event()
            mock_auto.side_effect = lambda *a, **k: called.set()
            maybe_auto_title(db, "sess-1", "hello", "hi there", history, runtime_validator=_v)
            assert called.wait(timeout=10), "auto_title thread never ran"
            kwargs = mock_auto.call_args.kwargs
            assert kwargs["runtime_validator"] is _v
