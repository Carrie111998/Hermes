from __future__ import annotations

import unittest

from hermes_cli.kanban_store.claims import issue_claim
from hermes_cli.kanban_store.events import append_event
from hermes_cli.kanban_store.types import EventRecord
from hermes_cli.kanban_surfaces.service import Actor, KanbanSecurityService

from .helpers import TempRoot, add_task, database


class SurfaceTests(unittest.TestCase):
    def test_read_requires_role(self):
        with TempRoot() as root:
            conn = database(root)
            service = KanbanSecurityService(conn=conn)
            with self.assertRaises(PermissionError):
                service.publication_queue(Actor("x", frozenset()))

    def test_event_surface_is_task_filtered(self):
        with TempRoot() as root:
            conn = database(root)
            for task in ("a", "b"):
                append_event(
                    conn,
                    EventRecord(
                        event_uuid=task,
                        task_id=task,
                        run_id=None,
                        claim_generation=None,
                        event_type="x",
                        source="test",
                        severity="info",
                        retention_class="audit",
                        payload={},
                    ),
                )
            service = KanbanSecurityService(conn=conn)
            page = service.event_page(
                Actor("reader", frozenset({"kanban.read"})), task_id="a", cursor=None
            )
            self.assertEqual([item["task_id"] for item in page["events"]], ["a"])


if __name__ == "__main__":
    unittest.main()
