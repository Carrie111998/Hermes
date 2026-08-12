"""Progress heartbeat: dispatcher-side "still working on it" pings.

Covers the two halves of the feature together, because the interesting
failure modes live at the seam:

* ``kanban_db.emit_progress_events`` — emit a ``progress`` event for a
  running task once the interval has elapsed, and for nothing else.
* the gateway notifier — claim and render those events in plain English
  without ever unsubscribing the subscriber.
"""

import asyncio

from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb


class RecordingAdapter:
    def __init__(self):
        self.sent = []
        self.handled = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})

    async def handle_message(self, event):
        self.handled.append(event)


def _make_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    return runner


async def _run_one_notifier_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _board(tmp_path, monkeypatch, name):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / name))
    kb.init_db()


def _running_task(conn, *, title="build the thing", assignee="worker"):
    """Create a task and take it to 'running' the way a worker would."""
    tid = kb.create_task(conn, title=title, assignee=assignee)
    assert kb.claim_task(conn, tid) is not None
    return tid


def _age_run(conn, tid, seconds):
    """Backdate the task/run start so it looks like it's been running a while."""
    conn.execute(
        "UPDATE tasks SET started_at = started_at - ? WHERE id = ?",
        (seconds, tid),
    )
    conn.execute(
        "UPDATE task_runs SET started_at = started_at - ? WHERE task_id = ?",
        (seconds, tid),
    )
    conn.commit()


def _progress_events(conn, tid):
    return conn.execute(
        "SELECT payload, created_at FROM task_events "
        "WHERE task_id = ? AND kind = 'progress' ORDER BY id",
        (tid,),
    ).fetchall()


# ---------------------------------------------------------------------------
# (a) emitted only once the interval has elapsed
# ---------------------------------------------------------------------------

def test_progress_event_waits_for_the_interval(tmp_path, monkeypatch):
    _board(tmp_path, monkeypatch, "interval.db")
    conn = kb.connect()
    try:
        tid = _running_task(conn)

        # Just started — far too early to claim we've been at it a while.
        assert kb.emit_progress_events(conn, 300) == []
        assert _progress_events(conn, tid) == []

        _age_run(conn, tid, 400)
        assert kb.emit_progress_events(conn, 300) == [tid]
        rows = _progress_events(conn, tid)
        assert len(rows) == 1

        import json
        payload = json.loads(rows[0]["payload"])
        assert payload["task_id"] == tid
        assert payload["title"] == "build the thing"
        assert payload["assignee"] == "worker"
        assert payload["elapsed_minutes"] == 6  # 400s
        assert payload["note"] is None
        assert payload["board"]

        # Second tick right after: the interval restarts from the ping we
        # just wrote, so the user is not spammed every dispatcher tick.
        assert kb.emit_progress_events(conn, 300) == []
        assert len(_progress_events(conn, tid)) == 1

        # ...but once another interval passes, another ping goes out.
        conn.execute(
            "UPDATE task_events SET created_at = created_at - 400 "
            "WHERE task_id = ? AND kind = 'progress'",
            (tid,),
        )
        conn.commit()
        assert kb.emit_progress_events(conn, 300) == [tid]
        assert len(_progress_events(conn, tid)) == 2
    finally:
        conn.close()


def test_progress_event_carries_latest_heartbeat_note(tmp_path, monkeypatch):
    """The note the worker left is what makes the ping worth reading."""
    _board(tmp_path, monkeypatch, "note.db")
    conn = kb.connect()
    try:
        tid = _running_task(conn)
        assert kb.heartbeat_worker(conn, tid, note="ran the migration")
        assert kb.heartbeat_worker(conn, tid, note="now backfilling rows")
        _age_run(conn, tid, 900)

        assert kb.emit_progress_events(conn, 300) == [tid]
        import json
        payload = json.loads(_progress_events(conn, tid)[0]["payload"])
        assert payload["note"] == "now backfilling rows"

        # A note from BEFORE the last ping is not repeated: the user has
        # already seen it, and re-sending it reads as "no progress". Push
        # both events into the past, keeping the notes older than the ping.
        conn.execute(
            "UPDATE task_events SET created_at = created_at - 400 "
            "WHERE task_id = ? AND kind = 'progress'",
            (tid,),
        )
        conn.execute(
            "UPDATE task_events SET created_at = created_at - 800 "
            "WHERE task_id = ? AND kind = 'heartbeat'",
            (tid,),
        )
        conn.commit()
        assert kb.emit_progress_events(conn, 300) == [tid]
        payload = json.loads(_progress_events(conn, tid)[1]["payload"])
        assert payload["note"] is None
    finally:
        conn.close()


def test_progress_ping_disabled_by_zero_interval(tmp_path, monkeypatch):
    _board(tmp_path, monkeypatch, "disabled.db")
    conn = kb.connect()
    try:
        tid = _running_task(conn)
        _age_run(conn, tid, 3600)
        assert kb.emit_progress_events(conn, 0) == []
        assert _progress_events(conn, tid) == []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# (b) never emitted for a task that isn't running
# ---------------------------------------------------------------------------

def test_no_progress_event_for_non_running_tasks(tmp_path, monkeypatch):
    _board(tmp_path, monkeypatch, "not-running.db")
    conn = kb.connect()
    try:
        # ready (never claimed)
        ready_id = kb.create_task(conn, title="waiting to start", assignee="worker")
        conn.execute(
            "UPDATE tasks SET started_at = strftime('%s','now') - 3600 WHERE id = ?",
            (ready_id,),
        )
        # blocked
        blocked_id = _running_task(conn, title="stuck task")
        _age_run(conn, blocked_id, 3600)
        kb.block_task(conn, blocked_id, reason="needs credentials")
        conn.commit()

        assert kb.emit_progress_events(conn, 300) == []
        assert _progress_events(conn, ready_id) == []
        assert _progress_events(conn, blocked_id) == []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# (d) a completed task neither emits nor notifies
# ---------------------------------------------------------------------------

def test_completed_task_emits_no_progress_event(tmp_path, monkeypatch):
    _board(tmp_path, monkeypatch, "completed.db")
    conn = kb.connect()
    try:
        tid = _running_task(conn, title="already finished")
        _age_run(conn, tid, 3600)
        kb.complete_task(conn, tid, summary="all done")

        assert kb.emit_progress_events(conn, 300) == []
        assert _progress_events(conn, tid) == []
    finally:
        conn.close()


def test_notifier_skips_progress_ping_for_a_finished_task(tmp_path, monkeypatch):
    """A ping that lost the race with completion must not be delivered.

    The dispatcher can emit a progress event moments before the worker
    calls ``kanban_complete``. Sending "still working on it" next to (or
    after) the completion message is the exact confusing double-ping this
    feature must avoid, so the notifier drops it — while still advancing
    the cursor past it.
    """
    _board(tmp_path, monkeypatch, "raced.db")
    conn = kb.connect()
    try:
        tid = _running_task(conn, title="finished mid-ping")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        _age_run(conn, tid, 900)
        assert kb.emit_progress_events(conn, 300) == [tid]
        kb.complete_task(conn, tid, summary="wrapped up")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    texts = [s["text"] for s in adapter.sent]
    assert not any("Still working on" in t for t in texts), texts
    assert any("done" in t for t in texts), texts

    conn = kb.connect()
    try:
        _, remaining = kb.unseen_events_for_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-1",
            kinds=["progress", "completed"],
        )
    finally:
        conn.close()
    assert remaining == [], "cursor must advance past the dropped progress event"


# ---------------------------------------------------------------------------
# (c) the notifier claims + formats progress, and keeps the subscription
# ---------------------------------------------------------------------------

def test_notifier_delivers_progress_ping_and_keeps_subscription(tmp_path, monkeypatch):
    _board(tmp_path, monkeypatch, "notify-progress.db")
    conn = kb.connect()
    try:
        tid = _running_task(conn, title="reindex the archive", assignee="scribe")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        assert kb.heartbeat_worker(conn, tid, note="halfway through the backlog")
        _age_run(conn, tid, 7 * 60)
        assert kb.emit_progress_events(conn, 300) == [tid]
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1, adapter.sent
    text = adapter.sent[0]["text"]
    assert "Still working on reindex the archive" in text
    assert "running as scribe" in text
    assert "about 7 minutes in" in text
    assert "Latest update: halfway through the backlog" in text

    conn = kb.connect()
    try:
        # Subscription survives: progress is proof of life, not a terminal
        # event, so the user must still get the completion ping later.
        subs = kb.list_notify_subs(conn)
        assert [s["task_id"] for s in subs] == [tid]
        # Cursor advanced — the same ping is not re-delivered next tick.
        _, remaining = kb.unseen_events_for_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-1",
            kinds=["progress"],
        )
    finally:
        conn.close()
    assert remaining == []

    # Second tick with no new event: silence, and still subscribed.
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert len(adapter.sent) == 1


def test_notifier_progress_ping_without_a_note(tmp_path, monkeypatch):
    _board(tmp_path, monkeypatch, "notify-no-note.db")
    conn = kb.connect()
    try:
        tid = _running_task(conn, title="quiet worker", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        _age_run(conn, tid, 305)
        assert kb.emit_progress_events(conn, 300) == [tid]
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1, adapter.sent
    text = adapter.sent[0]["text"]
    assert "Still working on quiet worker" in text
    assert "No status update yet." in text


# ---------------------------------------------------------------------------
# dispatcher wiring
# ---------------------------------------------------------------------------

def test_dispatcher_tick_emits_progress_pings(tmp_path, monkeypatch):
    """The dispatcher tick is what actually drives the pings in production."""
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "home"))
    _board(tmp_path, monkeypatch, "dispatch-tick.db")
    conn = kb.connect()
    try:
        tid = _running_task(conn, title="long job")
        _age_run(conn, tid, 600)
    finally:
        conn.close()

    import hermes_cli.config as _cfg_mod
    monkeypatch.setattr(
        _cfg_mod, "load_config",
        lambda: {
            "kanban": {
                "dispatch_in_gateway": True,
                "dispatch_interval_seconds": 1,
                "auto_decompose": False,
                "progress_notify_interval_seconds": 300,
            }
        },
    )
    # Real dispatch would reclaim/respawn this hand-made running task; the
    # ping path is what's under test here.
    monkeypatch.setattr(kb, "dispatch_once", lambda conn, **kwargs: None)
    monkeypatch.setattr(kb, "reap_worker_zombies", lambda: [])

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True

    calls = {"n": 0}

    async def _to_thread(fn, *args, **kwargs):
        calls["n"] += 1
        result = fn(*args, **kwargs)
        if calls["n"] >= 3:  # reaper + _tick_once + _ready_nonempty
            runner._running = False
        return result

    async def _sleep(_delay):
        return None

    monkeypatch.setattr(asyncio, "to_thread", _to_thread)
    monkeypatch.setattr(asyncio, "sleep", _sleep)
    asyncio.run(
        asyncio.wait_for(runner._kanban_dispatcher_watcher(), timeout=5.0)
    )

    conn = kb.connect()
    try:
        assert len(_progress_events(conn, tid)) == 1
    finally:
        conn.close()


def test_dispatcher_tick_skips_progress_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "home"))
    _board(tmp_path, monkeypatch, "dispatch-tick-off.db")
    conn = kb.connect()
    try:
        tid = _running_task(conn, title="long job")
        _age_run(conn, tid, 600)
    finally:
        conn.close()

    import hermes_cli.config as _cfg_mod
    monkeypatch.setattr(
        _cfg_mod, "load_config",
        lambda: {
            "kanban": {
                "dispatch_in_gateway": True,
                "dispatch_interval_seconds": 1,
                "auto_decompose": False,
                "progress_notify_interval_seconds": 0,
            }
        },
    )
    monkeypatch.setattr(kb, "dispatch_once", lambda conn, **kwargs: None)
    monkeypatch.setattr(kb, "reap_worker_zombies", lambda: [])

    def _boom(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("progress ping must not run when disabled")

    monkeypatch.setattr(kb, "emit_progress_events", _boom)

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True

    calls = {"n": 0}

    async def _to_thread(fn, *args, **kwargs):
        calls["n"] += 1
        result = fn(*args, **kwargs)
        if calls["n"] >= 3:
            runner._running = False
        return result

    async def _sleep(_delay):
        return None

    monkeypatch.setattr(asyncio, "to_thread", _to_thread)
    monkeypatch.setattr(asyncio, "sleep", _sleep)
    asyncio.run(
        asyncio.wait_for(runner._kanban_dispatcher_watcher(), timeout=5.0)
    )

    conn = kb.connect()
    try:
        assert _progress_events(conn, tid) == []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# config default
# ---------------------------------------------------------------------------

def test_progress_notify_interval_default():
    from hermes_cli.config import DEFAULT_CONFIG
    kanban = DEFAULT_CONFIG.get("kanban", {})
    interval = kanban.get("progress_notify_interval_seconds")
    assert isinstance(interval, int) and interval == 300, (
        "kanban.progress_notify_interval_seconds should default to 300 "
        f"(5 minutes); got {interval!r}"
    )
