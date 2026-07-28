"""Worker alerts must survive a crash between queueing and delivery.

``BaseAdapter.handle_message`` documents that it "returns quickly by spawning
background tasks" — returning means the event entered an in-memory queue, not
that anyone received it. The watcher advanced the durable cursor on the very
next line, and ``collect_new_transitions`` only ever reads events *newer* than
the cursor, so a crash in that window lost the alert permanently: a failed
worker that nobody was ever told about.

The fix is a durable hand-off record written *before* the cursor moves, plus a
startup replay. These tests pin the ordering, the crash recovery, and the
absence of false replays.
"""

from __future__ import annotations

import json

import pytest

from gateway.worker_bridge_watchers import (
    PENDING_TEXT_MAX,
    clear_pending_injection,
    load_cursor,
    load_pending_injection,
    save_cursor,
    save_pending_injection,
)


@pytest.fixture
def state_file(tmp_path):
    return tmp_path / "gateway_alerts_cursor.json"


# ── the durable record ───────────────────────────────────────────────────────

def test_no_pending_record_on_a_fresh_state_file(state_file):
    assert load_pending_injection(state_file) is None


def test_pending_record_round_trips(state_file):
    save_pending_injection(state_file, 4242, "worker task-9 FAILED")
    pending = load_pending_injection(state_file)
    assert pending is not None
    assert pending["head"] == 4242
    assert pending["text"] == "worker task-9 FAILED"


def test_pending_record_coexists_with_the_cursor(state_file):
    """Both live in one atomically-written payload; neither may clobber the other."""
    save_cursor(state_file, 100)
    save_pending_injection(state_file, 150, "alert")
    assert load_cursor(state_file) == 100
    assert load_pending_injection(state_file)["head"] == 150

    save_cursor(state_file, 150)
    assert load_pending_injection(state_file) is not None, (
        "advancing the cursor must not erase an unconfirmed hand-off"
    )
    assert load_cursor(state_file) == 150


def test_clear_removes_only_the_pending_record(state_file):
    save_cursor(state_file, 7)
    save_pending_injection(state_file, 9, "alert")
    clear_pending_injection(state_file)
    assert load_pending_injection(state_file) is None
    assert load_cursor(state_file) == 7, "clearing must not disturb the cursor"


def test_clear_is_idempotent(state_file):
    save_cursor(state_file, 3)
    clear_pending_injection(state_file)
    clear_pending_injection(state_file)
    assert load_cursor(state_file) == 3


def test_oversized_alert_text_is_bounded(state_file):
    save_pending_injection(state_file, 1, "x" * (PENDING_TEXT_MAX * 3))
    assert len(load_pending_injection(state_file)["text"]) == PENDING_TEXT_MAX


# ── malformed records must not resurrect a bogus alert ───────────────────────

@pytest.mark.parametrize("bad", [
    {"pending_injection": "not-a-dict"},
    {"pending_injection": {}},
    {"pending_injection": {"head": 5}},                 # no text
    {"pending_injection": {"head": 5, "text": ""}},     # empty text
    {"pending_injection": {"head": 5, "text": "   "}},  # whitespace only
])
def test_malformed_pending_records_are_ignored(state_file, bad):
    state_file.write_text(json.dumps(bad), encoding="utf-8")
    assert load_pending_injection(state_file) is None


def test_unreadable_state_file_is_not_fatal(state_file):
    state_file.write_text("{ this is not json", encoding="utf-8")
    assert load_pending_injection(state_file) is None
    assert load_cursor(state_file) is None


# ── the crash window this exists to close ────────────────────────────────────

def test_crash_between_queueing_and_delivery_leaves_the_alert_recoverable(state_file):
    """Simulate the exact sequence, cut short at the crash point."""
    save_cursor(state_file, 1000)

    # watcher: persist hand-off, queue the event... then the process dies.
    save_pending_injection(state_file, 1050, "task-42 timed_out")
    save_cursor(state_file, 1050)
    # <-- crash here: handle_message never delivered

    # restart: the cursor has moved past the event, so the ONLY way the alert
    # is still recoverable is the hand-off record.
    assert load_cursor(state_file) == 1050
    recovered = load_pending_injection(state_file)
    assert recovered is not None, "the alert would be lost forever"
    assert recovered["text"] == "task-42 timed_out"
    assert recovered["head"] == 1050


def test_clean_cycle_leaves_nothing_to_replay(state_file):
    """After a confirmed replay there must be no duplicate on the next start."""
    save_pending_injection(state_file, 10, "alert")
    save_cursor(state_file, 10)
    clear_pending_injection(state_file)          # replay issued
    assert load_pending_injection(state_file) is None


def test_write_is_atomic_leaving_no_temp_file(state_file):
    save_pending_injection(state_file, 1, "alert")
    leftovers = list(state_file.parent.glob("*.tmp"))
    assert leftovers == [], f"atomic write left temp files behind: {leftovers}"


# ── the ordering contract in the watcher source ──────────────────────────────

def test_handoff_is_persisted_before_the_cursor_advances():
    """Pin the order: record, then queue, then advance.

    Reordering these three lines silently reopens the loss window, and no
    behavioural test can see it because handle_message reports success either
    way.
    """
    import inspect

    from gateway.worker_bridge_watchers import GatewayWorkerBridgeWatchersMixin

    src = inspect.getsource(GatewayWorkerBridgeWatchersMixin._worker_bridge_tick)
    save_at = src.index("save_pending_injection")
    queue_at = src.index("adapter.handle_message")
    cursor_at = src.rindex("save_cursor")
    assert save_at < queue_at < cursor_at, (
        "hand-off must be persisted before handle_message, and the cursor must "
        "advance only after both"
    )
