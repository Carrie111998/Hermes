"""Durable interrupted-turn records in state.db.

A turn that starts running and never reaches a terminal frame leaves a row in
``interrupted_turns``; ``session.resume`` reads it to decide whether to continue
the interrupted prompt. These records used to live in a JSON sidecar that every
process rewrote in full and any process could delete. The properties pinned
here are the ones the file could not have:

* a write touches one conversation's row and no other's, even from a second
  process handle;
* only the process that recorded a turn may retire it, so a process that never
  ran the turn cannot delete the record of one that is still running;
* records imported from the legacy file carry no owner and stay retirable by
  anyone, and their keys are resolved to the compression-lineage root the table
  is keyed on.
"""

from __future__ import annotations

import os
import time

from hermes_state import SessionDB


def _owner(tag: str) -> str:
    return f"pid={os.getpid()}:platform={tag}"


def test_interrupted_turn_roundtrip(tmp_path):
    db = SessionDB(tmp_path / "state.db")

    assert db.record_interrupted_turn(
        "abc", "fix the bug", attempts=1, owner=_owner("a")
    )

    record = db.read_interrupted_turn("abc")
    assert record is not None
    assert record["prompt"] == "fix the bug"
    assert record["attempts"] == 1
    assert record["owner"] == _owner("a")
    assert abs(record["started_at"] - time.time()) < 5

    assert db.clear_interrupted_turn("abc", owner=_owner("a"))
    assert db.read_interrupted_turn("abc") is None


def test_empty_prompt_records_nothing(tmp_path):
    db = SessionDB(tmp_path / "state.db")

    assert not db.record_interrupted_turn("abc", "   ", owner=_owner("a"))
    assert not db.record_interrupted_turn("", "prompt", owner=_owner("a"))
    assert db.read_interrupted_turn("abc") is None


def test_foreign_owner_cannot_retire_a_live_record(tmp_path):
    """The record of a running turn survives another process retiring it.

    This is the lease-timeout path: a second process submits on a conversation
    the first is already running, its engine waits for the turn lease, times
    out, and the gateway retires the record as it emits the terminal error
    frame. The turn it retired belongs to someone else.
    """
    path = tmp_path / "state.db"
    running = SessionDB(path)
    timing_out = SessionDB(path)

    running.record_interrupted_turn(
        "conv", "the turn that is actually running", owner=_owner("running")
    )

    assert not timing_out.clear_interrupted_turn("conv", owner=_owner("timing-out"))

    survivor = running.read_interrupted_turn("conv")
    assert survivor is not None
    assert survivor["prompt"] == "the turn that is actually running"
    assert running.clear_interrupted_turn("conv", owner=_owner("running"))


def test_write_does_not_clobber_another_conversation(tmp_path):
    """Two handles recording different conversations both keep their record.

    The sidecar loaded the whole map, changed one key and stored the map back,
    so a write for one conversation could drop a record for another. A row is
    a row.
    """
    path = tmp_path / "state.db"
    first = SessionDB(path)
    second = SessionDB(path)

    first.record_interrupted_turn("conv-a", "prompt for A", owner=_owner("first"))
    second.record_interrupted_turn("conv-b", "prompt for B", owner=_owner("second"))

    assert first.read_interrupted_turn("conv-a")["prompt"] == "prompt for A"
    assert first.read_interrupted_turn("conv-b")["prompt"] == "prompt for B"


def test_recording_takes_ownership_of_the_row(tmp_path):
    """A new turn's record replaces the spent one and carries the new owner.

    A turn only starts after the previous one ended, so the row it replaces
    describes a turn that is over — usually one whose process died, which is
    exactly the case whose attempts counter has to keep advancing for the
    crash-loop breaker to work.
    """
    path = tmp_path / "state.db"
    crashed = SessionDB(path)
    restarted = SessionDB(path)

    crashed.record_interrupted_turn(
        "conv", "the interrupted prompt", attempts=0, owner=_owner("crashed")
    )
    assert restarted.record_interrupted_turn(
        "conv", "the interrupted prompt", attempts=1, owner=_owner("restarted")
    )

    record = restarted.read_interrupted_turn("conv")
    assert record["attempts"] == 1
    assert record["owner"] == _owner("restarted")
    # The dead process's owner string no longer retires it; the live one does.
    assert not restarted.clear_interrupted_turn("conv", owner=_owner("crashed"))
    assert restarted.clear_interrupted_turn("conv", owner=_owner("restarted"))


def test_force_retires_regardless_of_owner(tmp_path):
    """The scheduler's policy deletion: past every window, actionable by none."""
    db = SessionDB(tmp_path / "state.db")
    db.record_interrupted_turn("conv", "stale prompt", owner=_owner("someone-else"))

    assert db.clear_interrupted_turn("conv", owner=_owner("scheduler"), force=True)
    assert db.read_interrupted_turn("conv") is None


def test_imported_record_has_no_owner_and_stays_retirable(tmp_path):
    """Legacy records never recorded an owner, so nobody is locked out of them."""
    db = SessionDB(tmp_path / "state.db")

    assert db.import_interrupted_turns(
        [("conv", {"prompt": "legacy prompt", "attempts": 1, "started_at": time.time()})]
    ) == 1

    record = db.read_interrupted_turn("conv")
    assert record["prompt"] == "legacy prompt"
    assert record["attempts"] == 1
    assert record["owner"] is None
    assert record["cause"] == "migrated"

    assert db.clear_interrupted_turn("conv", owner=_owner("any-process"))
    assert db.read_interrupted_turn("conv") is None


def test_import_does_not_overwrite_a_live_record(tmp_path):
    """A row written by a running process is newer than anything being imported."""
    db = SessionDB(tmp_path / "state.db")
    db.record_interrupted_turn("conv", "live prompt", owner=_owner("live"))

    assert db.import_interrupted_turns(
        [("conv", {"prompt": "legacy prompt", "started_at": time.time() - 60})]
    ) == 0

    record = db.read_interrupted_turn("conv")
    assert record["prompt"] == "live prompt"
    assert record["owner"] == _owner("live")


def test_records_are_keyed_on_the_conversation_root(tmp_path):
    """Compression segments share one record, as they share one turn lease."""
    db = SessionDB(tmp_path / "state.db")
    db.create_session("root", source="test")
    db.end_session("root", "compression")
    db.create_session("child", source="test", parent_session_id="root")

    db.record_interrupted_turn("root", "prompt before rotation", owner=_owner("a"))

    # The resume after the rotation looks the record up under the child id.
    record = db.read_interrupted_turn("child")
    assert record is not None
    assert record["prompt"] == "prompt before rotation"


def test_import_translates_a_segment_key_to_the_root(tmp_path):
    """Legacy records were filed under the segment, not the lineage root.

    Without the translation a record written before a rotation would import to
    a row no post-rotation resume ever reads.
    """
    db = SessionDB(tmp_path / "state.db")
    db.create_session("root", source="test")
    db.end_session("root", "compression")
    db.create_session("child", source="test", parent_session_id="root")

    assert db.import_interrupted_turns(
        [(
            "root",
            {"prompt": "prompt from before the rotation", "started_at": time.time()},
        )]
    ) == 1

    record = db.read_interrupted_turn("child")
    assert record is not None
    assert record["prompt"] == "prompt from before the rotation"
    assert record["owner"] is None


def test_two_legacy_segments_collapse_to_one_row_newest_first(tmp_path):
    """Both segments of a rotated conversation resolve to the same row."""
    db = SessionDB(tmp_path / "state.db")
    db.create_session("root", source="test")
    db.end_session("root", "compression")
    db.create_session("child", source="test", parent_session_id="root")
    now = time.time()

    written = db.import_interrupted_turns([
        ("child", {"prompt": "after the rotation", "started_at": now}),
        ("root", {"prompt": "before the rotation", "started_at": now - 600}),
    ])

    assert written == 1
    assert db.read_interrupted_turn("child")["prompt"] == "after the rotation"


def test_records_older_than_a_day_are_swept_on_write(tmp_path):
    """Same bound the sidecar enforced on every write."""
    db = SessionDB(tmp_path / "state.db")
    db.import_interrupted_turns([
        ("ancient", {"prompt": "two days ago", "started_at": time.time() - 48 * 3600}),
        ("recent", {"prompt": "an hour ago", "started_at": time.time() - 3600}),
    ])
    assert db.read_interrupted_turn("ancient") is not None

    db.record_interrupted_turn("fresh", "now", owner=_owner("a"))

    assert db.read_interrupted_turn("ancient") is None
    assert db.read_interrupted_turn("recent") is not None
    assert db.read_interrupted_turn("fresh") is not None


def test_missing_record_reads_as_none(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    assert db.read_interrupted_turn("nobody") is None
    assert not db.clear_interrupted_turn("nobody", owner=_owner("a"))


def test_two_racing_claimants_produce_exactly_one_winner(tmp_path):
    """Two processes that both find one orphan: one runs it, one stands down.

    Both read the record before either writes, which is the race a crash on a
    shared HERMES_HOME produces when two clients resume the same conversation
    at once. The claim is a compare-and-swap on the record they read, so the
    second update matches no row.
    """
    path = tmp_path / "state.db"
    first, second = SessionDB(path), SessionDB(path)
    first.record_interrupted_turn("shared", "the interrupted prompt", owner=_owner("a"))

    seen_first = first.read_interrupted_turn("shared")
    seen_second = second.read_interrupted_turn("shared")
    claimed_first = first.claim_interrupted_turn("shared", expected=seen_first)
    claimed_second = second.claim_interrupted_turn("shared", expected=seen_second)

    assert claimed_first is not None
    assert claimed_first["prompt"] == "the interrupted prompt"
    assert claimed_second is None
    # The loser leaves the record exactly as the winner left it.
    survivor = second.read_interrupted_turn("shared")
    assert survivor["attempts"] == 1


def test_a_claim_bumps_the_attempt_count_durably(tmp_path):
    """The crash-loop ceiling has to advance before the caller does anything."""
    db = SessionDB(tmp_path / "state.db")
    db.record_interrupted_turn("abc", "crashy prompt", attempts=1, owner=_owner("a"))

    claimed = db.claim_interrupted_turn(
        "abc", expected=db.read_interrupted_turn("abc")
    )

    assert claimed["attempts"] == 2
    assert db.read_interrupted_turn("abc")["attempts"] == 2


def test_a_claim_against_a_record_that_moved_on_abstains(tmp_path):
    """The owning process re-recorded it, so it is a live turn, not an orphan."""
    db = SessionDB(tmp_path / "state.db")
    db.record_interrupted_turn("abc", "prompt", attempts=1, owner=_owner("a"))
    stale = dict(db.read_interrupted_turn("abc"), attempts=0)

    assert db.claim_interrupted_turn("abc", expected=stale) is None
    assert db.read_interrupted_turn("abc")["attempts"] == 1
    assert db.read_interrupted_turn("abc")["owner"] == _owner("a")


def test_a_re_record_between_read_and_claim_defeats_the_claim(tmp_path):
    """The ABA the counter alone cannot see: same attempts, different turn.

    ``record_interrupted_turn`` is last-writer-wins and stamps ``attempts=0``
    on every user-initiated turn, so the value a resuming process read off an
    orphan recurs the moment somebody starts a *new* turn on the same
    conversation. The prologue that writes that row runs long before the
    engine takes the turn lease, so "fresh record, no lease yet" is an
    ordinary state and the lease peek reads it as free. Were the counter the
    whole token, this claim would win and the resuming process would
    auto-continue a prompt another process is running right now.
    """
    path = tmp_path / "state.db"
    resuming, live = SessionDB(path), SessionDB(path)
    resuming.record_interrupted_turn(
        "shared", "the orphaned prompt", attempts=0, owner=_owner("crashed")
    )

    gate_read = resuming.read_interrupted_turn("shared")
    assert gate_read["attempts"] == 0
    # The other process starts a live user turn. Same counter value, new turn.
    time.sleep(0.02)
    live.record_interrupted_turn(
        "shared", "the prompt that is running right now", attempts=0, owner=_owner("live")
    )
    assert live.read_interrupted_turn("shared")["attempts"] == 0

    assert resuming.claim_interrupted_turn("shared", expected=gate_read) is None
    survivor = live.read_interrupted_turn("shared")
    assert survivor["prompt"] == "the prompt that is running right now"
    assert survivor["attempts"] == 0
    assert survivor["owner"] == _owner("live")


def test_a_re_record_of_the_same_prompt_by_the_same_owner_defeats_the_claim(tmp_path):
    """The narrow leg: only ``started_at`` moved, and that is enough.

    A process that crashed and came back re-runs its own interrupted prompt
    under its own owner stamp. Attempts, prompt and owner all match what the
    resuming process read; the write is visible only in the timestamp, which
    ``record_interrupted_turn`` stamps fresh on every write.
    """
    path = tmp_path / "state.db"
    resuming, live = SessionDB(path), SessionDB(path)
    resuming.record_interrupted_turn("shared", "same prompt", owner=_owner("same"))

    gate_read = resuming.read_interrupted_turn("shared")
    time.sleep(0.02)
    live.record_interrupted_turn("shared", "same prompt", owner=_owner("same"))
    fresh = live.read_interrupted_turn("shared")
    assert fresh["attempts"] == gate_read["attempts"]
    assert fresh["prompt"] == gate_read["prompt"]
    assert fresh["owner"] == gate_read["owner"]
    assert fresh["started_at"] > gate_read["started_at"]

    assert resuming.claim_interrupted_turn("shared", expected=gate_read) is None
    assert live.read_interrupted_turn("shared")["attempts"] == 0


def test_a_claim_leaves_the_true_owner_able_to_retire_its_record(tmp_path):
    """The claim must not take the stamp the retire check reads.

    A claim can land on a record whose owner is alive — the row it matched may
    have been re-recorded by a turn that is running, or written by a process
    whose lease lapsed while it kept working. If the claim restamped ``owner``,
    that process could never retire its own record when its turn concluded, and
    the record of a finished turn would sit there for a later resume to re-run.
    """
    path = tmp_path / "state.db"
    running, claimant = SessionDB(path), SessionDB(path)
    running.record_interrupted_turn("shared", "the live prompt", owner=_owner("running"))

    claimed = claimant.claim_interrupted_turn(
        "shared", expected=claimant.read_interrupted_turn("shared")
    )

    assert claimed is not None
    assert claimed["owner"] == _owner("running")
    # The turn concludes in the process that actually ran it.
    assert running.clear_interrupted_turn("shared", owner=_owner("running"))
    assert running.read_interrupted_turn("shared") is None


def test_a_claim_leaves_the_cause_column_alone(tmp_path):
    """``cause`` describes where the record came from, not who holds it."""
    db = SessionDB(tmp_path / "state.db")
    db.import_interrupted_turns([("abc", {"prompt": "legacy", "attempts": 0, "started_at": time.time()})])
    assert db.read_interrupted_turn("abc")["cause"] == "migrated"

    claimed = db.claim_interrupted_turn(
        "abc", expected=db.read_interrupted_turn("abc")
    )

    assert claimed["cause"] == "migrated"


def test_a_claim_against_a_retired_record_abstains(tmp_path):
    db = SessionDB(tmp_path / "state.db")

    assert (
        db.claim_interrupted_turn(
            "abc",
            expected={"attempts": 0, "started_at": time.time(), "prompt": "p", "owner": None},
        )
        is None
    )


def test_a_claim_resolves_the_compression_lineage_root(tmp_path):
    """Same key space as the turn lease, so a rotation cannot hide a record."""
    db = SessionDB(tmp_path / "state.db")
    db.create_session("root", source="test")
    db.end_session("root", "compression")
    db.create_session("child", source="test", parent_session_id="root")
    db.record_interrupted_turn("root", "recorded before the rotation", owner=None)

    claimed = db.claim_interrupted_turn(
        "child", expected=db.read_interrupted_turn("child")
    )

    assert claimed["prompt"] == "recorded before the rotation"
