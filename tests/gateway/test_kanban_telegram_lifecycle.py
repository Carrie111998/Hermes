import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


async def _run_notifier(*, enabled=True, throttle=60, platform_name="telegram", setup=None):
    from gateway.config import Platform
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._owns_kanban_dispatcher_lock = lambda: True
    runner._running = True
    runner._kanban_sub_fail_counts = {}
    adapter = MagicMock()
    adapter.send = AsyncMock()
    platform = Platform(platform_name)
    runner.adapters = {platform: adapter}
    if setup:
        setup(runner, adapter)
    original_sleep = asyncio.sleep
    ticks = 0

    async def fast_sleep(_delay):
        nonlocal ticks
        await original_sleep(0)
        ticks += 1
        if ticks >= 3:
            runner._running = False

    cfg = {"kanban": {
        "enhanced_telegram_notifications": enabled,
        "telegram_notification_throttle_seconds": throttle,
    }}
    with patch("gateway.run.asyncio.sleep", side_effect=fast_sleep), \
         patch("hermes_cli.config.load_config", return_value=cfg):
        await asyncio.wait_for(runner._kanban_notifier_watcher(interval=0), timeout=10)
    return adapter


def test_nexus_start_and_milestone_formats_are_plain_concise_and_friendly():
    from gateway.kanban_watchers import _format_enhanced_telegram_message

    task = SimpleNamespace(
        id="t_554db1f1",
        title="Audit payment retries [ops] profile=default run=19 status=running",
        assignee="helix",
        result=None,
    )
    started = _format_enhanced_telegram_message("spawned", task, {})
    update = _format_enhanced_telegram_message(
        "commented", task, {"milestone": "Gateway retry policy validated"}
    )

    assert started == "🟡 Helix started: Audit payment retries [ops]"
    assert update == "🔵 Helix update: Gateway retry policy validated"
    for message in (started, update):
        assert len(message) <= 500
        assert "t_554db1f1" not in message
        assert "profile=" not in message
        assert "run=" not in message
        assert "status=" not in message
    assert "[ops]" in started


def test_nexus_formatter_preserves_normal_english_labels_and_links():
    from gateway.kanban_watchers import _telegram_plain_text

    text = (
        "status: ready for review; run: the migration again "
        "[release notes] [ops] https://example.test t_deadbeef "
        "[kanban] [board:finance] profile=default"
    )

    assert _telegram_plain_text(text) == (
        "status: ready for review; run: the migration again "
        "[release notes] [ops] https://example.test"
    )


def test_nexus_blocked_reply_and_completed_outcome_formats():
    from gateway.kanban_watchers import _format_enhanced_telegram_message

    task = SimpleNamespace(
        id="t_secret", title="Ship checkout", assignee="ccreviewer",
        result="Fallback result",
    )
    blocked = _format_enhanced_telegram_message(
        "blocked", task, {"reason": "Choose Stripe or Adyen"}
    )
    preserved = _format_enhanced_telegram_message(
        "blocked", task, {"reason": "Need region. Reply: EU or US"}
    )
    complete = _format_enhanced_telegram_message(
        "completed", task,
        {"summary": "Checkout shipped\nNext: approve production rollout"},
    )

    assert blocked == (
        "🔴 Decision needed: Choose Stripe or Adyen. "
        "Reply: provide the missing decision or input"
    )
    assert preserved == "🔴 Decision needed: Need region. Reply: EU or US"
    assert complete == "✅ Complete: Checkout shipped. Next: approve production rollout"
    assert "Reviewer" not in complete  # terminal format intentionally leads with outcome


def test_nexus_formatter_force_redacts_secrets_and_caps_length():
    from gateway.kanban_watchers import _format_enhanced_telegram_message

    secret = "sk-" + "a" * 48
    task = SimpleNamespace(
        id="t_abc", title="Investigate " + secret + " " + ("x" * 900),
        assignee="api_specialist", result=None,
    )
    message = _format_enhanced_telegram_message("spawned", task, {})

    assert secret not in message
    assert len(message) <= 500
    assert message.startswith("🟡 Api Specialist started: Investigate")


@pytest.mark.asyncio
async def test_enhanced_telegram_delivers_first_actual_spawn_only_and_routes_topic(kanban_home):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="Reconcile invoices", assignee="helix")
        kb.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-1",
            thread_id="topic-9", delivery_metadata={"topic_name": "finance"},
        )
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "spawned", {"pid": 111})
            kb._append_event(conn, tid, "spawned", {"pid": 222})
    finally:
        conn.close()

    adapter = await _run_notifier()

    adapter.send.assert_awaited_once_with(
        "chat-1", "🟡 Helix started: Reconcile invoices",
        metadata={"topic_name": "finance", "thread_id": "topic-9"},
    )


@pytest.mark.asyncio
async def test_enhanced_telegram_filters_dedupes_throttles_comments_and_never_heartbeats(kanban_home):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="Migrate ledger", assignee="data_specialist")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-2")
        kb.add_comment(conn, tid, "worker", "ordinary internal note")
        kb.add_comment(conn, tid, "worker", "MiLeStOnE: schema migrated")
        kb.add_comment(conn, tid, "worker", "milestone: schema migrated")
        kb.add_comment(conn, tid, "worker", "milestone: indexes verified")
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "heartbeat", {"note": "still working"})
    finally:
        conn.close()

    adapter = await _run_notifier(throttle=60)

    messages = [call.args[1] for call in adapter.send.await_args_list]
    assert messages == ["🔵 Data Specialist update: schema migrated"]


@pytest.mark.asyncio
async def test_enhanced_telegram_preserves_blocked_completed_and_disabled_cursor_semantics(kanban_home):
    conn = kb.connect()
    try:
        blocked = kb.create_task(conn, title="Choose region", assignee="helix")
        completed = kb.create_task(conn, title="Publish report", assignee="writer")
        for tid in (blocked, completed):
            kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-3")
        kb.add_comment(conn, blocked, "worker", "milestone: must not replay")
    finally:
        conn.close()

    disabled_adapter = await _run_notifier(enabled=False)
    disabled_adapter.send.assert_not_awaited()

    conn = kb.connect()
    try:
        kb.block_task(conn, blocked, reason="Select EU or US")
        kb.complete_task(conn, completed, summary="Report published\nhttps://example.test/report")
    finally:
        conn.close()

    adapter = await _run_notifier(enabled=True)
    messages = [call.args[1] for call in adapter.send.await_args_list]
    assert "🔵 Helix update: must not replay" not in messages
    assert "🔴 Decision needed: Select EU or US. Reply: provide the missing decision or input" in messages
    assert "✅ Complete: Report published. https://example.test/report" in messages


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,platform_name", [(False, "telegram"), (True, "discord")])
async def test_gate_off_and_non_telegram_keep_exact_legacy_message_and_event_summary(
    kanban_home, enabled, platform_name,
):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="Legacy title", assignee="writer")
        kb.add_notify_sub(conn, task_id=tid, platform=platform_name, chat_id="legacy-chat")
        with kb.write_txn(conn):
            run_id = conn.execute(
                "INSERT INTO task_runs (task_id, status, started_at, summary) VALUES (?, 'completed', 1, ?)",
                (tid, "full run summary"),
            ).lastrowid
            kb._append_event(conn, tid, "completed", {"summary": "event payload summary"}, run_id=run_id)
    finally:
        conn.close()

    adapter = await _run_notifier(enabled=enabled, platform_name=platform_name)

    adapter.send.assert_awaited_once_with(
        "legacy-chat",
        f"✔ [default] @writer Kanban {tid} done — Legacy title\nevent payload summary",
        metadata={},
    )


@pytest.mark.asyncio
async def test_enhanced_run_summary_does_not_mutate_attachment_event_payload(kanban_home):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="Artifact task", assignee="writer")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="artifact-chat")
        with kb.write_txn(conn):
            run_id = conn.execute(
                "INSERT INTO task_runs (task_id, status, started_at, summary) VALUES (?, 'completed', 1, ?)",
                (tid, "full run summary /tmp/full.txt"),
            ).lastrowid
            kb._append_event(
                conn, tid, "completed",
                {"summary": "event summary", "artifacts": ["/tmp/original.txt"]},
                run_id=run_id,
            )
    finally:
        conn.close()

    captured = []

    def setup(runner, _adapter):
        async def capture(**kwargs):
            captured.append(dict(kwargs["event_payload"]))
        runner._deliver_kanban_artifacts = capture

    adapter = await _run_notifier(setup=setup)

    assert adapter.send.await_args.args[1] == "✅ Complete: full run summary /tmp/full.txt"
    assert captured == [{"summary": "event summary", "artifacts": ["/tmp/original.txt"]}]


@pytest.mark.asyncio
async def test_late_subscription_gets_one_later_actual_respawn_start(kanban_home):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="Retry task", assignee="helix")
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "spawned", {"pid": 1})
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="late-chat")
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "spawned", {"pid": 2})
            kb._append_event(conn, tid, "spawned", {"pid": 3})
    finally:
        conn.close()

    adapter = await _run_notifier()

    adapter.send.assert_awaited_once_with(
        "late-chat", "🟡 Helix started: Retry task", metadata={},
    )


@pytest.mark.asyncio
async def test_milestone_throttle_uses_last_delivered_and_state_is_bounded(kanban_home):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="Chatty task", assignee="helix")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chatty")
        ids = [kb.add_comment(conn, tid, "worker", f"milestone: step {i}") for i in range(4)]
        with kb.write_txn(conn):
            for comment_id, created_at in zip(ids, (1000, 1030, 1060, 1090)):
                conn.execute("UPDATE task_comments SET created_at = ? WHERE id = ?", (created_at, comment_id))
                conn.execute(
                    "UPDATE task_events SET created_at = ? WHERE task_id = ? AND json_extract(payload, '$.comment_id') = ?",
                    (created_at, tid, comment_id),
                )
    finally:
        conn.close()

    state = {}
    adapter = await _run_notifier(throttle=60, setup=lambda runner, _adapter: state.update(runner=runner))

    assert [call.args[1] for call in adapter.send.await_args_list] == [
        "🔵 Helix update: step 0", "🔵 Helix update: step 2",
    ]
    assert not hasattr(state["runner"], "_kanban_telegram_milestone_seen")
    assert len(state["runner"]._kanban_telegram_milestone_last) == 1


@pytest.mark.asyncio
async def test_exact_milestone_duplicate_is_windowed_not_global(kanban_home):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="Repeated checkpoint", assignee="helix")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="repeat-chat")
        ids = [kb.add_comment(conn, tid, "worker", "milestone: checkpoint") for _ in range(3)]
        with kb.write_txn(conn):
            for comment_id, created_at in zip(ids, (1000, 1030, 1060)):
                conn.execute("UPDATE task_comments SET created_at = ? WHERE id = ?", (created_at, comment_id))
                conn.execute(
                    "UPDATE task_events SET created_at = ? WHERE task_id = ? AND json_extract(payload, '$.comment_id') = ?",
                    (created_at, tid, comment_id),
                )
    finally:
        conn.close()

    adapter = await _run_notifier(throttle=60)

    assert [call.args[1] for call in adapter.send.await_args_list] == [
        "🔵 Helix update: checkpoint", "🔵 Helix update: checkpoint",
    ]


@pytest.mark.asyncio
async def test_heartbeat_is_not_claimed_but_later_event_advances_past_it(kanban_home):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="Heartbeat task", assignee="helix")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="heartbeat-chat")
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "heartbeat", {"note": "alive"})
            kb._append_event(conn, tid, "blocked", {"reason": "Need input"})
            heartbeat_id = conn.execute(
                "SELECT id FROM task_events WHERE task_id = ? AND kind = 'heartbeat' ORDER BY id DESC LIMIT 1",
                (tid,),
            ).fetchone()[0]
            completed_id = conn.execute(
                "SELECT id FROM task_events WHERE task_id = ? AND kind = 'blocked' ORDER BY id DESC LIMIT 1",
                (tid,),
            ).fetchone()[0]
    finally:
        conn.close()

    adapter = await _run_notifier()

    adapter.send.assert_awaited_once()
    conn = kb.connect()
    try:
        sub = kb.list_notify_subs(conn)[0]
    finally:
        conn.close()
    assert heartbeat_id < completed_id == sub["last_event_id"]


@pytest.mark.asyncio
async def test_enrichment_exception_does_not_lose_completed_or_blocked_events(
    kanban_home, monkeypatch,
):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="Fail-safe task", assignee="helix")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="safe-chat")
        with kb.write_txn(conn):
            run_id = conn.execute(
                "INSERT INTO task_runs (task_id, status, started_at, summary) VALUES (?, 'completed', 1, ?)",
                (tid, "run summary"),
            ).lastrowid
            kb._append_event(conn, tid, "blocked", {"reason": "Need approval"})
            kb._append_event(conn, tid, "completed", {"summary": "event summary"}, run_id=run_id)
            last_id = conn.execute(
                "SELECT MAX(id) FROM task_events WHERE task_id = ?", (tid,),
            ).fetchone()[0]
    finally:
        conn.close()

    real_connect = kb.connect

    class EnrichmentFailureConnection:
        def __init__(self, inner):
            self.inner = inner

        def execute(self, sql, params=()):
            if "SELECT summary FROM task_runs" in sql:
                raise sqlite3.OperationalError("synthetic enrichment failure")
            return self.inner.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self.inner, name)

    monkeypatch.setattr(kb, "connect", lambda *a, **kw: EnrichmentFailureConnection(real_connect(*a, **kw)))
    adapter = await _run_notifier()
    monkeypatch.setattr(kb, "connect", real_connect)

    assert [call.args[1] for call in adapter.send.await_args_list] == [
        "🔴 Decision needed: Need approval. Reply: provide the missing decision or input",
        "✅ Complete: event summary",
    ]
    conn = kb.connect()
    try:
        assert kb.list_notify_subs(conn)[0]["last_event_id"] == last_id
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_enhanced_display_sanitizing_does_not_change_wake_handoff(kanban_home):
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="Wake task", assignee="helix", session_id="creator-session",
        )
        kb.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="wake-chat",
            delivery_mode="notify+wake",
        )
        with kb.write_txn(conn):
            kb._append_event(
                conn, tid, "completed",
                {"summary": "status: ready [ops] run: migration t_deadbeef"},
            )
    finally:
        conn.close()

    wakes = []

    async def capture_wake(_adapter, *, text, session_id, **_kwargs):
        wakes.append((text, session_id))

    def setup(_runner, adapter):
        adapter.supports_async_delivery = True

    with patch("gateway.wake.deliver_wake", side_effect=capture_wake):
        await _run_notifier(setup=setup)

    assert len(wakes) == 1
    assert "status: ready [ops] run: migration t_deadbeef" in wakes[0][0]
    assert wakes[0][1] == "creator-session"


@pytest.mark.asyncio
async def test_enhanced_empty_completion_does_not_inject_none_handoff(kanban_home):
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="Empty handoff", assignee="helix", session_id="creator-session",
        )
        kb.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="wake-chat",
            delivery_mode="notify+wake",
        )
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "completed", {"summary": None})
    finally:
        conn.close()

    wakes = []

    async def capture_wake(_adapter, *, text, session_id, **_kwargs):
        wakes.append((text, session_id))

    def setup(_runner, adapter):
        adapter.supports_async_delivery = True

    with patch("gateway.wake.deliver_wake", side_effect=capture_wake):
        await _run_notifier(setup=setup)

    assert len(wakes) == 1
    assert "None" not in wakes[0][0]
    assert wakes[0][1] == "creator-session"
