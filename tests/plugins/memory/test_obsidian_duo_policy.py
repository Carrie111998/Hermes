import pytest

from plugins.memory.obsidian_duo.contracts import (
    Authority,
    MemoryCandidate,
    MemoryRecord,
    MemoryStatus,
    Verification,
)
from plugins.memory.obsidian_duo.policy import EventKind, MemoryPolicy
from plugins.memory.obsidian_duo.vault import ParsedNote


def test_explicit_remember_promotes_safe_content():
    decision = MemoryPolicy().evaluate(
        MemoryCandidate("Use concise status updates", metadata={"event_kind": EventKind.EXPLICIT_REMEMBER.value})
    )

    assert decision.action == "promote"


def test_inference_without_evidence_is_staged():
    decision = MemoryPolicy().evaluate(
        MemoryCandidate("The user may prefer dark mode", metadata={"confidence": 0.99})
    )

    assert decision.action == "stage"


@pytest.mark.parametrize("content", ["C:\\secret\\token.txt", "terminal output: password=sk-proj-1234567890abcdefghijklmnop"])
def test_transient_or_secret_content_is_rejected(content):
    metadata = {"event_kind": EventKind.TURN.value}
    decision = MemoryPolicy().evaluate(MemoryCandidate(content, metadata=metadata))

    assert decision.action in {"discard", "reject"}


def test_user_edit_becomes_user_confirmed():
    old = MemoryRecord("mem_1", "old", "fact", "global")
    parsed = ParsedNote(
        path=None,
        memory_id="mem_1",
        metadata={"memory_type": "fact", "scope": "global"},
        body="corrected",
    )

    updated = MemoryPolicy().apply_user_edit(old, parsed)

    assert updated.content == "corrected"
    assert updated.authority is Authority.USER
    assert updated.verification is Verification.USER_CONFIRMED


def test_conflicting_important_memory_is_not_overwritten():
    existing = [
        MemoryRecord(
            "mem_old", "Use the blue theme", "preference", "global",
            importance=1.0, authority=Authority.USER, verification=Verification.USER_CONFIRMED,
        )
    ]
    candidate = MemoryCandidate(
        "Use the red theme",
        memory_type="preference",
        metadata={"contradicts": "mem_old"},
    )

    decision = MemoryPolicy().merge_or_conflict(existing, candidate)

    assert decision.action == "conflict"
    assert decision.memory_id == "mem_old"
