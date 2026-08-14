"""Honest delivery-age rendering for async completion re-injections.

The 2026-08-13 incident formatter computed "(X ago)" as
``completed_at - dispatched_at`` — the PRODUCER's runtime — so a 21-hour-old
restart-recovered event rendered ``Dispatched: 01:52 (0s ago)``. Age lines
must be measured against NOW (the delivery moment); producer runtime stays a
separate ``Duration`` figure; and a completion delivered long after it was
produced carries an explicit staleness notice.
"""

import time

from tools.process_registry import (
    _DELIVERY_LAG_NOTICE_S,
    _format_async_delegation,
)


def _event(**overrides):
    now = time.time()
    evt = {
        "type": "async_delegation",
        "delegation_id": "deleg_age",
        "goal": "Check the thing",
        "status": "completed",
        "summary": "All good",
        "api_calls": 3,
        "duration_seconds": 35.0,
        "dispatched_at": now - 47,
        "completed_at": now - 12,
    }
    evt.update(overrides)
    return evt


def test_recovered_completion_shows_real_delivery_age_not_zero():
    now = time.time()
    twenty_one_hours = 21 * 3600
    text = _format_async_delegation(_event(
        dispatched_at=now - twenty_one_hours - 35,
        completed_at=now - twenty_one_hours,
    ))
    assert "(0s ago)" not in text
    assert "21h" in text  # visibly ~21 hours old at delivery time
    assert "delivery was delayed" in text
    assert "Duration: 35.0s" in text  # producer runtime stays distinct


def test_fresh_completion_has_no_staleness_notice():
    text = _format_async_delegation(_event())
    assert "delivery was delayed" not in text
    assert "Dispatched:" in text


def test_staleness_notice_threshold_is_the_documented_boundary():
    now = time.time()
    just_inside = _format_async_delegation(
        _event(completed_at=now - (_DELIVERY_LAG_NOTICE_S - 30))
    )
    just_outside = _format_async_delegation(
        _event(completed_at=now - (_DELIVERY_LAG_NOTICE_S + 30))
    )
    assert "delivery was delayed" not in just_inside
    assert "delivery was delayed" in just_outside


def test_batch_completion_uses_delivery_age_and_staleness_notice():
    now = time.time()
    text = _format_async_delegation(_event(
        is_batch=True,
        results=[{"task_index": 0, "status": "completed", "summary": "ok"}],
        goals=["task one"],
        total_duration_seconds=40.0,
        dispatched_at=now - 7200 - 40,
        completed_at=now - 7200,
    ))
    assert "(0s ago)" not in text
    assert "2h" in text
    assert "delivery was delayed" in text


def test_notification_block_reports_occurrence_age():
    now = time.time()
    stale = _format_async_delegation({
        "type": "async_delegation",
        "kind": "notification",
        "delegation_id": "chauffeur/r1/13",
        "goal": "Ride update",
        "status": "driver_assigned",
        "summary": "Driver assigned: Alex, ETA 4 min",
        "dispatched_at": now - 3 * 3600,
        "completed_at": now - 3 * 3600,
    })
    assert "[BACKGROUND NOTIFICATION — chauffeur/r1/13]" in stale
    assert "3h" in stale
    assert "delivery was delayed" in stale
    assert "Driver assigned: Alex, ETA 4 min" in stale

    fresh = _format_async_delegation({
        "type": "async_delegation",
        "kind": "notification",
        "delegation_id": "chauffeur/r1/14",
        "status": "eta_checkpoint",
        "summary": "1 minute away",
        "dispatched_at": now - 2,
        "completed_at": now - 2,
    })
    assert "delivery was delayed" not in fresh
    assert "1 minute away" in fresh
