"""The TUI notification poller must stay bound to the home its session started in.

``_start_notification_poller`` starts one daemon thread per TUI session. The
loop calls ``claim_event_delivery`` / ``complete_event_delivery`` /
``release_event_delivery``, which reach ``tools.async_delegation._connect()``
-> ``_db_path()`` -> ``parent.mkdir()`` + ``sqlite3.connect()``. That last call
CREATES ``<home>/state.db``.

The thread's lifetime is bounded by the *session*, not by the scope that
started it, so a tick landing after ``HERMES_HOME`` moves resolves the restored
home — under pytest, the real ``~/.hermes`` — and materialises a state.db there.

The rule (GBrain ``concepts/import-time-hermes-home-snapshot-bug``): resolve at
the moment the value's meaning is fixed, then CARRY it. For this thread that
moment is session start.
"""

import queue as _queue_mod
import sqlite3
import threading
import types

import pytest

import tools.async_delegation as ad
from tui_gateway import server


def _session(**extra):
    return {
        "agent": types.SimpleNamespace(),
        "session_key": "session-key",
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "attached_images": [],
        "image_counter": 0,
        "cols": 80,
        "slash_worker": None,
        "show_reasoning": False,
        "tool_progress_mode": "all",
        **extra,
    }


@pytest.fixture()
def homes(tmp_path, monkeypatch):
    home_a = tmp_path / "home_a"
    home_b = tmp_path / "home_b"
    home_a.mkdir()
    home_b.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home_a))
    return home_a, home_b


def _delegation_event(sid):
    return {
        "type": "async_delegation",
        "delegation_id": "deleg_bind_test",
        "origin_ui_session_id": sid,
        "session_id": "proc_bind_test",
        "command": "echo hi",
        "exit_code": 0,
        "output": "hi",
    }


# ---------------------------------------------------------------------------
# The capture: the poller must be handed a path, or there is nothing to carry.
# ---------------------------------------------------------------------------


def test_start_notification_poller_captures_the_db_path(homes, monkeypatch):
    """Capture happens at session start — the moment the db's meaning is fixed."""
    home_a, _ = homes
    started = {}

    class _RecordingThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None, **_):
            started["args"] = args
            started["kwargs"] = kwargs or {}

        def start(self):
            pass

    monkeypatch.setattr(server, "_wire_agent_terminal_output", lambda: None)
    monkeypatch.setattr(server.threading, "Thread", _RecordingThread)

    server._start_notification_poller("sid_capture", _session())

    carried = list(started["args"]) + list(started["kwargs"].values())
    assert home_a / "state.db" in carried, (
        "_start_notification_poller did not capture the delegation db path, so "
        "the poller thread has nothing to carry and must resolve it live"
    )


# ---------------------------------------------------------------------------
# The carry: a tick landing after the env moved must not follow it.
# ---------------------------------------------------------------------------


def test_poller_loop_delivery_stays_with_the_captured_home(homes, monkeypatch):
    from tools.process_registry import process_registry

    home_a, home_b = homes
    sid = "sid_bind"
    sess = _session()
    sess["running"] = False

    isolated: _queue_mod.Queue = _queue_mod.Queue()
    monkeypatch.setattr(process_registry, "completion_queue", isolated)
    monkeypatch.setattr(server, "_emit", lambda *a, **kw: None)
    monkeypatch.setattr(server, "_run_prompt_submit", lambda *a, **kw: None)
    monkeypatch.setattr(
        "tools.process_registry.format_process_notification",
        lambda evt: "[IMPORTANT: delegation finished]",
    )

    stop = threading.Event()
    isolated.put(_delegation_event(sid))
    stop.set()  # exactly one iteration, then the drain

    # The moment monkeypatch teardown restores the env under the thread.
    monkeypatch.setenv("HERMES_HOME", str(home_b))

    server._notification_poller_loop(stop, sid, sess, db_path=home_a / "state.db")

    assert not (home_b / "state.db").exists(), (
        "the poller followed HERMES_HOME after the env moved — on a real run "
        "that call CREATES ~/.hermes/state.db from a background thread"
    )
    assert (home_a / "state.db").exists(), (
        "delivery never reached the db captured at session start"
    )


# ---------------------------------------------------------------------------
# The leaf seams in tools/async_delegation.py.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda db: ad.claim_event_delivery(
            {"type": "async_delegation", "delegation_id": "d1"}, "tui-poller", db_path=db
        ),
        lambda db: ad.complete_event_delivery(
            {"type": "async_delegation", "delegation_id": "d1"}, "claim1", db_path=db
        ),
        lambda db: ad.release_event_delivery(
            {"type": "async_delegation", "delegation_id": "d1"}, "claim1", db_path=db
        ),
    ],
    ids=["claim", "complete", "release"],
)
def test_delivery_helpers_write_only_to_the_carried_db(homes, monkeypatch, call):
    home_a, home_b = homes
    monkeypatch.setenv("HERMES_HOME", str(home_b))

    call(home_a / "state.db")

    assert not (home_b / "state.db").exists(), (
        "delivery helper resolved HERMES_HOME instead of using the carried path"
    )


def test_connect_skips_when_the_captured_home_is_gone(tmp_path):
    """A thread bound to a deleted pytest tmp_path must not recreate it."""
    gone = tmp_path / "deleted_home"  # never created

    with pytest.raises(ad.BoundHomeMissing):
        ad._connect(db_path=gone / "state.db")

    assert not gone.exists(), (
        "connect recreated a home that no longer exists — a thread bound to a "
        "deleted tmp_path must leave no litter"
    )


def test_delivery_helpers_swallow_a_missing_bound_home(tmp_path, homes):
    """The poller must degrade to "not claimed", not crash, when its home is gone."""
    gone = tmp_path / "deleted_home" / "state.db"
    evt = {"type": "async_delegation", "delegation_id": "d1"}

    assert ad.claim_event_delivery(evt, "tui-poller", db_path=gone) is None
    ad.complete_event_delivery(evt, "claim1", db_path=gone)
    ad.release_event_delivery(evt, "claim1", db_path=gone)


def test_direct_callers_still_resolve_live(homes):
    """Passing no path keeps the current, correct behaviour for live callers."""
    home_a, _ = homes

    conn = ad._connect()
    try:
        assert isinstance(conn, sqlite3.Connection)
    finally:
        conn.close()

    assert (home_a / "state.db").exists()
