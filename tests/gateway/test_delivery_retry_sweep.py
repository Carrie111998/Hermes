"""Tests for the in-process delivery retry sweep (``sweep_retryable``).

``sweep_recoverable`` only claims rows whose owner process is DEAD, so it
recovers crashes and nothing else. When the PLATFORM rejects the final send
(a Discord 5xx blip) the gateway stays alive, keeps owning the row, and the
answer sat ``failed``/``attempts=0`` until an operator restarted the process.

Contract asserted here:
- a live-owned ``failed`` row is invisible to ``sweep_recoverable`` (the hole)
- ``sweep_retryable`` claims it, but only after its backoff has elapsed
- claiming flips it to ``attempting`` and spends exactly one attempt
- the two sweeps partition by owner liveness and can never claim one row
- retries always carry a marker (a 5xx can hide a delivery that landed)
- the attempts cap / stale cutoff abandon poison rows, budget shared
- pid reuse by a different process is not mistaken for us
"""

import os
import time

import pytest

from gateway import delivery_ledger as dl


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(dl, "_db_path", lambda: home / "state.db")
    yield


def _record(oid="ob-1", platform="discord", **kw):
    dl.record_obligation(
        obligation_id=oid,
        session_key=kw.get("session_key", "agent:main:discord:channel:C1"),
        platform=platform,
        chat_id=kw.get("chat_id", "C1"),
        thread_id=kw.get("thread_id", "1539072364405330062"),
        content=kw.get("content", "the final answer"),
    )


def _row(oid):
    with dl._connect() as conn:
        r = conn.execute(
            """SELECT state, attempts, owner_pid, owner_started_at
               FROM delivery_obligations WHERE obligation_id=?""",
            (oid,),
        ).fetchone()
    return None if r is None else {
        "state": r[0], "attempts": r[1],
        "owner_pid": r[2], "owner_started_at": r[3],
    }


def _set(oid, **cols):
    sets = ", ".join(f"{k}=?" for k in cols)
    with dl._connect() as conn:
        conn.execute(
            f"UPDATE delivery_obligations SET {sets} WHERE obligation_id=?",
            (*cols.values(), oid),
        )
        conn.commit()


def _fail_and_age(oid, seconds_ago):
    """Mark failed, then backdate updated_at to simulate elapsed backoff."""
    dl.mark_failed(oid, "503 Service Unavailable")
    _set(oid, updated_at=time.time() - seconds_ago)


# --------------------------------------------------------------------------
# The hole this sweep exists to close
# --------------------------------------------------------------------------

def test_crash_sweep_ignores_failed_row_owned_by_live_process():
    """Regression for the 2026-08-21 incident: a 503 on the final send left
    the answer stranded because the OWNING gateway never died."""
    _record()
    _fail_and_age("ob-1", 3600)

    assert dl.sweep_recoverable() == []          # crash path cannot see it
    assert _row("ob-1")["attempts"] == 0         # nothing ever retried it

    claimed = dl.sweep_retryable()               # retry path does
    assert [c["obligation_id"] for c in claimed] == ["ob-1"]


def test_claim_flips_to_attempting_and_spends_one_attempt():
    _record()
    _fail_and_age("ob-1", 3600)

    claimed = dl.sweep_retryable()

    assert claimed[0]["attempts"] == 1
    assert claimed[0]["content"] == "the final answer"
    assert claimed[0]["chat_id"] == "C1"
    row = _row("ob-1")
    assert row["state"] == "attempting"
    assert row["attempts"] == 1


def test_retry_always_carries_marker():
    """A 5xx can hide a message that actually landed - never a silent dup."""
    _record()
    _fail_and_age("ob-1", 3600)

    assert dl.sweep_retryable()[0]["needs_marker"] is True


def test_retry_marker_does_not_claim_a_gateway_restart():
    """The crash wording would be a lie here: the gateway never went down."""
    assert "restart" not in dl.RETRY_MARKER.lower()
    assert "duplicate" in dl.RETRY_MARKER.lower()


# --------------------------------------------------------------------------
# Backoff
# --------------------------------------------------------------------------

def test_row_is_not_claimed_before_backoff_elapses():
    _record()
    dl.mark_failed("ob-1", "503")  # updated_at = now

    assert dl.sweep_retryable() == []
    assert _row("ob-1")["state"] == "failed"     # untouched, still retryable


def test_backoff_widens_with_each_attempt():
    """attempts=1 must wait longer than the first retry did."""
    _record()
    _fail_and_age("ob-1", dl.RETRY_BACKOFF_SECONDS[0] + 1)
    assert dl.sweep_retryable()                  # first retry fires

    # That retry failed too; the second backoff step is longer, so an age
    # that satisfied step 0 must NOT satisfy step 1.
    _fail_and_age("ob-1", dl.RETRY_BACKOFF_SECONDS[0] + 1)
    assert _row("ob-1")["attempts"] == 1
    assert dl.sweep_retryable() == []

    _fail_and_age("ob-1", dl.RETRY_BACKOFF_SECONDS[1] + 1)
    assert dl.sweep_retryable()


# --------------------------------------------------------------------------
# Bounds: poison rows cannot spin
# --------------------------------------------------------------------------

def test_attempts_cap_abandons_instead_of_retrying_forever():
    _record()
    _fail_and_age("ob-1", 3600)
    _set("ob-1", attempts=dl.MAX_ATTEMPTS)

    assert dl.sweep_retryable() == []
    assert _row("ob-1")["state"] == "abandoned"


def test_stale_cutoff_abandons_an_old_row():
    _record()
    _fail_and_age("ob-1", 3600)
    _set("ob-1", created_at=time.time() - dl.STALE_AFTER_SECONDS - 1)

    assert dl.sweep_retryable() == []
    assert _row("ob-1")["state"] == "abandoned"


def test_attempts_budget_is_shared_with_the_crash_path():
    """Two sweeps, one budget - a row cannot get 3 retries per sweep."""
    _record()
    _fail_and_age("ob-1", 3600)
    _set("ob-1", attempts=dl.MAX_ATTEMPTS - 1)

    assert len(dl.sweep_retryable()) == 1
    assert _row("ob-1")["attempts"] == dl.MAX_ATTEMPTS

    _fail_and_age("ob-1", 3600)
    assert dl.sweep_retryable() == []
    assert _row("ob-1")["state"] == "abandoned"


# --------------------------------------------------------------------------
# Ownership partition
# --------------------------------------------------------------------------

def test_row_owned_by_another_process_is_left_alone():
    _record()
    _fail_and_age("ob-1", 3600)
    _set("ob-1", owner_pid=os.getpid() + 424242)

    assert dl.sweep_retryable() == []
    assert _row("ob-1")["state"] == "failed"


def test_pid_reuse_by_a_different_process_is_not_mistaken_for_us():
    """Same pid number, different process start time - not ours to retry."""
    _record()
    _fail_and_age("ob-1", 3600)
    row = _row("ob-1")
    if row["owner_started_at"] is None:
        pytest.skip("process start time unavailable on this platform")
    _set("ob-1", owner_started_at=int(row["owner_started_at"]) + 99999)

    assert dl.sweep_retryable() == []
    assert _row("ob-1")["state"] == "failed"


def test_delivered_and_pending_rows_are_never_retried():
    _record("ob-pending")
    _record("ob-delivered")
    dl.mark_delivered("ob-delivered")
    _set("ob-pending", updated_at=time.time() - 3600)
    _set("ob-delivered", updated_at=time.time() - 3600)

    assert dl.sweep_retryable() == []


# --------------------------------------------------------------------------
# Platform filter and config gate
# --------------------------------------------------------------------------

def test_absent_platform_does_not_burn_an_attempt():
    _record(platform="discord")
    _fail_and_age("ob-1", 3600)

    assert dl.sweep_retryable(deliverable_platforms={"slack"}) == []
    row = _row("ob-1")
    assert row["attempts"] == 0        # budget preserved for a later pass
    assert row["state"] == "failed"

    assert dl.sweep_retryable(deliverable_platforms={"discord"})


def test_retry_sweep_gate_defaults_on_and_honours_false():
    assert dl.retry_sweep_enabled({}) is True
    assert dl.retry_sweep_enabled({"gateway": {}}) is True
    assert dl.retry_sweep_enabled(
        {"gateway": {"delivery_retry_sweep": False}}
    ) is False
    assert dl.retry_sweep_enabled(
        {"gateway": {"delivery_retry_sweep": "off"}}
    ) is False
