"""Behavioral coverage for the real Phase-1 Feishu pilot entry path."""

from __future__ import annotations

import json
import shlex

import pytest

from agent import kanban_auto_handoff
from agent import kanban_handoff_scope as handoff_scope
from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_cli import kanban_db as kb
from hermes_cli.kanban import run_slash


_BASE_IDENTITY = {
    "platform": "feishu",
    "scope_id": "pilot-tenant",
    "chat_type": "group",
    "chat_id": "pilot-group",
    "thread_id": "",
    "user_id": "pilot-user",
    "notifier_profile": "default",
    "session_key": "agent:default:feishu:group:pilot-group:pilot-user",
    "message_id": "pilot-message",
}


@pytest.fixture(autouse=True)
def _fresh_board():
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()


def _config(workspace: str) -> dict:
    return {
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
                        "chat_id": "pilot-group",
                        "user_id": "pilot-user",
                    }
                ],
            },
        },
    }


def _identity(**overrides: str) -> dict[str, str]:
    identity = dict(_BASE_IDENTITY)
    identity.update(overrides)
    return identity


def _control_origin(config: dict, identity: dict[str, str]) -> dict[str, str]:
    decision = handoff_scope.decide_gateway_origin(config, identity)
    assert decision["authorized"] is True
    return {
        **identity,
        "operation_slot": "slash",
        "short_handoff_policy": str(decision["task_policy_json"]),
    }


def _create_command(
    workspace: str,
    *,
    title: str = "pilot text mechanism",
    validation_class: str | None = "text_mechanism",
) -> str:
    parts = [
        "create",
        shlex.quote(title),
        "--assignee",
        "default",
        "--workspace",
        shlex.quote(f"dir:{workspace}"),
        "--max-retries",
        "1",
    ]
    if validation_class is not None:
        parts.extend(["--validation-class", validation_class])
    parts.append("--json")
    return " ".join(parts)


def _write_counts() -> tuple[int, int, int, int]:
    with kb.connect_closing() as conn:
        return tuple(
            int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "tasks",
                "task_events",
                "kanban_control_bindings",
                "kanban_control_creations",
            )
        )


def _source() -> SessionSource:
    return SessionSource(
        platform=Platform.FEISHU,
        scope_id="pilot-tenant",
        chat_type="group",
        chat_id="transport-group",
        chat_id_alt="pilot-group",
        user_id="transport-user",
        user_id_alt="pilot-user",
        profile="default",
    )


def _gateway_runner(config: dict, identity: dict[str, str] | None) -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner._kanban_handoff_policy_for_source = lambda _source: (
        kanban_auto_handoff.build_dispatcher_policy_snapshot(config)
    )

    async def trusted_identity(_event):
        return dict(identity) if identity is not None else None

    runner._trusted_kanban_control_identity = trusted_identity
    return runner


def test_real_run_slash_creates_authorized_text_mechanism_task(tmp_path):
    workspace = tmp_path / "approved-pilot"
    workspace.mkdir()
    workspace_value = str(workspace.resolve())
    config = _config(workspace_value)
    identity = _identity(message_id="direct-slash-message")

    output = run_slash(
        _create_command(workspace_value),
        control_origin=_control_origin(config, identity),
    )

    created = json.loads(output)
    assert created["validation_class"] == "text_mechanism"
    assert created["workspace_kind"] == "dir"
    assert created["workspace_path"] == workspace_value
    assert created["max_retries"] == 1
    assert _write_counts() == (1, 1, 1, 1)


@pytest.mark.asyncio
async def test_real_gateway_handler_creates_authorized_text_mechanism_task(
    tmp_path,
    monkeypatch,
):
    workspace = tmp_path / "approved-pilot"
    workspace.mkdir()
    workspace_value = str(workspace.resolve())
    config = _config(workspace_value)
    identity = _identity(message_id="gateway-create-message")
    monkeypatch.setattr(
        handoff_scope,
        "_load_current_dispatcher_config",
        lambda: config,
    )
    runner = _gateway_runner(config, identity)
    event = MessageEvent(
        text=f"/kanban {_create_command(workspace_value)}",
        source=_source(),
        message_id=identity["message_id"],
    )

    output = await GatewayRunner._handle_kanban_command(runner, event)

    created = json.loads(output)
    assert created["validation_class"] == "text_mechanism"
    assert created["workspace_kind"] == "dir"
    assert created["workspace_path"] == workspace_value
    assert created["max_retries"] == 1
    assert _write_counts() == (1, 1, 1, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "identity_overrides", "internal", "validation_class"),
    [
        ("wrong-group", {"chat_id": "other-group"}, False, "text_mechanism"),
        ("wrong-user", {"user_id": "other-user"}, False, "text_mechanism"),
        ("internal", {}, True, "text_mechanism"),
        ("invalid-class", {}, False, "untrusted_class"),
    ],
)
async def test_gateway_rejects_unsafe_text_mechanism_entries_without_writes(
    tmp_path,
    monkeypatch,
    case,
    identity_overrides,
    internal,
    validation_class,
):
    workspace = tmp_path / "approved-pilot"
    workspace.mkdir()
    workspace_value = str(workspace.resolve())
    config = _config(workspace_value)
    identity = _identity(
        message_id=f"{case}-message",
        **identity_overrides,
    )
    monkeypatch.setattr(
        handoff_scope,
        "_load_current_dispatcher_config",
        lambda: config,
    )
    runner = _gateway_runner(config, identity)
    event = MessageEvent(
        text=(
            f"/kanban {_create_command(workspace_value, validation_class=validation_class)}"
        ),
        source=_source(),
        message_id=identity["message_id"],
        internal=internal,
    )

    output = await GatewayRunner._handle_kanban_command(runner, event)

    assert output
    assert _write_counts() == (0, 0, 0, 0)


@pytest.mark.asyncio
async def test_gateway_rejects_untrusted_text_mechanism_entry_without_writes(
    tmp_path,
    monkeypatch,
):
    workspace = tmp_path / "approved-pilot"
    workspace.mkdir()
    workspace_value = str(workspace.resolve())
    config = _config(workspace_value)
    monkeypatch.setattr(
        handoff_scope,
        "_load_current_dispatcher_config",
        lambda: config,
    )
    runner = _gateway_runner(config, None)
    event = MessageEvent(
        text=f"/kanban {_create_command(workspace_value)}",
        source=_source(),
        message_id="untrusted-message",
    )

    output = await GatewayRunner._handle_kanban_command(runner, event)

    assert output
    assert _write_counts() == (0, 0, 0, 0)


@pytest.mark.asyncio
async def test_gateway_does_not_infer_text_mechanism_from_task_prose(
    tmp_path,
    monkeypatch,
):
    workspace = tmp_path / "approved-pilot"
    workspace.mkdir()
    workspace_value = str(workspace.resolve())
    config = _config(workspace_value)
    identity = _identity(message_id="no-class-flag-message")
    monkeypatch.setattr(
        handoff_scope,
        "_load_current_dispatcher_config",
        lambda: config,
    )
    runner = _gateway_runner(config, identity)
    event = MessageEvent(
        text=(
            "/kanban "
            + _create_command(
                workspace_value,
                title="please use text_mechanism review",
                validation_class=None,
            )
        ),
        source=_source(),
        message_id=identity["message_id"],
    )

    output = await GatewayRunner._handle_kanban_command(runner, event)

    created = json.loads(output)
    assert created["validation_class"] == "code"
    assert _write_counts() == (1, 1, 1, 1)


@pytest.mark.parametrize("origin_kind", ["missing", "empty-policy"])
def test_database_rejects_untrusted_text_mechanism_before_any_write(
    tmp_path,
    origin_kind,
):
    workspace = tmp_path / "approved-pilot"
    workspace.mkdir()
    identity = _identity(message_id=f"db-{origin_kind}-message")
    control_origin = None
    if origin_kind == "empty-policy":
        control_origin = {
            **identity,
            "operation_slot": "slash",
            "short_handoff_policy": "",
        }

    with kb.connect_closing() as conn:
        with pytest.raises(
            ValueError,
            match="authorized managed short-task control origin",
        ):
            kb.create_task(
                conn,
                title="must not write",
                assignee="default",
                workspace_kind="dir",
                workspace_path=str(workspace.resolve()),
                validation_class="text_mechanism",
                control_origin=control_origin,
            )

    assert _write_counts() == (0, 0, 0, 0)


def test_database_rejects_text_mechanism_outside_exact_allowed_directory(
    tmp_path,
):
    approved = tmp_path / "approved-pilot"
    outside = tmp_path / "outside-pilot"
    approved.mkdir()
    outside.mkdir()
    config = _config(str(approved.resolve()))
    identity = _identity(message_id="outside-workspace-message")

    with kb.connect_closing() as conn:
        with pytest.raises(ValueError, match="not one exact approved pilot directory"):
            kb.create_task(
                conn,
                title="must stay in approved directory",
                assignee="default",
                workspace_kind="dir",
                workspace_path=str(outside.resolve()),
                validation_class="text_mechanism",
                control_origin=_control_origin(config, identity),
            )

    assert _write_counts() == (0, 0, 0, 0)
