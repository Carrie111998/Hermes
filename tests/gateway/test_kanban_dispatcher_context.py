import asyncio

import pytest

from agent.delegation_context import delegated_child_context
from gateway.kanban_watchers import _run_parent_process_call
from hermes_cli import kanban_db as kb


def test_gateway_dispatcher_does_not_inherit_delegated_child_context(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))

    def dispatch_once():
        conn = kb.connect()
        try:
            return kb.create_task(conn, title="gateway-owned dispatch")
        finally:
            conn.close()

    def child_write():
        conn = kb.connect()
        try:
            return kb.create_task(conn, title="forbidden child write")
        finally:
            conn.close()

    async def exercise_boundary():
        with delegated_child_context("child-session"):
            with pytest.raises(PermissionError, match="delegate_task child contexts"):
                await asyncio.to_thread(child_write)

            assert await asyncio.to_thread(_run_parent_process_call, dispatch_once)

            with pytest.raises(PermissionError, match="delegate_task child contexts"):
                await asyncio.to_thread(child_write)

    asyncio.run(exercise_boundary())
