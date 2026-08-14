from __future__ import annotations

import sqlite3

import pytest


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def _push():
    return {
        "version": "route-capability-v1",
        "adapter_type": "telegram",
        "adapter_version": "1",
        "route_kind": "push",
        "supports_async_delivery": True,
        "creator_wake_applicable": False,
        "creator_session_id": None,
        "artifact_transport": "telegram",
        "artifact_policy_version": "artifact-policy-v1",
    }


def _non_push():
    row = _push()
    row.update(
        adapter_type="api_server",
        route_kind="non_push",
        supports_async_delivery=False,
        creator_wake_applicable=True,
        creator_session_id="session-1",
        artifact_transport="creator_session",
    )
    return row


def test_push_and_non_push_freeze_different_required_shapes():
    from hermes_cli.kanban_delivery_outbox import init_schema, materialize_parent

    conn = _conn()
    init_schema(conn)
    push = materialize_parent(conn, source=_source("e1"), capability=_push(), text="done")
    wake = materialize_parent(conn, source=_source("e2"), capability=_non_push(), text="ignored")
    push_kinds = [r["kind"] for r in conn.execute("select * from kanban_delivery_children where parent_id=?", (push,))]
    wake_kinds = [r["kind"] for r in conn.execute("select * from kanban_delivery_children where parent_id=?", (wake,))]
    assert push_kinds == ["primary_text"]
    assert wake_kinds == ["creator_wake"]


def test_materialization_is_deterministic_and_capability_drift_cannot_reshape():
    from hermes_cli.kanban_delivery_outbox import init_schema, materialize_parent

    conn = _conn()
    init_schema(conn)
    first = materialize_parent(conn, source=_source("e1"), capability=_push(), text="done")
    second = materialize_parent(conn, source=_source("e1"), capability=_non_push(), text="changed")
    assert first == second
    rows = conn.execute("select kind from kanban_delivery_children where parent_id=?", (first,)).fetchall()
    assert [r["kind"] for r in rows] == ["primary_text"]


def test_invalid_non_push_without_wake_is_atomic():
    from hermes_cli.kanban_delivery_outbox import CapabilityError, init_schema, materialize_parent

    conn = _conn()
    init_schema(conn)
    invalid = _non_push()
    invalid["creator_session_id"] = None
    with pytest.raises(CapabilityError):
        materialize_parent(conn, source=_source("e1"), capability=invalid, text="x")
    assert conn.execute("select count(*) from kanban_delivery_parents").fetchone()[0] == 0
    assert conn.execute("select count(*) from kanban_delivery_children").fetchone()[0] == 0


def test_seven_state_transitions_attempts_receipt_and_parent_gate():
    from hermes_cli.kanban_delivery_outbox import (
        init_schema,
        lease_child,
        mark_failed,
        mark_sending,
        mark_sent,
        materialize_parent,
        parent_complete,
    )

    conn = _conn()
    init_schema(conn)
    parent = materialize_parent(
        conn,
        source=_source("e1"),
        capability=_push(),
        text="done",
        artifacts=[{"manifest_id": "m1", "sha256": "a" * 64, "ordinal": 0}],
    )
    children = conn.execute("select child_id from kanban_delivery_children where parent_id=? order by ordinal", (parent,)).fetchall()
    text, artifact = [r[0] for r in children]
    token = lease_child(conn, text, "worker", now=10, lease_seconds=5)
    mark_sending(conn, text, token, now=11)
    mark_sent(conn, text, token, receipt="safe:1", now=12)
    assert not parent_complete(conn, parent)
    token2 = lease_child(conn, artifact, "worker", now=10, lease_seconds=5)
    mark_sending(conn, artifact, token2, now=11)
    mark_failed(conn, artifact, token2, error_class="timeout", now=12, retry_at=13)
    token3 = lease_child(conn, artifact, "worker2", now=13, lease_seconds=5)
    mark_sending(conn, artifact, token3, now=14)
    mark_sent(conn, artifact, token3, receipt="safe:2", now=15)
    assert parent_complete(conn, parent)
    assert conn.execute("select count(*) from kanban_delivery_attempts").fetchone()[0] == 3


def _source(event: str):
    return {
        "board_uuid": "board-1",
        "event_id": event,
        "subscription_id": "sub-1",
        "notifier_profile": "athena",
        "platform": "telegram",
        "destination": "chat:1",
        "payload_schema_version": "v1",
    }
