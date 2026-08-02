"""Tests for the gateway delivery-obligation ledger (gateway/delivery_ledger.py).

State machine, dead-owner claiming, attempts cap, stale cutoff, retention,
id stability, and the startup redelivery sweep's contract:
- pending rows redeliver plainly (send never started, no dup risk)
- attempting/failed rows carry the recovered-reply marker (honest
  at-least-once; ambiguity is labeled, never silently resent)
- rows owned by a LIVE process are never claimed
- poison rows abandon at the attempts cap / stale cutoff
"""

import json
import sqlite3
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway import delivery_ledger as dl


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    """Isolated state.db per test (autouse HERMES_HOME isolation already
    redirects get_hermes_home; make the redirect explicit and per-test)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(dl, "_db_path", lambda: home / "state.db")
    yield


def _record(oid="ob-1", session_key="agent:main:slack:channel:C1", **kw):
    dl.record_obligation(
        obligation_id=oid,
        session_key=session_key,
        platform=kw.get("platform", "slack"),
        chat_id=kw.get("chat_id", "C1"),
        thread_id=kw.get("thread_id", "171.001"),
        content=kw.get("content", "the final answer"),
    )


def _row(oid):
    with dl._connect() as conn:
        r = conn.execute(
            """SELECT state, attempts, owner_pid, content,
                      provider_message_id, delivery_route, chunk_count,
                      effective_thread_id, thread_fallback
               FROM delivery_obligations WHERE obligation_id=?""",
            (oid,),
        ).fetchone()
    return None if r is None else {
        "state": r[0], "attempts": r[1], "owner_pid": r[2], "content": r[3],
        "provider_message_id": r[4], "delivery_route": r[5],
        "chunk_count": r[6], "effective_thread_id": r[7],
        "thread_fallback": r[8],
    }


def _orphan(oid):
    """Make the row look like it belongs to a dead process."""
    with dl._connect() as conn:
        conn.execute(
            "UPDATE delivery_obligations SET owner_pid=999999999, "
            "owner_started_at=1 WHERE obligation_id=?",
            (oid,),
        )


class TestStateMachine:
    def test_record_starts_pending(self):
        _record()
        assert _row("ob-1")["state"] == "pending"

    def test_full_happy_path(self):
        _record()
        dl.mark_attempting("ob-1")
        assert _row("ob-1")["state"] == "attempting"
        dl.mark_delivered(
            "ob-1",
            provider_message_id=481,
            delivery_route="telegram.markdown_v2",
            chunk_count=2,
            effective_thread_id="17585",
            thread_fallback=False,
        )
        row = _row("ob-1")
        assert row["state"] == "delivered"
        assert row["provider_message_id"] == "481"
        assert row["delivery_route"] == "telegram.markdown_v2"
        assert row["chunk_count"] == 2
        assert row["effective_thread_id"] == "17585"
        assert row["thread_fallback"] == 0

    def test_idempotent_mark_without_receipt_does_not_erase_receipt(self):
        _record()
        dl.mark_delivered(
            "ob-1",
            provider_message_id="481",
            delivery_route="telegram.rich",
            chunk_count=1,
            thread_fallback=True,
        )
        dl.mark_delivered("ob-1")

        row = _row("ob-1")
        assert row["provider_message_id"] == "481"
        assert row["delivery_route"] == "telegram.rich"
        assert row["chunk_count"] == 1
        assert row["thread_fallback"] == 1

    def test_receipt_rejects_unstructured_metadata(self):
        _record()
        dl.mark_delivered(
            "ob-1",
            provider_message_id=object(),
            delivery_route="telegram.rich token=must-not-persist",
            chunk_count="one",
            effective_thread_id=object(),
            thread_fallback="yes",
        )

        row = _row("ob-1")
        assert row["state"] == "delivered"
        assert row["provider_message_id"] is None
        assert row["delivery_route"] is None
        assert row["chunk_count"] is None
        assert row["effective_thread_id"] is None
        assert row["thread_fallback"] is None

    def test_failed_records_error(self):
        _record()
        dl.mark_attempting("ob-1")
        dl.mark_failed("ob-1", "chat_not_found")
        assert _row("ob-1")["state"] == "failed"

    def test_rerecord_same_id_is_idempotent(self):
        _record()
        dl.mark_attempting("ob-1")
        _record()  # INSERT OR REPLACE resets to pending — same turn re-record
        assert _row("ob-1")["state"] == "pending"

class TestObligationId:
    def test_stable_and_distinct(self):
        a = dl.compute_obligation_id("sk1", "msg1", "hello")
        assert a == dl.compute_obligation_id("sk1", "msg1", "hello")
        # Different thread (baked into session_key) → different id. This is
        # the cron-topic collision class from the earlier outbox attempt.
        assert a != dl.compute_obligation_id("sk1:threadB", "msg1", "hello")
        assert a != dl.compute_obligation_id("sk1", "msg2", "hello")
        assert a != dl.compute_obligation_id("sk1", "msg1", "other")
        assert len(a) == 24


class TestSchemaMigration:
    def test_existing_ledger_adds_receipt_columns_without_losing_rows(self):
        path = dl._db_path()
        with sqlite3.connect(path) as conn:
            conn.execute(
                """CREATE TABLE delivery_obligations (
                    obligation_id TEXT PRIMARY KEY,
                    session_key TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    thread_id TEXT,
                    content TEXT NOT NULL,
                    state TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    owner_pid INTEGER,
                    owner_started_at INTEGER,
                    last_error TEXT
                )"""
            )
            conn.execute(
                """INSERT INTO delivery_obligations
                   VALUES ('old-1', 'session', 'telegram', 'chat', NULL,
                           'answer', 'pending', 0, 1, 1, NULL, NULL, NULL)"""
            )

        with dl._connect() as conn:
            columns = {
                row[1] for row in conn.execute(
                    "PRAGMA table_info(delivery_obligations)"
                )
            }
            state = conn.execute(
                "SELECT state FROM delivery_obligations WHERE obligation_id='old-1'"
            ).fetchone()[0]

        assert {
            "provider_message_id", "delivery_route", "chunk_count",
            "effective_thread_id", "thread_fallback",
        } <= columns
        assert state == "pending"


class TestDebugRows:
    def test_receipt_is_visible_without_exposing_content(self):
        _record(content="private final answer")
        dl.mark_delivered(
            "ob-1",
            provider_message_id="481",
            delivery_route="telegram.rich",
            chunk_count=1,
            thread_fallback=True,
        )

        [row] = json.loads(dl.debug_rows())

        assert row["provider_message_id"] == "481"
        assert row["delivery_route"] == "telegram.rich"
        assert row["chunk_count"] == 1
        assert row["effective_thread_id"] is None
        assert row["thread_fallback"] is True
        assert "content" not in row
        assert "private final answer" not in dl.debug_rows()


class TestSweep:
    def test_live_owner_rows_never_claimed(self):
        _record()  # owner = this (live) process
        assert dl.sweep_recoverable() == []

    def test_dead_owner_pending_claimed_without_marker(self):
        _record()
        _orphan("ob-1")
        claimed = dl.sweep_recoverable()
        assert len(claimed) == 1
        assert claimed[0]["needs_marker"] is False
        assert claimed[0]["attempts"] == 1
        # Claim re-stamps ownership: a second sweep in the same (live)
        # process must not double-claim.
        assert dl.sweep_recoverable() == []


class TestPrune:
    def test_old_delivered_rows_pruned(self):
        _record()
        dl.mark_delivered("ob-1")
        with dl._connect() as conn:
            conn.execute(
                "UPDATE delivery_obligations SET updated_at=? WHERE obligation_id=?",
                (time.time() - dl._RETENTION_SECONDS - 60, "ob-1"),
            )
        dl._prune()
        assert _row("ob-1") is None


class TestLedgerEnabled:
    def test_default_on(self):
        assert dl.ledger_enabled({}) is True
        assert dl.ledger_enabled({"gateway": {}}) is True


class TestGatewayRedeliverySweep:
    """Drive the real GatewayRunner._redeliver_pending_obligations."""

    @staticmethod
    def _runner(adapter=None):
        from gateway.config import Platform
        from gateway.run import GatewayRunner

        runner = object.__new__(GatewayRunner)
        runner.adapters = {Platform.SLACK: adapter} if adapter else {}
        _store = MagicMock()
        _store.clear_resume_pending = AsyncMock()
        _store._store = None
        runner.session_store = None
        runner._async_session_store = _store
        return runner

    @staticmethod
    def _adapter(success=True):
        adapter = MagicMock()
        adapter.send = AsyncMock(
            return_value=MagicMock(
                success=success,
                error="" if success else "nope",
                message_id="m-recovered" if success else None,
                delivery_route="slack.web_api" if success else None,
                chunk_count=1 if success else None,
                effective_thread_id="171.001" if success else None,
                thread_fallback=False if success else None,
            )
        )
        return adapter

    @pytest.mark.asyncio
    async def test_pending_redelivers_plain_and_clears_resume(self):
        _record()  # pending
        _orphan("ob-1")
        adapter = self._adapter()
        runner = self._runner(adapter)

        n = await runner._redeliver_pending_obligations()

        assert n == 1
        sent = adapter.send.call_args.kwargs
        assert sent["content"] == "the final answer"  # no marker
        assert sent["metadata"] == {"thread_id": "171.001"}
        row = _row("ob-1")
        assert row["state"] == "delivered"
        assert row["provider_message_id"] == "m-recovered"
        assert row["delivery_route"] == "slack.web_api"
        assert row["chunk_count"] == 1
        assert row["effective_thread_id"] == "171.001"
        assert row["thread_fallback"] == 0
        runner._async_session_store.clear_resume_pending.assert_awaited_once_with(
            "agent:main:slack:channel:C1"
        )

    @pytest.mark.asyncio
    async def test_attempting_redelivers_with_marker(self):
        _record()
        dl.mark_attempting("ob-1")
        _orphan("ob-1")
        adapter = self._adapter()
        runner = self._runner(adapter)

        await runner._redeliver_pending_obligations()

        sent = adapter.send.call_args.kwargs
        assert sent["content"].startswith(dl.RECOVERED_MARKER)
        assert sent["content"].endswith("the final answer")


class TestAttemptsOnlySpentOnRealSends:
    """``attempts`` is the redelivery budget — it must buy a send.

    ``self.adapters`` only holds a platform after its ``connect()`` succeeded,
    and the sweep claimed every dead-owner row regardless. A platform that
    failed to connect this boot therefore burned one attempt per boot while
    the caller's ``adapter is None`` branch skipped it without sending — so
    after MAX_ATTEMPTS boots the row abandoned having never been sent once,
    losing exactly the response the ledger exists to guarantee. That failure
    correlates with the crash that created the obligation: the network
    trouble that killed the send tends to still be there on the next boot.
    """

    def test_absent_platform_does_not_burn_attempts(self):
        _record(platform="telegram")
        dl.mark_attempting("ob-1")

        for _ in range(dl.MAX_ATTEMPTS + 2):
            _orphan("ob-1")
            assert dl.sweep_recoverable(deliverable_platforms={"discord"}) == []

        row = dl.debug_rows()
        assert "abandoned" not in row
        with dl._connect() as conn:
            state, attempts = conn.execute(
                "SELECT state, attempts FROM delivery_obligations "
                "WHERE obligation_id=?", ("ob-1",),
            ).fetchone()
        assert attempts == 0, "an unsendable boot must not spend the budget"
        assert state == "attempting"

    def test_row_still_delivers_once_its_platform_returns(self):
        _record(platform="telegram")
        for _ in range(dl.MAX_ATTEMPTS + 2):
            _orphan("ob-1")
            dl.sweep_recoverable(deliverable_platforms={"discord"})

        _orphan("ob-1")
        claimed = dl.sweep_recoverable(deliverable_platforms={"telegram"})
        assert len(claimed) == 1
        assert claimed[0]["attempts"] == 1


class TestUnconnectedPlatformKeepsItsBudget:
    """End-to-end through the real runner: boots where the platform failed to
    connect must not consume the row's redelivery budget."""

    @staticmethod
    def _runner_without_slack():
        from gateway.run import GatewayRunner

        runner = object.__new__(GatewayRunner)
        runner.adapters = {}  # slack failed to connect this boot
        _store = MagicMock()
        _store.clear_resume_pending = AsyncMock()
        _store._store = None
        runner.session_store = None
        runner._async_session_store = _store
        return runner

    @pytest.mark.asyncio
    async def test_row_survives_boots_where_its_platform_is_down(self):
        _record(platform="slack")
        dl.mark_attempting("ob-1")

        for _ in range(dl.MAX_ATTEMPTS + 1):
            _orphan("ob-1")
            runner = self._runner_without_slack()
            assert await runner._redeliver_pending_obligations() == 0

        assert _row("ob-1")["state"] != "abandoned", (
            "the obligation was abandoned without a single send being attempted"
        )
        assert _row("ob-1")["attempts"] == 0

