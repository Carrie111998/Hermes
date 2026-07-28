"""Tests for the lightweight Slack/Discord channel session-continuity hint.

Salvaged from PR #36220 (metamon-p), ported onto the current SessionStore.

Covers:
- SessionStore records the previous session_id on auto-reset (and only then).
- prev_session_id survives a to_dict() → from_dict() roundtrip (gateway restart).
- build_channel_continuity_note() emits a hint only for Slack/Discord sessions
  that were auto-reset with real prior activity, and stays silent otherwise.
"""

from datetime import datetime, timedelta

import pytest

from gateway.config import (
    ContextRolloverPolicy,
    GatewayConfig,
    Platform,
    SessionResetPolicy,
)
from gateway.session import (
    SessionEntry,
    SessionSource,
    SessionStore,
    build_channel_continuity_note,
)
from gateway.session_continuity import build_context_rollover_checkpoint


@pytest.fixture()
def _isolated_db(tmp_path, monkeypatch):
    import hermes_state

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


def _make_store(tmp_path, policy=None):
    config = GatewayConfig()
    if policy:
        config.default_reset_policy = policy
    return SessionStore(sessions_dir=tmp_path / "sessions", config=config)


def _slack_source(thread_id=None):
    return SessionSource(
        platform=Platform.SLACK,
        chat_id="C123",
        chat_type="thread" if thread_id else "channel",
        user_id="U1",
        thread_id=thread_id,
    )


# ---------------------------------------------------------------------------
# SessionStore records prev_session_id on auto-reset
# ---------------------------------------------------------------------------

class TestPrevSessionIdCapture:
    def test_prev_session_id_set_on_auto_reset(self, _isolated_db, tmp_path):
        store = _make_store(tmp_path, SessionResetPolicy(mode="idle", idle_minutes=1))
        source = _slack_source(thread_id="T9")

        entry1 = store.get_or_create_session(source)
        assert entry1.prev_session_id is None  # fresh session, nothing replaced

        entry1.last_prompt_tokens = 4000  # had real conversation
        entry1.updated_at = datetime.now() - timedelta(minutes=5)
        store._save()

        entry2 = store.get_or_create_session(source)
        assert entry2.was_auto_reset is True
        assert entry2.reset_had_activity is True
        assert entry2.prev_session_id == entry1.session_id


class TestModelAwareContextRollover:
    @staticmethod
    def _store(tmp_path, policy, has_active_processes_fn=None):
        return SessionStore(
            sessions_dir=tmp_path / "sessions",
            config=GatewayConfig(context_rollover=policy),
            has_active_processes_fn=has_active_processes_fn,
        )

    @staticmethod
    def _source(platform=Platform.TELEGRAM):
        return SessionSource(
            platform=platform,
            chat_id="chat-1",
            chat_type="dm",
            user_id="user-1",
        )

    def test_rolls_at_model_aware_threshold_and_links_child(
        self, _isolated_db, tmp_path
    ):
        store = self._store(
            tmp_path,
            ContextRolloverPolicy(enabled=True, threshold_ratio=0.70),
        )
        source = self._source()
        previous = store.get_or_create_session(source)
        previous.metadata["route_marker"] = "keep"
        previous.model_override = {
            "model": "grok-4.5",
            "provider": "xai-oauth",
        }
        store.append_to_transcript(
            previous.session_id,
            {"role": "user", "content": "Keep the model-aware decision."},
        )
        store.update_session(
            previous.session_key,
            last_prompt_tokens=350_000,
            last_input_budget_tokens=500_000,
        )

        current = store.get_or_create_session(source)
        previous_row = store._db.get_session(previous.session_id)
        current_row = store._db.get_session(current.session_id)

        assert current.session_id != previous.session_id
        assert current.auto_reset_reason == "context_rollover"
        assert current.prev_session_id == previous.session_id
        assert current.metadata["route_marker"] == "keep"
        assert current.model_override == previous.model_override
        assert previous_row["end_reason"] == "context_rollover"
        assert current_row["parent_session_id"] == previous.session_id
        assert (
            store._db.get_conversation_root(current.session_id)
            == store._db.get_conversation_root(previous.session_id)
        )
        checkpoint = store.build_continuity_checkpoint(current)
        assert "CONTEXT SEGMENT ROLLOVER" in checkpoint
        assert "Keep the model-aware decision." in checkpoint

    def test_stays_in_segment_below_model_aware_threshold(
        self, _isolated_db, tmp_path
    ):
        store = self._store(
            tmp_path,
            ContextRolloverPolicy(enabled=True, threshold_ratio=0.70),
        )
        source = self._source(Platform.DISCORD)
        previous = store.get_or_create_session(source)
        store.update_session(
            previous.session_key,
            last_prompt_tokens=349_999,
            last_input_budget_tokens=500_000,
        )

        current = store.get_or_create_session(source)

        assert current.session_id == previous.session_id

    def test_lower_absolute_cap_wins(self, _isolated_db, tmp_path):
        store = self._store(
            tmp_path,
            ContextRolloverPolicy(
                enabled=True,
                threshold_ratio=0.70,
                max_prompt_tokens=300_000,
            ),
        )
        source = self._source()
        previous = store.get_or_create_session(source)
        store.update_session(
            previous.session_key,
            last_prompt_tokens=300_000,
            last_input_budget_tokens=500_000,
        )

        current = store.get_or_create_session(source)

        assert current.auto_reset_reason == "context_rollover"

    def test_ratio_only_waits_for_measured_budget(self, _isolated_db, tmp_path):
        store = self._store(
            tmp_path,
            ContextRolloverPolicy(enabled=True, threshold_ratio=0.70),
        )
        source = self._source()
        previous = store.get_or_create_session(source)
        store.update_session(
            previous.session_key,
            last_prompt_tokens=400_000,
            last_input_budget_tokens=0,
        )

        current = store.get_or_create_session(source)

        assert current.session_id == previous.session_id

    def test_excluded_platform_does_not_roll(self, _isolated_db, tmp_path):
        store = self._store(
            tmp_path,
            ContextRolloverPolicy(enabled=True, threshold_ratio=0.70),
        )
        source = self._source(Platform.WEBHOOK)
        previous = store.get_or_create_session(source)
        store.update_session(
            previous.session_key,
            last_prompt_tokens=400_000,
            last_input_budget_tokens=500_000,
        )

        current = store.get_or_create_session(source)

        assert current.session_id == previous.session_id

    def test_active_background_work_defers_rollover(
        self, _isolated_db, tmp_path
    ):
        store = self._store(
            tmp_path,
            ContextRolloverPolicy(enabled=True, threshold_ratio=0.70),
            has_active_processes_fn=lambda _session_key: True,
        )
        source = self._source()
        previous = store.get_or_create_session(source)
        store.update_session(
            previous.session_key,
            last_prompt_tokens=400_000,
            last_input_budget_tokens=500_000,
        )

        current = store.get_or_create_session(source)

        assert current.session_id == previous.session_id


# ---------------------------------------------------------------------------
# build_channel_continuity_note
# ---------------------------------------------------------------------------

def _reset_entry(platform, prev="20260101_000000_abc", had_activity=True):
    return SessionEntry(
        session_key="k",
        session_id="20260101_010000_def",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=platform,
        was_auto_reset=True,
        auto_reset_reason="daily",
        reset_had_activity=had_activity,
        prev_session_id=prev,
    )


class TestBuildChannelContinuityNote:
    def test_slack_channel_emits_hint(self):
        entry = _reset_entry(Platform.SLACK)
        note = build_channel_continuity_note(entry, _slack_source())
        assert note is not None
        assert "session_search" in note
        assert entry.prev_session_id in note
        assert "channel" in note

    def test_discord_thread_uses_thread_wording(self):
        entry = _reset_entry(Platform.DISCORD)
        source = SessionSource(
            platform=Platform.DISCORD,
            chat_id="c",
            chat_type="thread",
            thread_id="T1",
        )
        note = build_channel_continuity_note(entry, source)
        assert note is not None
        assert "thread" in note

    def test_other_platform_returns_none_for_timed_reset(self):
        entry = _reset_entry(Platform.TELEGRAM)
        source = SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="c",
            user_id="u",
        )
        assert build_channel_continuity_note(entry, source) is None

    def test_no_activity_returns_none(self):
        entry = _reset_entry(Platform.SLACK, had_activity=False)
        assert build_channel_continuity_note(entry, _slack_source()) is None

    def test_no_prev_session_id_returns_none(self):
        entry = _reset_entry(Platform.SLACK, prev=None)
        assert build_channel_continuity_note(entry, _slack_source()) is None

    def test_context_rollover_carries_deterministic_checkpoint(self):
        entry = _reset_entry(Platform.TELEGRAM)
        entry.auto_reset_reason = "context_rollover"
        checkpoint = build_context_rollover_checkpoint(
            previous_session_id=entry.prev_session_id,
            prompt_tokens=120000,
            messages=[
                {"role": "user", "content": "Keep the exact acceptance criteria."},
                {"role": "assistant", "content": "The route and tests are in place."},
            ],
        )
        source = SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="c",
            user_id="u",
        )

        note = build_channel_continuity_note(
            entry,
            source,
            continuity_checkpoint=checkpoint,
        )

        assert "CONTEXT SEGMENT ROLLOVER" in note
        assert "120,000" in note
        assert "Keep the exact acceptance criteria." in note
        assert "The route and tests are in place." in note
        assert "session_search" in note


class TestContextRolloverCheckpoint:
    def test_uses_real_dialogue_and_excludes_generated_or_tool_rows(self):
        checkpoint = build_context_rollover_checkpoint(
            previous_session_id="previous-1",
            prompt_tokens=135000,
            messages=[
                {"role": "assistant", "content": "Earlier useful outcome."},
                {
                    "role": "user",
                    "content": "[CONTEXT COMPACTION - REFERENCE ONLY] stale summary",
                },
                {"role": "tool", "content": "secret tool exhaust", "tool_name": "exec"},
                {"role": "user", "content": "Latest real request."},
                {"role": "assistant", "content": "Latest real result."},
            ],
        )

        assert "Earlier useful outcome." in checkpoint
        assert "Latest real request." in checkpoint
        assert "Latest real result." in checkpoint
        assert "stale summary" not in checkpoint
        assert "secret tool exhaust" not in checkpoint

    def test_bounds_large_messages_and_marks_truncation(self):
        checkpoint = build_context_rollover_checkpoint(
            previous_session_id="previous-1",
            prompt_tokens=135000,
            messages=[
                {"role": "user", "content": "x" * 5000},
                {"role": "assistant", "content": "y" * 5000},
            ],
            max_message_chars=400,
            max_excerpt_chars=900,
        )

        assert "[content truncated]" in checkpoint
        assert len(checkpoint) < 1800

    def test_custom_bounds_keep_a_single_dialogue_block_within_excerpt_cap(self):
        checkpoint = build_context_rollover_checkpoint(
            previous_session_id="previous-1",
            prompt_tokens=135000,
            messages=[{"role": "assistant", "content": "x" * 5000}],
            max_message_chars=5000,
            max_excerpt_chars=200,
        )

        excerpt = checkpoint.split(
            "Latest real dialogue from the previous session:\n\n",
            maxsplit=1,
        )[1].split("\n\nFor earlier detail", maxsplit=1)[0]

        assert len(excerpt) <= 200

    def test_store_loads_only_recent_user_and_assistant_rows(
        self, _isolated_db, tmp_path
    ):
        store = _make_store(tmp_path)
        source = SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="chat-1",
            user_id="user-1",
        )
        entry = store.get_or_create_session(source)
        store.append_to_transcript(
            entry.session_id,
            {"role": "user", "content": "oldest"},
        )
        store.append_to_transcript(
            entry.session_id,
            {"role": "tool", "content": "tool noise", "tool_name": "exec"},
        )
        store.append_to_transcript(
            entry.session_id,
            {"role": "assistant", "content": "middle"},
        )
        store.append_to_transcript(
            entry.session_id,
            {"role": "user", "content": "latest"},
        )

        rows = store.load_recent_transcript_messages(entry.session_id, limit=2)

        assert [(row["role"], row["content"]) for row in rows] == [
            ("assistant", "middle"),
            ("user", "latest"),
        ]
