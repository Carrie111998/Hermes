"""CLI consumer-complete durable recovery.

The gateway watcher sweeps the durable store, but the CLI drains only the
in-memory queue — so an order-DEFERRED claim (higher stream sequence drained
before its lower sibling) used to strand the row until a process restart.
The CLI idle drain now runs the same throttled durable sweep, making live
recovery consumer-complete: reversed wakes deliver low→high without restart.
"""

import queue
import threading

import pytest

from cli import HermesCLI
from tools import async_delegation as ad


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    monkeypatch.setattr(ad, "_SWEEP_MIN_PENDING_AGE_S", 0.0)
    monkeypatch.setattr(ad, "_SWEEP_REWAKE_INTERVAL_S", 0.0)
    monkeypatch.setattr(ad, "_SWEEP_SCAN_INTERVAL_S", 0.0)
    import tools.process_registry as pr_module

    registry = pr_module.ProcessRegistry()
    monkeypatch.setattr(pr_module, "process_registry", registry)
    yield registry
    ad._reset_for_tests()


def _cli():
    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = "cli-session"
    cli._session_db = None
    cli._pending_input = queue.Queue()
    return cli


def test_cli_reversed_ordered_wakes_recover_low_to_high_without_restart(_isolated):
    for seq in (1, 2):
        ad.publish_background_notification(
            summary=f"cli stream event {seq}",
            session_key="cli-session",
            notification_id=f"clistream/{seq}",
            stream_id="clistream",
            sequence=seq,
        )
    # Reverse the immediate wakes: the higher sequence is drained first.
    wakes = {}
    while not _isolated.completion_queue.empty():
        evt = _isolated.completion_queue.get_nowait()
        wakes[evt["delegation_id"]] = evt
    _isolated.completion_queue.put(wakes["clistream/2"])
    _isolated.completion_queue.put(wakes["clistream/1"])

    cli = _cli()
    # First drain: seq2's claim is DEFERRED (lower sibling undelivered) and
    # the queue copy is consumed; seq1 delivers. Subsequent idle drains run
    # the durable sweep, which re-wakes seq2 — no restart involved.
    delivered = []
    for _ in range(5):
        cli._drain_process_notifications("cli-idle")
        while not cli._pending_input.empty():
            delivered.append(cli._pending_input.get_nowait())
        if len(delivered) == 2:
            break

    assert len(delivered) == 2
    assert "cli stream event 1" in delivered[0]
    assert "cli stream event 2" in delivered[1]
    assert ad.get_durable_delegation("clistream/1")["delivery_state"] == "delivered"
    assert ad.get_durable_delegation("clistream/2")["delivery_state"] == "delivered"


def test_cli_sweep_recovers_a_lost_wake_for_its_own_session(_isolated):
    ad.publish_background_notification(
        summary="lost wake body",
        session_key="cli-session",
        notification_id="cli_lost",
    )
    while not _isolated.completion_queue.empty():
        _isolated.completion_queue.get_nowait()  # the immediate wake is lost
    ad._clear_wake_activity("cli_lost")

    cli = _cli()
    delivered = []
    for _ in range(3):
        cli._drain_process_notifications("cli-idle")
        while not cli._pending_input.empty():
            delivered.append(cli._pending_input.get_nowait())
        if delivered:
            break

    assert len(delivered) == 1
    assert "lost wake body" in delivered[0]
    assert ad.get_durable_delegation("cli_lost")["delivery_state"] == "delivered"


def test_cli_sweep_does_not_deliver_a_foreign_sessions_event(_isolated):
    ad.publish_background_notification(
        summary="belongs to another window",
        session_key="other-session",
        notification_id="cli_foreign",
    )
    while not _isolated.completion_queue.empty():
        _isolated.completion_queue.get_nowait()
    ad._clear_wake_activity("cli_foreign")

    cli = _cli()
    for _ in range(3):
        cli._drain_process_notifications("cli-idle")
    assert cli._pending_input.empty()
    row = ad.get_durable_delegation("cli_foreign")
    assert row["delivery_state"] == "pending"  # left for its owner
    assert row["delivery_attempts"] == 0

    # A sweep-restored durable replay stays queued for the owner, not dropped.
    assert not _isolated.completion_queue.empty()
