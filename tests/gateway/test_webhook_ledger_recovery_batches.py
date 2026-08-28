"""Bounded, indexed pagination regressions for webhook recovery authority."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import gateway.platforms.webhook_ledger as ledger_module
from gateway.platforms.webhook_ledger import (
    DEFAULT_RECOVERY_BATCH_SIZE,
    MAXIMUM_RECOVERY_BATCH_SIZE,
    MAXIMUM_RECOVERY_PROFILES,
    AdmitDisposition,
    OperationState,
    RecoveryCursor,
    WebhookLedgerConfigurationError,
    WebhookLedgerCorruptionError,
    WebhookLedgerError,
    WebhookOperationLedger,
)
from tests.gateway.test_webhook_ledger import (
    _admit_and_prepare,
    _envelope,
    _snapshots,
    _stage,
)


def _set_dead_owner(db_path: Path, owner_instance: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """UPDATE webhook_operations
                  SET owner_pid=NULL, owner_started_at=NULL
                WHERE owner_instance=?""",
            (owner_instance,),
        )


def _prepare_for_profile(
    ledger: WebhookOperationLedger,
    *,
    profile: str,
    operation_id: str,
):
    admitted = ledger.admit(
        _envelope(
            delivery_id=operation_id,
            trace_id=operation_id,
            profile=profile,
        )
    )
    assert admitted.disposition is AdmitDisposition.ACCEPTED
    assert admitted.authority is not None
    return ledger.prepare(admitted.authority, **_snapshots(operation_id))


def test_delivery_ready_pages_are_strictly_capped_and_cursor_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"now": 1000.0}
    monkeypatch.setattr(ledger_module.time, "time", lambda: clock["now"])
    ledger = WebhookOperationLedger(
        tmp_path / "state.db",
        instance_id="delivery-owner",
        max_records=64,
    )
    expected: list[str] = []
    for index in range(DEFAULT_RECOVERY_BATCH_SIZE * 2 + 3):
        clock["now"] = 1000.0 + index
        authority = _admit_and_prepare(
            ledger,
            delivery_id=f"delivery-page-{index:03d}",
            trace_id=f"delivery-page-{index:03d}",
        )
        expected.append(authority.operation_id)
        _stage(ledger, authority, content=f"durable output {index}")

    cursor = None
    observed: list[str] = []
    page_sizes: list[int] = []
    while True:
        page = ledger.list_delivery_ready(limit=3, after=cursor)
        assert page.event_ready == ()
        assert page.released == ()
        assert page.indeterminate == ()
        assert page.scanned_count == len(page.delivery_ready) <= 3
        page_sizes.append(page.scanned_count)
        observed.extend(item.operation_id for item in page.delivery_ready)
        if not page.has_more:
            assert page.next_cursor is None
            break
        assert page.next_cursor is not None
        assert cursor is None or page.next_cursor > cursor
        cursor = page.next_cursor

    assert observed == expected
    assert len(observed) == len(set(observed))
    assert page_sizes == [3] * (len(expected) // 3) + [len(expected) % 3]
    assert len(ledger.current_delivery_ready()) == DEFAULT_RECOVERY_BATCH_SIZE


def test_dead_owner_scan_cursor_advances_past_live_head_without_starvation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"now": 100.0}
    monkeypatch.setattr(ledger_module.time, "time", lambda: clock["now"])
    db_path = tmp_path / "state.db"
    live = WebhookOperationLedger(db_path, instance_id="live-owner", max_records=64)
    dead = WebhookOperationLedger(db_path, instance_id="dead-owner", max_records=64)
    replacement = WebhookOperationLedger(
        db_path,
        instance_id="replacement-owner",
        max_records=64,
    )

    live_ids: list[str] = []
    for index in range(3):
        clock["now"] = 100.0 + index
        live_ids.append(
            _admit_and_prepare(
                live,
                delivery_id=f"live-{index}",
                trace_id=f"live-{index}",
            ).operation_id
        )
    dead_ids: list[str] = []
    for index in range(5):
        clock["now"] = 200.0 + index
        dead_ids.append(
            _admit_and_prepare(
                dead,
                delivery_id=f"dead-{index}",
                trace_id=f"dead-{index}",
            ).operation_id
        )
    _set_dead_owner(db_path, dead.instance_id)

    first = replacement.recover_dead_owners_page(now=300.0, limit=3)
    assert first.scanned_count == 3
    assert first.event_ready == ()
    assert first.delivery_ready == ()
    assert first.has_more
    assert first.next_cursor is not None

    cursor = first.next_cursor
    recovered: list[str] = []
    scanned = first.scanned_count
    while True:
        page = replacement.recover_dead_owners_page(
            now=300.0,
            limit=3,
            after=cursor,
        )
        assert page.scanned_count <= 3
        assert (
            len(page.event_ready)
            + len(page.delivery_ready)
            + len(page.released)
            + len(page.indeterminate)
            <= page.scanned_count
        )
        recovered.extend(item.operation_id for item in page.event_ready)
        scanned += page.scanned_count
        if not page.has_more:
            assert page.next_cursor is None
            break
        assert page.next_cursor is not None
        assert page.next_cursor > cursor
        cursor = page.next_cursor

    assert scanned == len(live_ids) + len(dead_ids)
    assert recovered == dead_ids
    assert len(recovered) == len(set(recovered))
    with sqlite3.connect(db_path) as conn:
        live_owners = dict(
            conn.execute(
                """SELECT operation_id, owner_instance
                     FROM webhook_operations
                    WHERE operation_id IN (?, ?, ?)""",
                live_ids,
            )
        )
    assert live_owners == dict.fromkeys(live_ids, live.instance_id)

    rediscovered = replacement.list_current_recovery_ready(limit=3)
    assert [item.operation_id for item in rediscovered.event_ready] == dead_ids[:3]
    assert rediscovered.delivery_ready == ()
    assert rediscovered.has_more
    assert rediscovered.next_cursor is not None


def test_retirement_pages_leave_selection_and_preserve_exact_owner_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"now": 100.0}
    monkeypatch.setattr(ledger_module.time, "time", lambda: clock["now"])
    db_path = tmp_path / "state.db"
    old = WebhookOperationLedger(db_path, instance_id="old-owner", max_records=64)
    peer = WebhookOperationLedger(db_path, instance_id="peer-owner", max_records=64)
    replacement = WebhookOperationLedger(
        db_path,
        instance_id="replacement-owner",
        max_records=64,
    )

    old_ids: list[str] = []
    for index in range(7):
        clock["now"] = 100.0 + index
        old_ids.append(
            _admit_and_prepare(
                old,
                delivery_id=f"old-{index}",
                trace_id=f"old-{index}",
            ).operation_id
        )
    clock["now"] = 200.0
    peer_authority = _admit_and_prepare(
        peer,
        delivery_id="peer-stays-owned",
        trace_id="peer-stays-owned",
    )

    page_sizes: list[int] = []
    while True:
        page = replacement.retire_owner_instance_page(
            old.instance_id,
            now=300.0,
            limit=3,
        )
        page_sizes.append(page.scanned_count)
        # READY rows leave the exact owner selection without being returned as
        # scheduled work. Exhaustion must use has_more, never tuple emptiness.
        assert page.event_ready == page.delivery_ready == ()
        assert page.released == page.indeterminate == ()
        assert page.scanned_count <= 3
        assert page.next_cursor is None
        if not page.has_more:
            break

    assert page_sizes == [3, 3, 1]
    with sqlite3.connect(db_path) as conn:
        remaining = conn.execute(
            """SELECT COUNT(*) FROM webhook_operations
                WHERE owner_instance=? AND state IN (
                    'preparing','ready','running','delivery_ready','delivering'
                )""",
            (old.instance_id,),
        ).fetchone()
    assert remaining == (0,)
    peer_state = replacement.lookup_session(peer_authority.session_key)
    assert peer_state is not None
    assert peer_state.owner_instance == peer.instance_id

    recovered: list[str] = []
    cursor = None
    while True:
        page = replacement.recover_dead_owners_page(
            now=301.0,
            limit=3,
            after=cursor,
        )
        recovered.extend(item.operation_id for item in page.event_ready)
        if not page.has_more:
            break
        assert page.next_cursor is not None
        cursor = page.next_cursor
    assert recovered == old_ids


def test_profile_filtered_recovery_skips_excluded_dead_front_without_starvation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"now": 100.0}
    monkeypatch.setattr(ledger_module.time, "time", lambda: clock["now"])
    db_path = tmp_path / "state.db"
    excluded_owner = WebhookOperationLedger(
        db_path,
        instance_id="excluded-owner",
        max_records=64,
    )
    included_owner = WebhookOperationLedger(
        db_path,
        instance_id="included-owner",
        max_records=64,
    )
    replacement = WebhookOperationLedger(
        db_path,
        instance_id="replacement-owner",
        max_records=64,
    )

    excluded_ids: list[str] = []
    for index in range(5):
        clock["now"] = 100.0 + index
        excluded_ids.append(
            _prepare_for_profile(
                excluded_owner,
                profile="excluded",
                operation_id=f"excluded-{index}",
            ).operation_id
        )
    included_ids: list[str] = []
    for index in range(3):
        clock["now"] = 200.0 + index
        included_ids.append(
            _prepare_for_profile(
                included_owner,
                profile="included",
                operation_id=f"included-{index}",
            ).operation_id
        )
    _set_dead_owner(db_path, excluded_owner.instance_id)
    _set_dead_owner(db_path, included_owner.instance_id)

    first = replacement.recover_dead_owners_page(
        now=300.0,
        limit=2,
        profiles=("included",),
    )
    assert [item.operation_id for item in first.event_ready] == included_ids[:2]
    assert first.scanned_count == 2
    assert first.has_more
    assert first.next_cursor is not None
    second = replacement.recover_dead_owners_page(
        now=300.0,
        limit=2,
        after=first.next_cursor,
        profiles=("included",),
    )
    assert [item.operation_id for item in second.event_ready] == included_ids[2:]
    assert second.scanned_count == 1
    assert not second.has_more

    with sqlite3.connect(db_path) as conn:
        excluded_rows = conn.execute(
            """SELECT operation_id, profile, generation, owner_instance
                 FROM webhook_operations
                WHERE profile='excluded'
                ORDER BY created_at, operation_id"""
        ).fetchall()
    assert excluded_rows == [
        (operation_id, "excluded", 1, excluded_owner.instance_id)
        for operation_id in excluded_ids
    ]

    assert (
        replacement.recover_dead_owners_page(profiles=())
        == ledger_module.RecoveryBatch()
    )


def test_admission_persists_physical_authority_profile_not_route_alias(
    tmp_path: Path,
) -> None:
    ledger = WebhookOperationLedger(tmp_path / "state.db")
    envelope = _envelope(
        delivery_id="physical-profile",
        trace_id="physical-profile",
        profile="default",
    )
    object.__setattr__(envelope, "authority_profile", "alpha")

    admitted = ledger.admit(envelope)

    assert admitted.disposition is AdmitDisposition.ACCEPTED
    assert admitted.authority is not None
    assert admitted.authority.profile == "alpha"
    with sqlite3.connect(ledger.db_path) as conn:
        persisted = conn.execute(
            """SELECT profile FROM webhook_operations
                WHERE operation_id='physical-profile'"""
        ).fetchone()
    assert persisted == ("alpha",)


def test_current_owner_pages_filter_exact_physical_profiles_without_starvation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"now": 100.0}
    monkeypatch.setattr(ledger_module.time, "time", lambda: clock["now"])
    ledger = WebhookOperationLedger(
        tmp_path / "state.db",
        instance_id="shared-owner",
        max_records=64,
    )
    excluded_ids: list[str] = []
    for index in range(5):
        clock["now"] = 100.0 + index
        authority = _prepare_for_profile(
            ledger,
            profile="excluded",
            operation_id=f"current-excluded-{index}",
        )
        excluded_ids.append(authority.operation_id)
        _stage(ledger, authority)
    included_ids: list[str] = []
    for index in range(2):
        clock["now"] = 200.0 + index
        authority = _prepare_for_profile(
            ledger,
            profile="included",
            operation_id=f"current-included-{index}",
        )
        included_ids.append(authority.operation_id)
        _stage(ledger, authority)

    first = ledger.list_delivery_ready(limit=1, profiles=("included",))
    assert [item.operation_id for item in first.delivery_ready] == included_ids[:1]
    assert first.has_more
    assert first.next_cursor is not None
    second = ledger.list_current_recovery_ready(
        limit=1,
        after=first.next_cursor,
        profiles=("included",),
    )
    assert [item.operation_id for item in second.delivery_ready] == included_ids[1:]
    assert not second.has_more
    assert all(
        item.operation_id not in excluded_ids
        for item in (*first.delivery_ready, *second.delivery_ready)
    )
    assert (
        ledger.list_current_recovery_ready(profiles=()) == ledger_module.RecoveryBatch()
    )


def test_recovery_queries_are_partial_indexed_and_never_unbounded(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.db"
    ledger = WebhookOperationLedger(db_path, instance_id="query-owner")
    staged = _admit_and_prepare(
        ledger,
        delivery_id="query-plan",
        trace_id="query-plan",
    )
    _stage(ledger, staged)

    statements: list[str] = []
    original_connect = ledger._connect

    def traced_connect() -> sqlite3.Connection:
        conn = original_connect()
        conn.set_trace_callback(statements.append)
        return conn

    ledger._connect = traced_connect  # type: ignore[method-assign]
    ledger.list_delivery_ready(limit=1)
    ledger.list_delivery_ready(limit=1, profiles=("default",))
    ledger.list_current_recovery_ready(limit=1)
    ledger.list_current_recovery_ready(limit=1, profiles=("default",))
    ledger.recover_dead_owners_page(limit=1)
    ledger.recover_dead_owners_page(limit=1, profiles=("default",))
    ledger.retire_owner_instance_page("missing-owner", limit=1)

    bounded_indexes = {
        "idx_webhook_operations_owner_delivery_ready",
        "idx_webhook_operations_owner_current_recovery",
        "idx_webhook_operations_owner_profile_delivery_ready",
        "idx_webhook_operations_owner_profile_current_recovery",
        "idx_webhook_operations_recovery_order",
        "idx_webhook_operations_owner_recovery_order",
        "idx_webhook_operations_profile_recovery_order",
    }
    normalized_statements = [
        " ".join(statement.lower().split()) for statement in statements
    ]
    for index_name in bounded_indexes:
        matching = [
            statement
            for statement in normalized_statements
            if f"indexed by {index_name}" in statement
        ]
        assert matching, index_name
        assert all(" limit " in statement for statement in matching)
        assert all(
            "count(" not in statement and "sum(" not in statement
            for statement in matching
        )

    with sqlite3.connect(db_path) as conn:
        plans = {
            "idx_webhook_operations_recovery_order": conn.execute(
                """EXPLAIN QUERY PLAN
                   SELECT operation_id, created_at
                     FROM webhook_operations INDEXED BY
                          idx_webhook_operations_recovery_order
                    WHERE state IN (
                        'preparing','ready','running','delivery_ready','delivering'
                    )
                    ORDER BY created_at, operation_id
                    LIMIT 8"""
            ).fetchall(),
            "idx_webhook_operations_owner_recovery_order": conn.execute(
                """EXPLAIN QUERY PLAN
                   SELECT operation_id, created_at
                     FROM webhook_operations INDEXED BY
                          idx_webhook_operations_owner_recovery_order
                    WHERE owner_instance=? AND state IN (
                        'preparing','ready','running','delivery_ready','delivering'
                    )
                    ORDER BY created_at, operation_id
                    LIMIT 8""",
                (ledger.instance_id,),
            ).fetchall(),
            "idx_webhook_operations_owner_delivery_ready": conn.execute(
                """EXPLAIN QUERY PLAN
                   SELECT o.operation_id, o.created_at
                     FROM webhook_operations AS o INDEXED BY
                          idx_webhook_operations_owner_delivery_ready
                     JOIN webhook_targets AS t ON t.operation_id=o.operation_id
                    WHERE o.owner_instance=? AND o.state='delivery_ready'
                      AND t.state='pending'
                    ORDER BY o.created_at, o.operation_id
                    LIMIT 8""",
                (ledger.instance_id,),
            ).fetchall(),
            "idx_webhook_operations_profile_recovery_order": conn.execute(
                """EXPLAIN QUERY PLAN
                   SELECT operation_id, created_at
                     FROM webhook_operations INDEXED BY
                          idx_webhook_operations_profile_recovery_order
                    WHERE profile=? AND state IN (
                        'preparing','ready','running','delivery_ready','delivering'
                    )
                    ORDER BY created_at, operation_id
                    LIMIT 8""",
                ("default",),
            ).fetchall(),
        }
    for index_name, plan in plans.items():
        detail = " ".join(str(row[3]).lower() for row in plan)
        assert index_name in detail
        assert "temp b-tree" not in detail


@pytest.mark.parametrize(
    "invalid_limit", [0, -1, True, MAXIMUM_RECOVERY_BATCH_SIZE + 1]
)
def test_recovery_batch_limit_is_strict(
    tmp_path: Path,
    invalid_limit: int,
) -> None:
    ledger = WebhookOperationLedger(tmp_path / f"state-{invalid_limit}.db")
    with pytest.raises(ValueError, match="recovery batch limit"):
        ledger.recover_dead_owners_page(limit=invalid_limit)
    with pytest.raises(ValueError, match="recovery batch limit"):
        ledger.list_current_recovery_ready(limit=invalid_limit)
    with pytest.raises(ValueError, match="recovery batch limit"):
        ledger.list_delivery_ready(limit=invalid_limit)
    with pytest.raises(ValueError, match="recovery batch limit"):
        ledger.retire_instance_page(limit=invalid_limit)


@pytest.mark.parametrize("invalid_now", [float("nan"), float("inf"), "not-a-time"])
def test_dead_owner_recovery_rejects_nonfinite_time(
    tmp_path: Path,
    invalid_now: object,
) -> None:
    ledger = WebhookOperationLedger(tmp_path / "state.db")
    with pytest.raises(WebhookLedgerError, match="timestamp must be finite"):
        ledger.recover_dead_owners_page(now=invalid_now)


def test_shadowed_recovery_index_fails_closed_on_open(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    WebhookOperationLedger(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP INDEX idx_webhook_operations_recovery_order")
        conn.execute(
            """CREATE INDEX idx_webhook_operations_recovery_order
                 ON webhook_operations(updated_at)
               WHERE state='ready'"""
        )

    with pytest.raises(
        WebhookLedgerCorruptionError,
        match="bounded recovery indexes",
    ):
        WebhookOperationLedger(db_path)


def test_persisted_limit_mismatch_has_deterministic_configuration_subtype(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.db"
    WebhookOperationLedger(db_path, max_records=8)
    with pytest.raises(
        WebhookLedgerConfigurationError,
        match="limits do not match persisted authority",
    ):
        WebhookOperationLedger(db_path, max_records=9)


def test_recovery_cursor_rejects_noncanonical_or_nonfinite_values(
    tmp_path: Path,
) -> None:
    ledger = WebhookOperationLedger(tmp_path / "state.db")
    for cursor in (
        RecoveryCursor(float("nan"), "operation"),
        RecoveryCursor(1.0, " operation "),
    ):
        with pytest.raises(ValueError, match="recovery cursor"):
            ledger.recover_dead_owners_page(after=cursor)
    with pytest.raises(ValueError, match="RecoveryCursor"):
        ledger.recover_dead_owners_page(after=(1.0, "operation"))  # type: ignore[arg-type]


def test_recovery_profile_filter_is_canonical_and_capped(tmp_path: Path) -> None:
    ledger = WebhookOperationLedger(tmp_path / "state.db")
    with pytest.raises(ValueError, match="iterable of profile names"):
        ledger.recover_dead_owners_page(profiles="default")
    with pytest.raises(ValueError, match="profile-count limit"):
        ledger.recover_dead_owners_page(
            profiles=tuple(
                f"profile-{index}" for index in range(MAXIMUM_RECOVERY_PROFILES + 1)
            )
        )
    with pytest.raises(ValueError, match="recovery profile"):
        ledger.recover_dead_owners_page(profiles=(" default ",))


def test_default_batch_is_below_hard_recovery_cap() -> None:
    assert 1 <= DEFAULT_RECOVERY_BATCH_SIZE < MAXIMUM_RECOVERY_BATCH_SIZE
    assert MAXIMUM_RECOVERY_BATCH_SIZE <= 16


def test_current_recovery_ready_excludes_ordinary_generation_one_event(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.db"
    ordinary = WebhookOperationLedger(db_path, instance_id="ordinary-owner")
    dead = WebhookOperationLedger(db_path, instance_id="dead-owner")
    ledger = WebhookOperationLedger(db_path, instance_id="recovery-owner")
    ordinary_ready = _admit_and_prepare(
        ordinary,
        delivery_id="ordinary-event-ready",
        trace_id="ordinary-event-ready",
    )
    dead_ready = _admit_and_prepare(
        dead,
        delivery_id="recovered-event-ready",
        trace_id="recovered-event-ready",
    )
    _set_dead_owner(db_path, dead.instance_id)
    claimed = ledger.recover_dead_owners_page(limit=3)
    assert [item.operation_id for item in claimed.event_ready] == [
        dead_ready.operation_id
    ]
    event_ready = claimed.event_ready[0]
    delivery_ready = _admit_and_prepare(
        ledger,
        delivery_id="delivery-ready",
        trace_id="delivery-ready",
    )
    delivery_ready = _stage(ledger, delivery_ready)

    page = ledger.list_current_recovery_ready(limit=3)

    assert [item.operation_id for item in page.event_ready] == [
        event_ready.operation_id
    ]
    assert [item.operation_id for item in page.delivery_ready] == [
        delivery_ready.operation_id
    ]
    assert page.scanned_count == 2
    assert not page.has_more
    assert ordinary_ready.operation_id not in {
        item.operation_id for item in (*page.event_ready, *page.delivery_ready)
    }
    assert all(
        item.state in {OperationState.READY, OperationState.DELIVERY_READY}
        for item in (*page.event_ready, *page.delivery_ready)
    )
