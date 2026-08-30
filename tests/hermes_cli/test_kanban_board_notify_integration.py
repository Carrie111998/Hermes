"""Integration tests for board-pin lookups at Kanban call sites."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_board_notify as board_notify


_VALID_DEST = {
    "platform": "discord",
    "chat_id": "111",
    "thread_id": "222",
}


@pytest.fixture
def isolated_kanban_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for variable in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_WORKSPACES_ROOT",
    ):
        monkeypatch.delenv(variable, raising=False)
    kb._INITIALIZED_PATHS.clear()
    return home


def _write_raw_pin(conn: sqlite3.Connection, board_id: str, raw: str) -> None:
    """Persist an intentionally untrusted pin row for a migration fixture."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS kanban_board_notify "
        "(board_id TEXT PRIMARY KEY NOT NULL, dest_json TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO kanban_board_notify (board_id, dest_json) VALUES (?, ?) "
        "ON CONFLICT(board_id) DO UPDATE SET dest_json = excluded.dest_json",
        (board_id, raw),
    )
    conn.commit()


def _discord_subscriptions(conn, task_id: str) -> list[dict]:
    return [
        row for row in kb.list_notify_subs(conn, task_id)
        if row["platform"] == "discord"
    ]


def test_inheritance_uses_validated_pin_and_ignores_malformed_pin(
    isolated_kanban_home,
):
    conn = kb.connect()
    try:
        pinned_parent = kb.create_task(conn, title="pinned parent", assignee="worker")
        kb.add_notify_sub(
            conn,
            task_id=pinned_parent,
            platform="discord",
            chat_id="channel-1",
            thread_id="thread-1",
        )
        board_notify.set_board_notify(conn, "default", _VALID_DEST)
        pinned_child = kb.create_task(
            conn,
            title="pinned child",
            assignee="worker",
            parents=[pinned_parent],
        )

        board_notify.clear_board_notify(conn, "default")
        malformed_parent = kb.create_task(
            conn, title="malformed parent", assignee="worker"
        )
        kb.add_notify_sub(
            conn,
            task_id=malformed_parent,
            platform="discord",
            chat_id="channel-2",
            thread_id="thread-2",
        )
        _write_raw_pin(conn, "default", "not-json")
        malformed_child = kb.create_task(
            conn,
            title="malformed child",
            assignee="worker",
            parents=[malformed_parent],
        )
        pinned_subscriptions = _discord_subscriptions(conn, pinned_child)
        malformed_subscriptions = _discord_subscriptions(conn, malformed_child)
    finally:
        conn.close()

    assert pinned_subscriptions == []
    assert [sub["chat_id"] for sub in malformed_subscriptions] == [
        "channel-2"
    ]


def test_create_source_falls_back_to_card_route_for_malformed_pin(
    isolated_kanban_home,
):
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="create source", assignee="worker")
        _write_raw_pin(conn, "default", json.dumps({"platform": "discord"}))
        subscribed = kb.subscribe_notify_source_on_create(
            conn,
            task_id=task_id,
            platform="discord",
            chat_id="333",
            thread_id="444",
            chat_type="thread",
            board="default",
        )
        subscriptions = _discord_subscriptions(conn, task_id)
    finally:
        conn.close()

    assert subscribed is True
    assert [(sub["chat_id"], sub["thread_id"]) for sub in subscriptions] == [
        ("333", "444")
    ]


def test_manual_discord_subscribe_refuses_only_a_valid_board_pin(
    isolated_kanban_home,
):
    from hermes_cli.kanban import run_slash

    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="manual subscribe", assignee="worker")
        _write_raw_pin(conn, "default", "{\"platform\": \"discord\"}")
    finally:
        conn.close()

    malformed_result = run_slash(
        f"notify-subscribe {task_id} --platform discord --chat-id 555 "
        "--thread-id 666"
    )

    conn = kb.connect()
    try:
        malformed_subscriptions = _discord_subscriptions(conn, task_id)
        board_notify.set_board_notify(conn, "default", _VALID_DEST)
    finally:
        conn.close()

    valid_result = run_slash(
        f"notify-subscribe {task_id} --platform discord --chat-id 777 "
        "--thread-id 888"
    )

    conn = kb.connect()
    try:
        final_subscriptions = _discord_subscriptions(conn, task_id)
    finally:
        conn.close()

    assert "Subscribed" in malformed_result
    assert [(sub["chat_id"], sub["thread_id"]) for sub in malformed_subscriptions] == [
        ("555", "666")
    ]
    assert "boards notify-pin" in valid_result
    assert [(sub["chat_id"], sub["thread_id"]) for sub in final_subscriptions] == [
        ("555", "666")
    ]


def test_cross_board_conflict_ignores_malformed_pin_but_reserves_valid_pin(
    isolated_kanban_home,
):
    kb.create_board("alpha", legacy_unscoped=True)
    kb.create_board("beta", legacy_unscoped=True)
    alpha = kb.connect(board="alpha")
    beta = kb.connect(board="beta")
    try:
        _write_raw_pin(beta, "beta", "not-json")
        first = kb.set_board_notify(
            alpha,
            platform="discord",
            chat_id="999",
            thread_id="1000",
            board="alpha",
        )
        board_notify.clear_board_notify(alpha, "alpha")
        board_notify.set_board_notify(beta, "beta", {
            "platform": "discord",
            "chat_id": "999",
            "thread_id": "1000",
        })
        with pytest.raises(ValueError, match="already belongs to board 'beta'"):
            kb.set_board_notify(
                alpha,
                platform="discord",
                chat_id="999",
                thread_id="1000",
                board="alpha",
            )
    finally:
        alpha.close()
        beta.close()

    assert first["chat_id"] == "999"
