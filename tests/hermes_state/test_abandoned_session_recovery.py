import sqlite3
import threading
import time

import pytest

from hermes_cli.active_sessions import recover_abandoned_session_rows
from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    session_db = SessionDB(db_path=tmp_path / "state.db")
    try:
        yield session_db
    finally:
        session_db.close()


def _old_cli_session(db, session_id="orphan", *, age_seconds=172800):
    db.create_session(session_id=session_id, source="cli")
    started_at = time.time() - age_seconds
    db._conn.execute(
        "UPDATE sessions SET started_at = ? WHERE id = ?",
        (started_at, session_id),
    )
    return started_at


def test_recover_abandoned_session_is_metadata_only_and_idempotent(db):
    started_at = _old_cli_session(db)
    db.set_session_title("orphan", "Preserved session")
    db.append_message("orphan", role="user", content="keep me", timestamp=started_at)

    first = db.recover_abandoned_sessions(
        older_than_seconds=86400,
        active_session_ids=set(),
        eligible_started_after=started_at - 1,
        now=time.time(),
    )
    second = db.recover_abandoned_sessions(
        older_than_seconds=86400,
        active_session_ids=set(),
        eligible_started_after=started_at - 1,
        now=time.time(),
    )

    row = db.get_session("orphan")
    assert first["recovered_ids"] == ["orphan"]
    assert second["recovered_ids"] == []
    assert row["ended_at"] is not None
    assert row["end_reason"] == "orphan_recovered"
    assert row["title"] == "Preserved session"
    assert [m["content"] for m in db.get_messages("orphan")] == ["keep me"]


def test_recovered_session_remains_discoverable_and_resumable(db):
    started_at = _old_cli_session(db, session_id="resumable")
    db.append_message(
        "resumable", role="user", content="still searchable", timestamp=started_at
    )

    db.recover_abandoned_sessions(
        older_than_seconds=86400,
        active_session_ids=set(),
        eligible_started_after=started_at - 1,
        now=time.time(),
    )

    assert "resumable" in {
        row["id"] for row in db.search_sessions(source="cli", limit=100)
    }
    db.reopen_session("resumable")
    reopened = db.get_session("resumable")
    assert reopened is not None
    assert reopened["ended_at"] is None
    assert reopened["end_reason"] is None
    assert [m["content"] for m in db.get_messages("resumable")] == [
        "still searchable"
    ]


def test_live_lease_is_reported_and_never_recovered(db):
    started_at = _old_cli_session(db, session_id="live")

    result = db.recover_abandoned_sessions(
        older_than_seconds=86400,
        active_session_ids={"live"},
        eligible_started_after=started_at - 1,
        now=time.time(),
    )

    assert result["recovered_ids"] == []
    assert result["excluded"] == {"live": ["active_lease"]}
    assert db.get_session("live")["ended_at"] is None


def test_recent_activity_arriving_before_recovery_write_keeps_row_open(db):
    started_at = _old_cli_session(db, session_id="recent-writer")
    db.append_message(
        "recent-writer", role="user", content="old", timestamp=started_at
    )
    # Simulate another writer committing activity immediately before recovery's
    # BEGIN IMMEDIATE transaction. The locked classifier must see the new tip.
    db.append_message(
        "recent-writer", role="assistant", content="new", timestamp=time.time()
    )

    result = db.recover_abandoned_sessions(
        older_than_seconds=86400,
        active_session_ids=set(),
        eligible_started_after=started_at - 1,
        now=time.time(),
    )

    assert result["candidate_ids"] == []
    row = db.get_session("recent-writer")
    assert row is not None
    assert row["ended_at"] is None


def test_pre_lease_epoch_session_is_reported_as_unproven_legacy(db):
    started_at = _old_cli_session(db, session_id="legacy")

    result = db.recover_abandoned_sessions(
        older_than_seconds=86400,
        active_session_ids=set(),
        eligible_started_after=started_at + 1,
        now=time.time(),
    )

    assert result["recovered_ids"] == []
    assert result["excluded"] == {"legacy": ["before_recovery_epoch"]}
    assert db.get_session("legacy")["ended_at"] is None


def test_direct_apply_requires_epoch_and_active_owner_snapshot(db):
    started_at = _old_cli_session(db, session_id="direct-boundary")
    now = time.time()

    missing_snapshot = db.recover_abandoned_sessions(
        older_than_seconds=86400,
        eligible_started_after=started_at - 1,
        now=now,
        apply=True,
    )
    missing_epoch = db.recover_abandoned_sessions(
        older_than_seconds=86400,
        active_session_ids=set(),
        now=now,
        apply=True,
    )

    assert missing_snapshot["skipped"] == "missing_active_session_snapshot"
    assert missing_epoch["skipped"] == "missing_epoch"
    assert db.get_session("direct-boundary")["ended_at"] is None


def test_gateway_owned_row_is_never_recovered(db):
    started_at = _old_cli_session(db, session_id="gateway-owned")
    db._conn.execute(
        "UPDATE sessions SET session_key = ?, chat_id = ?, thread_id = ? WHERE id = ?",
        ("telegram:chat:thread", "chat", "thread", "gateway-owned"),
    )

    result = db.recover_abandoned_sessions(
        older_than_seconds=86400,
        active_session_ids=set(),
        eligible_started_after=started_at - 1,
        now=time.time(),
    )

    assert result["recovered_ids"] == []
    assert result["excluded"] == {"gateway-owned": ["gateway_owned"]}
    assert db.get_session("gateway-owned")["ended_at"] is None


def test_ambiguous_or_protected_rows_are_never_recovered(db):
    now = time.time()
    started = {}
    for session_id in (
        "pinned",
        "archived",
        "handoff",
        "handoff-completed",
        "compression",
        "lineage-parent",
        "lineage-child",
        "delegation",
        "delegation-finalizing",
        "delegation-undelivered",
    ):
        started[session_id] = _old_cli_session(db, session_id=session_id)

    db._conn.execute("UPDATE sessions SET pinned = 1 WHERE id = 'pinned'")
    db._conn.execute("UPDATE sessions SET archived = 1 WHERE id = 'archived'")
    db._conn.execute(
        "UPDATE sessions SET handoff_state = 'running' WHERE id = 'handoff'"
    )
    db._conn.execute(
        "UPDATE sessions SET handoff_state = 'completed' "
        "WHERE id = 'handoff-completed'"
    )
    db._conn.execute(
        "INSERT INTO compression_locks(session_id, holder, acquired_at, expires_at) "
        "VALUES ('compression', 'test', ?, ?)",
        (now - 10, now + 3600),
    )
    db._conn.execute(
        "UPDATE sessions SET parent_session_id = 'lineage-parent' "
        "WHERE id = 'lineage-child'"
    )
    db._conn.execute(
        "INSERT INTO async_delegations("
        "delegation_id, origin_session, parent_session_id, state, dispatched_at, updated_at"
        ") VALUES ('deleg-1', 'delegation', 'delegation', 'running', ?, ?)",
        (now - 10, now - 10),
    )
    db._conn.execute(
        "INSERT INTO async_delegations("
        "delegation_id, origin_session, parent_session_id, state, dispatched_at, updated_at"
        ") VALUES ('deleg-2', 'delegation-finalizing', "
        "'delegation-finalizing', 'finalizing', ?, ?)",
        (now - 10, now - 10),
    )
    db._conn.execute(
        "INSERT INTO async_delegations("
        "delegation_id, origin_session, parent_session_id, state, dispatched_at, "
        "updated_at, delivery_state, delivery_claim, delivery_claimed_at"
        ") VALUES ('deleg-3', 'delegation-undelivered', 'delegation-undelivered', "
        "'completed', ?, ?, 'pending', 'claim', ?)",
        (now - 10, now - 10, now - 5),
    )

    result = db.recover_abandoned_sessions(
        older_than_seconds=86400,
        active_session_ids=set(),
        eligible_started_after=min(started.values()) - 1,
        now=now,
    )

    assert result["recovered_ids"] == []
    assert result["excluded"] == {
        "archived": ["archived"],
        "compression": ["compression_active"],
        "delegation": ["delegation_active"],
        "delegation-finalizing": ["delegation_active"],
        "delegation-undelivered": ["delegation_active"],
        "handoff": ["handoff_active"],
        "handoff-completed": ["handoff_related"],
        "lineage-child": ["live_parent"],
        "lineage-parent": ["live_child"],
        "pinned": ["pinned"],
    }
    assert all(db.get_session(session_id)["ended_at"] is None for session_id in started)


def test_origin_session_id_only_active_delegation_is_never_recovered(db):
    now = time.time()
    started_at = _old_cli_session(db, session_id="delegation-origin-id-only")
    columns = {
        row["name"] for row in db._conn.execute("PRAGMA table_info(async_delegations)")
    }
    if "origin_session_id" not in columns:
        db._conn.execute(
            "ALTER TABLE async_delegations "
            "ADD COLUMN origin_session_id TEXT NOT NULL DEFAULT ''"
        )
    db._conn.execute(
        "INSERT INTO async_delegations("
        "delegation_id, origin_session, origin_session_id, state, "
        "dispatched_at, updated_at"
        ") VALUES ('deleg-origin-id', 'unrelated-session-key', "
        "'delegation-origin-id-only', 'running', ?, ?)",
        (now - 10, now - 10),
    )

    result = db.recover_abandoned_sessions(
        older_than_seconds=86400,
        active_session_ids=set(),
        eligible_started_after=started_at - 1,
        now=now,
    )

    assert result["recovered_ids"] == []
    assert result["excluded"] == {
        "delegation-origin-id-only": ["delegation_active"]
    }
    assert db.get_session("delegation-origin-id-only")["ended_at"] is None


def test_external_routing_references_are_never_recovered(db):
    now = time.time()
    routing_started = _old_cli_session(db, session_id="routing-ref")
    topic_started = _old_cli_session(db, session_id="topic-ref")
    db._conn.execute(
        "INSERT INTO gateway_routing(scope, session_key, entry_json, updated_at) "
        "VALUES ('', 'route', ?, ?)",
        ('{"session_id":"routing-ref"}', now),
    )
    db.apply_telegram_topic_migration()
    db._conn.execute(
        "INSERT INTO telegram_dm_topic_bindings("
        "chat_id, thread_id, user_id, session_key, session_id, linked_at, updated_at"
        ") VALUES ('chat', 'thread', 'user', 'topic-key', 'topic-ref', ?, ?)",
        (now, now),
    )

    result = db.recover_abandoned_sessions(
        older_than_seconds=86400,
        active_session_ids=set(),
        eligible_started_after=min(routing_started, topic_started) - 1,
        now=now,
    )

    assert result["recovered_ids"] == []
    assert result["excluded"] == {
        "routing-ref": ["gateway_routing_reference"],
        "topic-ref": ["telegram_topic_binding"],
    }


def test_dry_run_reports_candidate_without_mutating(db, monkeypatch):
    started_at = _old_cli_session(db, session_id="dry-run")
    writes_before = db._write_count

    def reject_write(*_args, **_kwargs):
        raise AssertionError("dry-run classification must not open a write transaction")

    monkeypatch.setattr(db, "_execute_write", reject_write)

    result = db.recover_abandoned_sessions(
        older_than_seconds=86400,
        active_session_ids=set(),
        eligible_started_after=started_at - 1,
        now=time.time(),
        apply=False,
    )

    assert result["candidate_ids"] == ["dry-run"]
    assert result["recovered_ids"] == []
    assert db._write_count == writes_before
    assert db.get_session("dry-run")["ended_at"] is None


def test_lifecycle_recovery_epoch_is_created_once(db):
    assert db.get_or_create_lifecycle_recovery_epoch(now=100.0) == 100.0
    assert db.get_or_create_lifecycle_recovery_epoch(now=200.0) == 100.0


@pytest.mark.parametrize(
    "invalid_epoch",
    ["not-a-number", "-inf", "nan", "0", "-1", "99999999999"],
)
def test_invalid_lifecycle_epoch_never_recovers_legacy_row(
    db, tmp_path, monkeypatch, invalid_epoch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    _old_cli_session(db, session_id="legacy-invalid-epoch", age_seconds=172800)
    db._conn.execute(
        "INSERT INTO state_meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        ("session_lifecycle_owner_registry_epoch", invalid_epoch),
    )

    result = recover_abandoned_session_rows(
        db,
        apply=True,
        older_than_seconds=86400,
        now=time.time(),
    )

    assert result["recovered_ids"] == []
    assert db.get_session("legacy-invalid-epoch")["ended_at"] is None
    row = db._conn.execute(
        "SELECT value FROM state_meta "
        "WHERE key = 'session_lifecycle_owner_registry_epoch'"
    ).fetchone()
    assert row["value"] == invalid_epoch


def test_lifecycle_recovery_attempt_interval_is_claimed_atomically(db):
    assert db.claim_lifecycle_recovery_attempt(now=100.0, interval_seconds=10.0)
    assert not db.claim_lifecycle_recovery_attempt(now=105.0, interval_seconds=10.0)
    assert db.claim_lifecycle_recovery_attempt(now=111.0, interval_seconds=10.0)


@pytest.mark.parametrize(
    "invalid_attempt",
    ["not-a-number", "-inf", "nan", "0", "-1", "99999999999"],
)
def test_invalid_recovery_attempt_marker_suppresses_unattended_work(db, invalid_attempt):
    key = "session_lifecycle_recovery_last_attempt_at"
    db._conn.execute(
        "INSERT INTO state_meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, invalid_attempt),
    )

    assert not db.claim_lifecycle_recovery_attempt(now=100.0, interval_seconds=10.0)
    row = db._conn.execute("SELECT value FROM state_meta WHERE key = ?", (key,)).fetchone()
    assert row["value"] == invalid_attempt


def test_startup_scan_is_not_starved_by_legacy_rows(db):
    now = time.time()
    legacy_started = _old_cli_session(db, session_id="legacy-old", age_seconds=259200)
    eligible_started = _old_cli_session(db, session_id="eligible", age_seconds=172800)

    result = db.recover_abandoned_sessions(
        older_than_seconds=86400,
        active_session_ids=set(),
        eligible_started_after=(legacy_started + eligible_started) / 2,
        limit=1,
        now=now,
        report_legacy_exclusions=False,
    )

    assert result["recovered_ids"] == ["eligible"]
    assert db.get_session("legacy-old")["ended_at"] is None


def test_manual_audit_candidates_are_not_starved_by_legacy_exclusions(db):
    now = time.time()
    legacy_started = []
    for index in range(3):
        legacy_started.append(
            _old_cli_session(
                db, session_id=f"legacy-{index}", age_seconds=259200 + index
            )
        )
    eligible_started = _old_cli_session(
        db, session_id="manual-eligible", age_seconds=172800
    )

    result = db.recover_abandoned_sessions(
        older_than_seconds=86400,
        active_session_ids=set(),
        eligible_started_after=(max(legacy_started) + eligible_started) / 2,
        limit=1,
        now=now,
        apply=False,
        report_legacy_exclusions=True,
    )

    assert result["candidate_ids"] == ["manual-eligible"]
    assert result["recovered_ids"] == []
    assert len(result["excluded"]) == 1
    assert next(iter(result["excluded"].values())) == ["before_recovery_epoch"]


def test_protected_rows_do_not_consume_candidate_batch_limit(db):
    now = time.time()
    protected_ids = (
        "pinned-old",
        "handoff-old",
        "compression-old",
        "delegation-old",
        "routing-old",
        "topic-old",
        "lineage-parent-old",
        "lineage-child-old",
        "live-lease-old",
    )
    for offset, session_id in enumerate(protected_ids):
        _old_cli_session(db, session_id=session_id, age_seconds=250000 - offset * 1000)
    _old_cli_session(db, session_id="recoverable-later", age_seconds=230000)
    db._conn.execute("UPDATE sessions SET pinned = 1 WHERE id = 'pinned-old'")
    db._conn.execute(
        "UPDATE sessions SET handoff_state = 'completed' WHERE id = 'handoff-old'"
    )
    db._conn.execute(
        "INSERT INTO compression_locks(session_id, holder, acquired_at, expires_at) "
        "VALUES ('compression-old', 'test', ?, ?)",
        (now - 10, now + 3600),
    )
    db._conn.execute(
        "INSERT INTO async_delegations("
        "delegation_id, origin_session, parent_session_id, state, dispatched_at, updated_at"
        ") VALUES ('starvation-delegation', 'delegation-old', 'delegation-old', "
        "'running', ?, ?)",
        (now - 10, now - 10),
    )
    db._conn.execute(
        "INSERT INTO gateway_routing(scope, session_key, entry_json, updated_at) "
        "VALUES ('', 'starvation-route', ?, ?)",
        ('{"session_id":"routing-old"}', now),
    )
    db.apply_telegram_topic_migration()
    db._conn.execute(
        "INSERT INTO telegram_dm_topic_bindings("
        "chat_id, thread_id, user_id, session_key, session_id, linked_at, updated_at"
        ") VALUES ('chat', 'thread', 'user', 'topic-key', 'topic-old', ?, ?)",
        (now, now),
    )
    db._conn.execute(
        "UPDATE sessions SET parent_session_id = 'lineage-parent-old' "
        "WHERE id = 'lineage-child-old'"
    )

    result = db.recover_abandoned_sessions(
        older_than_seconds=86400,
        active_session_ids={"live-lease-old"},
        eligible_started_after=now - 300000,
        limit=2,
        now=now,
        apply=True,
    )

    assert result["recovered_ids"] == ["recoverable-later"]
    assert all(db.get_session(session_id)["ended_at"] is None for session_id in protected_ids)


def test_recovery_rechecks_recent_activity_after_waiting_for_writer(db):
    started_at = _old_cli_session(db, session_id="writer-race")
    writer = sqlite3.connect(db.db_path, isolation_level=None, timeout=5.0)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute(
        "INSERT INTO messages(session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
        ("writer-race", "user", "recent", time.time()),
    )
    started = threading.Event()
    finished = threading.Event()
    outcome = {}

    def recover():
        started.set()
        try:
            outcome["result"] = db.recover_abandoned_sessions(
                older_than_seconds=86400,
                active_session_ids=set(),
                eligible_started_after=started_at - 1,
                now=time.time(),
            )
        except Exception as exc:
            outcome["error"] = exc
        finally:
            finished.set()

    worker = threading.Thread(target=recover)
    worker.start()
    assert started.wait(timeout=2)
    assert not finished.wait(timeout=0.1)
    writer.execute("COMMIT")
    writer.close()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert "error" not in outcome
    assert outcome["result"]["recovered_ids"] == []
    assert db.get_session("writer-race")["ended_at"] is None
