from __future__ import annotations

import asyncio
import json

from gateway.config import Platform
from gateway.kanban_proactive_supervisor import (
    ProactiveSupervisorConfig,
    consume_supervisor_reply,
    reconcile_board,
)
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb
from hermes_cli import config as hermes_config


def _config(**overrides) -> ProactiveSupervisorConfig:
    values = {
        "enabled": True,
        "platform": "discord",
        "chat_id": "hermes-command-channel",
        "thread_id": "",
        "chat_type": "channel",
        "recovery_limit": 1,
    }
    values.update(overrides)
    return ProactiveSupervisorConfig.from_mapping(values)


def _latest_event(conn, task_id: str, kind: str):
    row = conn.execute(
        "SELECT * FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id DESC LIMIT 1",
        (task_id, kind),
    ).fetchone()
    assert row is not None
    return row


def test_cli_created_protected_gate_gets_profile_owned_subscription(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="Publish release", assignee="forge")
        assert kb.list_notify_subs(conn, task_id) == []
        reason = "Need Kevin's product decision: ship the alternate navigation now?"
        assert kb.block_task(conn, task_id, reason=reason, kind="needs_input")
        blocked = _latest_event(conn, task_id, "blocked")

        result = reconcile_board(
            conn,
            board="default",
            config=_config(),
            notifier_profile="default",
        )

        assert result.protected_gates == [task_id]
        sub = kb.list_notify_subs(conn, task_id)[0]
        assert sub["platform"] == "discord"
        assert sub["chat_id"] == "hermes-command-channel"
        assert sub["notifier_profile"] == "default"
        assert sub["last_event_id"] == blocked["id"] - 1
        assert sub["delivery_metadata"]["_kanban_proactive_supervisor"] is True
    finally:
        conn.close()


def test_agent_owned_block_is_recovered_once_and_never_asks(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="Run tests", assignee="forge")
        assert kb.block_task(
            conn,
            task_id,
            reason="pytest worker lost its temporary directory",
            kind="transient",
        )

        first = reconcile_board(
            conn,
            board="default",
            config=_config(),
            notifier_profile="default",
        )
        second = reconcile_board(
            conn,
            board="default",
            config=_config(),
            notifier_profile="default",
        )

        assert first.recovered == [task_id]
        assert second.recovered == []
        assert kb.get_task(conn, task_id).status == "ready"
        recovery_events = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'supervisor_recovery'",
            (task_id,),
        ).fetchall()
        assert len(recovery_events) == 1
        payload = json.loads(recovery_events[0]["payload"])
        assert payload["source_kind"] == "blocked"
        assert kb.list_notify_subs(conn, task_id) == []
    finally:
        conn.close()


def test_recovery_budget_is_durable_and_exhaustion_is_status_only(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="Flaky operation", assignee="forge")
        assert kb.block_task(conn, task_id, reason="temporary backend failure", kind="transient")
        first = reconcile_board(conn, board="default", config=_config(), notifier_profile="default")
        assert first.recovered == [task_id]

        assert kb.block_task(conn, task_id, reason="temporary backend failure", kind="transient")
        exhausted = reconcile_board(conn, board="default", config=_config(), notifier_profile="default")

        assert exhausted.recovery_exhausted == [task_id]
        assert kb.get_task(conn, task_id).status == "triage"
        exhausted_event = conn.execute(
            "SELECT * FROM task_events WHERE task_id = ? "
            "AND kind IN ('blocked', 'gave_up', 'block_loop_detected') "
            "ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        assert exhausted_event is not None
        sub = kb.list_notify_subs(conn, task_id)[0]
        assert sub["last_event_id"] == exhausted_event["id"] - 1
        assert sub["delivery_metadata"]["_kanban_proactive_supervisor"] is True
    finally:
        conn.close()


def test_reply_to_protected_prompt_resumes_same_task_graph(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="Choose release path", assignee="forge")
        assert kb.block_task(
            conn,
            task_id,
            reason="Need Kevin's business decision: release to the current customers?",
            kind="needs_input",
        )
    finally:
        conn.close()

    quoted = (
        "Hermes needs one decision to continue Choose release path.\n"
        "Reply to this message with the decision.\n"
        f"[kanban-supervisor:default:{task_id}]"
    )
    result = consume_supervisor_reply(
        reply_to_text=quoted,
        answer="Yes, release to current customers only.",
        author="Kevin",
    )

    assert result is not None
    assert result.task_id == task_id
    assert result.board == "default"
    assert result.resumed is True
    conn = kb.connect()
    try:
        task = kb.get_task(conn, task_id)
        assert task.status == "ready"
        comments = kb.list_comments(conn, task_id)
        assert comments[-1].author == "Kevin"
        assert comments[-1].body == "Yes, release to current customers only."
    finally:
        conn.close()


def test_disabled_supervisor_is_a_noop(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="No supervisor", assignee="forge")
        assert kb.block_task(
            conn,
            task_id,
            reason="Need Kevin's product decision",
            kind="needs_input",
        )
        result = reconcile_board(
            conn,
            board="default",
            config=_config(enabled=False),
            notifier_profile="default",
        )
        assert result.changed is False
        assert kb.get_task(conn, task_id).status == "blocked"
        assert kb.list_notify_subs(conn, task_id) == []
    finally:
        conn.close()


class _RecordingAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})

    async def handle_message(self, event):
        return None


def test_gateway_tick_discovers_unsubscribed_gate_and_sends_replyable_prompt(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="Approve deployment", assignee="forge")
        assert kb.block_task(
            conn,
            task_id,
            reason="Need Kevin's business decision before production deployment",
            kind="needs_input",
        )
    finally:
        conn.close()

    monkeypatch.setattr(
        hermes_config,
        "load_config",
        lambda: {
            "kanban": {
                "proactive_supervisor": {
                    "enabled": True,
                    "platform": "telegram",
                    "chat_id": "hermes-command-channel",
                    "chat_type": "dm",
                    "recovery_limit": 1,
                }
            }
        },
    )
    adapter = _RecordingAdapter()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    runner._active_profile_name = lambda: "default"

    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    asyncio.run(runner._kanban_notifier_watcher(interval=1))

    assert len(adapter.sent) == 1
    delivery = adapter.sent[0]
    assert delivery["chat_id"] == "hermes-command-channel"
    assert "needs one decision" in delivery["text"]
    assert f"[kanban-supervisor:default:{task_id}]" in delivery["text"]
    assert "_kanban_proactive_supervisor" not in delivery["metadata"]
