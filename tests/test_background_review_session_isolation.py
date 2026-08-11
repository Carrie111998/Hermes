"""Tests for background-review session-store isolation (hermes_state).

The background skill/memory review fork shares the parent's ``session_id`` for
prompt-cache warmth. Without the ``_persist_disabled`` isolation it wrote its
harness turn ("Review the conversation above and update the skill library…")
plus its curator-mode reply into the user's REAL session, and the next live
turn re-read that injected user message as a standing instruction — the agent
"became" the curator and refused the actual task.

``_strip_background_review_harness`` is the load-on-read defense-in-depth that
removes any such stray harness message (and the assistant reply that followed
it) so a polluted session resumes clean.
"""

from hermes_state import (
    _is_background_review_harness_message,
    _strip_background_review_harness,
)


class TestIsBackgroundReviewHarnessMessage:
    def test_matches_skill_review_prompt(self):
        msg = {"role": "user", "content": "Review the conversation above and update the skill library now."}
        assert _is_background_review_harness_message(msg) is True

    def test_matches_memory_review_prompt(self):
        msg = {"role": "system", "content": "Review the conversation above and consider saving to memory."}
        assert _is_background_review_harness_message(msg) is True


    def test_ignores_normal_user_message(self):
        msg = {"role": "user", "content": "Please review my PR and update the changelog."}
        assert _is_background_review_harness_message(msg) is False

    def test_ignores_assistant_role(self):
        # An assistant message that quotes the harness text is not itself a harness prompt.
        msg = {"role": "assistant", "content": "Review the conversation above and update the skill library"}
        assert _is_background_review_harness_message(msg) is False




class TestStripBackgroundReviewHarness:
    def test_strips_harness_and_following_assistant_reply(self):
        messages = [
            {"role": "user", "content": "What's the weather?"},
            {"role": "assistant", "content": "It's sunny."},
            {"role": "user", "content": "Review the conversation above and update the skill library."},
            {"role": "assistant", "content": "Nothing to save."},
            {"role": "user", "content": "Thanks, now book a flight."},
        ]
        out = _strip_background_review_harness(messages)
        contents = [m["content"] for m in out]
        assert contents == ["What's the weather?", "It's sunny.", "Thanks, now book a flight."]







class TestGetMessagesAsConversationStripsHarness:
    """The load-on-read wiring: get_messages_as_conversation must actually call
    _strip_background_review_harness, so a session polluted with stray harness
    rows resumes clean end-to-end (not just the pure helper in isolation)."""

    def test_polluted_session_resumes_without_harness(self):
        import tempfile
        from pathlib import Path
        from hermes_state import SessionDB

        with tempfile.TemporaryDirectory() as tmp:
            db = SessionDB(db_path=Path(tmp) / "t.db")
            try:
                db.create_session(session_id="s1", source="cli")
                db.append_message("s1", role="user", content="What's the weather?")
                db.append_message("s1", role="assistant", content="It's sunny.")
                # Stray background-review pollution written by an older build.
                db.append_message(
                    "s1", role="user",
                    content="Review the conversation above and update the skill library with anything useful.",
                )
                db.append_message("s1", role="assistant", content="I'll act as the curator now.")
                db.append_message("s1", role="user", content="Thanks, now book a flight.")

                conv = db.get_messages_as_conversation("s1")
                contents = [m["content"] for m in conv]

                # Harness user turn AND its curator-mode assistant reply are gone.
                assert not any(
                    isinstance(c, str) and c.lstrip().startswith("Review the conversation above")
                    for c in contents
                )
                assert "I'll act as the curator now." not in contents
                # Genuine turns survive in order.
                assert contents == ["What's the weather?", "It's sunny.", "Thanks, now book a flight."]
            finally:
                db.close()


class TestPersistDisabledHardStop:
    """The isolation wiring: a _persist_disabled agent must never write to the
    session store via _flush_messages_to_session_db, even with a live db set."""

    def test_flush_is_a_noop_when_persist_disabled(self):
        import os
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from hermes_state import SessionDB

        with tempfile.TemporaryDirectory() as tmp:
            db = SessionDB(db_path=Path(tmp) / "t.db")
            try:
                with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
                    from run_agent import AIAgent
                    agent = AIAgent(
                        api_key="test-key",
                        base_url="https://openrouter.ai/api/v1",
                        model="test/model",
                        quiet_mode=True,
                        session_db=db,
                        session_id="s-review",
                        skip_context_files=True,
                        skip_memory=True,
                    )
                agent._ensure_db_session()
                agent._persist_disabled = True

                agent._flush_messages_to_session_db(
                    [{"role": "user", "content": "Review the conversation above and update the skill library."},
                     {"role": "assistant", "content": "curator reply"}],
                    [],
                )

                # Nothing written: the hard-stop fired before any append.
                assert db.get_messages("s-review") == []
            finally:
                db.close()


class TestPersistSessionConstructor:
    def test_persist_session_false_never_touches_session_db_or_json_snapshot(self):
        """An opt-in private worker keeps its turn in memory only."""
        import os
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from hermes_state import SessionDB

        with tempfile.TemporaryDirectory() as tmp:
            db = SessionDB(db_path=Path(tmp) / "t.db")
            try:
                with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
                    from run_agent import AIAgent
                    agent = AIAgent(
                        api_key="test-key",
                        base_url="https://openrouter.ai/api/v1",
                        model="test/model",
                        quiet_mode=True,
                        session_db=db,
                        session_id="s-private",
                        skip_context_files=True,
                        skip_memory=True,
                        persist_session=False,
                    )

                assert agent._persist_disabled is True
                assert agent._session_db is None
                assert agent._session_json_enabled is False
                assert agent._end_session_on_close is False

                messages = [
                    {"role": "user", "content": "private customer body"},
                    {"role": "assistant", "content": "private draft"},
                ]
                with patch.object(agent, "_save_session_log") as save_log:
                    agent._persist_session(messages, [])

                save_log.assert_not_called()
                assert agent._session_messages == messages
                assert db.get_messages("s-private") == []
            finally:
                db.close()

    def test_private_no_store_forces_an_empty_tool_memory_and_artifact_surface(self, monkeypatch):
        """The stricter worker mode cannot be weakened by caller options."""
        import os
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "hermes-home"
            with patch.dict(
                os.environ,
                {"OPENROUTER_API_KEY": "test-key", "HERMES_HOME": str(home)},
            ):
                from run_agent import AIAgent
                agent = AIAgent(
                    api_key="test-key",
                    base_url="https://openrouter.ai/api/v1",
                    model="test/model",
                    session_id="s-private-strict",
                    enabled_toolsets=["memory", "terminal"],
                    skip_memory=False,
                    save_trajectories=True,
                    checkpoints_enabled=True,
                    private_no_store=True,
                )

            assert agent._private_no_store is True
            assert agent._persist_disabled is True
            assert agent._session_db is None
            assert agent._session_json_enabled is False
            assert agent.logs_dir is None
            assert agent.enabled_toolsets == []
            assert agent.tools == []
            assert agent._memory_store is None
            assert agent._memory_manager is None
            assert agent.compression_enabled is False
            assert agent.save_trajectories is False
            assert agent._checkpoint_mgr.enabled is False
            assert not list(home.glob("sessions/session_s-private-strict*"))

    def test_persist_session_false_blocks_trajectory_and_request_dump_artifacts(self):
        """No-store must hold even when optional diagnostic writers are enabled."""
        import os
        from pathlib import Path
        from unittest.mock import patch

        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
            from run_agent import AIAgent
            agent = AIAgent(
                api_key="test-key",
                base_url="https://openrouter.ai/api/v1",
                model="test/model",
                quiet_mode=True,
                session_id="s-private-artifacts",
                skip_context_files=True,
                skip_memory=True,
                save_trajectories=True,
                persist_session=False,
            )

        assert agent.save_trajectories is False
        with patch("run_agent._save_trajectory_to_file") as save_trajectory:
            agent._save_trajectory(
                [{"role": "user", "content": "private customer body"}],
                "private customer body",
                True,
            )
        save_trajectory.assert_not_called()

        with patch(
            "agent.agent_runtime_helpers.dump_api_request_debug",
            return_value=Path("/tmp/private-request.json"),
        ) as dump_request:
            assert agent._dump_api_request_debug(
                {"messages": [{"role": "user", "content": "private customer body"}]},
                reason="test",
            ) is None
        dump_request.assert_not_called()
