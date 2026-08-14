from __future__ import annotations

import sqlite3
import asyncio
import hashlib

import pytest


@pytest.fixture
def authority_lease(tmp_path, monkeypatch):
    from hermes_cli.dispatcher_authority import acquire_machine_dispatcher

    monkeypatch.setenv("HERMES_STATE_ROOT", str(tmp_path / "state"))
    acquired = acquire_machine_dispatcher("outbox-test")
    assert acquired.lease is not None
    yield acquired.lease
    acquired.lease.release()


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


def test_push_and_non_push_freeze_different_required_shapes(authority_lease):
    from hermes_cli.kanban_delivery_outbox import init_schema, materialize_parent

    conn = _conn()
    init_schema(conn)
    push = materialize_parent(conn, source=_source("e1"), capability=_push(), text="done")
    wake = materialize_parent(conn, source=_source("e2"), capability=_non_push(), text="ignored")
    push_kinds = [r["kind"] for r in conn.execute("select * from kanban_delivery_children where parent_id=?", (push,))]
    wake_kinds = [r["kind"] for r in conn.execute("select * from kanban_delivery_children where parent_id=?", (wake,))]
    assert push_kinds == ["primary_text"]
    assert wake_kinds == ["creator_wake"]


def test_push_with_required_creator_wake_freezes_text_and_wake_children(authority_lease):
    from hermes_cli.kanban_delivery_outbox import init_schema, materialize_parent

    conn = _conn()
    init_schema(conn)
    capability = _push()
    capability.update(
        creator_wake_applicable=True,
        creator_session_id="session-1",
        wake_required=True,
    )
    parent = materialize_parent(
        conn, source=_source("e1"), capability=capability, text="done"
    )
    rows = conn.execute(
        "select kind,required from kanban_delivery_children where parent_id=? order by ordinal",
        (parent,),
    ).fetchall()
    assert [(row["kind"], row["required"]) for row in rows] == [
        ("primary_text", 1),
        ("creator_wake", 1),
    ]


def test_materialization_is_deterministic_and_rejects_any_retry_snapshot_drift(authority_lease):
    from hermes_cli.kanban_delivery_outbox import CapabilityError, init_schema, materialize_parent

    conn = _conn()
    init_schema(conn)
    first = materialize_parent(conn, source=_source("e1"), capability=_push(), text="done")
    assert materialize_parent(
        conn, source=_source("e1"), capability=_push(), text="done"
    ) == first
    with pytest.raises(CapabilityError, match="snapshot"):
        materialize_parent(
            conn, source=_source("e1"), capability=_non_push(), text="changed"
        )
    drifted_source = _source("e1")
    drifted_source["destination"] = "changed-route"
    with pytest.raises(CapabilityError, match="snapshot"):
        materialize_parent(
            conn, source=drifted_source, capability=_push(), text="done"
        )
    rows = conn.execute("select kind from kanban_delivery_children where parent_id=?", (first,)).fetchall()
    assert [r["kind"] for r in rows] == ["primary_text"]


def test_route_capability_registry_rejects_unknown_adapter_version_and_transport(authority_lease):
    from hermes_cli.kanban_delivery_outbox import CapabilityError, init_schema, materialize_parent

    conn = _conn()
    init_schema(conn)
    invalid = _push()
    invalid.update(
        adapter_type="not-registered",
        adapter_version="unregistered-version",
        artifact_transport="not-registered",
    )
    with pytest.raises(CapabilityError, match="unregistered"):
        materialize_parent(conn, source=_source("e1"), capability=invalid, text="done")
    assert conn.execute("select count(*) from kanban_delivery_parents").fetchone()[0] == 0


def test_invalid_non_push_without_wake_is_atomic(authority_lease):
    from hermes_cli.kanban_delivery_outbox import CapabilityError, init_schema, materialize_parent

    conn = _conn()
    init_schema(conn)
    invalid = _non_push()
    invalid["creator_session_id"] = None
    with pytest.raises(CapabilityError):
        materialize_parent(conn, source=_source("e1"), capability=invalid, text="x")
    assert conn.execute("select count(*) from kanban_delivery_parents").fetchone()[0] == 0
    assert conn.execute("select count(*) from kanban_delivery_children").fetchone()[0] == 0


def test_seven_state_transitions_attempts_receipt_and_parent_gate(tmp_path, authority_lease):
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
    artifact_path = tmp_path / "artifact.bin"
    artifact_path.write_bytes(b"artifact")
    parent = materialize_parent(
        conn,
        source=_source("e1"),
        capability=_push(),
        text="done",
        artifacts=[{
            "manifest_id": "m1",
            "sha256": hashlib.sha256(b"artifact").hexdigest(),
            "ordinal": 0,
            "path": str(artifact_path),
        }],
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


def test_due_children_preserve_payload_and_recover_expired_uncertain_send(authority_lease):
    from hermes_cli.kanban_delivery_outbox import (
        due_children,
        init_schema,
        lease_child,
        mark_sending,
        materialize_parent,
        recover_expired,
    )

    conn = _conn()
    init_schema(conn)
    parent = materialize_parent(
        conn,
        source=_source("e1"),
        capability=_push(),
        text="durable body",
    )
    child = due_children(conn, parent_id=parent, now=10)[0]
    assert child["kind"] == "primary_text"
    assert child["component"]["text"] == "durable body"

    token = lease_child(conn, child["child_id"], "notifier-a", now=10, lease_seconds=5)
    mark_sending(conn, child["child_id"], token, now=11)
    assert due_children(conn, parent_id=parent, now=12) == []
    assert recover_expired(conn, now=16) == [child["child_id"]]
    retried = due_children(conn, parent_id=parent, now=16)[0]
    assert retried["state"] == "failed"
    assert retried["last_error_class"] == "uncertain_after_expired_sending"


def test_audit_requires_explicit_completion_permission_and_is_append_only(authority_lease):
    from hermes_cli.kanban_delivery_outbox import (
        audit_dead_child,
        init_schema,
        lease_child,
        mark_dead,
        mark_failed,
        mark_sending,
        materialize_parent,
        parent_complete,
    )

    conn = _conn()
    init_schema(conn)
    parent = materialize_parent(conn, source=_source("e1"), capability=_push(), text="done")
    child_id = conn.execute(
        "select child_id from kanban_delivery_children where parent_id=?", (parent,)
    ).fetchone()[0]
    token = lease_child(conn, child_id, "notifier", now=7, lease_seconds=5)
    mark_sending(conn, child_id, token, now=8)
    mark_failed(conn, child_id, token, error_class="permanent", now=9, retry_at=99)
    mark_dead(conn, child_id, error_class="permanent", now=10)
    assert not parent_complete(conn, parent)
    audit_dead_child(
        conn,
        child_id,
        actor="operator",
        reason_code="approved-terminal-disposition",
        evidence="ticket:1",
        completion_permitted=True,
        now=11,
    )
    assert parent_complete(conn, parent)
    assert conn.execute("select count(*) from kanban_delivery_audit").fetchone()[0] == 1


def test_process_parent_acks_each_child_and_retries_only_failed_sibling(tmp_path, authority_lease):
    from hermes_cli.kanban_delivery_outbox import (
        init_schema,
        materialize_parent,
        parent_complete,
        process_parent,
    )

    conn = _conn()
    init_schema(conn)
    artifact_path = tmp_path / "artifact.bin"
    artifact_path.write_bytes(b"artifact")
    parent = materialize_parent(
        conn,
        source=_source("e1"),
        capability=_push(),
        text="done",
        artifacts=[{
            "manifest_id": "m1",
            "sha256": hashlib.sha256(b"artifact").hexdigest(),
            "path": str(artifact_path),
        }],
    )
    calls = []

    async def first(child):
        calls.append(child["kind"])
        if child["kind"] == "artifact_upload":
            raise TimeoutError("upload timed out")
        return f"safe:{child['child_id']}"

    assert asyncio.run(process_parent(conn, parent, owner="n1", send_child=first, now=10)) is False
    assert calls == ["primary_text", "artifact_upload"]
    assert not parent_complete(conn, parent)

    calls.clear()

    async def second(child):
        calls.append(child["kind"])
        return f"safe:{child['child_id']}"

    assert asyncio.run(process_parent(conn, parent, owner="n2", send_child=second, now=20)) is True
    assert calls == ["artifact_upload"]
    assert parent_complete(conn, parent)


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
