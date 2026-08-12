import asyncio

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb


class PushAdapter:
    def __init__(self, wake_failures=0):
        self.sent = []
        self.handled = []
        self.wake_failures = wake_failures

    async def send(self, chat_id, text, metadata=None):
        self.sent.append((chat_id, text, metadata))

    async def handle_message(self, event):
        self.handled.append(event)
        if self.wake_failures:
            self.wake_failures -= 1
            raise RuntimeError("wake failed")


class NonPushAdapter:
    supports_async_delivery = False

    def __init__(self):
        self.send_calls = 0

    async def send(self, chat_id, text, metadata=None):
        self.send_calls += 1


async def _run_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _runner(adapter, platform=Platform.TELEGRAM):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {platform: adapter}
    runner._profile_adapters = {}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    return runner


def _completed_sub(
    *, platform="telegram", chat_id="chat-1", thread_id="", profile="", chat_type="group"
):
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="delivery mode",
            assignee="worker",
            session_id="origin-session",
        )
        kb.add_notify_sub(
            conn,
            task_id=task_id,
            platform=platform,
            chat_id=chat_id,
            thread_id=thread_id,
            notifier_profile=profile,
            chat_type=chat_type,
        )
        kb.complete_task(conn, task_id, summary="done")
        return task_id
    finally:
        conn.close()


def _unblocked_sub():
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="silent delivery", assignee="worker")
        assert kb.block_task(conn, task_id, reason="waiting")
        kb.add_notify_sub(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="chat-1",
        )
        before = kb.list_notify_subs(conn, task_id)[0]["last_event_id"]
        assert kb.unblock_task(conn, task_id)
        return task_id, before
    finally:
        conn.close()


def _event_sub(kind, *, platform="telegram", chat_id="chat-1"):
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="agent-only event",
            assignee="worker",
            session_id="origin-session",
        )
        kb.add_notify_sub(
            conn,
            task_id=task_id,
            platform=platform,
            chat_id=chat_id,
        )
        before = kb.list_notify_subs(conn, task_id)[0]["last_event_id"]
        payload = {
            "status": {"status": "running"},
            "review_requested": {"summary": "ready"},
            "block_loop_detected": {"reason": "needs input", "recurrences": 2},
        }[kind]
        kb._append_event(conn, task_id, kind, payload)
        return task_id, before
    finally:
        conn.close()


def _configure(monkeypatch, value=...):
    import hermes_cli.config as config

    kanban = {} if value is ... else {"notification_delivery_mode": value}
    monkeypatch.setattr(config, "load_config", lambda: {"kanban": kanban})


def _subs(task_id):
    conn = kb.connect()
    try:
        return kb.list_notify_subs(conn, task_id)
    finally:
        conn.close()


@pytest.mark.parametrize("configured", [..., "text_and_agent", "unknown", None])
def test_default_and_invalid_modes_send_native_text(tmp_path, monkeypatch, configured):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    kb.init_db()
    _configure(monkeypatch, configured)
    _completed_sub()
    adapter = PushAdapter()

    asyncio.run(_run_tick(monkeypatch, _runner(adapter)))

    assert len(adapter.sent) == 1
    assert len(adapter.handled) == 1


def test_agent_only_uses_wake_as_delivery_and_skips_native_artifacts(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    kb.init_db()
    _configure(monkeypatch, "agent_only")
    task_id = _completed_sub()
    adapter = PushAdapter()
    runner = _runner(adapter)
    artifact_calls = []

    async def record_artifacts(**kwargs):
        artifact_calls.append(kwargs)

    runner._deliver_kanban_artifacts = record_artifacts
    asyncio.run(_run_tick(monkeypatch, runner))

    assert adapter.sent == []
    assert len(adapter.handled) == 1
    assert artifact_calls == []
    assert _subs(task_id) == []


def test_agent_only_failed_wake_rewinds_and_retries_without_loss(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    kb.init_db()
    _configure(monkeypatch, "agent_only")
    task_id = _completed_sub()
    adapter = PushAdapter(wake_failures=1)
    runner = _runner(adapter)

    asyncio.run(_run_tick(monkeypatch, runner))
    assert adapter.sent == []
    assert len(adapter.handled) == 1
    assert len(_subs(task_id)) == 1

    runner._running = True
    asyncio.run(_run_tick(monkeypatch, runner))
    assert adapter.sent == []
    assert len(adapter.handled) == 2
    assert _subs(task_id) == []


def test_agent_only_drops_subscription_after_twelfth_failed_wake(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    kb.init_db()
    _configure(monkeypatch, "agent_only")
    task_id = _completed_sub()
    adapter = PushAdapter(wake_failures=12)
    runner = _runner(adapter)

    for _ in range(11):
        asyncio.run(_run_tick(monkeypatch, runner))
        runner._running = True

    assert len(adapter.handled) == 11
    assert len(_subs(task_id)) == 1

    asyncio.run(_run_tick(monkeypatch, runner))

    assert adapter.sent == []
    assert len(adapter.handled) == 12
    assert _subs(task_id) == []


def test_agent_only_preserves_discord_group_thread_profile_source(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    kb.init_db()
    _configure(monkeypatch, "agent_only")
    _completed_sub(
        platform="discord",
        chat_id="guild-channel",
        thread_id="thread-42",
        profile="writer",
        chat_type="group",
    )
    adapter = PushAdapter()
    runner = _runner(PushAdapter())
    runner._profile_adapters = {"writer": {Platform.DISCORD: adapter}}
    runner._active_profile_name = lambda: "default"

    asyncio.run(_run_tick(monkeypatch, runner))

    assert adapter.sent == []
    assert len(adapter.handled) == 1
    source = adapter.handled[0].source
    assert source.platform is Platform.DISCORD
    assert source.chat_id == "guild-channel"
    assert source.chat_type == "group"
    assert source.thread_id == "thread-42"
    assert source.profile == "writer"


def test_agent_only_does_not_change_non_push_delivery(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    kb.init_db()
    _configure(monkeypatch, "agent_only")
    _completed_sub(platform="api_server", chat_id="origin-session")
    adapter = NonPushAdapter()
    runner = _runner(adapter, Platform.API_SERVER)
    wakes = []

    async def record_wake(adapter, *, text, session_id="", source=None):
        wakes.append((session_id, source))

    import gateway.wake as wake

    monkeypatch.setattr(wake, "deliver_wake", record_wake)
    asyncio.run(_run_tick(monkeypatch, runner))

    assert adapter.send_calls == 0
    assert wakes == [("origin-session", None)]


def test_agent_only_non_push_status_advances_without_wake(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    kb.init_db()
    _configure(monkeypatch, "agent_only")
    task_id, before = _event_sub(
        "status", platform="api_server", chat_id="origin-session",
    )
    adapter = NonPushAdapter()
    wakes = []

    async def record_wake(adapter, *, text, session_id="", source=None):
        wakes.append((session_id, source))

    import gateway.wake as wake

    monkeypatch.setattr(wake, "deliver_wake", record_wake)
    asyncio.run(_run_tick(monkeypatch, _runner(adapter, Platform.API_SERVER)))

    assert adapter.send_calls == 0
    assert wakes == []
    assert _subs(task_id)[0]["last_event_id"] > before


@pytest.mark.parametrize(
    "kind", ["status", "review_requested", "block_loop_detected"],
)
def test_agent_only_push_wakes_for_agent_delivery_events(
    tmp_path, monkeypatch, kind,
):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    kb.init_db()
    _configure(monkeypatch, "agent_only")
    task_id, before = _event_sub(kind)
    adapter = PushAdapter()

    asyncio.run(_run_tick(monkeypatch, _runner(adapter)))

    assert adapter.sent == []
    assert len(adapter.handled) == 1
    assert _subs(task_id)[0]["last_event_id"] > before


@pytest.mark.parametrize("mode", ["text_and_agent", "agent_only"])
def test_silent_claim_advances_without_send_or_wake(
    tmp_path, monkeypatch, caplog, mode,
):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    kb.init_db()
    _configure(monkeypatch, mode)
    task_id, before = _unblocked_sub()
    adapter = PushAdapter()

    asyncio.run(_run_tick(monkeypatch, _runner(adapter)))

    assert _subs(task_id)[0]["last_event_id"] > before
    assert adapter.sent == []
    assert adapter.handled == []
    assert not any(
        "kanban notifier tick failed" in record.getMessage()
        for record in caplog.records
    )
