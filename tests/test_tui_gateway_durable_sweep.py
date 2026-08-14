"""TUI/Desktop consumer-complete durable recovery.

Each Desktop session runs one notification poller against the shared
completion queue. The pollers now run the same throttled durable sweep as the
gateway watcher (one process-global SQLite scan per interval regardless of
session count), so a lost queue wake or an order-DEFERRED stream claim
recovers live instead of waiting for a restart — and a deferred claim must
release the poller's optimistic ``running`` flag instead of wedging the
session busy forever.
"""

import threading
import time
import types

import pytest

import tools.async_delegation as ad
from tui_gateway import server


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


def _session(session_key="tui-session-key"):
    return {
        "agent": types.SimpleNamespace(),
        "session_key": session_key,
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "transport": None,
        "attached_images": [],
    }


def _run_poller_until(monkeypatch, session, done, timeout=8.0):
    """Drive the real poller loop in a thread until ``done()`` or timeout."""
    delivered = []

    def _fake_run_prompt_submit(rid, sid, sess, text, **_kwargs):
        delivered.append(text)
        with sess["history_lock"]:
            sess["running"] = False

    monkeypatch.setattr(server, "_run_prompt_submit", _fake_run_prompt_submit)
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)

    stop = threading.Event()
    thread = threading.Thread(
        target=server._notification_poller_loop,
        args=(stop, "tui-sid", session),
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not done(delivered):
        time.sleep(0.05)
    stop.set()
    thread.join(timeout=3)
    return delivered


def test_tui_reversed_ordered_wakes_recover_low_to_high_without_restart(
    _isolated, monkeypatch,
):
    for seq in (1, 2):
        ad.publish_background_notification(
            summary=f"tui stream event {seq}",
            session_key="tui-session-key",
            notification_id=f"tuistream/{seq}",
            stream_id="tuistream",
            sequence=seq,
        )
    # Reverse the immediate wakes: seq2 is dequeued first, its claim is
    # DEFERRED (lower sibling undelivered), and the sweep must re-wake it
    # after seq1 delivers — all in one live process.
    wakes = {}
    while not _isolated.completion_queue.empty():
        evt = _isolated.completion_queue.get_nowait()
        wakes[evt["delegation_id"]] = evt
    _isolated.completion_queue.put(wakes["tuistream/2"])
    _isolated.completion_queue.put(wakes["tuistream/1"])

    delivered = _run_poller_until(
        monkeypatch, _session(), done=lambda d: len(d) >= 2,
    )

    assert len(delivered) == 2
    assert "tui stream event 1" in delivered[0]
    assert "tui stream event 2" in delivered[1]
    assert ad.get_durable_delegation("tuistream/1")["delivery_state"] == "delivered"
    assert ad.get_durable_delegation("tuistream/2")["delivery_state"] == "delivered"


def test_tui_poller_recovers_a_lost_wake_without_restart(_isolated, monkeypatch):
    ad.publish_background_notification(
        summary="tui lost wake body",
        session_key="tui-session-key",
        notification_id="tui_lost",
    )
    while not _isolated.completion_queue.empty():
        _isolated.completion_queue.get_nowait()  # the immediate wake is lost
    ad._clear_wake_activity("tui_lost")

    delivered = _run_poller_until(
        monkeypatch, _session(), done=lambda d: len(d) >= 1,
    )

    assert len(delivered) == 1
    assert "tui lost wake body" in delivered[0]
    assert ad.get_durable_delegation("tui_lost")["delivery_state"] == "delivered"


def test_tui_deferred_claim_releases_the_running_flag(_isolated, monkeypatch):
    """A claim refusal must reset the poller's optimistic busy flag, or every
    later event requeues forever behind a phantom turn."""
    for seq in (1, 2):
        ad.publish_background_notification(
            summary=f"wedge stream event {seq}",
            session_key="tui-session-key",
            notification_id=f"wedge/{seq}",
            stream_id="wedge",
            sequence=seq,
        )
    wakes = {}
    while not _isolated.completion_queue.empty():
        evt = _isolated.completion_queue.get_nowait()
        wakes[evt["delegation_id"]] = evt
    # Only the HIGHER sequence's wake arrives; its claim defers.
    _isolated.completion_queue.put(wakes["wedge/2"])

    session = _session()
    delivered = _run_poller_until(
        monkeypatch, session, done=lambda d: len(d) >= 2,
    )

    # Both eventually deliver (sweep re-wakes seq1 then seq2, in order), which
    # is only possible if the deferred first claim released `running`.
    assert len(delivered) == 2
    assert "wedge stream event 1" in delivered[0]
    assert "wedge stream event 2" in delivered[1]
    with session["history_lock"]:
        assert session["running"] is False
