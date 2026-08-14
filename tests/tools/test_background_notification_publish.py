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
    assert ad.sweep_undelivered_completions(target, force_scan=True) == 1
    evt = target.get_nowait()
    assert evt["delegation_id"] == "notif_lost"
    assert evt["restored"] is True  # durable replay: fail closed on ownership


def test_sweep_grace_lets_the_immediate_wake_win(_isolated, monkeypatch):
    monkeypatch.setattr(ad, "_SWEEP_MIN_PENDING_AGE_S", 60.0)
    _publish("notif_fresh")
    _drain(_isolated)
    ad._clear_wake_activity("notif_fresh")

    target = queue.Queue()
    assert ad.sweep_undelivered_completions(target, force_scan=True) == 0


def test_repeated_sweeps_do_not_flood_while_a_copy_is_queued(_isolated, monkeypatch):
    monkeypatch.setattr(ad, "_SWEEP_MIN_PENDING_AGE_S", 0.0)
    monkeypatch.setattr(ad, "_SWEEP_REWAKE_INTERVAL_S", 30.0)
    _publish("notif_inflight")
    _drain(_isolated)
    ad._clear_wake_activity("notif_inflight")

    target = queue.Queue()
    assert ad.sweep_undelivered_completions(target, force_scan=True) == 1
    for _ in range(5):
        assert ad.sweep_undelivered_completions(target, force_scan=True) == 0
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
    assert ad.sweep_undelivered_completions(target, force_scan=True) == 1
    _drain(target)  # the swept wake is ALSO lost
    assert ad.sweep_undelivered_completions(target, force_scan=True) == 0  # inside re-arm window
    time.sleep(0.2)
    # Past the re-arm window the row is re-woken — never suppressed blindly.
    assert ad.sweep_undelivered_completions(target, force_scan=True) == 1
    assert target.get_nowait()["delegation_id"] == "notif_relost"


def test_sweep_skips_rows_with_an_active_delivery_claim(_isolated, monkeypatch):
    monkeypatch.setattr(ad, "_SWEEP_MIN_PENDING_AGE_S", 0.0)
    _publish("notif_claimed")
    _drain(_isolated)
    assert ad.claim_completion_delivery("notif_claimed", "claim-x")
    ad._clear_wake_activity("notif_claimed")

    target = queue.Queue()
    assert ad.sweep_undelivered_completions(target, force_scan=True) == 0


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
    assert ad.sweep_undelivered_completions(target, force_scan=True) == 3
    order = [target.get_nowait()["delegation_id"] for _ in range(3)]
    assert order == ["early", "middle", "late"]


def test_sweep_terminally_drops_rows_past_the_staleness_cap(_isolated, monkeypatch):
    monkeypatch.setattr(ad, "_SWEEP_MIN_PENDING_AGE_S", 0.0)
    _publish("notif_ancient", occurred_at=time.time() - (ad._MAX_COMPLETION_REPLAY_AGE_S + 3600))
    _drain(_isolated)
    ad._clear_wake_activity("notif_ancient")

    target = queue.Queue()
    assert ad.sweep_undelivered_completions(target, force_scan=True) == 0
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
    assert ad.sweep_undelivered_completions(target, force_scan=True) == 0

    # Same PID but a DEAD incarnation (start-time mismatch): adoptable.
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute(
            "UPDATE async_delegations SET owner_started_at=1 WHERE delegation_id=?",
            ("notif_foreign",),
        )
    assert ad.sweep_undelivered_completions(target, force_scan=True) == 1


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
    assert ad.sweep_undelivered_completions(registry.completion_queue, force_scan=True) == 1

    # Legacy unfiltered drain (no session_key, no ownership callback) must
    # not adopt the durable replay of another session's addressed event.
    assert registry.drain_notifications() == []
    assert registry.completion_queue.qsize() == 1
    ad._reset_for_tests()


# ---------------------------------------------------------------------------
# Idempotency key binds IMMUTABLE content (republish split-brain)
# ---------------------------------------------------------------------------


def test_republish_with_different_summary_is_a_conflict_not_a_wake(
    _isolated, monkeypatch,
):
    monkeypatch.setattr(ad, "_SWEEP_MIN_PENDING_AGE_S", 0.0)
    _publish("notif_bind", summary="original body")
    _drain(_isolated)

    result = _publish("notif_bind", summary="DIFFERENT body")
    assert result["status"] == "conflict"
    assert "summary" in result["error"]
    assert _drain(_isolated) == []  # never enqueue content that differs from durable truth
    # The durable row is untouched — a sweep re-wake still carries the
    # original payload, so immediate and recovered delivery can never split.
    ad._clear_wake_activity("notif_bind")
    target = queue.Queue()
    assert ad.sweep_undelivered_completions(target, force_scan=True) == 1
    assert target.get_nowait()["summary"] == "original body"


def test_republish_with_different_routing_is_a_conflict(_isolated):
    _publish("notif_route", session_key="agent:main:telegram:dm:1:2")
    _drain(_isolated)

    result = _publish("notif_route", session_key="agent:main:slack:dm:9:9")
    assert result["status"] == "conflict"
    assert "session_key" in result["error"]
    assert _drain(_isolated) == []


def test_identical_republish_rewakes_the_persisted_payload(_isolated):
    first = _publish("notif_same", occurred_at=1_000_000.0)
    assert first["status"] == "published"
    _drain(_isolated)  # immediate wake lost

    # Retry omits occurred_at (regenerated "now" default) — mutable
    # timestamps must not conflict, and the re-wake must carry the DURABLE
    # payload: first-publish occurrence time, not the retry's.
    retry = _publish("notif_same")
    assert retry["status"] == "republished"
    wakes = _drain(_isolated)
    assert len(wakes) == 1
    assert wakes[0]["completed_at"] == 1_000_000.0


# ---------------------------------------------------------------------------
# Stream position is stable at publish time (late/duplicate sequences)
# ---------------------------------------------------------------------------


def test_duplicate_stream_sequence_under_a_new_id_is_a_conflict(_isolated):
    _publish("p/1", stream_id="p", sequence=1)
    _drain(_isolated)

    result = _publish("p/1-retry-new-id", stream_id="p", sequence=1)
    assert result["status"] == "conflict"
    assert "already bound" in result["error"]
    assert _row("p/1-retry-new-id") is None  # refused before any durable write
    assert _drain(_isolated) == []


def test_late_lower_sequence_after_delivered_higher_is_rejected(_isolated):
    _publish("q/2", stream_id="q", sequence=2)
    _drain(_isolated)
    assert ad.claim_completion_delivery("q/2", "c2")
    assert ad.complete_completion_delivery("q/2", "c2")

    # seq 2 reached the user with NO watermark: a late seq 1 can neither be
    # delivered in order nor silently retired — out-of-order contract breach.
    result = _publish("q/1", stream_id="q", sequence=1)
    assert result["status"] == "rejected"
    assert "out-of-order" in result["error"]
    assert _row("q/1") is None
    assert _drain(_isolated) == []


def test_late_lower_sequence_covered_by_delivered_watermark_is_born_superseded(
    _isolated,
):
    _publish("v/2", stream_id="v", sequence=2, supersedes_before_sequence=2)
    _drain(_isolated)
    assert ad.claim_completion_delivery("v/2", "c2")
    assert ad.complete_completion_delivery("v/2", "c2")

    result = _publish("v/1", stream_id="v", sequence=1)
    assert result["status"] == "superseded"
    row = _row("v/1")
    assert row["delivery_state"] == "superseded"  # durable record kept, queryable
    assert _drain(_isolated) == []  # no wake, no delivery
    assert not ad.claim_completion_delivery("v/1", "late")


def test_late_lower_sequence_is_legal_while_higher_siblings_are_pending(_isolated):
    _publish("l/2", stream_id="l", sequence=2)
    result = _publish("l/1", stream_id="l", sequence=1)
    assert result["status"] == "published"
    # Claim ordering can still repair the order: low delivers, then high.
    assert not ad.claim_completion_delivery("l/2", "c2")
    assert ad.claim_completion_delivery("l/1", "c1")
    assert ad.complete_completion_delivery("l/1", "c1")
    assert ad.claim_completion_delivery("l/2", "c2")


def test_watermark_above_own_sequence_is_invalid(_isolated):
    with pytest.raises(ValueError):
        _publish("bad/1", stream_id="bad", sequence=1, supersedes_before_sequence=2)


# ---------------------------------------------------------------------------
# Claimed-old vs superseding-terminal race (symmetric deferral protocol)
# ---------------------------------------------------------------------------


def test_old_claim_wins_terminal_defers_until_old_completes(_isolated):
    """Interleaving 1: seq1 claimed first — the covered live claim blocks the
    terminal; after seq1 completes, order old→terminal is honest."""
    _publish("a/1", stream_id="a", sequence=1)
    _publish("a/2", stream_id="a", sequence=2, supersedes_before_sequence=2)

    turn_order = []
    assert ad.claim_completion_delivery("a/1", "c1")
    # Terminal MUST defer: its watermark covers seq1, but seq1 holds a live
    # claim — racing past it could retire a turn the user just received.
    assert not ad.claim_completion_delivery("a/2", "c2")
    assert _row("a/2")["delivery_attempts"] == 0  # deferral burns no budget
    assert _row("a/1")["delivery_state"] == "pending"  # never rewritten under claim

    assert ad.complete_completion_delivery("a/1", "c1")
    turn_order.append("a/1")
    assert ad.claim_completion_delivery("a/2", "c2")
    assert ad.complete_completion_delivery("a/2", "c2")
    turn_order.append("a/2")

    assert turn_order == ["a/1", "a/2"]  # accepted user-turn order, old→terminal
    assert _row("a/1")["delivery_state"] == "delivered"  # NOT superseded — it was seen
    assert _row("a/2")["delivery_state"] == "delivered"
    assert _row("a/1")["delivery_attempts"] == 1
    assert _row("a/2")["delivery_attempts"] == 1


def test_released_old_row_blocks_terminal_until_it_resolves(_isolated):
    """A covered row with ANY recorded attempt crossed custody: claim history
    is monotonic and cannot be retracted, so the terminal stays blocked until
    the old row resolves delivered — never retired mid-lifecycle."""
    _publish("b/1", stream_id="b", sequence=1)
    _publish("b/2", stream_id="b", sequence=2, supersedes_before_sequence=2)

    turn_order = []
    assert ad.claim_completion_delivery("b/1", "c1")
    assert not ad.claim_completion_delivery("b/2", "c2")  # deferred while claimed
    assert ad.release_completion_delivery("b/1", "c1")  # old delivery failed

    # attempts=1: the row crossed custody — terminal remains blocked.
    assert not ad.claim_completion_delivery("b/2", "c2")
    assert _row("b/2")["delivery_attempts"] == 0

    # The old row retries through its own lifecycle and delivers...
    assert ad.claim_completion_delivery("b/1", "c1b")
    assert ad.complete_completion_delivery("b/1", "c1b")
    turn_order.append("b/1")
    # ...and only then may the terminal deliver.
    assert ad.claim_completion_delivery("b/2", "c2")
    assert ad.complete_completion_delivery("b/2", "c2")
    turn_order.append("b/2")

    assert turn_order == ["b/1", "b/2"]
    assert _row("b/1")["delivery_state"] == "delivered"  # NEVER superseded
    assert _row("b/1")["delivery_attempts"] == 2
    assert _row("b/2")["delivery_attempts"] == 1


def test_expired_unacked_claim_blocks_terminal_until_old_reclaims(_isolated):
    """Claim-TTL expiry does not erase custody: the old consumer may have
    injected before its ack was lost, or may still finish after lease expiry.
    The terminal must wait for the old row to resolve delivered."""
    _publish("e/1", stream_id="e", sequence=1)
    _publish("e/2", stream_id="e", sequence=2, supersedes_before_sequence=2)

    turn_order = []
    assert ad.claim_completion_delivery("e/1", "c1")
    # The claim expires without an ack (consumer crashed / ack lost).
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute(
            "UPDATE async_delegations SET delivery_claimed_at=? WHERE delegation_id='e/1'",
            (time.time() - ad._DELIVERY_CLAIM_TTL_S - 10,),
        )
    # Terminal remains blocked: attempts>0 is irrevocable custody.
    assert not ad.claim_completion_delivery("e/2", "c2")
    assert _row("e/1")["delivery_state"] == "pending"  # never superseded
    assert _row("e/2")["delivery_attempts"] == 0  # deferral burns no budget

    # The old row re-claims past the expired lease and completes...
    assert ad.claim_completion_delivery("e/1", "c1b")
    assert ad.complete_completion_delivery("e/1", "c1b")
    turn_order.append("e/1")
    assert ad.claim_completion_delivery("e/2", "c2")
    assert ad.complete_completion_delivery("e/2", "c2")
    turn_order.append("e/2")

    assert turn_order == ["e/1", "e/2"]  # old→terminal, always
    assert _row("e/1")["delivery_attempts"] == 2
    assert _row("e/2")["delivery_attempts"] == 1


def test_attempted_old_row_that_drops_via_retry_cap_unblocks_terminal(_isolated):
    """The other legal resolution: the old row exhausts its retry budget and
    terminally drops — only then does the blocked terminal deliver."""
    _publish("f/1", stream_id="f", sequence=1)
    _publish("f/2", stream_id="f", sequence=2, supersedes_before_sequence=2)

    for attempt in range(ad._MAX_DELIVERY_ATTEMPTS):
        assert ad.claim_completion_delivery("f/1", f"c{attempt}")
        assert not ad.claim_completion_delivery("f/2", "cterm")  # blocked throughout
        assert ad.release_completion_delivery("f/1", f"c{attempt}")

    assert _row("f/1")["delivery_state"] == "dropped"  # retry cap, honest terminal
    assert ad.claim_completion_delivery("f/2", "cterm")
    assert ad.complete_completion_delivery("f/2", "cterm")
    assert _row("f/2")["delivery_state"] == "delivered"
    assert _row("f/1")["delivery_state"] == "dropped"  # never rewritten to superseded


def test_delivered_watermark_fallback_never_retires_an_attempted_row(_isolated):
    """Even against an already-delivered watermark, a covered row that
    crossed custody resolves through its own lifecycle."""
    _publish("g/1", stream_id="g", sequence=1)
    _publish("g/2", stream_id="g", sequence=2, supersedes_before_sequence=2)

    # Force the pathological state directly: the terminal is delivered while
    # the covered row has recorded attempts (e.g. its ack was lost).
    now = time.time()
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute(
            "UPDATE async_delegations SET delivery_attempts=1 WHERE delegation_id='g/1'",
        )
        conn.execute(
            "UPDATE async_delegations SET delivery_state='delivered', delivered_at=?, "
            "updated_at=? WHERE delegation_id='g/2'",
            (now, now),
        )
    # The fallback must NOT flip g/1 to superseded; its claim proceeds and the
    # row finishes its own delivery honestly (at-least-once custody).
    assert ad.claim_completion_delivery("g/1", "c1")
    assert _row("g/1")["delivery_state"] == "pending"
    assert ad.complete_completion_delivery("g/1", "c1")
    assert _row("g/1")["delivery_state"] == "delivered"


def test_terminal_claim_wins_old_row_defers_then_supersedes(_isolated):
    """Interleaving 2: terminal claimed first — the covered old row must not
    slip a turn in under the in-flight superseding delivery."""
    _publish("c/1", stream_id="c", sequence=1)
    _publish("c/2", stream_id="c", sequence=2, supersedes_before_sequence=2)

    assert ad.claim_completion_delivery("c/2", "c2")
    # Old row defers: a live superseding claim covers it.
    assert not ad.claim_completion_delivery("c/1", "c1")
    assert _row("c/1")["delivery_attempts"] == 0

    assert ad.complete_completion_delivery("c/2", "c2")
    assert _row("c/1")["delivery_state"] == "superseded"  # retired at terminal ack
    assert not ad.claim_completion_delivery("c/1", "late")  # can never surface
    # Exactly ONE user turn was ever grantable in this stream.
    assert _row("c/2")["delivery_state"] == "delivered"
    assert _row("c/2")["delivery_attempts"] == 1


def test_watermark_ack_never_rewrites_a_live_claimed_row(_isolated):
    """Transactional backstop: even if claim gates were bypassed (TTL-expiry
    race), the terminal's ack must not rewrite a row under a live claim."""
    _publish("d/1", stream_id="d", sequence=1)
    _publish("d/2", stream_id="d", sequence=2, supersedes_before_sequence=2)

    now = time.time()
    with ad._DB_LOCK, ad._transaction() as conn:
        # Force the forbidden interleaving directly in SQL: both rows hold
        # live claims simultaneously.
        conn.execute(
            "UPDATE async_delegations SET delivery_claim='c1', delivery_claimed_at=? "
            "WHERE delegation_id='d/1'",
            (now,),
        )
        conn.execute(
            "UPDATE async_delegations SET delivery_claim='c2', delivery_claimed_at=?, "
            "delivery_attempts=1 WHERE delegation_id='d/2'",
            (now,),
        )
    assert ad.complete_completion_delivery("d/2", "c2")
    row = _row("d/1")
    assert row["delivery_state"] == "pending"  # live claim NOT rewritten
    # The in-flight old turn completes honestly...
    assert ad.complete_completion_delivery("d/1", "c1")
    assert _row("d/1")["delivery_state"] == "delivered"


# ---------------------------------------------------------------------------
# Pruning never deletes pending durable truth
# ---------------------------------------------------------------------------


def test_fifty_one_pending_notifications_are_all_retained(_isolated):
    for i in range(51):
        _publish(f"backlog/{i}")
    with ad._DB_LOCK, ad._transaction() as conn:
        pending = conn.execute(
            "SELECT COUNT(*) FROM async_delegations WHERE delivery_state='pending'"
        ).fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM async_delegations").fetchone()[0]
    assert pending == 51  # the terminal-history cap must not delete obligations
    assert total == 51


def test_terminal_history_cap_only_touches_delivery_terminal_rows(
    _isolated, monkeypatch,
):
    monkeypatch.setattr(ad, "_MAX_RETAINED_COMPLETED", 3)
    for i in range(5):
        nid = f"hist/{i}"
        _publish(nid)
        assert ad.claim_completion_delivery(nid, f"c{i}")
        assert ad.complete_completion_delivery(nid, f"c{i}")
    for i in range(4):
        _publish(f"owed/{i}")
    ad._prune_durable_records()
    with ad._DB_LOCK, ad._transaction() as conn:
        pending = conn.execute(
            "SELECT COUNT(*) FROM async_delegations WHERE delivery_state='pending'"
        ).fetchone()[0]
        delivered = conn.execute(
            "SELECT COUNT(*) FROM async_delegations WHERE delivery_state='delivered'"
        ).fetchone()[0]
    assert pending == 4  # untouched
    assert delivered <= 3  # history bounded


def test_pending_overflow_transitions_to_dropped_not_deleted(
    _isolated, monkeypatch,
):
    monkeypatch.setattr(ad, "_MAX_DURABLE_PENDING", 50)
    for i in range(51):
        _publish(f"of/{i}", summary=f"overflow body {i}")
    with ad._DB_LOCK, ad._transaction() as conn:
        pending = conn.execute(
            "SELECT COUNT(*) FROM async_delegations WHERE delivery_state='pending'"
        ).fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM async_delegations").fetchone()[0]
    assert pending == 50
    assert total == 51  # nothing deleted
    oldest = _row("of/0")
    assert oldest["delivery_state"] == "dropped"  # honest disposition
    assert oldest["result"]["summary"] == "overflow body 0"  # still queryable


# ---------------------------------------------------------------------------
# Boot restore shares the sweep's owner-eligibility predicate
# ---------------------------------------------------------------------------


def test_boot_restore_leaves_live_foreign_owners_rows_alone(_isolated):
    from gateway.status import get_process_start_time

    _publish("restore_foreign")
    _publish("restore_dead")
    _drain(_isolated)
    sibling_pid = os.getppid()  # provably live foreign process
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute(
            "UPDATE async_delegations SET owner_pid=?, owner_started_at=? "
            "WHERE delegation_id='restore_foreign'",
            (sibling_pid, get_process_start_time(sibling_pid)),
        )
        # Same PID number, dead incarnation: adoptable.
        conn.execute(
            "UPDATE async_delegations SET owner_pid=?, owner_started_at=1 "
            "WHERE delegation_id='restore_dead'",
            (sibling_pid,),
        )
    target = queue.Queue()
    assert ad.restore_undelivered_completions(target) == 1
    restored = target.get_nowait()
    assert restored["delegation_id"] == "restore_dead"
    # The live sibling's row is untouched: still pending, no attempts burned.
    row = _row("restore_foreign")
    assert row["delivery_state"] == "pending"
    assert row["delivery_attempts"] == 0


# ---------------------------------------------------------------------------
# Process-global scan throttle (multi-poller invariant)
# ---------------------------------------------------------------------------


def test_concurrent_pollers_within_one_interval_trigger_one_db_scan(
    _isolated, monkeypatch,
):
    """N per-session TUI pollers + CLI drain + gateway tick share ONE scan."""
    monkeypatch.setattr(ad, "_SWEEP_SCAN_INTERVAL_S", 60.0)
    monkeypatch.setattr(ad, "_SWEEP_MIN_PENDING_AGE_S", 0.0)
    _publish("throttled")
    _drain(_isolated)
    ad._clear_wake_activity("throttled")

    connects = []
    real_connect = ad._connect

    def _counting_connect():
        connects.append(1)
        return real_connect()

    monkeypatch.setattr(ad, "_connect", _counting_connect)
    target = queue.Queue()
    first = ad.sweep_undelivered_completions(target)
    assert first == 1
    scans_after_first = len(connects)
    # Nine more pollers wake inside the same interval: zero additional scans.
    results = [ad.sweep_undelivered_completions(target) for _ in range(9)]
    assert results == [0] * 9
    assert len(connects) == scans_after_first
    # And a forced call (explicit one-shot recovery / tests) still bypasses.
    assert ad.sweep_undelivered_completions(target, force_scan=True) == 0  # re-arm holds
    assert len(connects) > scans_after_first


def test_production_recovery_latency_contract():
    """Operational bound: lost wakes must recover in single-digit seconds.

    Grace + the ~2s gateway watcher tick bounds first discovery; the re-arm
    interval bounds re-discovery of a lost swept wake. A 1-minute-lifetime
    notification (e.g. an ETA) must not spend a large fraction of its life
    waiting on recovery.
    """
    assert ad._SWEEP_MIN_PENDING_AGE_S <= 5.0
    assert ad._SWEEP_REWAKE_INTERVAL_S <= 5.0
