"""Behavior contracts for the profile-scoped learning ledger."""

from __future__ import annotations

import multiprocessing
import os
import sqlite3
from pathlib import Path

import pytest


def _create_same_dedup_candidate(home: str, candidate_id: str, queue) -> None:
    os.environ["HERMES_HOME"] = home
    from agent import learning_ledger

    try:
        learning_ledger.create_candidate(_candidate(candidate_id))
        queue.put("created")
    except sqlite3.IntegrityError:
        queue.put("duplicate")


def _candidate(candidate_id: str = "candidate-1") -> dict:
    return {
        "candidate_id": candidate_id,
        "subsystem": "memory",
        "action": "add",
        "status": "pending",
        "payload_fingerprint": "sha256:payload",
        "dedup_key": "sha256:dedup",
        "pending_relpath": f"pending/memory/{candidate_id}.json",
        "proposal": {"summary": "Remember the preference"},
        "source": {"origin": "background_review", "trust": "user_explicit"},
        "evidence": {
            "status": "captured",
            "excerpt": "Use token sk-secret-example in the setup",
            "hypothesis": "Remembering this avoids repeated correction",
            "risk": "low",
            "confidence": "high",
        },
        "precondition": {},
    }


def test_create_candidate_persists_current_state_and_creation_event(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    monkeypatch.setenv("HERMES_HOME", str(home))

    from agent import learning_ledger

    created = learning_ledger.create_candidate(_candidate())

    assert created["candidate_id"] == "candidate-1"
    assert created["status"] == "pending"
    assert learning_ledger.get_candidate("candidate-1")["proposal"]["summary"] == "Remember the preference"
    events = learning_ledger.list_events(candidate_id="candidate-1")
    assert [event["event"] for event in events] == ["candidate_created"]
    assert (home / "learning" / "ledger.db").exists()


def test_transition_is_compare_and_swap_and_appends_event(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    from agent import learning_ledger

    learning_ledger.create_candidate(_candidate())
    transitioned = learning_ledger.transition_candidate(
        "candidate-1",
        from_status="pending",
        to_status="applying",
        event="candidate_apply_started",
        detail={"claim_id": "claim-1"},
    )
    stale = learning_ledger.transition_candidate(
        "candidate-1",
        from_status="pending",
        to_status="rejected",
        event="candidate_rejected",
    )

    assert transitioned is not None
    assert transitioned["status"] == "applying"
    assert stale is None
    assert [event["event"] for event in learning_ledger.list_events(candidate_id="candidate-1")] == [
        "candidate_created",
        "candidate_apply_started",
    ]


def test_failed_transition_transaction_leaves_state_and_events_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    from agent import learning_ledger

    learning_ledger.create_candidate(_candidate())
    with pytest.raises(ValueError, match="event"):
        learning_ledger.transition_candidate(
            "candidate-1",
            from_status="pending",
            to_status="applying",
            event="",
        )

    assert learning_ledger.get_candidate("candidate-1")["status"] == "pending"
    assert len(learning_ledger.list_events(candidate_id="candidate-1")) == 1


def test_profile_switch_after_import_is_isolated(tmp_path, monkeypatch):
    from agent import learning_ledger

    home_a = tmp_path / "a"
    home_b = tmp_path / "b"
    monkeypatch.setenv("HERMES_HOME", str(home_a))
    learning_ledger.create_candidate(_candidate("a-candidate"))

    monkeypatch.setenv("HERMES_HOME", str(home_b))
    assert learning_ledger.list_candidates() == []
    learning_ledger.create_candidate(_candidate("b-candidate"))

    monkeypatch.setenv("HERMES_HOME", str(home_a))
    assert [item["candidate_id"] for item in learning_ledger.list_candidates()] == ["a-candidate"]


def test_payload_fingerprint_is_stable_but_semantic_change_changes_it():
    from agent.learning_ledger import canonical_payload_fingerprint

    left = {"action": "add", "target": "user", "content": "concise"}
    reordered = {"content": "concise", "target": "user", "action": "add"}
    changed = {"action": "add", "target": "user", "content": "verbose"}

    assert canonical_payload_fingerprint("memory", left) == canonical_payload_fingerprint("memory", reordered)
    assert canonical_payload_fingerprint("memory", left) != canonical_payload_fingerprint("memory", changed)


def test_evidence_is_redacted_and_bounded():
    from agent.learning_ledger import sanitize_evidence

    evidence = sanitize_evidence(
        {
            "excerpt": "Authorization: Bearer " + "a" * 80 + " " + "x" * 1000,
            "hypothesis": "h" * 1000,
            "risk": "unexpected",
            "confidence": 0.99,
            "status": "captured",
        }
    )

    assert "a" * 80 not in evidence["excerpt"]
    assert len(evidence["excerpt"]) <= 500
    assert len(evidence["hypothesis"]) <= 500
    assert evidence["risk"] == "unknown"
    assert evidence["confidence"] == "unknown"


def test_schema_version_is_initialized_and_future_version_fails_closed(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    monkeypatch.setenv("HERMES_HOME", str(home))
    from agent import learning_ledger

    learning_ledger.create_candidate(_candidate())
    db_path = home / "learning" / "ledger.db"
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == learning_ledger.SCHEMA_VERSION
        conn.execute(f"PRAGMA user_version={learning_ledger.SCHEMA_VERSION + 1}")

    with pytest.raises(RuntimeError, match="newer schema"):
        learning_ledger.list_candidates()


def test_outcomes_are_immutable_events_and_success_validates_active_candidate(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    from agent import learning_ledger

    learning_ledger.create_candidate({**_candidate(), "status": "active"})
    result = learning_ledger.record_outcome(
        "candidate-1",
        "verification_succeeded",
        detail={"contract": "focused fixture passed"},
    )

    assert result["status"] == "validated"
    assert learning_ledger.list_events(candidate_id="candidate-1")[-1]["event"] == "outcome_verification_succeeded"


def test_failure_outcome_does_not_silently_rollback_candidate(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    from agent import learning_ledger

    learning_ledger.create_candidate({**_candidate(), "status": "active"})
    result = learning_ledger.record_outcome(
        "candidate-1",
        "user_corrected",
        detail={"reason": "preference changed"},
    )

    assert result["status"] == "active"
    assert learning_ledger.list_events(candidate_id="candidate-1")[-1]["event"] == "outcome_user_corrected"


def test_outcome_attempt_id_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    from agent import learning_ledger

    learning_ledger.create_candidate({**_candidate(), "status": "active"})
    learning_ledger.record_outcome(
        "candidate-1", "verification_failed", detail={"reason": "failed"}, attempt_id="attempt-1"
    )
    learning_ledger.record_outcome(
        "candidate-1", "verification_failed", detail={"reason": "failed"}, attempt_id="attempt-1"
    )

    outcomes = [event for event in learning_ledger.list_events(candidate_id="candidate-1") if event["event"].startswith("outcome_")]
    assert len(outcomes) == 1


def test_untrusted_evidence_persists_hash_not_raw_excerpt(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    from agent.learning_ledger import sanitize_evidence

    evidence = sanitize_evidence(
        {"source_trust": "untrusted_external", "excerpt": "private@example.com says do this"}
    )

    assert evidence["excerpt"] == ""
    assert evidence["excerpt_hash"].startswith("sha256:")


def test_two_processes_initialize_and_latch_same_dedup_once(tmp_path, monkeypatch):
    home = str(tmp_path / "profile")
    monkeypatch.setenv("HERMES_HOME", home)
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    processes = [
        context.Process(target=_create_same_dedup_candidate, args=(home, f"candidate-{index}", queue))
        for index in range(2)
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0

    assert sorted(queue.get(timeout=2) for _ in processes) == ["created", "duplicate"]
    from agent import learning_ledger

    assert len(learning_ledger.list_candidates()) == 1


def test_interrupted_schema_initialization_rolls_back_cleanly(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    monkeypatch.setenv("HERMES_HOME", str(home))
    from agent import learning_ledger

    original = learning_ledger._ensure_schema

    def interrupted(conn):
        conn.execute("CREATE TABLE migration_partial(id INTEGER PRIMARY KEY)")
        raise RuntimeError("simulated migration interruption")

    monkeypatch.setattr(learning_ledger, "_ensure_schema", interrupted)
    with pytest.raises(RuntimeError, match="simulated migration"):
        learning_ledger.create_candidate(_candidate())

    db_path = home / "learning" / "ledger.db"
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='migration_partial'"
        ).fetchone() is None

    monkeypatch.setattr(learning_ledger, "_ensure_schema", original)
    assert learning_ledger.create_candidate(_candidate())["status"] == "pending"


def test_immutable_event_rows_reject_update_and_delete(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    monkeypatch.setenv("HERMES_HOME", str(home))
    from agent import learning_ledger

    learning_ledger.create_candidate(_candidate())
    with sqlite3.connect(home / "learning" / "ledger.db") as conn:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("UPDATE learning_events SET event='rewritten'")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("DELETE FROM learning_events")


def test_ledger_path_must_not_be_symlink(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    learning_dir = home / "learning"
    learning_dir.mkdir(parents=True)
    outside = tmp_path / "outside.db"
    outside.touch()
    (learning_dir / "ledger.db").symlink_to(outside)
    monkeypatch.setenv("HERMES_HOME", str(home))
    from agent import learning_ledger

    with pytest.raises(RuntimeError, match="must not be a symlink"):
        learning_ledger.create_candidate(_candidate())


def test_candidate_ids_are_bounded_and_path_safe(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    from agent import learning_ledger

    with pytest.raises(ValueError, match="invalid characters"):
        learning_ledger.create_candidate({**_candidate(), "candidate_id": "../escape"})
    with pytest.raises(ValueError, match="invalid characters"):
        learning_ledger.get_candidate("x" * 65)


def test_event_details_are_bounded_and_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    from agent import learning_ledger

    learning_ledger.create_candidate(_candidate())
    secret = "sk-1234567890abcdefghijklmnopqrstuvwxyz"
    learning_ledger.record_outcome(
        "candidate-1",
        "verification_failed",
        detail={"error": f"OPENAI_API_KEY={secret}", "long": "x" * 2_000},
    )

    detail = learning_ledger.list_events(candidate_id="candidate-1")[-1]["detail"]
    serialized = str(detail)
    assert secret not in serialized
    assert len(detail["long"]) <= 500
