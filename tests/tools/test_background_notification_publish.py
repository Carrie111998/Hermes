"""Producer API + durable-sweep + stream-contract tests for async_delegation.

Covers the supported plugin-facing persist+wake rail
(``publish_background_notification``), the live durable-truth sweep that
recovers lost queue wakes without a process restart
(``sweep_undelivered_completions``), and the opt-in stream ordering /
supersession contract enforced at the claim chokepoint.

Background (2026-08-13 production incident): two durable pending rows sat
undelivered for ~21h because the only durable restore ran in the
ProcessRegistry constructor, and the restart then replayed them AFTER their
stream's terminal event with a formatter claiming "0s ago".
"""

import os
import queue
import time
from types import SimpleNamespace

import pytest

from tools import async_delegation as ad


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Fresh HERMES_HOME state.db and an isolated wake queue per test."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    import tools.process_registry as pr_module

    wake_queue = queue.Queue()
    monkeypatch.setattr(
        pr_module, "process_registry", SimpleNamespace(completion_queue=wake_queue)
    )
    yield wake_queue
    ad._reset_for_tests()


def _publish(notification_id, **kwargs):
    defaults = dict(
        summary=f"update for {notification_id}",
        session_key="agent:main:telegram:dm:1:2",
        title="Ride update",
        notification_id=notification_id,
    )
    defaults.update(kwargs)
    return ad.publish_background_notification(**defaults)


def _row(notification_id):
    return ad.get_durable_delegation(notification_id)


def _drain(q):
    items = []
    while not q.empty():
        items.append(q.get_nowait())
    return items


# ---------------------------------------------------------------------------
# Producer API
# ---------------------------------------------------------------------------


def test_publish_persists_pending_row_and_wakes_queue(_isolated):
    result = _publish("notif_a")
    assert result == {"status": "published", "notification_id": "notif_a"}

    row = _row("notif_a")
    assert row["delivery_state"] == "pending"
    assert row["delivery_attempts"] == 0
    assert row["result"]["summary"] == "update for notif_a"

    events = _drain(_isolated)
    assert len(events) == 1
    evt = events[0]
    assert evt["type"] == "async_delegation"
    assert evt["kind"] == "notification"
    assert evt["delegation_id"] == "notif_a"
    # Live wake — NOT a durable replay; owning-session drains may consume it.
    assert "restored" not in evt


def test_publish_validates_inputs(_isolated):
    with pytest.raises(ValueError):
        ad.publish_background_notification(summary="  ")
    with pytest.raises(ValueError):
        ad.publish_background_notification(summary="x", stream_id="s")
    with pytest.raises(ValueError):
        ad.publish_background_notification(summary="x", sequence=1)
    with pytest.raises(ValueError):
        ad.publish_background_notification(summary="x", supersedes_before_sequence=1)


def test_publish_is_idempotent_per_notification_id(_isolated):
    _publish("notif_idem")
    republished = _publish("notif_idem")
    assert republished["status"] == "republished"
    # Re-publishing a pending id refreshes the wake without a second row or
    # burned attempts.
    assert _row("notif_idem")["delivery_attempts"] == 0
    assert len(_drain(_isolated)) == 2  # two wake copies are claim-safe

    assert ad.claim_completion_delivery("notif_idem", "claim-1")
    assert ad.complete_completion_delivery("notif_idem", "claim-1")
    duplicate = _publish("notif_idem")
    assert duplicate["status"] == "duplicate"
    assert _row("notif_idem")["delivery_state"] == "delivered"
    assert _drain(_isolated) == []  # a finished delivery is never re-woken


# ---------------------------------------------------------------------------
# Claim contract: duplicates and retry budget
# ---------------------------------------------------------------------------


def test_refused_duplicate_claim_burns_no_attempt_budget(_isolated):
    _publish("notif_dup")
    assert ad.claim_completion_delivery("notif_dup", "claim-a")
    assert _row("notif_dup")["delivery_attempts"] == 1
    # Second copy of the same completion races in: refused, budget untouched.
    assert not ad.claim_completion_delivery("notif_dup", "claim-b")
    assert _row("notif_dup")["delivery_attempts"] == 1


# ---------------------------------------------------------------------------
# Stream contract: ordering (opt-in) and supersession (explicit)
# ---------------------------------------------------------------------------


def test_ordered_stream_blocks_higher_sequence_until_lower_delivered(_isolated):
    _publish("s/1", stream_id="s", sequence=1)
    _publish("s/2", stream_id="s", sequence=2)

    # Wakes can race and present the higher sequence first — the claim
    # chokepoint refuses it while an undelivered lower sibling remains, and
    # the refusal burns no attempt budget.
    assert not ad.claim_completion_delivery("s/2", "c2")
    assert _row("s/2")["delivery_attempts"] == 0
    assert _row("s/2")["delivery_state"] == "pending"

    assert ad.claim_completion_delivery("s/1", "c1")
    assert ad.complete_completion_delivery("s/1", "c1")
    assert ad.claim_completion_delivery("s/2", "c2")
    assert ad.complete_completion_delivery("s/2", "c2")


def test_sequenced_rows_without_watermark_never_lose_events(_isolated):
    """Sequencing alone is ordering, never supersession — nothing is dropped."""
    for seq in (1, 2, 3):
        _publish(f"o/{seq}", stream_id="o", sequence=seq)
    for seq in (1, 2, 3):
        claim = f"c{seq}"
        assert ad.claim_completion_delivery(f"o/{seq}", claim)
        assert ad.complete_completion_delivery(f"o/{seq}", claim)
    assert all(_row(f"o/{seq}")["delivery_state"] == "delivered" for seq in (1, 2, 3))


def test_superseding_terminal_delivers_and_atomically_retires_lower_pending(_isolated):
    _publish("r/13", stream_id="r", sequence=13)
    _publish("r/22", stream_id="r", sequence=22)
    _publish("r/69", stream_id="r", sequence=69, supersedes_before_sequence=69,
             status="ended_unverified")

    # The terminal event's own watermark covers the undelivered lower rows,
    # so the in-order gate does not block it.
    assert ad.claim_completion_delivery("r/69", "c69")
    assert ad.complete_completion_delivery("r/69", "c69")

    assert _row("r/13")["delivery_state"] == "superseded"
    assert _row("r/22")["delivery_state"] == "superseded"
    # A late claim (e.g. a restored queue copy) can never resurrect them.
    assert not ad.claim_completion_delivery("r/13", "late")
    assert _row("r/13")["delivery_state"] == "superseded"


def test_delivered_watermark_blocks_lower_rows_that_surface_later(_isolated):
    """A missed old event arriving after the superseding delivery never claims."""
    _publish("w/5", stream_id="w", sequence=5, supersedes_before_sequence=5)
    assert ad.claim_completion_delivery("w/5", "c5")
    assert ad.complete_completion_delivery("w/5", "c5")

    _publish("w/3", stream_id="w", sequence=3)  # stale event persisted late
    assert not ad.claim_completion_delivery("w/3", "c3")
    assert _row("w/3")["delivery_state"] == "superseded"
    assert _row("w/3")["delivery_attempts"] == 0


# ---------------------------------------------------------------------------
# Live durable sweep — lost-wake recovery without restart
# ---------------------------------------------------------------------------


def test_sweep_recovers_lost_wake_and_stamps_durable_replay(_isolated, monkeypatch):
    monkeypatch.setattr(ad, "_SWEEP_MIN_PENDING_AGE_S", 0.0)
    _publish("notif_lost")
    _drain(_isolated)  # the immediate wake is lost
    ad._clear_wake_activity("notif_lost")

    target = queue.Queue()
    assert ad.sweep_undelivered_completions(target) == 1
    evt = target.get_nowait()
    assert evt["delegation_id"] == "notif_lost"
    assert evt["restored"] is True  # durable replay: fail closed on ownership


def test_sweep_grace_lets_the_immediate_wake_win(_isolated, monkeypatch):
    monkeypatch.setattr(ad, "_SWEEP_MIN_PENDING_AGE_S", 60.0)
    _publish("notif_fresh")
    _drain(_isolated)
    ad._clear_wake_activity("notif_fresh")

    target = queue.Queue()
    assert ad.sweep_undelivered_completions(target) == 0


def test_repeated_sweeps_do_not_flood_while_a_copy_is_queued(_isolated, monkeypatch):
    monkeypatch.setattr(ad, "_SWEEP_MIN_PENDING_AGE_S", 0.0)
    monkeypatch.setattr(ad, "_SWEEP_REWAKE_INTERVAL_S", 30.0)
    _publish("notif_inflight")
    _drain(_isolated)
    ad._clear_wake_activity("notif_inflight")

    target = queue.Queue()
    assert ad.sweep_undelivered_completions(target) == 1
    for _ in range(5):
        assert ad.sweep_undelivered_completions(target) == 0
    assert target.qsize() == 1
    assert _row("notif_inflight")["delivery_attempts"] == 0


def test_sweep_rewakes_a_lost_swept_copy_within_the_rearm_bound(
    _isolated, monkeypatch
):
    monkeypatch.setattr(ad, "_SWEEP_MIN_PENDING_AGE_S", 0.0)
    monkeypatch.setattr(ad, "_SWEEP_REWAKE_INTERVAL_S", 0.15)
    _publish("notif_relost")
    _drain(_isolated)
    ad._clear_wake_activity("notif_relost")

    target = queue.Queue()
    assert ad.sweep_undelivered_completions(target) == 1
    _drain(target)  # the swept wake is ALSO lost
    assert ad.sweep_undelivered_completions(target) == 0  # inside re-arm window
    time.sleep(0.2)
    # Past the re-arm window the row is re-woken — never suppressed blindly.
    assert ad.sweep_undelivered_completions(target) == 1
    assert target.get_nowait()["delegation_id"] == "notif_relost"


def test_sweep_skips_rows_with_an_active_delivery_claim(_isolated, monkeypatch):
    monkeypatch.setattr(ad, "_SWEEP_MIN_PENDING_AGE_S", 0.0)
    _publish("notif_claimed")
    _drain(_isolated)
    assert ad.claim_completion_delivery("notif_claimed", "claim-x")
    ad._clear_wake_activity("notif_claimed")

    target = queue.Queue()
    assert ad.sweep_undelivered_completions(target) == 0


def test_sweep_orders_missed_records_by_occurrence(_isolated, monkeypatch):
    monkeypatch.setattr(ad, "_SWEEP_MIN_PENDING_AGE_S", 0.0)
    now = time.time()
    _publish("late", occurred_at=now - 10)
    _publish("early", occurred_at=now - 60)
    _publish("middle", occurred_at=now - 30)
    _drain(_isolated)
    for nid in ("late", "early", "middle"):
        ad._clear_wake_activity(nid)

    target = queue.Queue()
    assert ad.sweep_undelivered_completions(target) == 3
    order = [target.get_nowait()["delegation_id"] for _ in range(3)]
    assert order == ["early", "middle", "late"]


def test_sweep_terminally_drops_rows_past_the_staleness_cap(_isolated, monkeypatch):
    monkeypatch.setattr(ad, "_SWEEP_MIN_PENDING_AGE_S", 0.0)
    _publish("notif_ancient", occurred_at=time.time() - (ad._MAX_COMPLETION_REPLAY_AGE_S + 3600))
    _drain(_isolated)
    ad._clear_wake_activity("notif_ancient")

    target = queue.Queue()
    assert ad.sweep_undelivered_completions(target) == 0
    assert target.empty()
    assert _row("notif_ancient")["delivery_state"] == "dropped"


def test_sweep_leaves_rows_owned_by_a_live_sibling_process_alone(
    _isolated, monkeypatch
):
    monkeypatch.setattr(ad, "_SWEEP_MIN_PENDING_AGE_S", 0.0)
    from gateway.status import get_process_start_time

    _publish("notif_foreign")
    _drain(_isolated)
    ad._clear_wake_activity("notif_foreign")

    sibling_pid = os.getppid()  # provably live foreign process
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute(
            "UPDATE async_delegations SET owner_pid=?, owner_started_at=? WHERE delegation_id=?",
            (sibling_pid, get_process_start_time(sibling_pid), "notif_foreign"),
        )
    target = queue.Queue()
    assert ad.sweep_undelivered_completions(target) == 0

    # Same PID but a DEAD incarnation (start-time mismatch): adoptable.
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute(
            "UPDATE async_delegations SET owner_started_at=1 WHERE delegation_id=?",
            ("notif_foreign",),
        )
    assert ad.sweep_undelivered_completions(target) == 1


def test_unfiltered_legacy_drain_leaves_swept_events_for_their_owner(
    tmp_path, monkeypatch
):
    """Sweep-discovered rows are durable replays — fail closed on ownership."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    monkeypatch.setattr(ad, "_SWEEP_MIN_PENDING_AGE_S", 0.0)
    import tools.process_registry as pr_module

    registry = pr_module.ProcessRegistry()
    monkeypatch.setattr(pr_module, "process_registry", registry)

    ad.publish_background_notification(
        summary="addressed elsewhere",
        session_key="agent:main:telegram:dm:9:9",
        notification_id="notif_owned",
    )
    while not registry.completion_queue.empty():
        registry.completion_queue.get_nowait()
    ad._clear_wake_activity("notif_owned")
    assert ad.sweep_undelivered_completions(registry.completion_queue) == 1

    # Legacy unfiltered drain (no session_key, no ownership callback) must
    # not adopt the durable replay of another session's addressed event.
    assert registry.drain_notifications() == []
    assert registry.completion_queue.qsize() == 1
    ad._reset_for_tests()


def test_production_recovery_latency_contract():
    """Operational bound: lost wakes must recover in single-digit seconds.

    Grace + the ~2s gateway watcher tick bounds first discovery; the re-arm
    interval bounds re-discovery of a lost swept wake. A 1-minute-lifetime
    notification (e.g. an ETA) must not spend a large fraction of its life
    waiting on recovery.
    """
    assert ad._SWEEP_MIN_PENDING_AGE_S <= 5.0
    assert ad._SWEEP_REWAKE_INTERVAL_S <= 5.0
