"""Real-DB safety tests for gateway control of detached Kanban tasks.

These tests deliberately exercise ``_route_background_kanban_control`` rather
than mocking it at the ``/stop`` or ``/steer`` command boundary.  The route is
load-bearing: notification delivery must not grant mutation authority, every
actor field must match, cross-board ambiguity must fail closed, and a platform
redelivery must be exactly once even after later controls or board archival.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource
from gateway.slash_commands import GatewaySlashCommandsMixin
from hermes_cli import kanban_db as kb


_ACTOR = {
    "platform": "feishu",
    "scope_id": "tenant-1",
    "chat_type": "group",
    "chat_id": "group-1",
    "thread_id": "thread-1",
    "user_id": "user-1",
    "notifier_profile": "default",
    "session_key": "agent:default:feishu:group:group-1:user-1",
}

_FAKE_WORKER_IDENTITY = {
    "owner_node_id": "gateway-control-test-node",
    "owner_boot_id": "gateway-control-test-boot",
    "worker_pid": 424242,
    "worker_start_token": "gateway-control-test-start",
    "worker_pgid": 424242,
}


@pytest.fixture(autouse=True)
def _fresh_kanban_db(monkeypatch):
    # HERMES_HOME is unique per test via tests/conftest.py.  Clearing the
    # process-local path cache makes that isolation explicit for this module.
    kb._INITIALIZED_PATHS.clear()
    monkeypatch.setattr(
        kb, "_short_task_handoff_dispatch_enabled", lambda: True
    )
    kb.init_db()


def _identity(message_id: str, **overrides: str) -> dict[str, str]:
    result = dict(_ACTOR)
    result.update(overrides)
    result["message_id"] = message_id
    return result


def _origin(message_id: str, **overrides: str) -> dict[str, str]:
    result = _identity(message_id, **overrides)
    result["operation_slot"] = "slash"
    return result


def _source() -> SessionSource:
    return SessionSource(
        platform=Platform.FEISHU,
        scope_id=_ACTOR["scope_id"],
        chat_type=_ACTOR["chat_type"],
        chat_id="transport-chat-id",
        chat_id_alt=_ACTOR["chat_id"],
        thread_id=_ACTOR["thread_id"],
        user_id="transport-user-id",
        user_id_alt=_ACTOR["user_id"],
        profile=_ACTOR["notifier_profile"],
    )


def _event(message_id: str, *, text: str = "/stop", internal: bool = False):
    return MessageEvent(
        text=text,
        source=_source(),
        message_id=message_id,
        internal=internal,
    )


class _RouteRunner(GatewaySlashCommandsMixin):
    def __init__(
        self,
        identity: dict[str, str] | None,
        *,
        policy_enabled: bool = True,
    ) -> None:
        self.identity = dict(identity) if identity is not None else None
        self.policy_enabled = policy_enabled
        self.policy_lookups = 0
        self.identity_lookups = 0
        self._running_agents = {}
        self.adapters = {}
        self.async_session_store = SimpleNamespace(
            get_or_create_session=self._get_or_create_session
        )

    async def _get_or_create_session(self, _source):
        return SimpleNamespace(session_key=_ACTOR["session_key"])

    def _kanban_handoff_policy_for_source(self, _source):
        self.policy_lookups += 1
        return {"enabled": self.policy_enabled}

    async def _trusted_kanban_control_identity(self, _event):
        self.identity_lookups += 1
        return dict(self.identity) if self.identity is not None else None

    def _sibling_thread_run_keys(self, _source, _session_key):
        return []

    def _is_user_authorized(self, _source):
        return True

    async def _interrupt_and_clear_session(self, *_args, **_kwargs):
        raise AssertionError("test should route to background kanban control")


def _ensure_board(board: str) -> None:
    if board != kb.DEFAULT_BOARD:
        kb.create_board(board)


def _create_task(
    *,
    board: str = kb.DEFAULT_BOARD,
    title: str = "controlled task",
    creation_message_id: str,
    controlled: bool = True,
) -> str:
    _ensure_board(board)
    with kb.connect(board=board) as conn:
        return kb.create_task(
            conn,
            title=title,
            assignee="default",
            control_origin=(
                _origin(creation_message_id) if controlled else None
            ),
        )


def _snapshot(board: str = kb.DEFAULT_BOARD) -> dict[str, Any]:
    with kb.connect(board=board) as conn:
        return {
            "controls": conn.execute(
                "SELECT COUNT(*) FROM task_handoff_controls"
            ).fetchone()[0],
            "comments": conn.execute(
                "SELECT COUNT(*) FROM task_comments "
                "WHERE author LIKE 'handoff-control:%'"
            ).fetchone()[0],
            "events": conn.execute(
                "SELECT COUNT(*) FROM task_events "
                "WHERE kind = 'handoff_control_recorded'"
            ).fetchone()[0],
            "tasks": tuple(
                tuple(row)
                for row in conn.execute(
                    "SELECT id, status, current_run_id, worker_pid, claim_lock, "
                    "block_kind FROM tasks ORDER BY id"
                ).fetchall()
            ),
        }


@pytest.mark.asyncio
async def test_notification_subscription_never_grants_control_but_binding_does():
    notify_only = _create_task(
        title="notification only",
        creation_message_id="unused",
        controlled=False,
    )
    with kb.connect() as conn:
        kb.add_notify_sub(
            conn,
            task_id=notify_only,
            platform=_ACTOR["platform"],
            chat_id=_ACTOR["chat_id"],
            thread_id=_ACTOR["thread_id"],
            user_id=_ACTOR["user_id"],
            notifier_profile=_ACTOR["notifier_profile"],
        )
    before = _snapshot()
    runner = _RouteRunner(_identity("notify-only-stop"))

    denied = await runner._route_background_kanban_control(
        _event("notify-only-stop"),
        kind="stop",
        message="stop the task",
    )

    assert denied == {"status": "none"}
    assert _snapshot() == before

    controlled = _create_task(
        title="exactly bound",
        creation_message_id="create-controlled",
    )
    runner.identity = _identity("bound-steer")
    accepted = await runner._route_background_kanban_control(
        _event("bound-steer", text="/steer use path B"),
        kind="steer",
        message="use path B",
    )

    assert accepted == {
        "status": "routed",
        "task_id": controlled,
        "kind": "steer",
    }
    after = _snapshot()
    assert after["controls"] == 1
    assert after["comments"] == 1
    assert after["events"] == 1
    with kb.connect() as conn:
        assert kb.get_task(conn, notify_only).status == "ready"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "mismatched_value"),
    [
        ("platform", "slack"),
        ("scope_id", "tenant-2"),
        ("chat_type", "dm"),
        ("chat_id", "group-2"),
        ("thread_id", "thread-2"),
        ("user_id", "user-2"),
        ("notifier_profile", "other-profile"),
        ("session_key", "agent:other-session"),
    ],
)
async def test_every_actor_field_mismatch_is_zero_write(
    field: str,
    mismatched_value: str,
):
    task_id = _create_task(creation_message_id="create-field-matrix")
    before = _snapshot()
    runner = _RouteRunner(
        _identity("mismatch-control", **{field: mismatched_value})
    )

    result = await runner._route_background_kanban_control(
        _event("mismatch-control"),
        kind="stop",
        message="must not land",
    )

    assert result == {"status": "none"}
    assert _snapshot() == before
    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).status == "ready"


@pytest.mark.asyncio
async def test_zero_or_multiple_cross_board_matches_never_guess():
    alpha = _create_task(
        board="alpha",
        title="alpha unrelated actor",
        creation_message_id="create-alpha-unrelated",
    )
    beta = _create_task(
        board="beta",
        title="beta unrelated actor",
        creation_message_id="create-beta-unrelated",
    )
    zero_before = {board: _snapshot(board) for board in ("alpha", "beta")}
    zero_runner = _RouteRunner(_identity("zero-match", user_id="other-user"))

    zero = await zero_runner._route_background_kanban_control(
        _event("zero-match"),
        kind="stop",
        message="do not guess",
    )

    assert zero == {"status": "none"}
    assert {board: _snapshot(board) for board in ("alpha", "beta")} == zero_before

    # Re-create this scenario in two new boards with the exact same trusted
    # actor. Different creation deliveries make both bindings valid, so the
    # gateway must surface ambiguity rather than choose by recency or board.
    gamma = _create_task(
        board="gamma",
        title="gamma exact actor",
        creation_message_id="create-gamma",
    )
    delta = _create_task(
        board="delta",
        title="delta exact actor",
        creation_message_id="create-delta",
    )
    multi_before = {board: _snapshot(board) for board in ("gamma", "delta")}
    multi_runner = _RouteRunner(_identity("multi-match"))

    multiple = await multi_runner._route_background_kanban_control(
        _event("multi-match"),
        kind="steer",
        message="must not choose a project",
    )

    assert multiple["status"] == "ambiguous"
    assert set(multiple["task_ids"]) == {alpha, beta, gamma, delta}
    assert {board: _snapshot(board) for board in ("gamma", "delta")} == multi_before
    assert alpha != beta


@pytest.mark.asyncio
async def test_concurrent_same_delivery_is_exactly_once():
    task_id = _create_task(creation_message_id="create-concurrent")
    runner = _RouteRunner(_identity("same-delivery"))
    event = _event("same-delivery")

    results = await asyncio.gather(
        runner._route_background_kanban_control(
            event,
            kind="stop",
            message="pause exactly once",
        ),
        runner._route_background_kanban_control(
            event,
            kind="stop",
            message="pause exactly once",
        ),
    )

    assert sorted(result["status"] for result in results) == [
        "already_processed",
        "routed",
    ]
    assert {result.get("task_id") for result in results} == {task_id}
    snapshot = _snapshot()
    assert snapshot["controls"] == 1
    assert snapshot["comments"] == 1
    assert snapshot["events"] == 1
    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        assert task.status == "blocked"
        assert task.block_kind == "needs_input"


@pytest.mark.asyncio
async def test_real_stop_handler_routes_plain_chinese_background_reason():
    task_id = _create_task(creation_message_id="create-real-stop")
    runner = _RouteRunner(_identity("real-stop"))

    reply = await runner._handle_stop_command(_event("real-stop"))

    assert getattr(reply, "text", str(reply)) == "已安全暂停当前短任务。"
    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        assert task.status == "blocked"
        row = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? "
            "AND kind = 'blocked' ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        assert row is not None
        payload = json.loads(row["payload"])
        assert payload["reason"] == "用户已要求停止这项短任务。"
        assert "User requested" not in payload["reason"]


@pytest.mark.asyncio
async def test_signal_failure_is_saved_but_waiting_and_never_replaced(monkeypatch):
    task_id = _create_task(creation_message_id="create-running")
    fake_identity = dict(_FAKE_WORKER_IDENTITY)
    monkeypatch.setattr(
        kb,
        "_capture_process_group_identity",
        lambda _pid: dict(fake_identity),
    )
    with kb.connect() as conn:
        claimed = kb.claim_task(conn, task_id, claimer="gateway-control-test")
        assert claimed is not None
        assert kb._set_worker_pid(
            conn,
            task_id,
            fake_identity["worker_pid"],
            expected_run_id=claimed.current_run_id,
            expected_claim_lock=claimed.claim_lock,
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_runs SET handoff_safety_required = 1 WHERE id = ?",
                (int(claimed.current_run_id),),
            )
    monkeypatch.setattr(
        kb,
        "_verified_supervised_worker_identity",
        lambda _run, _pid: dict(fake_identity),
    )
    monkeypatch.setattr(
        kb,
        "_signal_verified_process_group",
        lambda _identity: "permission denied",
    )
    runner = _RouteRunner(_identity("signal-failure"))

    first = await runner._route_background_kanban_control(
        _event("signal-failure", text="/steer safer path"),
        kind="steer",
        message="safer path",
    )

    assert first["status"] == "saved_but_waiting"
    assert first["task_id"] == task_id
    assert first["kind"] == "steer"
    first_snapshot = _snapshot()
    assert first_snapshot["controls"] == 1
    assert first_snapshot["comments"] == 1
    assert first_snapshot["events"] == 1
    with kb.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM task_exit_gates "
            "WHERE child_task_id = ? AND released_at IS NULL",
            (task_id,),
        ).fetchone()[0] == 1
        task = kb.get_task(conn, task_id)
        assert task.status == "todo"
        assert task.current_run_id is None
        assert task.worker_pid == fake_identity["worker_pid"]
        assert kb.recompute_ready(conn) == 0
        assert kb.claim_task(conn, task_id, claimer="replacement") is None

    replay = await runner._route_background_kanban_control(
        _event("signal-failure", text="/steer safer path"),
        kind="steer",
        message="safer path",
    )

    assert replay["status"] == "saved_but_waiting"
    assert _snapshot() == first_snapshot


@pytest.mark.asyncio
async def test_delayed_old_stop_replay_is_neutral_after_newer_steer():
    task_id = _create_task(creation_message_id="create-replay-order")
    runner = _RouteRunner(_identity("old-stop"))

    stopped = await runner._route_background_kanban_control(
        _event("old-stop"),
        kind="stop",
        message="pause",
    )
    assert stopped["status"] == "routed"
    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).status == "blocked"

    runner.identity = _identity("newer-steer")
    steered = await runner._route_background_kanban_control(
        _event("newer-steer", text="/steer use path B"),
        kind="steer",
        message="use path B",
    )
    assert steered["status"] == "routed"
    after_steer = _snapshot()
    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).status != "blocked"

    runner.identity = _identity("old-stop")
    replay = await runner._route_background_kanban_control(
        _event("old-stop"),
        kind="stop",
        message="pause",
    )

    assert replay == {
        "status": "already_processed",
        "task_id": task_id,
        "kind": "stop",
    }
    assert _snapshot() == after_steer


@pytest.mark.asyncio
async def test_archived_receipt_replay_never_retargets_new_chain():
    old_task = _create_task(
        board="alpha",
        title="old project",
        creation_message_id="create-old-project",
    )
    runner = _RouteRunner(_identity("old-delivery"))
    first = await runner._route_background_kanban_control(
        _event("old-delivery", text="/steer old direction"),
        kind="steer",
        message="old direction",
    )
    assert first["status"] == "routed"
    assert first["task_id"] == old_task

    archived = kb.remove_board("alpha", archive=True)
    archived_db = Path(archived["new_path"]) / "kanban.db"
    new_task = _create_task(
        board="beta",
        title="new project",
        creation_message_id="create-new-project",
    )
    beta_before = _snapshot("beta")

    replay = await runner._route_background_kanban_control(
        _event("old-delivery", text="/steer old direction"),
        kind="steer",
        message="old direction",
    )

    assert replay == {
        "status": "already_processed",
        "task_id": old_task,
        "kind": "steer",
    }
    assert _snapshot("beta") == beta_before
    with kb.connect(board="beta") as conn:
        assert kb.get_task(conn, new_task).status == "ready"
    with kb.connect(db_path=archived_db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM task_handoff_controls"
        ).fetchone()[0] == 1


@pytest.mark.asyncio
async def test_internal_event_is_rejected_before_policy_identity_or_db_lookup():
    task_id = _create_task(creation_message_id="create-internal-guard")
    before = _snapshot()
    runner = _RouteRunner(_identity("synthetic-control"))

    result = await runner._route_background_kanban_control(
        _event("synthetic-control", internal=True),
        kind="stop",
        message="synthetic completion text",
    )

    assert result["status"] == "error"
    assert "internal" in result["error"]
    assert runner.policy_lookups == 0
    assert runner.identity_lookups == 0
    assert _snapshot() == before
    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).status == "ready"


@pytest.mark.asyncio
async def test_disabled_policy_with_missing_identity_preserves_legacy_fallback():
    task_id = _create_task(creation_message_id="create-disabled-missing-id")
    before = _snapshot()
    runner = _RouteRunner(None, policy_enabled=False)

    result = await runner._route_background_kanban_control(
        _event("", text="/steer normal turn"),
        kind="steer",
        message="normal turn",
    )

    assert result == {"status": "disabled"}
    assert runner.policy_lookups == 1
    assert runner.identity_lookups == 1
    assert _snapshot() == before
    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).status == "ready"


@pytest.mark.asyncio
async def test_disabled_policy_keeps_existing_exact_binding_stoppable():
    task_id = _create_task(creation_message_id="create-before-rollback")
    runner = _RouteRunner(
        _identity("stop-after-rollback"),
        policy_enabled=False,
    )

    result = await runner._route_background_kanban_control(
        _event("stop-after-rollback"),
        kind="stop",
        message="stop after rollback",
    )

    assert result == {
        "status": "routed",
        "task_id": task_id,
        "kind": "stop",
    }
    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).status == "blocked"
