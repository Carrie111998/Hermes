"""Gateway /kanban mutations honor the task's frozen creation origin."""

from __future__ import annotations

import pytest

from agent.kanban_handoff_scope import decide_gateway_origin
from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource
from gateway.slash_commands import GatewaySlashCommandsMixin
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_control_guard import MANAGED_CONTROL_DENIED_MESSAGE


_IDENTITY = {
    "platform": "feishu",
    "scope_id": "tenant-1",
    "chat_type": "group",
    "chat_id": "group-1",
    "thread_id": "",
    "user_id": "user-1",
    "notifier_profile": "default",
    "session_key": "agent:default:feishu:group:group-1:user-1",
    "message_id": "message-1",
}


@pytest.fixture(autouse=True)
def _fresh_board():
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()


def _policy_json(workspace: str) -> str:
    config = {
        "agent": {"max_turns": 90},
        "terminal": {"backend": "local"},
        "kanban": {
            "failure_limit": 2,
            "short_task_handoff": {
                "enabled": True,
                "soft_iteration_limit": 4,
                "max_handoffs": 1,
                "allowed_workspace_roots": [workspace],
                "allowed_origins": [
                    {
                        "platform": "feishu",
                        "chat_type": "group",
                        "chat_id": "group-1",
                        "user_id": "user-1",
                    }
                ],
            },
        },
    }
    result = decide_gateway_origin(config, _IDENTITY)
    assert result["authorized"] is True
    return str(result["task_policy_json"])


def _create_task(workspace: str) -> str:
    with kb.connect_closing() as conn:
        return kb.create_task(
            conn,
            title="managed gateway task",
            assignee="default",
            workspace_kind="dir",
            workspace_path=workspace,
            control_origin={
                **_IDENTITY,
                "operation_slot": "slash",
                "short_handoff_policy": _policy_json(workspace),
            },
        )


@pytest.fixture
def managed_task_id(tmp_path) -> str:
    workspace = tmp_path / "managed-control-workspace"
    workspace.mkdir()
    return _create_task(str(workspace.resolve()))


def _source() -> SessionSource:
    return SessionSource(
        platform=Platform.FEISHU,
        scope_id="tenant-1",
        chat_type="group",
        chat_id="transport-group",
        chat_id_alt="group-1",
        user_id="transport-user",
        user_id_alt="user-1",
        profile="default",
    )


class _Runner(GatewaySlashCommandsMixin):
    def __init__(self, identity):
        self.identity = identity

    async def _trusted_kanban_control_identity(self, _event):
        return dict(self.identity) if self.identity is not None else None


def _event(task_id: str, *, internal: bool = False) -> MessageEvent:
    return MessageEvent(
        text=f"/kanban comment {task_id} 网关写入",
        source=_source(),
        message_id="message-2",
        internal=internal,
    )


def _comment_count(task_id: str) -> int:
    with kb.connect_closing() as conn:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM task_comments WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
        )


@pytest.mark.asyncio
async def test_same_bound_feishu_group_can_write_through_gateway(managed_task_id):
    task_id = managed_task_id
    runner = _Runner({**_IDENTITY, "message_id": "message-2"})

    output = await runner._handle_kanban_command(_event(task_id))

    assert output == f"Comment added to {task_id}"
    assert _comment_count(task_id) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identity",
    [
        {**_IDENTITY, "chat_id": "other-group"},
        {**_IDENTITY, "chat_type": "dm", "chat_id": "dm-user-1"},
        {**_IDENTITY, "user_id": "other-user"},
        {
            **_IDENTITY,
            "notifier_profile": "coding-man",
            "session_key": "agent:coding-man:feishu:group:group-1:user-1",
        },
    ],
    ids=["other-group", "direct-message", "other-user", "codingman"],
)
async def test_other_gateway_origins_cannot_write(identity, managed_task_id):
    task_id = managed_task_id
    runner = _Runner({**identity, "message_id": "message-2"})

    output = await runner._handle_kanban_command(_event(task_id))

    assert output == MANAGED_CONTROL_DENIED_MESSAGE
    assert _comment_count(task_id) == 0


@pytest.mark.asyncio
async def test_internal_gateway_event_cannot_borrow_visible_source_identity(
    managed_task_id,
):
    task_id = managed_task_id
    runner = _Runner({**_IDENTITY, "message_id": "message-2"})

    output = await runner._handle_kanban_command(
        _event(task_id, internal=True),
    )

    assert output == MANAGED_CONTROL_DENIED_MESSAGE
    assert _comment_count(task_id) == 0
