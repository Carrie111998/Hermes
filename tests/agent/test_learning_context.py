"""Evidence-context contracts for autonomous background learning."""

from __future__ import annotations


def test_build_learning_metadata_uses_bounded_redacted_user_evidence():
    from agent.learning_context import build_learning_metadata

    messages = [
        {"role": "system", "content": "secret system text"},
        {"role": "user", "content": "Please remember this Authorization: Bearer " + "a" * 80 + " " + "x" * 1000},
        {"role": "assistant", "content": "Understood"},
    ]

    metadata = build_learning_metadata(messages, session_id="session-1", platform="cli")

    assert metadata["source"]["session_id"] == "session-1"
    assert metadata["source"]["trust"] == "user_supplied_unverified"
    assert metadata["evidence"]["trigger"] == "preference"
    assert "a" * 80 not in metadata["evidence"]["excerpt"]
    assert len(metadata["evidence"]["excerpt"]) <= 500
    assert "system text" not in metadata["evidence"]["excerpt"]


def test_learning_metadata_scope_is_context_local():
    from agent.learning_context import current_learning_metadata, learning_metadata_scope

    assert current_learning_metadata() == {}
    metadata = {"source": {"session_id": "s"}, "evidence": {"status": "captured"}}
    with learning_metadata_scope(metadata):
        assert current_learning_metadata() == metadata
    assert current_learning_metadata() == {}
