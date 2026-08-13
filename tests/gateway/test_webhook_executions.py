"""Execution registry tests (Task 11).

The webhook adapter records an observable execution per delivery, exposes
status/cancel endpoints, and advances the record to completed/failed via the
task done-callback instead of losing fire-and-forget tasks silently.
"""

from __future__ import annotations

import asyncio

import pytest

from aiohttp.test_utils import TestClient, TestServer
from aiohttp import web

from gateway.config import PlatformConfig
from gateway.platforms.webhook import WebhookAdapter


def _adapter(routes=None):
    return WebhookAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "host": "127.0.0.1",
                "port": 0,
                "routes": routes or {"a": {"secret": "s", "prompt": "p"}},
            },
        )
    )


class TestExecutionRegistry:
    def test_record_execution_sets_accepted(self):
        adapter = _adapter()
        rec = adapter._record_execution("d1", "a", None)
        assert rec["state"] == "accepted"
        assert adapter._executions["d1"]["state"] == "accepted"

    def test_mark_execution_advances_state(self):
        adapter = _adapter()
        adapter._record_execution("d1", "a", None)
        adapter._mark_execution("d1", "completed")
        assert adapter._executions["d1"]["state"] == "completed"

    def test_mark_execution_unknown_is_noop(self):
        adapter = _adapter()
        adapter._mark_execution("nope", "completed")  # must not raise

    def test_prune_executions_evicts_stale(self):
        adapter = _adapter()
        adapter._record_execution("old", "a", None)
        adapter._executions["old"]["created_at"] = 0.0  # force stale
        adapter._prune_executions(time_now := 99999.0)
        assert "old" not in adapter._executions

    @pytest.mark.asyncio
    async def test_status_returns_record(self):
        adapter = _adapter()
        adapter._record_execution("d1", "a", "default")
        app = web.Application()
        app.router.add_get("/e/{delivery_id}", adapter._handle_execution_status)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.get("/e/d1")
            assert resp.status == 200
            data = await resp.json()
            assert data["delivery_id"] == "d1"
            assert data["state"] == "accepted"

    @pytest.mark.asyncio
    async def test_status_unknown_404(self):
        adapter = _adapter()
        app = web.Application()
        app.router.add_get("/e/{delivery_id}", adapter._handle_execution_status)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.get("/e/nope")
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_cancel_cancels_running_task(self):
        adapter = _adapter()
        adapter._record_execution("d1", "a", None)

        async def _slow():
            await asyncio.sleep(60)

        task = asyncio.create_task(_slow())
        adapter._execution_tasks["d1"] = task
        app = web.Application()
        app.router.add_post("/c/{delivery_id}", adapter._handle_execution_cancel)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/c/d1")
            assert resp.status == 200
            data = await resp.json()
            assert data["status"] == "cancelled"
        assert task.cancelled() or task.done()
        assert adapter._executions["d1"]["state"] == "cancelled"
