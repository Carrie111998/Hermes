"""Tests for session handoff (CLI to gateway platform).

The handoff state machine lives on the ``sessions`` table:

    None  → "pending" → "running" → ("completed" | "failed")

CLI side calls ``request_handoff`` and poll-waits on ``get_handoff_state``.
Gateway side iterates ``list_pending_handoffs``, calls ``claim_handoff`` to
flip pending → running, and finishes with ``complete_handoff`` or
``fail_handoff``.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import time

import pytest

from hermes_state import SessionDB


class TestHandoffStateDB:
    """Test the handoff schema + helper methods on SessionDB."""

    @pytest.fixture
    def db(self, tmp_path, monkeypatch):
        home = tmp_path / ".hermes"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        return SessionDB(db_path=home / "state.db")

    def _make_session(
        self, db, session_id, source="cli", title=None, session_key=None
    ):
        """Insert a session row directly for testing."""
        def _do(conn):
            conn.execute(
                "INSERT OR IGNORE INTO sessions "
                "(id, source, title, started_at, session_key) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, source, title, time.time(), session_key),
            )
        db._execute_write(_do)

    def _move_claimed_route(self, db, session_id, owner):
        source_key = owner["source_session_key"]
        destination_key = f"{source_key}:discord"
        destination_json = json.dumps(
            {"session_id": session_id, "session_key": destination_key}
        )
        assert db.move_gateway_routing_entry_if_owned(
            source_key,
            destination_key,
            session_id,
            destination_json,
            scope=owner["routing_scope"],
            handoff_claim_token=owner["token"],
        )
        return destination_key

    def _save_source_route(self, db, session_id, owner):
        source_key = owner["source_session_key"]
        assert db.save_gateway_routing_entry(
            source_key,
            json.dumps({"session_id": session_id, "session_key": source_key}),
            scope=owner["routing_scope"],
        )





    def test_list_pending_handoffs_excludes_running_and_terminal(self, db):
        a, b, c, d = "sess-a", "sess-b", "sess-c", "sess-d"
        for sid in (a, b, c, d):
            self._make_session(db, sid)

        db.request_handoff(a, "telegram")
        db.request_handoff(b, "discord")
        db.request_handoff(c, "telegram")
        db.claim_handoff(c)  # c is now running, not pending
        db.request_handoff(d, "slack")
        db.claim_handoff(d)
        db.complete_handoff(d)  # d is terminal

        pending = db.list_pending_handoffs()
        ids = [r["id"] for r in pending]
        assert set(ids) == {a, b}
        assert db.list_claimed_webhook_handoffs() == []

    def test_owned_webhook_claim_is_listed_and_token_fenced(self, db):
        sid = "sess-owned-webhook"
        session_key = "agent:main:webhook:webhook:alerts:delivery"
        self._make_session(
            db,
            sid,
            source="webhook",
            session_key=session_key,
        )
        owner = {
            "token": "owner-token",
            "pid": 12345,
            "process_start_time": 67890,
            "host": "test-host",
            "instantiation_epoch": "test-epoch",
            "routing_scope": "/tmp/test-sessions",
            "source_session_key": session_key,
            "active_session_key": session_key,
        }
        self._save_source_route(db, sid, owner)
        assert db.request_handoff_once(sid, "discord") is True
        assert db.claim_webhook_handoff(sid, json.dumps(owner)) is True

        claimed = db.list_claimed_webhook_handoffs()
        assert [row["id"] for row in claimed] == [sid]
        durable_owner = dict(owner)
        durable_owner["lock_protocol"] = "state-db-sidecar-v1"
        assert json.loads(claimed[0]["_handoff_claim_owner"]) == durable_owner
        self._move_claimed_route(db, sid, owner)
        assert db.complete_claimed_webhook_handoff(sid, "stale-token") is False
        assert db.get_handoff_state(sid)["state"] == "running"
        assert db.complete_claimed_webhook_handoff(sid, owner["token"]) is True
        assert db.get_handoff_state(sid) == {
            "state": "completed",
            "platform": "discord",
            "error": None,
        }
        assert db.get_meta(f"webhook_handoff_owner:{sid}") is None
        assert db.list_claimed_webhook_handoffs() == []

    def test_webhook_claim_owner_insert_failure_rolls_back_running_state(self, db):
        sid = "sess-owner-insert-rollback"
        session_key = "agent:main:webhook:webhook:alerts:insert-rollback"
        self._make_session(
            db,
            sid,
            source="webhook",
            session_key=session_key,
        )
        owner_key = f"webhook_handoff_owner:{sid}"
        db.set_meta(owner_key, "preexisting-owner")
        owner = {
            "token": "new-owner",
            "pid": 12345,
            "process_start_time": 67890,
            "host": "test-host",
            "instantiation_epoch": "test-epoch",
            "routing_scope": "/tmp/test-sessions",
            "source_session_key": session_key,
            "active_session_key": session_key,
        }
        self._save_source_route(db, sid, owner)
        assert db.request_handoff_once(sid, "discord") is True

        with pytest.raises(sqlite3.IntegrityError):
            db.claim_webhook_handoff(sid, json.dumps(owner))

        assert db.get_handoff_state(sid)["state"] == "pending"
        assert db.get_meta(owner_key) == "preexisting-owner"

    def test_webhook_completion_owner_delete_failure_rolls_back_state(self, db):
        sid = "sess-owner-delete-rollback"
        session_key = "agent:main:webhook:webhook:alerts:delete-rollback"
        self._make_session(
            db,
            sid,
            source="webhook",
            session_key=session_key,
        )
        owner_key = f"webhook_handoff_owner:{sid}"
        owner = {
            "token": "completion-owner",
            "pid": 12345,
            "process_start_time": 67890,
            "host": "test-host",
            "instantiation_epoch": "test-epoch",
            "routing_scope": "/tmp/test-sessions",
            "source_session_key": session_key,
            "active_session_key": session_key,
        }
        self._save_source_route(db, sid, owner)
        assert db.request_handoff_once(sid, "discord") is True
        assert db.claim_webhook_handoff(sid, json.dumps(owner)) is True
        destination_key = self._move_claimed_route(db, sid, owner)

        def _install_failure_trigger(conn):
            conn.execute(
                "CREATE TRIGGER fail_handoff_owner_delete "
                "BEFORE DELETE ON state_meta "
                "WHEN OLD.key LIKE 'webhook_handoff_owner:%' "
                "BEGIN SELECT RAISE(ABORT, 'owner delete failed'); END"
            )

        db._execute_write(_install_failure_trigger)
        with pytest.raises(sqlite3.IntegrityError, match="owner delete failed"):
            db.complete_claimed_webhook_handoff(sid, owner["token"])

        assert db.get_handoff_state(sid)["state"] == "running"
        durable_owner = dict(owner)
        durable_owner["active_session_key"] = destination_key
        durable_owner["lock_protocol"] = "state-db-sidecar-v1"
        assert json.loads(db.get_meta(owner_key)) == durable_owner

        db._execute_write(
            lambda conn: conn.execute("DROP TRIGGER fail_handoff_owner_delete")
        )
        assert db.complete_claimed_webhook_handoff(sid, owner["token"]) is True
        assert db.get_handoff_state(sid)["state"] == "completed"
        assert db.get_meta(owner_key) is None


    def test_complete_handoff_clears_error(self, db):
        sid = "sess-complete"
        self._make_session(db, sid)
        db.request_handoff(sid, "telegram")
        db.claim_handoff(sid)
        db.fail_handoff(sid, "transient")
        # User retries; mock the watcher path
        db.request_handoff(sid, "telegram")
        db.claim_handoff(sid)
        db.complete_handoff(sid)

        state = db.get_handoff_state(sid)
        assert state["state"] == "completed"
        assert state["error"] is None

    def test_request_handoff_once_never_reopens_terminal_row(self, db):
        sid = "sess-request-once"
        self._make_session(db, sid, source="webhook")

        assert db.request_handoff_once(sid, "discord") is True
        assert db.claim_handoff(sid) is True
        db.complete_handoff(sid)

        # A repeated delivery cannot reopen the completed handoff or replace
        # its destination metadata.
        assert db.request_handoff_once(sid, "telegram") is False
        assert db.get_handoff_state(sid) == {
            "state": "completed",
            "platform": "discord",
            "error": None,
        }

        # Interactive handoffs intentionally retain their existing explicit
        # retry behavior after a terminal result.
        assert db.request_handoff(sid, "telegram") is True
        assert db.get_handoff_state(sid)["state"] == "pending"
        assert db.get_handoff_state(sid)["platform"] == "telegram"

    def test_concurrent_request_handoff_once_has_one_winner(self, db):
        sid = "sess-request-once-race"
        self._make_session(db, sid, source="webhook")
        contenders = [SessionDB(db_path=db.db_path) for _ in range(8)]

        try:
            with ThreadPoolExecutor(max_workers=len(contenders)) as pool:
                futures = [
                    pool.submit(
                        contender.request_handoff_once,
                        sid,
                        f"platform-{index}",
                    )
                    for index, contender in enumerate(contenders)
                ]
                results = [future.result() for future in futures]
        finally:
            for contender in contenders:
                contender.close()

        assert sum(results) == 1
        assert db.get_handoff_state(sid)["state"] == "pending"

    def test_concurrent_webhook_claim_has_one_durable_owner(self, db):
        sid = "sess-webhook-claim-race"
        session_key = "agent:main:webhook:webhook:alerts:claim-race"
        self._make_session(
            db,
            sid,
            source="webhook",
            session_key=session_key,
        )
        assert db.request_handoff_once(sid, "discord") is True
        contenders = [SessionDB(db_path=db.db_path) for _ in range(8)]
        owners = [
            {
                "token": f"owner-{index}",
                "pid": 10_000 + index,
                "process_start_time": 20_000 + index,
                "host": "test-host",
                "instantiation_epoch": "test-epoch",
                "routing_scope": "/tmp/test-sessions",
                "source_session_key": session_key,
                "active_session_key": session_key,
            }
            for index in range(len(contenders))
        ]
        self._save_source_route(db, sid, owners[0])

        try:
            with ThreadPoolExecutor(max_workers=len(contenders)) as pool:
                futures = [
                    pool.submit(
                        contender.claim_webhook_handoff,
                        sid,
                        json.dumps(owner),
                    )
                    for contender, owner in zip(contenders, owners)
                ]
                results = [future.result() for future in futures]
        finally:
            for contender in contenders:
                contender.close()

        assert sum(results) == 1
        winning_owner = owners[results.index(True)]
        claimed = db.list_claimed_webhook_handoffs()
        assert [row["id"] for row in claimed] == [sid]
        durable_owner = dict(winning_owner)
        durable_owner["lock_protocol"] = "state-db-sidecar-v1"
        assert json.loads(claimed[0]["_handoff_claim_owner"]) == durable_owner
        assert db.get_handoff_state(sid)["state"] == "running"




    def test_full_pending_to_completed_flow(self, db):
        """End-to-end sequence the CLI + gateway watcher follow."""
        sid = "sess-flow"
        self._make_session(db, sid, title="my session")
        db.append_message(sid, "user", "Hello")
        db.append_message(sid, "assistant", "Hi there!")

        # CLI: request handoff
        assert db.request_handoff(sid, "telegram") is True
        assert db.get_handoff_state(sid)["state"] == "pending"

        # Gateway watcher: discover + claim
        pending = db.list_pending_handoffs()
        assert len(pending) == 1
        assert pending[0]["id"] == sid
        assert db.claim_handoff(sid) is True
        assert db.get_handoff_state(sid)["state"] == "running"

        # Gateway uses get_messages to load the transcript (real flow uses
        # session_store.switch_session which reads the same table).
        messages = db.get_messages(sid)
        assert [m["role"] for m in messages] == ["user", "assistant"]

        # Gateway: mark completed
        db.complete_handoff(sid)
        assert db.get_handoff_state(sid)["state"] == "completed"
        assert db.list_pending_handoffs() == []


class TestHandoffCommandRegistration:
    """Slash-command surface checks."""

    def test_command_registered(self):
        from hermes_cli.commands import resolve_command
        cmd = resolve_command("handoff")
        assert cmd is not None
        assert cmd.name == "handoff"
        assert cmd.category == "Session"

    def test_command_is_cli_only(self):
        """`/handoff` is initiated from the CLI; gateway shouldn't expose it."""
        from hermes_cli.commands import resolve_command, GATEWAY_KNOWN_COMMANDS
        cmd = resolve_command("handoff")
        assert cmd is not None
        assert cmd.cli_only is True
        assert "handoff" not in GATEWAY_KNOWN_COMMANDS
