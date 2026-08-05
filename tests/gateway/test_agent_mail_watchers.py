import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any, cast

import gateway.agent_mail_watchers as watcher
from gateway.agent_mail_watchers import _wake_text


def test_agent_mail_wake_is_internal_and_bounded():
    text = _wake_text(
        "SilverLens",
        {"id": 42, "sender": "IronPaw", "subject": "test", "importance": "normal", "thread_id": None, "body_md": "x" * 7000},
    )
    assert text.startswith("[INTERNAL AGENT MAIL WAKE")
    assert "SilverLens" in text
    assert "message 42" in text
    assert len(text) < 6800


def test_adapter_failure_does_not_advance_delivery_cursor():
    """A failed wake must be retried; it cannot consume the message cursor."""
    with tempfile.TemporaryDirectory() as directory:
        state_file = Path(directory) / "ironpaw.json"
        state_file.write_text(json.dumps({"last_seen_message_id": 10}))
        original_fetch = watcher._fetch_unread
        original_deliver = watcher.deliver_wake

        async def failing_deliver(*_args, **_kwargs):
            raise RuntimeError("adapter unavailable")

        try:
            watcher._fetch_unread = lambda *_args: [
                {"id": 11, "sender": "BrightTower", "subject": "retry", "importance": "normal", "thread_id": None, "body_md": "x"}
            ]
            watcher.deliver_wake = failing_deliver
            try:
                asyncio.run(
                    watcher._deliver_unread_once(
                        adapter=object(),
                        source=cast(Any, object()),
                        state_file=state_file,
                        identity="IronPaw",
                        project_key="project",
                        batch_size=3,
                        profile="default",
                    )
                )
            except RuntimeError as exc:
                assert str(exc) == "adapter unavailable"
            else:
                raise AssertionError("adapter failure must escape the delivery batch")
            assert json.loads(state_file.read_text())["last_seen_message_id"] == 10
        finally:
            watcher._fetch_unread = original_fetch
            watcher.deliver_wake = original_deliver


def test_successful_delivery_advances_only_its_profile_cursor_and_never_marks_read():
    """Delivery ownership is a cursor, not implicit inbox handling."""
    with tempfile.TemporaryDirectory() as directory:
        ironpaw_state = Path(directory) / "default.json"
        daery_state = Path(directory) / "daery.json"
        ironpaw_state.write_text(json.dumps({"last_seen_message_id": 10}))
        daery_state.write_text(json.dumps({"last_seen_message_id": 77}))
        original_fetch = watcher._fetch_unread
        original_deliver = watcher.deliver_wake
        original_mark = watcher._mark_read
        delivered = []

        async def accepted_deliver(_adapter, *, text, source, message_id):
            delivered.append((text, source, message_id))

        def must_not_mark_read(*_args, **_kwargs):
            raise AssertionError("gateway delivery must not mark Agent Mail read")

        try:
            watcher._fetch_unread = lambda *_args: [
                {"id": 11, "sender": "BrightTower", "subject": "accepted", "importance": "normal", "thread_id": None, "body_md": "x"}
            ]
            watcher.deliver_wake = accepted_deliver
            watcher._mark_read = must_not_mark_read
            count = asyncio.run(
                watcher._deliver_unread_once(
                    adapter=object(),
                    source=cast(Any, object()),
                    state_file=ironpaw_state,
                    identity="IronPaw",
                    project_key="project",
                    batch_size=3,
                    profile="default",
                )
            )
            assert count == 1
            assert delivered[0][2] == "agent-mail:11"
            assert json.loads(ironpaw_state.read_text())["last_seen_message_id"] == 11
            assert json.loads(daery_state.read_text())["last_seen_message_id"] == 77
        finally:
            watcher._fetch_unread = original_fetch
            watcher.deliver_wake = original_deliver
            watcher._mark_read = original_mark


def test_fetch_unread_is_recipient_scoped():
    with tempfile.TemporaryDirectory() as directory:
        db_path = Path(directory) / "mail.sqlite3"
        con = watcher.sqlite3.connect(db_path)
        try:
            con.executescript(
                """
                create table projects (id integer primary key, human_key text);
                create table agents (id integer primary key, project_id integer, name text, retired_at text);
                create table messages (id integer primary key, project_id integer, sender_id integer, subject text, body_md text, created_ts text, importance text, thread_id text);
                create table message_recipients (message_id integer, agent_id integer, read_ts text);
                insert into projects values (1, 'project');
                insert into agents values (1, 1, 'BrightTower', null);
                insert into agents values (2, 1, 'SilverHarbor', null);
                insert into agents values (3, 1, 'IronPaw', null);
                insert into messages values (11, 1, 1, 'Silver only', 'a', '2026-01-01T00:00:00Z', 'normal', null);
                insert into messages values (12, 1, 1, 'Iron only', 'b', '2026-01-01T00:00:01Z', 'normal', null);
                insert into messages values (13, 1, 1, 'Read Silver', 'c', '2026-01-01T00:00:02Z', 'normal', null);
                insert into message_recipients values (11, 2, null);
                insert into message_recipients values (12, 3, null);
                insert into message_recipients values (13, 2, '2026-01-01T00:00:03Z');
                """
            )
            con.commit()
        finally:
            con.close()
        original_db = watcher._MAIL_DB
        try:
            watcher._MAIL_DB = db_path
            assert [row["id"] for row in watcher._fetch_unread("SilverHarbor", "project", 0, 10)] == [11]
            assert [row["id"] for row in watcher._fetch_unread("IronPaw", "project", 0, 10)] == [12]
        finally:
            watcher._MAIL_DB = original_db


if __name__ == "__main__":
    test_agent_mail_wake_is_internal_and_bounded()
    test_adapter_failure_does_not_advance_delivery_cursor()
    test_successful_delivery_advances_only_its_profile_cursor_and_never_marks_read()
    test_fetch_unread_is_recipient_scoped()
    print("PASS: Agent Mail watcher lifecycle tests")
