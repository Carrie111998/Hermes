from __future__ import annotations

import threading

import pytest

from agent.session_contracts import (
    SessionAuthorization,
    SessionAuthorizationError,
    StaleSessionRevisionError,
    TurnCommand,
    TurnIdempotencyConflictError,
    TurnLeaseConflictError,
    TurnState,
)
from hermes_state import SessionDB


@pytest.fixture
def journal(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("session-1", "desktop")
    db.create_session("foreign-session", "desktop")
    authorization = SessionAuthorization(
        principal="desktop:conversation-1",
        allowed_session_ids=frozenset({"session-1"}),
    )
    return db, authorization


def _command(*, revision: int, content: str = "Remember OLIVE-42.") -> TurnCommand:
    return TurnCommand(
        session_id="session-1",
        turn_id="turn-1",
        idempotency_key="desktop-delivery-1",
        expected_revision=revision,
        user_event={"role": "user", "content": content},
    )


def test_append_turn_is_durable_idempotent_and_snapshot_addressable(journal) -> None:
    db, authorization = journal

    first = db.append_turn(_command(revision=0), authorization=authorization)
    replay = db.append_turn(_command(revision=0), authorization=authorization)
    snapshot = db.read_session_snapshot("session-1", authorization=authorization)

    assert first.appended is True
    assert replay.appended is False
    assert replay.event_id == first.event_id
    assert replay.event_revision == first.event_revision
    assert snapshot.revision == first.revision
    assert [event.event_id for event in snapshot.events] == [first.event_id]
    assert snapshot.events[0].message["content"] == "Remember OLIVE-42."


def test_stale_revision_and_idempotency_conflicts_do_not_append(journal) -> None:
    db, authorization = journal
    first = db.append_turn(_command(revision=0), authorization=authorization)

    with pytest.raises(StaleSessionRevisionError):
        db.append_turn(
            TurnCommand(
                session_id="session-1",
                turn_id="turn-2",
                idempotency_key="desktop-delivery-2",
                expected_revision=0,
                user_event={"role": "user", "content": "This is stale."},
            ),
            authorization=authorization,
        )
    with pytest.raises(TurnIdempotencyConflictError):
        db.append_turn(
            _command(revision=first.revision, content="Different payload."),
            authorization=authorization,
        )

    snapshot = db.read_session_snapshot("session-1", authorization=authorization)
    assert len(snapshot.events) == 1


def test_authorization_rejects_foreign_session_before_read_or_append(journal) -> None:
    db, authorization = journal

    with pytest.raises(SessionAuthorizationError):
        db.read_session_snapshot("foreign-session", authorization=authorization)
    with pytest.raises(SessionAuthorizationError):
        db.append_turn(
            TurnCommand(
                session_id="foreign-session",
                turn_id="turn-foreign",
                idempotency_key="delivery-foreign",
                expected_revision=0,
                user_event={"role": "user", "content": "Do not append."},
            ),
            authorization=authorization,
        )

    assert db.get_messages_as_conversation("foreign-session") == []


def test_snapshot_survives_new_session_db_process_object(journal) -> None:
    db, authorization = journal
    receipt = db.append_turn(_command(revision=0), authorization=authorization)

    reopened = SessionDB(db_path=db.db_path)
    snapshot = reopened.read_session_snapshot(
        "session-1",
        authorization=authorization,
    )

    assert snapshot.revision == receipt.revision
    assert snapshot.events[0].event_id == receipt.event_id


def test_turn_execution_lease_serializes_and_terminal_replay_is_stable(
    journal,
) -> None:
    db, authorization = journal
    accepted = db.append_turn(_command(revision=0), authorization=authorization)
    lease = db.claim_turn_execution(
        "session-1",
        "turn-1",
        owner_id="gateway-process-1",
        lease_seconds=60,
        authorization=authorization,
    )

    with pytest.raises(TurnLeaseConflictError):
        db.claim_turn_execution(
            "session-1",
            "turn-1",
            owner_id="gateway-process-2",
            lease_seconds=60,
            authorization=authorization,
        )

    renewed = db.renew_turn_execution(
        lease,
        lease_seconds=120,
        authorization=authorization,
    )
    assert renewed.attempt == 1
    assert renewed.lease_expires_at > lease.lease_expires_at

    db.append_message("session-1", "assistant", "The code is OLIVE-42.")
    completed = db.finish_turn_execution(
        renewed,
        state=TurnState.COMPLETED,
        authorization=authorization,
    )
    replay = db.append_turn(_command(revision=0), authorization=authorization)

    assert completed.state is TurnState.COMPLETED
    assert completed.event_revision == accepted.event_revision
    assert completed.terminal_revision == completed.session_revision
    assert completed.session_revision > completed.event_revision
    assert replay.state is TurnState.COMPLETED
    assert replay.event_revision == accepted.event_revision
    assert replay.session_revision == completed.session_revision
    assert replay.appended is False


def test_expired_turn_execution_lease_can_be_reclaimed(
    journal, monkeypatch
) -> None:
    db, authorization = journal
    db.append_turn(_command(revision=0), authorization=authorization)
    monkeypatch.setattr("hermes_state.time.time", lambda: 100.0)
    first = db.claim_turn_execution(
        "session-1",
        "turn-1",
        owner_id="dead-process",
        lease_seconds=10,
        authorization=authorization,
    )
    monkeypatch.setattr("hermes_state.time.time", lambda: 111.0)
    recovered = db.claim_turn_execution(
        "session-1",
        "turn-1",
        owner_id="replacement-process",
        lease_seconds=10,
        authorization=authorization,
    )

    assert first.attempt == 1
    assert recovered.attempt == 2
    with pytest.raises(TurnLeaseConflictError):
        db.finish_turn_execution(
            first,
            state=TurnState.FAILED,
            authorization=authorization,
        )


def test_concurrent_duplicate_delivery_appends_exactly_one_user_event(
    journal,
) -> None:
    db, authorization = journal
    peer = SessionDB(db_path=db.db_path)
    barrier = threading.Barrier(2)
    receipts = []
    failures = []

    def deliver(store):
        try:
            barrier.wait(timeout=2)
            receipts.append(
                store.append_turn(_command(revision=0), authorization=authorization)
            )
        except Exception as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    first_thread = threading.Thread(target=deliver, args=(db,))
    second_thread = threading.Thread(target=deliver, args=(peer,))
    first_thread.start()
    second_thread.start()
    first_thread.join(timeout=5)
    second_thread.join(timeout=5)

    assert failures == []
    assert sorted(receipt.appended for receipt in receipts) == [False, True]
    snapshot = db.read_session_snapshot("session-1", authorization=authorization)
    assert len(snapshot.events) == 1
    assert {receipt.event_id for receipt in receipts} == {snapshot.events[0].event_id}
    peer.close()


def test_projection_rewrites_preserve_command_identity_and_monotonic_revision(
    journal,
) -> None:
    db, authorization = journal
    accepted = db.append_turn(_command(revision=0), authorization=authorization)
    db.append_message("session-1", "assistant", "The code is OLIVE-42.")
    before = db.read_session_snapshot("session-1", authorization=authorization)

    db.replace_messages(
        "session-1",
        [{"role": "user", "content": "Rewritten projection."}],
    )
    after = db.read_session_snapshot("session-1", authorization=authorization)
    replay = db.append_turn(_command(revision=0), authorization=authorization)
    all_rows = db.get_messages("session-1", include_inactive=True)

    assert after.revision > before.revision
    assert [event.message["content"] for event in after.events] == [
        "Rewritten projection."
    ]
    assert after.events[0].source_event_ids == tuple(
        event.event_id for event in before.events
    )
    assert replay.event_id == accepted.event_id
    assert replay.event_revision == accepted.event_revision
    assert replay.session_revision == after.revision
    assert any(row["id"] == accepted.projection_row_id for row in all_rows)


def test_snapshot_rebuilds_from_projection_journal_not_active_flags(journal) -> None:
    db, authorization = journal
    db.append_message("session-1", "user", "Original")
    db.archive_and_compact(
        "session-1",
        [{"role": "assistant", "content": "Derived checkpoint"}],
    )
    expected = db.read_session_snapshot("session-1", authorization=authorization)

    # Corrupt only the mutable materialized flags. The authoritative projection
    # journal must still rebuild the same snapshot.
    db._conn.execute(
        "UPDATE messages SET active = CASE WHEN active = 1 THEN 0 ELSE 1 END "
        "WHERE session_id = ?",
        ("session-1",),
    )
    rebuilt = db.read_session_snapshot("session-1", authorization=authorization)

    assert rebuilt == expected
    assert rebuilt.events[0].source_event_ids


def test_tool_execution_result_replays_without_redispatch(journal) -> None:
    db, authorization = journal
    db.append_turn(_command(revision=0), authorization=authorization)
    db.claim_turn_execution(
        "session-1",
        "turn-1",
        owner_id="gateway",
        lease_seconds=60,
        authorization=authorization,
    )
    claim = db.begin_tool_execution(
        "session-1",
        "turn-1",
        "call-1",
        "terminal",
        {"command": "touch marker"},
        may_have_side_effect=True,
    )
    completed = db.complete_tool_execution(claim, "created marker")
    replay = db.begin_tool_execution(
        "session-1",
        "turn-1",
        "call-1",
        "terminal",
        {"command": "touch marker"},
        may_have_side_effect=True,
    )

    assert completed.result == "created marker"
    assert replay.execute is False
    assert replay.result == "created marker"
    assert replay.attempt == 1


def test_abandoned_effectful_tool_becomes_uncertain_not_reexecuted(journal) -> None:
    db, authorization = journal
    db.append_turn(_command(revision=0), authorization=authorization)
    db.claim_turn_execution(
        "session-1",
        "turn-1",
        owner_id="dead-gateway",
        lease_seconds=60,
        authorization=authorization,
    )
    first = db.begin_tool_execution(
        "session-1",
        "turn-1",
        "call-unknown",
        "send_email",
        {"to": "operator@example.invalid"},
        may_have_side_effect=True,
    )
    recovered = db.begin_tool_execution(
        "session-1",
        "turn-1",
        "call-unknown",
        "send_email",
        {"to": "operator@example.invalid"},
        may_have_side_effect=True,
    )

    assert first.execute is True
    assert recovered.execute is False
    assert recovered.state.value == "uncertain"
    assert '"status":"uncertain"' in recovered.result


def test_abandoned_no_effect_tool_is_retryable(journal) -> None:
    db, authorization = journal
    db.append_turn(_command(revision=0), authorization=authorization)
    db.claim_turn_execution(
        "session-1",
        "turn-1",
        owner_id="gateway",
        lease_seconds=60,
        authorization=authorization,
    )
    first = db.begin_tool_execution(
        "session-1",
        "turn-1",
        "call-read",
        "read_file",
        {"path": "README.md"},
        may_have_side_effect=False,
    )
    retry = db.begin_tool_execution(
        "session-1",
        "turn-1",
        "call-read",
        "read_file",
        {"path": "README.md"},
        may_have_side_effect=False,
    )

    assert first.execute is True
    assert retry.execute is True
    assert retry.attempt == 2
