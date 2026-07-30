"""Kanban notifier behavior on stateless (api_server) subscriptions.

Covers the wrong-session-wake / silent-loss fixes:
* a SendResult(success=False) return (the API server's send() stub) rewinds
  the cursor instead of advancing past a never-delivered event;
* api_server subscriptions wake the creator's REAL session via the
  /v1/chat/completions self-post (raw task.session_id), never via
  handle_message (which would run under a build_session_key()-derived key
  that never matches the raw X-Hermes-Session-Id session real turns use).
"""

import asyncio

from gateway.config import Platform
from gateway.platforms.base import SendResult
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb

_REAL_ASYNCIO_SLEEP = asyncio.sleep


class SoftFailAdapter:
    """Push-capable adapter whose send() returns SendResult(success=False)
    WITHOUT raising — previously treated as delivered (event lost)."""

    def __init__(self):
        self.attempts = 0

    async def send(self, chat_id, text, metadata=None):
        self.attempts += 1
        return SendResult(success=False, error="soft failure")


class ApiServerLikeAdapter:
    supports_async_delivery = False

    def __init__(self):
        self._host = "127.0.0.1"
        self._port = 8642
        self._api_key = "k"
        self._model_name = "hermes"
        self.handle_message_calls = []
        self.send_calls = 0

    async def send(self, chat_id, text, metadata=None):
        self.send_calls += 1
        return SendResult(
            success=False,
            error="API server uses HTTP request/response, not send()",
        )

    async def handle_message(self, event):
        self.handle_message_calls.append(event)


async def _run_one_notifier_tick(monkeypatch, runner):
    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await _REAL_ASYNCIO_SLEEP(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _make_runner(adapters):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = adapters
    runner._kanban_sub_fail_counts = {}
    return runner


def _create_completed_subscription(platform, chat_id, session_id=None):
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="notify once", assignee="worker", session_id=session_id,
        )
        kb.add_notify_sub(conn, task_id=tid, platform=platform, chat_id=chat_id)
        kb.complete_task(conn, tid, summary="done once")
        return tid
    finally:
        conn.close()


def _unseen_terminal_events(tid, platform, chat_id):
    conn = kb.connect()
    try:
        _, events = kb.unseen_events_for_sub(
            conn,
            task_id=tid,
            platform=platform,
            chat_id=chat_id,
            kinds=["completed", "blocked", "gave_up", "crashed", "timed_out"],
        )
        return events
    finally:
        conn.close()


def test_apiserver_sub_wakes_real_session_via_self_post(tmp_path, monkeypatch):
    """An api_server subscription wakes the creator's REAL session by
    self-posting with the task's raw session_id — never handle_message (which
    would run the wake under a build_session_key()-derived key that can't
    match the raw X-Hermes-Session-Id session)."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "apiserver.db"))
    kb.init_db()
    tid = _create_completed_subscription(
        "api_server", "raw-sid-123", session_id="raw-sid-123",
    )

    posts = []

    async def fake_self_post(adapter, *, text, session_id):
        posts.append({"text": text, "session_id": session_id})

    import gateway.wake as wake_mod

    monkeypatch.setattr(wake_mod, "_self_post_chat_completion", fake_self_post)

    adapter = ApiServerLikeAdapter()
    runner = _make_runner({Platform.API_SERVER: adapter})
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert adapter.handle_message_calls == [], (
        "api_server wake must not go through handle_message (wrong-session bug)"
    )
    assert adapter.send_calls == 0, (
        "non-push delivery is the raw-session wake and must never call send()"
    )
    assert len(posts) == 1
    assert posts[0]["session_id"] == "raw-sid-123"
    assert tid in posts[0]["text"]
    # The wake self-post IS the delivery on this path (no separate text-ping
    # fallback is attempted for stateless api_server subs) — cursor advances
    # once the wake succeeds.
    assert _unseen_terminal_events(tid, "api_server", "raw-sid-123") == []


def test_apiserver_wake_failure_stays_pending_then_retries(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv(
        "HERMES_KANBAN_DB",
        str(tmp_path / "apiserver-wake-retry.db"),
    )
    kb.init_db()
    tid = _create_completed_subscription(
        "api_server",
        "raw-sid-retry",
        session_id="raw-sid-retry",
    )

    attempts = []

    async def flaky_self_post(adapter, *, text, session_id):
        attempts.append(session_id)
        if len(attempts) == 1:
            raise RuntimeError("transient wake failure")

    import gateway.wake as wake_mod

    monkeypatch.setattr(wake_mod, "_self_post_chat_completion", flaky_self_post)

    adapter = ApiServerLikeAdapter()
    runner = _make_runner({Platform.API_SERVER: adapter})
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert adapter.send_calls == 0
    assert attempts == ["raw-sid-retry"]
    conn = kb.connect()
    try:
        row = conn.execute(
            "SELECT state, attempts, last_error "
            "FROM kanban_notification_outbox"
        ).fetchone()
        assert row["state"] == "pending"
        assert row["attempts"] == 1
        assert "transient wake failure" in row["last_error"]
        assert len(kb.list_notify_subs(conn, tid)) == 1
    finally:
        conn.close()

    runner._running = True
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert adapter.send_calls == 0
    assert attempts == ["raw-sid-retry", "raw-sid-retry"]
    conn = kb.connect()
    try:
        assert kb.list_notify_subs(conn, tid) == []
    finally:
        conn.close()


def test_apiserver_slow_wake_keeps_outbox_lease_alive(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv(
        "HERMES_KANBAN_DB",
        str(tmp_path / "apiserver-wake-heartbeat.db"),
    )
    kb.init_db()
    tid = _create_completed_subscription(
        "api_server",
        "raw-sid-heartbeat",
        session_id="raw-sid-heartbeat",
    )
    monkeypatch.setattr(kb, "NOTIFICATION_LEASE_SECONDS", 1)

    adapter = ApiServerLikeAdapter()
    runner = _make_runner({Platform.API_SERVER: adapter})

    async def exercise():
        wake_started = asyncio.Event()
        release_wake = asyncio.Event()
        heartbeat_renewed = asyncio.Event()
        renewal_count = 0
        real_renew = runner._kanban_renew_notification_lease

        async def blocking_self_post(adapter, *, text, session_id):
            assert session_id == "raw-sid-heartbeat"
            wake_started.set()
            await release_wake.wait()

        def tracked_renew(*args, **kwargs):
            nonlocal renewal_count
            renewed = real_renew(*args, **kwargs)
            if renewed:
                renewal_count += 1
                if renewal_count >= 2:
                    heartbeat_renewed.set()
            return renewed

        import gateway.wake as wake_mod

        monkeypatch.setattr(
            wake_mod,
            "_self_post_chat_completion",
            blocking_self_post,
        )
        monkeypatch.setattr(
            runner,
            "_kanban_renew_notification_lease",
            tracked_renew,
        )

        async def controlled_sleep(delay):
            if delay == 5:
                return None
            if delay == 1:
                runner._running = False
            await _REAL_ASYNCIO_SLEEP(0)

        monkeypatch.setattr(asyncio, "sleep", controlled_sleep)
        watcher = asyncio.create_task(
            runner._kanban_notifier_watcher(interval=1)
        )
        await asyncio.wait_for(wake_started.wait(), timeout=2)
        await asyncio.wait_for(heartbeat_renewed.wait(), timeout=2)
        release_wake.set()
        await asyncio.wait_for(watcher, timeout=2)
        return renewal_count

    renewal_count = asyncio.run(exercise())

    assert renewal_count >= 2, (
        "the raw-session wake must stay inside the outbox lease heartbeat"
    )
    assert adapter.send_calls == 0
    conn = kb.connect()
    try:
        assert kb.list_notify_subs(conn, tid) == []
    finally:
        conn.close()
