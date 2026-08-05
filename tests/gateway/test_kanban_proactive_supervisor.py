from __future__ import annotations

import asyncio
import json
import re
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.kanban_proactive_supervisor import (
    ProactiveSupervisorConfig,
    consume_supervisor_reply,
    reconcile_board,
    render_supervisor_event,
)
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_cli import kanban_db as kb
from hermes_cli import config as hermes_config
from plugins.kanban.dashboard.plugin_api import _set_status_direct


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


def _gate_token(conn, task_id: str) -> str:
    metadata = kb.list_notify_subs(conn, task_id)[0]["delivery_metadata"]
    token = metadata["_kanban_supervisor_gate_token"]
    assert re.fullmatch(r"[a-f0-9]{32}", token)
    return token


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
        assert sub["delivery_metadata"]["_kanban_supervisor_event_id"] == blocked["id"]
        assert sub["delivery_metadata"]["_kanban_supervisor_mode"] == "protected_gate"
        assert sub["delivery_metadata"]["_kanban_supervisor_owned_subscription"] is True
    finally:
        conn.close()


def test_preexisting_subscription_is_upgraded_and_rewound_to_current_gate(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="Existing subscriber", assignee="forge")
        created = _latest_event(conn, task_id, "created")
        kb.add_notify_sub(
            conn,
            task_id=task_id,
            platform="discord",
            chat_id="hermes-command-channel",
            notifier_profile="default",
            delivery_metadata={"ordinary": True},
            start_event_id=created["id"],
        )
        assert kb.block_task(
            conn, task_id,
            reason="Need Kevin's product decision",
            kind="needs_input",
        )
        blocked = _latest_event(conn, task_id, "blocked")
        _, cursor, claimed = kb.claim_unseen_events_for_sub(
            conn,
            task_id=task_id,
            platform="discord",
            chat_id="hermes-command-channel",
        )
        assert [event.id for event in claimed] == [blocked["id"]]
        assert cursor == blocked["id"]

        result = reconcile_board(
            conn, board="default", config=_config(), notifier_profile="default"
        )
        sub = kb.list_notify_subs(conn, task_id)[0]

        assert result.protected_gates == [task_id]
        assert sub["last_event_id"] == blocked["id"] - 1
        assert sub["delivery_metadata"]["ordinary"] is True
        assert sub["delivery_metadata"]["_kanban_supervisor_owned_subscription"] is False
    finally:
        conn.close()

    # Reopen to prove the rewind was committed, not visible only in one
    # connection's uncommitted transaction.
    conn = kb.connect()
    try:
        sub = kb.list_notify_subs(conn, task_id)[0]
        assert sub["last_event_id"] == blocked["id"] - 1
    finally:
        conn.close()


@pytest.mark.parametrize(
    "reason",
    [
        "Need API key from Kevin",
        "Need approval to delete all customer records",
        "Need a decision on whether to ship this behavior",
        "Need authorization to remove the production database",
    ],
)
def test_untyped_legacy_protected_gate_is_not_auto_recovered(
    tmp_path, monkeypatch, reason
):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="Legacy credential gate", assignee="forge")
        assert kb.block_task(conn, task_id, reason=reason)

        result = reconcile_board(
            conn, board="default", config=_config(), notifier_profile="default"
        )

        assert result.protected_gates == [task_id]
        assert result.recovered == []
        assert kb.get_task(conn, task_id).status == "blocked"
    finally:
        conn.close()


@pytest.mark.parametrize("kind", ["needs_input", "capability"])
def test_typed_human_gate_is_protected_regardless_of_wording(
    tmp_path, monkeypatch, kind
):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="Typed human gate", assignee="forge")
        assert kb.block_task(
            conn,
            task_id,
            reason="Which option should I choose?",
            kind=kind,
        )

        result = reconcile_board(
            conn, board="default", config=_config(), notifier_profile="default"
        )

        assert result.protected_gates == [task_id]
        assert result.recovered == []
        assert kb.get_task(conn, task_id).status == "blocked"
    finally:
        conn.close()


@pytest.mark.parametrize("release_claim", [False, True])
def test_dispatcher_gave_up_is_typed_transient_and_recovered(
    tmp_path, monkeypatch, release_claim
):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="Dispatcher failure", assignee="forge")
        assert kb._record_task_failure(
            conn,
            task_id,
            "worker executable unavailable",
            outcome="spawn_failed",
            failure_limit=1,
            force_trip=True,
            release_claim=release_claim,
            end_run=False,
        )
        gave_up = _latest_event(conn, task_id, "gave_up")

        task = kb.get_task(conn, task_id)
        assert task.block_kind == "transient"
        assert json.loads(gave_up["payload"])["kind"] == "transient"

        result = reconcile_board(
            conn, board="default", config=_config(), notifier_profile="default"
        )

        assert result.recovered == [task_id]
        assert result.protected_gates == []
        assert kb.get_task(conn, task_id).status == "ready"
    finally:
        conn.close()


def test_unknown_typed_block_fails_closed_and_renders_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="Future blocker", assignee="forge")
        assert kb.block_task(conn, task_id, reason="Requires operator approval")
        blocked = _latest_event(conn, task_id, "blocked")
        payload = json.loads(blocked["payload"])
        payload["kind"] = "approval_required"
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET block_kind = 'approval_required' WHERE id = ?",
                (task_id,),
            )
            conn.execute(
                "UPDATE task_events SET payload = ? WHERE id = ?",
                (json.dumps(payload), blocked["id"]),
            )

        result = reconcile_board(
            conn, board="default", config=_config(), notifier_profile="default"
        )

        task = kb.get_task(conn, task_id)
        assert task is not None
        metadata = kb.list_notify_subs(conn, task_id)[0]["delivery_metadata"]
        rendered = render_supervisor_event(
            board="default",
            task=task,
            event=SimpleNamespace(id=blocked["id"], payload=json.dumps(payload)),
            delivery_metadata=metadata,
            current_event_id=blocked["id"],
        )
        assert result.protected_gates == [task_id]
        assert result.recovered == []
        assert task.status == "blocked"
        assert rendered and "Reply to this message" in rendered
    finally:
        conn.close()


def test_current_block_loop_triage_is_never_auto_recovered(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="Repeated transient", assignee="forge")
        assert kb.block_task(
            conn, task_id, reason="temporary backend failure", kind="transient"
        )
        assert kb.unblock_task(conn, task_id)
        assert kb.block_task(
            conn, task_id, reason="temporary backend failure", kind="transient"
        )
        task = kb.get_task(conn, task_id)
        assert task is not None and task.status == "triage"

        result = reconcile_board(
            conn, board="default", config=_config(), notifier_profile="default"
        )

        assert result.protected_gates == [task_id]
        assert result.recovered == []
        task = kb.get_task(conn, task_id)
        assert task is not None and task.status == "triage"
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? "
            "AND kind = 'supervisor_recovery'",
            (task_id,),
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_authenticated_gate_reply_failure_does_not_fall_through_to_agent(monkeypatch):
    from gateway import kanban_proactive_supervisor as supervisor

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._scale_to_zero_note_real_inbound = lambda: None
    runner._is_user_authorized = lambda source: True
    runner._active_profile_name = lambda: "default"
    event = MessageEvent(
        text="approve deletion",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id="hermes-command-channel",
            chat_type="channel",
            user_id="kevin",
            user_name="Kevin",
            profile="default",
        ),
        reply_to_text="Decision needed\n[kanban-gate:0123456789abcdef0123456789abcdef]",
        reply_to_is_own_message=True,
    )

    def fail_gate_lookup(**_kwargs):
        raise OSError("temporary database failure")

    monkeypatch.setattr(supervisor, "consume_supervisor_reply", fail_gate_lookup)

    response = asyncio.run(runner._handle_message(event))

    assert response is not None
    assert "could not process" in response.lower()
    assert "no action" in response.lower()


def test_supervisor_does_not_take_over_subscription_owned_by_another_profile(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="Profile-isolated gate", assignee="forge")
        kb.add_notify_sub(
            conn,
            task_id=task_id,
            platform="discord",
            chat_id="hermes-command-channel",
            notifier_profile="other-profile",
            delivery_metadata={"ordinary": True},
        )
        assert kb.block_task(
            conn, task_id, reason="Need Kevin's product decision", kind="needs_input"
        )

        result = reconcile_board(
            conn, board="default", config=_config(), notifier_profile="default"
        )
        sub = kb.list_notify_subs(conn, task_id)[0]

        assert result.protected_gates == []
        assert sub["notifier_profile"] == "other-profile"
        assert sub["delivery_metadata"] == {"ordinary": True}
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

        assert kb._record_task_failure(
            conn,
            task_id,
            "worker executable unavailable",
            outcome="spawn_failed",
            failure_limit=1,
            force_trip=True,
            release_claim=False,
            end_run=False,
        )
        exhausted = reconcile_board(conn, board="default", config=_config(), notifier_profile="default")

        assert exhausted.recovery_exhausted == [task_id]
        task = kb.get_task(conn, task_id)
        assert task is not None and task.status == "blocked"
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
        reconcile_board(
            conn,
            board="default",
            config=_config(),
            notifier_profile="default",
        )
        token = _gate_token(conn, task_id)
    finally:
        conn.close()

    quoted = (
        "Hermes needs one decision to continue Choose release path.\n"
        "Reply to this message with the decision.\n"
        f"[kanban-gate:{token}]"
    )
    result = consume_supervisor_reply(
        reply_to_text=quoted,
        answer="Yes, release to current customers only.",
        author="Kevin",
        platform="discord",
        chat_id="hermes-command-channel",
        notifier_profile="default",
        reply_to_is_own_message=True,
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

        duplicate = consume_supervisor_reply(
            reply_to_text=quoted,
            answer="duplicate",
            author="Kevin",
            platform="discord",
            chat_id="hermes-command-channel",
            notifier_profile="default",
            reply_to_is_own_message=True,
        )
        assert duplicate is not None
        assert duplicate.resumed is False
        assert duplicate.status == "not_waiting"
        assert len(kb.list_comments(conn, task_id)) == 1
    finally:
        conn.close()


def test_stale_prompt_cannot_resume_a_different_block_generation(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="Two gates", assignee="forge")
        assert kb.block_task(
            conn, task_id,
            reason="Need Kevin's product decision for gate A",
            kind="needs_input",
        )
        reconcile_board(conn, board="default", config=_config(), notifier_profile="default")
        token = _gate_token(conn, task_id)
        assert kb.unblock_task(conn, task_id)
        assert kb.block_task(
            conn, task_id,
            reason="temporary backend failure for gate B",
            kind="transient",
        )
        comments_before = len(kb.list_comments(conn, task_id))
    finally:
        conn.close()

    result = consume_supervisor_reply(
        reply_to_text=f"[kanban-gate:{token}]",
        answer="approve old gate A",
        author="Kevin",
        platform="discord",
        chat_id="hermes-command-channel",
        notifier_profile="default",
        reply_to_is_own_message=True,
    )
    assert result is not None
    assert result.resumed is False
    assert result.status == "stale_gate"
    conn = kb.connect()
    try:
        assert kb.get_task(conn, task_id).status == "blocked"
        assert len(kb.list_comments(conn, task_id)) == comments_before
    finally:
        conn.close()


def test_stale_prompt_cannot_resume_a_later_protected_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="Two protected gates", assignee="forge")
        assert kb.block_task(
            conn, task_id,
            reason="Need Kevin's product decision for gate A",
            kind="needs_input",
        )
        reconcile_board(conn, board="default", config=_config(), notifier_profile="default")
        stale_token = _gate_token(conn, task_id)
        assert kb.unblock_task(conn, task_id)
        assert kb.block_task(
            conn, task_id,
            reason="Need Kevin's credential for gate B",
            kind="needs_input",
        )
        reconcile_board(conn, board="default", config=_config(), notifier_profile="default")
        assert _gate_token(conn, task_id) != stale_token
        comments_before = len(kb.list_comments(conn, task_id))
    finally:
        conn.close()

    assert consume_supervisor_reply(
        reply_to_text=f"[kanban-gate:{stale_token}]",
        answer="approve old gate A",
        author="Kevin",
        platform="discord",
        chat_id="hermes-command-channel",
        notifier_profile="default",
        reply_to_is_own_message=True,
    ) is None
    conn = kb.connect()
    try:
        assert kb.get_task(conn, task_id).status == "triage"
        assert len(kb.list_comments(conn, task_id)) == comments_before
    finally:
        conn.close()


def test_forged_or_cross_route_prompt_marker_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="Protected", assignee="forge")
        assert kb.block_task(
            conn, task_id, reason="Need Kevin's credential", kind="needs_input"
        )
        reconcile_board(conn, board="default", config=_config(), notifier_profile="default")
        token = _gate_token(conn, task_id)
    finally:
        conn.close()

    common = {
        "reply_to_text": f"[kanban-gate:{token}]",
        "answer": "yes",
        "author": "Kevin",
        "platform": "discord",
        "notifier_profile": "default",
    }
    assert consume_supervisor_reply(
        **common, chat_id="hermes-command-channel", reply_to_is_own_message=False
    ) is None
    assert consume_supervisor_reply(
        **common, chat_id="different-channel", reply_to_is_own_message=True
    ) is None
    conn = kb.connect()
    try:
        assert kb.get_task(conn, task_id).status == "blocked"
        assert kb.list_comments(conn, task_id) == []
    finally:
        conn.close()


@pytest.mark.parametrize("historical_kind", ["blocked", "gave_up", "block_loop_detected"])
def test_historical_failure_cannot_promote_deliberate_manual_triage(
    tmp_path, monkeypatch, historical_kind
):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="Sticky triage", assignee="forge")
        if historical_kind == "blocked":
            assert kb.block_task(
                conn, task_id, reason="temporary backend failure", kind="transient"
            )
        elif historical_kind == "gave_up":
            assert kb._record_task_failure(
                conn,
                task_id,
                "temporary worker failure",
                outcome="spawn_failed",
                failure_limit=1,
                force_trip=True,
                release_claim=False,
                end_run=False,
            )
        else:
            assert kb.block_task(
                conn, task_id, reason="temporary backend failure", kind="transient"
            )
            assert kb.unblock_task(conn, task_id)
            assert kb.block_task(
                conn, task_id, reason="temporary backend failure", kind="transient"
            )
            assert kb.get_task(conn, task_id).status == "triage"

        # Deliberate dashboard moves create a newer state generation. The old
        # failure event must never become current again merely because the task
        # is now in a supervisable status.
        assert _set_status_direct(conn, task_id, "ready")
        assert _set_status_direct(conn, task_id, "triage")

        result = reconcile_board(
            conn, board="default", config=_config(), notifier_profile="default"
        )

        assert result.changed is False
        assert kb.get_task(conn, task_id).status == "triage"
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? "
            "AND kind = 'supervisor_recovery'", (task_id,),
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_recovery_compare_and_swap_rejects_stale_source_event(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="Racing transition", assignee="forge")
        assert kb.block_task(
            conn, task_id, reason="temporary backend failure", kind="transient"
        )
        blocked = _latest_event(conn, task_id, "blocked")
        assert kb.unblock_task(conn, task_id)
        assert _set_status_direct(conn, task_id, "triage")

        recovered, status = kb.supervisor_recover_task(
            conn,
            task_id,
            source_event_id=blocked["id"],
            source_kind="blocked",
            reason="temporary backend failure",
            recovery_limit=1,
        )

        assert recovered is False
        assert status == "stale_event"
        assert kb.get_task(conn, task_id).status == "triage"
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
        self.received = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})

    async def handle_message(self, event):
        self.received.append(event)
        return None


def test_gate_resolved_before_delivery_is_silent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="Already approved",
            assignee="forge",
            session_id="creator-session",
        )
        assert kb.block_task(
            conn, task_id, reason="Need Kevin's product decision", kind="needs_input"
        )
        reconcile_board(conn, board="default", config=_config(), notifier_profile="default")
        assert kb.unblock_task(conn, task_id)
    finally:
        conn.close()

    monkeypatch.setattr(
        hermes_config,
        "load_config",
        lambda: {"kanban": {"proactive_supervisor": {
            "enabled": True,
            "platform": "discord",
            "chat_id": "hermes-command-channel",
            "chat_type": "channel",
            "recovery_limit": 1,
        }}},
    )
    adapter = _RecordingAdapter()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.DISCORD: adapter}
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

    assert adapter.sent == []
    assert adapter.received == []


@pytest.mark.parametrize("reuse_ordinary_subscription", [False, True])
def test_recovered_followup_on_persistent_supervisor_sub_is_silent(
    tmp_path, monkeypatch, reuse_ordinary_subscription
):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="Recover followup",
            assignee="forge",
            session_id="creator-session",
        )
        if reuse_ordinary_subscription:
            kb.add_notify_sub(
                conn,
                task_id=task_id,
                platform="discord",
                chat_id="hermes-command-channel",
                notifier_profile="default",
                delivery_metadata={"ordinary": True},
            )
        assert kb.block_task(
            conn, task_id, reason="Need Kevin's product decision for gate A",
            kind="needs_input",
        )
        reconcile_board(conn, board="default", config=_config(), notifier_profile="default")
        assert kb.unblock_task(conn, task_id)
        assert kb.block_task(
            conn, task_id, reason="temporary followup failure", kind="transient"
        )
        recovered = reconcile_board(
            conn, board="default", config=_config(), notifier_profile="default"
        )
        assert recovered.recovered == [task_id]
        assert kb.get_task(conn, task_id).status == "ready"
    finally:
        conn.close()

    monkeypatch.setattr(
        hermes_config,
        "load_config",
        lambda: {"kanban": {"proactive_supervisor": {
            "enabled": True,
            "platform": "discord",
            "chat_id": "hermes-command-channel",
            "chat_type": "channel",
            "recovery_limit": 1,
        }}},
    )
    adapter = _RecordingAdapter()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.DISCORD: adapter}
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

    assert adapter.sent == []
    assert adapter.received == []


def test_gate_resolved_after_collection_before_send_is_silent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="Racing approval", assignee="forge")
        assert kb.block_task(
            conn, task_id, reason="Need Kevin's product decision", kind="needs_input"
        )
        reconcile_board(conn, board="default", config=_config(), notifier_profile="default")
    finally:
        conn.close()

    monkeypatch.setattr(
        hermes_config,
        "load_config",
        lambda: {"kanban": {"proactive_supervisor": {
            "enabled": True,
            "platform": "discord",
            "chat_id": "hermes-command-channel",
            "chat_type": "channel",
            "recovery_limit": 1,
        }}},
    )
    adapter = _RecordingAdapter()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.DISCORD: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    runner._active_profile_name = lambda: "default"

    real_authorization_adapter = runner._authorization_adapter
    resolved = False

    def resolve_after_collection(platform, profile=None):
        nonlocal resolved
        if not resolved:
            resolved = True
            race_conn = kb.connect()
            try:
                assert kb.unblock_task(race_conn, task_id)
            finally:
                race_conn.close()
        return real_authorization_adapter(platform, profile)

    runner._authorization_adapter = resolve_after_collection
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    asyncio.run(runner._kanban_notifier_watcher(interval=1))

    assert resolved is True
    assert adapter.sent == []


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
    assert re.search(r"\[kanban-gate:[a-f0-9]{32}\]", delivery["text"])
    assert "_kanban_proactive_supervisor" not in delivery["metadata"]
