"""The durable inbox that carries one external activation into a stored session.

The cross-process behaviour -- who is entitled to consume, and when -- is proved
by ``scripts/probe_external_turn_route.py``, which races two real gateways.
These cover the storage contract that probe depends on, at the level where a
regression is cheap to find: identity, claiming, reaping, and the fact that a
row is never silently lost.
"""

import os

import pytest

from tools.session_external_turns import (
    CONSUMED,
    claim_external_turn,
    enqueue_external_turn,
    mark_external_turn_consumed,
    pending_external_turns,
    release_external_turn,
)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))


def enqueue(event_id="E1", key="S", body="done", source="delegate-wave"):
    return enqueue_external_turn(
        event_id=event_id, target_session_key=key, body=body, source=source
    )


def test_re_enqueueing_one_event_does_not_produce_two_turns():
    """The producer may not know whether its last attempt landed.

    A wake whose outcome was ambiguous gets re-sent, and it must not become two
    announcements of one thing. ``event_id`` is the producer's identity for the
    event, so idempotence is structural rather than a de-dup heuristic.
    """
    assert enqueue() is True
    assert enqueue() is False
    assert len(pending_external_turns("S")) == 1


def test_an_event_is_only_visible_to_its_target_session():
    enqueue(event_id="E1", key="S1")
    enqueue(event_id="E2", key="S2")
    assert [r["event_id"] for r in pending_external_turns("S1")] == ["E1"]
    assert [r["event_id"] for r in pending_external_turns("S2")] == ["E2"]


def test_a_live_claim_hides_the_row_from_everyone_else():
    """Mutual exclusion: two processes must not both run one event."""
    enqueue()
    assert claim_external_turn("E1") is True
    # Claimed by a process that is alive -- this one -- so nobody else may see it.
    assert pending_external_turns("S") == []
    assert claim_external_turn("E1") is False


def test_a_released_row_becomes_available_again():
    """The owner found itself busy after claiming, so the event goes back.

    This is the path that keeps a busy session from swallowing an event: the
    claim is provisional until a turn actually starts.
    """
    enqueue()
    assert claim_external_turn("E1") is True
    release_external_turn("E1", "session became busy")
    rows = pending_external_turns("S")
    assert [r["event_id"] for r in rows] == ["E1"]
    assert rows[0]["last_error"] == "session became busy"
    assert claim_external_turn("E1") is True


def test_a_dead_claimer_does_not_take_the_event_with_it():
    """A process killed mid-claim must not strand the person's notification.

    The producer believes it handed the event over and will not re-send it, so
    if this row stayed invisible the announcement would simply never arrive.
    """
    enqueue()
    assert claim_external_turn("E1") is True

    from tools.session_external_turns import _transaction

    with _transaction() as conn:
        conn.execute(
            "UPDATE session_external_turns SET owner_pid = ?, owner_started_at = ? WHERE event_id = ?",
            (0x7FFFFFFE, 1.0, "E1"),
        )
    assert [r["event_id"] for r in pending_external_turns("S")] == ["E1"]
    assert claim_external_turn("E1") is True


def test_a_recycled_pid_does_not_look_like_a_live_claimer():
    """Identity is (pid, start time). The number alone is reused."""
    enqueue()
    assert claim_external_turn("E1") is True

    from tools.session_external_turns import _transaction

    with _transaction() as conn:
        conn.execute(
            "UPDATE session_external_turns SET owner_pid = ?, owner_started_at = ? WHERE event_id = ?",
            (os.getpid(), 1.0, "E1"),  # our pid, but not when we started
        )
    assert [r["event_id"] for r in pending_external_turns("S")] == ["E1"]


def test_a_consumed_event_is_finished_for_good():
    enqueue()
    claim_external_turn("E1")
    mark_external_turn_consumed("E1")
    assert pending_external_turns("S") == []
    assert claim_external_turn("E1") is False
    # And re-enqueueing the same id cannot resurrect it.
    assert enqueue() is False
    assert pending_external_turns("S") == []


def test_events_are_offered_oldest_first():
    enqueue(event_id="first")
    enqueue(event_id="second")
    assert [r["event_id"] for r in pending_external_turns("S")] == ["first", "second"]


def test_an_event_must_name_both_itself_and_its_target():
    with pytest.raises(ValueError):
        enqueue_external_turn(event_id="", target_session_key="S", body="x", source="dw")
    with pytest.raises(ValueError):
        enqueue_external_turn(event_id="E", target_session_key="", body="x", source="dw")


def test_the_row_carries_what_the_consumer_needs():
    enqueue(body="done - fixed the run filter", source="delegate-wave")
    row = pending_external_turns("S")[0]
    assert row["body"] == "done - fixed the run filter"
    assert row["source"] == "delegate-wave"
    assert row["state"] != CONSUMED
