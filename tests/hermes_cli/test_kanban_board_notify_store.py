"""Behavioral tests for the board notification pin module."""

from __future__ import annotations

import sqlite3

import pytest

from hermes_cli import kanban_board_notify as board_notify


VALID_DEST = {
    "platform": "discord",
    "chat_id": "1487950227108397056",
    "thread_id": "1491150227108397056",
}


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    try:
        yield connection
    finally:
        connection.close()


def test_parse_discord_board_pin_accepts_numeric_discord_destination():
    assert board_notify.parse_discord_board_pin(VALID_DEST) == VALID_DEST


@pytest.mark.parametrize(
    "dest",
    [
        None,
        {},
        {"platform": "discord", "chat_id": VALID_DEST["chat_id"]},
        {
            "platform": "discord",
            "chat_id": "not-a-snowflake",
            "thread_id": VALID_DEST["thread_id"],
        },
        {
            "platform": "discord",
            "chat_id": VALID_DEST["chat_id"],
            "thread_id": VALID_DEST["thread_id"],
            "junk": "unexpected",
        },
        {
            "platform": "slack",
            "chat_id": VALID_DEST["chat_id"],
            "thread_id": VALID_DEST["thread_id"],
        },
        {
            "platform": "discord",
            "chat_id": "",
            "thread_id": VALID_DEST["thread_id"],
        },
        {
            "platform": "discord",
            "chat_id": VALID_DEST["chat_id"],
            "thread_id": "123 trailing-junk",
        },
        {
            "platform": "Discord",
            "chat_id": VALID_DEST["chat_id"],
            "thread_id": VALID_DEST["thread_id"],
        },
        {
            "platform": "discord",
            "chat_id": f" {VALID_DEST['chat_id']}",
            "thread_id": VALID_DEST["thread_id"],
        },
        {
            "platform": "discord",
            "chat_id": VALID_DEST["chat_id"],
            "thread_id": f"{VALID_DEST['thread_id']}\n",
        },
    ],
)
def test_parse_discord_board_pin_rejects_malformed_destinations(dest):
    assert board_notify.parse_discord_board_pin(dest) is None


def test_board_notify_store_round_trips_and_clears_one_board(conn):
    assert board_notify.set_board_notify(conn, "project-a", VALID_DEST) == VALID_DEST
    assert board_notify.get_board_notify(conn, "project-a") == VALID_DEST

    replacement = {
        "platform": "discord",
        "chat_id": "1487950227108397071",
        "thread_id": "1491150227108397071",
    }
    assert board_notify.set_board_notify(conn, "project-a", replacement) == replacement
    assert board_notify.get_board_notify(conn, "project-a") == replacement
    assert conn.execute(
        "SELECT COUNT(*) FROM kanban_board_notify WHERE board_id = ?",
        ("project-a",),
    ).fetchone()[0] == 1

    assert board_notify.clear_board_notify(conn, "project-a") is None
    assert board_notify.get_board_notify(conn, "project-a") is None


def test_board_notify_store_rejects_invalid_destination(conn):
    with pytest.raises(ValueError):
        board_notify.set_board_notify(conn, "project-a", {"platform": "discord"})

    assert board_notify.get_board_notify(conn, "project-a") is None


def test_board_notify_store_fails_closed_for_malformed_persisted_destination(conn):
    board_notify.set_board_notify(conn, "project-a", VALID_DEST)
    conn.execute(
        "UPDATE kanban_board_notify SET dest_json = ? WHERE board_id = ?",
        ("{\"platform\":\"discord\",\"chat_id\":\"not-digit\"}", "project-a"),
    )
    conn.commit()

    assert board_notify.get_board_notify(conn, "project-a") is None
