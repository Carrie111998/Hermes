from __future__ import annotations

import time

from agent.speculative_compression import (
    SpeculativeCandidate,
    SpeculativeCompressionSettings,
    fingerprint_messages,
)
from hermes_state import SessionDB
from run_agent import AIAgent


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
