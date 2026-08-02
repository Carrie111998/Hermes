from __future__ import annotations

import threading
import time

from agent.speculative_compression import (
    SpeculativeCandidate,
    SpeculativeCompressionSettings,
    configure_speculative_compression,
    fingerprint_messages,
)
from hermes_state import SessionDB
from run_agent import AIAgent


class _RestoreManager:
    def __init__(self):
        self.restored = []
        self.invalidated = []

    def restore_candidate(self, session_id, candidate):
        self.restored.append((session_id, candidate))

    def invalidate_session(self, session_id):
        self.invalidated.append(session_id)


def _agent_with_candidate_db(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "speculative-commit-session"
    db.create_session(session_id, source="cli")
    agent = AIAgent(
        api_key="test-key",
        base_url="https://openrouter.ai/api/v1",
        model="test/model",
        quiet_mode=True,
        session_db=db,
        session_id=session_id,
        skip_context_files=True,
        skip_memory=True,
    )
    agent.speculative_compression_settings = SpeculativeCompressionSettings(
        enabled=True
    )
    agent.speculative_compression_enabled = True
    agent._emit_status = lambda *_args, **_kwargs: None
    agent._emit_warning = lambda *_args, **_kwargs: None
    return agent, db, session_id


def _messages():
    return [
        {"role": "user", "content": "old context"},
        {
            "role": "assistant",
            "content": "tool call",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "tool", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "result"},
        {"role": "user", "content": "current request"},
    ]


def _candidate(messages, session_id, *, created_at=None):
    return SpeculativeCandidate(
        session_id=session_id,
        source_fingerprint=fingerprint_messages(messages[:1]),
        boundary_fingerprint=fingerprint_messages([messages[1]]),
        cut_index=1,
        compress_start=0,
        original_count=len(messages),
        compressed_prefix=({"role": "user", "content": "prepared summary"},),
        created_at=time.monotonic() if created_at is None else created_at,
    )


def test_candidate_uses_existing_lock_and_commits_atomically(tmp_path):
    agent, db, session_id = _agent_with_candidate_db(tmp_path)
    messages = _messages()

    compressed, _prompt = agent._compress_context(
        messages,
        "sys",
        approx_tokens=100,
        speculative_candidate=_candidate(messages, session_id),
    )

    assert agent._speculative_install_status == "installed"
    assert compressed[0]["content"] == "prepared summary"
    assert compressed[1:] == messages[1:]
    assert db.get_compression_lock_holder(session_id) is None


def test_disabled_candidate_is_rejected_before_compression(tmp_path):
    agent, db, session_id = _agent_with_candidate_db(tmp_path)
    agent.speculative_compression_enabled = False
    messages = _messages()

    returned, _prompt = agent._compress_context(
        messages,
        "sys",
        approx_tokens=100,
        speculative_candidate=_candidate(messages, session_id),
    )

    assert agent._speculative_install_status == "rejected"
    assert returned == messages
    assert db.get_compression_lock_holder(session_id) is None


def test_stale_candidate_is_rejected_before_install(tmp_path):
    agent, db, session_id = _agent_with_candidate_db(tmp_path)
    agent.speculative_compression_settings = SpeculativeCompressionSettings(
        enabled=True, max_age_seconds=1
    )
    messages = _messages()

    returned, _prompt = agent._compress_context(
        messages,
        "sys",
        approx_tokens=100,
        speculative_candidate=_candidate(
            messages, session_id, created_at=time.monotonic() - 10
        ),
    )

    assert agent._speculative_install_status == "rejected"
    assert returned == messages
    assert db.get_compression_lock_holder(session_id) is None


def test_zero_max_age_expires_candidate_immediately(tmp_path):
    """max_age_seconds: 0 must mean immediate expiry at install time, not
    fall back to the 180s default (regression for the `or 180.0` bug)."""
    agent, db, session_id = _agent_with_candidate_db(tmp_path)
    agent.speculative_compression_settings = SpeculativeCompressionSettings(
        enabled=True, max_age_seconds=0
    )
    messages = _messages()

    returned, _prompt = agent._compress_context(
        messages,
        "sys",
        approx_tokens=100,
        speculative_candidate=_candidate(messages, session_id),
    )

    assert agent._speculative_install_status == "rejected"
    assert returned == messages
    assert db.get_compression_lock_holder(session_id) is None


def test_compressor_fingerprint_mismatch_rejects_candidate(tmp_path):
    """A candidate built under a different compressor (model/context change)
    must be rejected even when the transcript is unchanged."""
    agent, db, session_id = _agent_with_candidate_db(tmp_path)
    messages = _messages()

    candidate = _candidate(messages, session_id)
    candidate = SpeculativeCandidate(
        session_id=candidate.session_id,
        source_fingerprint=candidate.source_fingerprint,
        boundary_fingerprint=candidate.boundary_fingerprint,
        cut_index=candidate.cut_index,
        compress_start=candidate.compress_start,
        original_count=candidate.original_count,
        compressed_prefix=candidate.compressed_prefix,
        created_at=candidate.created_at,
        compressor_fingerprint="different-compressor",
    )

    returned, _prompt = agent._compress_context(
        messages,
        "sys",
        approx_tokens=100,
        speculative_candidate=candidate,
    )

    assert agent._speculative_install_status == "rejected"
    assert returned == messages
    assert db.get_compression_lock_holder(session_id) is None


def test_failed_durable_commit_rejects_candidate_and_releases_lock(
    tmp_path, monkeypatch
):
    agent, db, session_id = _agent_with_candidate_db(tmp_path)
    messages = _messages()

    def fail_commit(*_args, **_kwargs):
        raise RuntimeError("commit rejected")

    monkeypatch.setattr(db, "archive_and_compact", fail_commit)
    agent._compress_context(
        messages,
        "sys",
        approx_tokens=100,
        speculative_candidate=_candidate(messages, session_id),
    )

    assert agent._speculative_install_status == "rejected"
    assert db.get_compression_lock_holder(session_id) is None


def test_off_after_lock_wait_rejects_without_restoring_before_commit(
    tmp_path, monkeypatch
):
    """The kill switch linearizes after lock acquisition, before assemble."""
    agent, db, session_id = _agent_with_candidate_db(tmp_path)
    manager = _RestoreManager()
    agent._speculative_compression_manager = manager
    messages = _messages()
    candidate = _candidate(messages, session_id)
    other_holder = "other-thread-holder"
    assert db.try_acquire_compression_lock(session_id, other_holder)

    acquired = threading.Event()
    release_wait = threading.Event()
    real_try_acquire = db.try_acquire_compression_lock

    def wait_for_switch(*args, **kwargs):
        acquired.set()
        assert release_wait.wait(2)
        return real_try_acquire(*args, **kwargs)

    monkeypatch.setattr(db, "try_acquire_compression_lock", wait_for_switch)
    commit_calls = []
    monkeypatch.setattr(
        db,
        "publish_compression_child",
        lambda *args, **kwargs: commit_calls.append((args, kwargs)),
    )
    result = []

    def install():
        result.append(
            agent._compress_context(
                messages,
                "sys",
                approx_tokens=100,
                speculative_candidate=candidate,
            )
        )

    thread = threading.Thread(target=install)
    thread.start()
    assert acquired.wait(2)
    configure_speculative_compression(agent, False)
    db.release_compression_lock(session_id, other_holder)
    release_wait.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert result[0][0] == messages
    assert agent._speculative_install_status == "rejected"
    assert manager.restored == []
    assert commit_calls == []
    assert agent.session_id == session_id
    assert db.get_compression_lock_holder(session_id) is None


def test_off_after_candidate_match_rejects_without_db_rewrite(tmp_path, monkeypatch):
    """The post-gate epoch barrier rejects a switch during pure validation."""
    agent, db, session_id = _agent_with_candidate_db(tmp_path)
    manager = _RestoreManager()
    agent._speculative_compression_manager = manager
    messages = _messages()
    candidate = _candidate(messages, session_id)
    rows_before = db.get_messages_as_conversation(session_id, include_inactive=True)
    session_before = db.get_session(session_id)
    matches_started = threading.Event()
    resume_matches = threading.Event()
    real_matches = SpeculativeCandidate.matches

    def blocked_matches(self, *args, **kwargs):
        matches_started.set()
        assert resume_matches.wait(2)
        return real_matches(self, *args, **kwargs)

    monkeypatch.setattr(SpeculativeCandidate, "matches", blocked_matches)
    result = []

    def install():
        result.append(
            agent._compress_context(
                messages,
                "sys",
                approx_tokens=100,
                speculative_candidate=candidate,
            )
        )

    thread = threading.Thread(target=install)
    thread.start()
    assert matches_started.wait(2)
    configure_speculative_compression(agent, False)
    resume_matches.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert result[0][0] == messages
    assert agent._speculative_install_status == "rejected"
    assert manager.restored == []
    assert db.get_messages_as_conversation(session_id, include_inactive=True) == rows_before
    assert db.get_session(session_id) == session_before
    assert db.get_compression_lock_holder(session_id) is None


def test_speculative_and_sync_accounting_share_progress_and_warning_state(
    tmp_path, monkeypatch
):
    agent, _db, session_id = _agent_with_candidate_db(tmp_path)
    compressor = agent.context_compressor
    messages = _messages()

    no_progress = SpeculativeCandidate(
        session_id=session_id,
        source_fingerprint=fingerprint_messages(messages[:1]),
        boundary_fingerprint=fingerprint_messages([messages[1]]),
        cut_index=1,
        compress_start=0,
        original_count=len(messages),
        compressed_prefix=(messages[0],),
        created_at=time.monotonic(),
        made_progress=False,
    )
    returned, _ = agent._compress_context(
        messages, "sys", approx_tokens=100, speculative_candidate=no_progress
    )
    assert returned == messages
    assert compressor.compression_count == 1
    assert compressor._last_compression_made_progress is False
    assert compressor._ineffective_compression_count == 1

    installed_candidate = _candidate(messages, session_id)
    installed_candidate = SpeculativeCandidate(
        **{
            **installed_candidate.__dict__,
            "made_progress": True,
        }
    )
    installed, _ = agent._compress_context(
        messages,
        "sys",
        approx_tokens=100,
        speculative_candidate=installed_candidate,
    )
    assert installed[0]["content"] == "prepared summary"
    assert compressor.compression_count == 2
    assert compressor._last_compression_made_progress is True
    assert compressor._ineffective_compression_count == 1

    # The provider-level verdict after the successful candidate install is the
    # same real-usage accounting used by synchronous compaction.
    compressor.update_from_response({"prompt_tokens": compressor.threshold_tokens})
    assert compressor._ineffective_compression_count == 2

    def synchronous_fallback(current_messages, **_kwargs):
        compressor.compression_count += 1
        compressor._last_compression_made_progress = True
        return [
            {"role": "user", "content": "synchronous summary"},
            *current_messages[1:],
        ]

    monkeypatch.setattr(compressor, "compress", synchronous_fallback)
    agent._compression_feasibility_checked = True
    agent._compress_context(messages, "sys", approx_tokens=100, force=True)
    assert compressor.compression_count == 3
    assert compressor._last_compression_made_progress is False
    assert compressor._ineffective_compression_count == 2

    should_compress, block_reason = compressor.should_compress_info(
        compressor.threshold_tokens
    )
    assert should_compress is False
    assert block_reason == "ineffective"
