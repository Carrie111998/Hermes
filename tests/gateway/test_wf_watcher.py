"""Gateway-level acceptance for the embedded workflow watcher."""

from __future__ import annotations

import asyncio
import time

from gateway.run import GatewayRunner
from hermes_cli import kanban_db, wf_engine


def _build_overdue_board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "workflow.db"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    conn = kanban_db.connect()
    template_id, _ = wf_engine.register_template(
        conn,
        {
            "id": "gateway-watch",
            "entity": "entity",
            "correlation_keys": ["entity"],
            "disambiguators": [],
            "create_on": [],
            "steps": [
                {"key": "start", "advance_to": "wait"},
                {
                    "key": "wait",
                    "waits": [
                        {
                            "kind": "timer",
                            "after": 60,
                            "action": "advance",
                            "advance_to": "done",
                        }
                    ],
                },
                {"key": "done"},
            ],
        },
    )
    task_id = wf_engine.create_instance(
        conn,
        template_id=template_id,
        entity_key="gateway-entity",
        corr={"entity": "gateway-entity"},
        vars={},
        source_event_id=None,
    )
    setup = wf_engine.ingest_event(
        conn,
        source="synthetic",
        external_id="gateway-setup",
        payload={},
        corr={},
        event_type="setup",
    )
    assert setup is not None
    wf_engine.advance(conn, task_id, to_step="wait", event_id=setup)
    conn.execute(
        "UPDATE wf_wait SET timer_at = ? WHERE task_id = ? AND kind = 'timer'",
        (int(time.time()) - 60, task_id),
    )
    conn.close()
    return task_id


async def _one_gateway_tick(monkeypatch):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._wf_watcher(interval=1)


def test_gateway_restart_catches_due_timer_once(tmp_path, monkeypatch):
    task_id = _build_overdue_board(tmp_path, monkeypatch)

    asyncio.run(_one_gateway_tick(monkeypatch))
    # A brand-new runner represents a gateway restart. The durable external
    # id and satisfied wait make the second catch-up pass a no-op.
    asyncio.run(_one_gateway_tick(monkeypatch))

    conn = kanban_db.connect()
    try:
        assert conn.execute(
            "SELECT current_step_key FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()[0] == "done"
        assert conn.execute(
            "SELECT COUNT(*) FROM wf_event WHERE source = 'timer'"
        ).fetchone()[0] == 1
        assert conn.execute(
            """
            SELECT COUNT(*) FROM wf_transition
             WHERE task_id = ? AND step_key = 'wait'
            """,
            (task_id,),
        ).fetchone()[0] == 1
    finally:
        conn.close()
