from __future__ import annotations

import json
import unittest
import uuid

from hermes_cli.kanban_store.events import (
    EventSpoolWriter,
    decode_cursor,
    import_spool,
    read_events,
    safe_payload_json,
)
from hermes_cli.kanban_store.types import ContractError, EventRecord

from .helpers import TempRoot, database


def event(
    event_uuid: str, seq: int | None = None, task_id: str = "t"
):
    return EventRecord(
        event_uuid=event_uuid,
        task_id=task_id,
        run_id=1,
        claim_generation=1,
        event_type="worker.output",
        source="worker",
        severity="info",
        retention_class="output",
        payload={"token": "secret", "workspace_path": "/home/user/repo", "text": "ok"},
        stream="stdout" if seq else None,
        stream_seq=seq,
    )


class EventTests(unittest.TestCase):
    def test_pre_persistence_redaction(self):
        payload = json.loads(safe_payload_json({"api_key": "x", "cwd": "/a/b", "text": "ok"}))
        self.assertEqual(payload["api_key"], "***")
        self.assertEqual(payload["cwd"], "<path>/b")

    def test_spool_import_is_idempotent(self):
        with TempRoot() as root:
            conn = database(root)
            path = root / "events.jsonl"
            with EventSpoolWriter(path) as writer:
                writer.write(event(str(uuid.uuid4())))
            first = import_spool(conn, path)
            second = import_spool(conn, path)
            self.assertEqual(first["imported"], 1)
            self.assertEqual(second["duplicates"], 1)
            stored = json.loads(conn.execute("SELECT payload_json FROM task_events").fetchone()[0])
            self.assertEqual(stored["token"], "***")

    def test_cursor_is_bound_to_database(self):
        with TempRoot() as one, TempRoot() as two:
            c1, c2 = database(one), database(two)
            cursor = read_events(c1)["cursor"]
            with self.assertRaises(ContractError):
                decode_cursor(c2, cursor)


    def test_task_cursor_is_bound_to_task_scope(self):
        with TempRoot() as root:
            conn = database(root)
            from hermes_cli.kanban_store.events import append_event

            append_event(conn, event("a-1", task_id="task-a"))
            append_event(conn, event("b-1", task_id="task-b"))
            cursor = read_events(conn, task_id="task-a")["cursor"]
            with self.assertRaises(ContractError):
                decode_cursor(conn, cursor, task_id="task-b")
            with self.assertRaises(ContractError):
                read_events(conn, task_id="task-b", cursor=cursor)

    def test_task_filter_is_bounded(self):
        with TempRoot() as root:
            conn = database(root)
            from hermes_cli.kanban_store.events import append_event
            append_event(conn, event("e-1"))
            other = EventRecord(**{**event("e-2").__dict__}) if False else None
            row = read_events(conn, task_id="t", limit=1)
            self.assertEqual(len(row["events"]), 1)
            self.assertFalse(row["has_more"])


if __name__ == "__main__":
    unittest.main()
