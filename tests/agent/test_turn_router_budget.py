from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading

import pytest

from agent.turn_router_budget import TurnRouterBudgetLedger, utc_week_key
from agent.turn_router_budget import BudgetInvariantError


_PROCESS_RESERVE_SCRIPT = """
import json
import sys
from pathlib import Path
from agent.turn_router_budget import TurnRouterBudgetLedger

db_path = Path(sys.argv[1])
index = int(sys.argv[2])
now = float(sys.argv[3])
lease_seconds = float(sys.argv[4])
result = TurnRouterBudgetLedger(
    db_path=db_path,
    weekly_limit=3,
    owner_id=f"process-{index}",
    lease_seconds=lease_seconds,
).reserve(
    turn_id=f"process-turn-{index}",
    route_id="grok-review",
    now=now,
)
print(json.dumps({"allowed": result.allowed, "state": result.state}))
"""


def _reserve_in_process(
    db_path: Path,
    index: int,
    *,
    now: float = 1_722_470_400.0,
    lease_seconds: float = 300.0,
) -> dict:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            _PROCESS_RESERVE_SCRIPT,
            str(db_path),
            str(index),
            str(now),
            str(lease_seconds),
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_concurrent_reservations_cannot_overspend(tmp_path):
    db_path = tmp_path / "turn-router-budget.db"
    barrier = threading.Barrier(8)

    def reserve(index: int):
        ledger = TurnRouterBudgetLedger(
            db_path=db_path,
            weekly_limit=3,
            owner_id=f"worker-{index}",
        )
        barrier.wait()
        return ledger.reserve(
            turn_id=f"turn-{index}",
            route_id="grok-review",
            slots=1,
            now=1_722_470_400.0,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(reserve, range(8)))

    assert sum(result.allowed for result in results) == 3
    assert sum(result.slots for result in results if result.allowed) == 3
    assert all(result.reason_code == "weekly_budget_exhausted" for result in results if not result.allowed)

    status = TurnRouterBudgetLedger(db_path=db_path, weekly_limit=3).status(
        now=1_722_470_400.0
    )
    assert status.reserved_slots == 3
    assert status.committed_slots == 0
    assert status.available_slots == 0


def test_multi_slot_reservation_is_atomic(tmp_path):
    ledger = TurnRouterBudgetLedger(
        db_path=tmp_path / "turn-router-budget.db",
        weekly_limit=3,
    )

    first = ledger.reserve(
        turn_id="turn-frontier",
        route_id="frontier",
        slots=2,
        now=1_722_470_400.0,
    )
    denied = ledger.reserve(
        turn_id="turn-deep",
        route_id="grok-review",
        slots=2,
        now=1_722_470_401.0,
    )

    assert first.allowed is True
    assert denied.allowed is False
    assert denied.reason_code == "weekly_budget_exhausted"
    status = ledger.status(now=1_722_470_401.0)
    assert status.reserved_slots == 2
    assert status.available_slots == 1


def test_concurrent_multi_slot_reservations_never_partially_spend(tmp_path):
    db_path = tmp_path / "turn-router-budget.db"
    barrier = threading.Barrier(2)

    def reserve(index: int):
        ledger = TurnRouterBudgetLedger(
            db_path=db_path,
            weekly_limit=3,
            owner_id=f"frontier-worker-{index}",
        )
        barrier.wait()
        return ledger.reserve(
            turn_id=f"turn-frontier-{index}",
            route_id="frontier",
            slots=2,
            now=1_722_470_400.0,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, range(2)))

    assert sum(result.allowed for result in results) == 1
    assert sum(result.slots for result in results if result.allowed) == 2
    status = TurnRouterBudgetLedger(db_path=db_path, weekly_limit=3).status(
        now=1_722_470_400.0
    )
    assert status.reserved_slots == 2
    assert status.available_slots == 1


def test_duplicate_turn_reservation_is_idempotent(tmp_path):
    ledger = TurnRouterBudgetLedger(
        db_path=tmp_path / "turn-router-budget.db",
        weekly_limit=3,
    )

    first = ledger.reserve(
        turn_id="session:task:turn",
        route_id="grok-review",
        slots=1,
        now=1_722_470_400.0,
    )
    duplicate = ledger.reserve(
        turn_id="session:task:turn",
        route_id="grok-review",
        slots=1,
        now=1_722_470_402.0,
    )

    assert first.allowed is True
    assert duplicate.allowed is True
    assert duplicate.reservation_id == first.reservation_id
    assert duplicate.idempotent is True
    assert ledger.status(now=1_722_470_402.0).reserved_slots == 1


def test_utc_week_key_starts_monday_and_ignores_local_timezone():
    before = datetime(2026, 8, 2, 23, 59, 59, tzinfo=timezone.utc).timestamp()
    after = datetime(2026, 8, 3, 0, 0, 0, tzinfo=timezone.utc).timestamp()

    assert utc_week_key(before) == "2026-07-27"
    assert utc_week_key(after) == "2026-08-03"


def test_commit_is_idempotent_and_rejects_conflicting_submission_identity(tmp_path):
    ledger = TurnRouterBudgetLedger(
        db_path=tmp_path / "turn-router-budget.db",
        weekly_limit=2,
    )
    reservation = ledger.reserve(
        turn_id="turn-1",
        route_id="grok-review",
        now=1_722_470_400.0,
    )

    committed = ledger.commit(
        reservation.reservation_id,
        provider_submission_id="safe-submission-1",
        now=1_722_470_401.0,
    )
    duplicate = ledger.commit(
        reservation.reservation_id,
        provider_submission_id="safe-submission-1",
        now=1_722_470_402.0,
    )

    assert committed.state == "committed"
    assert duplicate.state == "committed"
    assert duplicate.idempotent is True
    assert ledger.status(now=1_722_470_402.0).committed_slots == 1

    try:
        ledger.commit(
            reservation.reservation_id,
            provider_submission_id="different-submission",
            now=1_722_470_403.0,
        )
    except BudgetInvariantError as exc:
        assert exc.reason_code == "provider_submission_conflict"
    else:
        raise AssertionError("conflicting submission identity must fail closed")


def test_release_and_refund_are_idempotent_and_restore_capacity(tmp_path):
    ledger = TurnRouterBudgetLedger(
        db_path=tmp_path / "turn-router-budget.db",
        weekly_limit=1,
    )
    before_dispatch = ledger.reserve(
        turn_id="turn-before",
        route_id="grok-review",
        now=1_722_470_400.0,
    )

    released = ledger.release(
        before_dispatch.reservation_id,
        reason_code="provider_not_dispatched",
        now=1_722_470_401.0,
    )
    released_again = ledger.release(
        before_dispatch.reservation_id,
        reason_code="provider_not_dispatched",
        now=1_722_470_402.0,
    )
    assert released.state == "released"
    assert released_again.idempotent is True

    accepted = ledger.reserve(
        turn_id="turn-accepted",
        route_id="grok-review",
        now=1_722_470_403.0,
    )
    ledger.commit(
        accepted.reservation_id,
        provider_submission_id="submission-accepted",
        now=1_722_470_404.0,
    )
    refunded = ledger.refund(
        accepted.reservation_id,
        reason_code="provider_explicitly_not_billed",
        now=1_722_470_405.0,
    )
    refunded_again = ledger.refund(
        accepted.reservation_id,
        reason_code="provider_explicitly_not_billed",
        now=1_722_470_406.0,
    )

    assert refunded.state == "refunded"
    assert refunded_again.idempotent is True
    assert ledger.status(now=1_722_470_406.0).available_slots == 1

    released_duplicate = ledger.reserve(
        turn_id="turn-before",
        route_id="grok-review",
        now=1_722_470_407.0,
    )
    refunded_duplicate = ledger.reserve(
        turn_id="turn-accepted",
        route_id="grok-review",
        now=1_722_470_408.0,
    )
    assert released_duplicate.reservation_id == released.reservation_id
    assert released_duplicate.state == "released"
    assert released_duplicate.allowed is False
    assert released_duplicate.idempotent is True
    assert refunded_duplicate.reservation_id == refunded.reservation_id
    assert refunded_duplicate.state == "refunded"
    assert refunded_duplicate.allowed is False
    assert refunded_duplicate.idempotent is True
    assert ledger.status(now=1_722_470_408.0).available_slots == 1


def test_refund_rejects_ambiguous_or_non_billing_reason(tmp_path):
    ledger = TurnRouterBudgetLedger(
        db_path=tmp_path / "turn-router-budget.db",
        weekly_limit=1,
    )
    reservation = ledger.reserve(
        turn_id="turn-no-refund",
        route_id="grok-review",
        now=1_722_470_400.0,
    )
    assert reservation.reservation_id is not None
    ledger.commit(
        reservation.reservation_id,
        provider_submission_id="safe-submission-no-refund",
        now=1_722_470_401.0,
    )

    with pytest.raises(BudgetInvariantError, match="refund_reason_not_allowed"):
        ledger.refund(
            reservation.reservation_id,
            reason_code="submission_uncertain",
            now=1_722_470_402.0,
        )

    assert ledger.get(reservation.reservation_id).state == "committed"
    assert ledger.status(now=1_722_470_402.0).committed_slots == 1


def test_ledger_capacity_and_idempotency_reset_at_utc_week_boundary(tmp_path):
    ledger = TurnRouterBudgetLedger(
        db_path=tmp_path / "turn-router-budget.db",
        weekly_limit=1,
    )
    before = datetime(2026, 8, 2, 23, 59, 59, tzinfo=timezone.utc).timestamp()
    after = datetime(2026, 8, 3, 0, 0, 0, tzinfo=timezone.utc).timestamp()

    week_one = ledger.reserve(
        turn_id="same-turn",
        route_id="grok-review",
        now=before,
    )
    week_two = ledger.reserve(
        turn_id="same-turn",
        route_id="grok-review",
        now=after,
    )

    assert week_one.allowed is True
    assert week_one.week_key == "2026-07-27"
    assert week_two.allowed is True
    assert week_two.week_key == "2026-08-03"
    assert week_two.reservation_id != week_one.reservation_id
    assert ledger.status(now=after).reserved_slots == 1


def test_expired_reservation_is_reaped_atomically_by_next_reserve(tmp_path):
    ledger = TurnRouterBudgetLedger(
        db_path=tmp_path / "turn-router-budget.db",
        weekly_limit=1,
        lease_seconds=10,
    )
    stale = ledger.reserve(
        turn_id="turn-stale",
        route_id="grok-review",
        now=1_722_470_400.0,
    )
    replacement = ledger.reserve(
        turn_id="turn-replacement",
        route_id="grok-review",
        now=1_722_470_411.0,
    )

    assert stale.allowed is True
    assert replacement.allowed is True
    assert ledger.get(stale.reservation_id).state == "expired"
    assert ledger.status(now=1_722_470_411.0).reserved_slots == 1


def test_durable_cooldown_blocks_reservation_until_utc_expiry(tmp_path):
    db_path = tmp_path / "turn-router-budget.db"
    ledger = TurnRouterBudgetLedger(db_path=db_path, weekly_limit=2)
    ledger.set_cooldown(
        scope="grok",
        reason_code="provider_rate_limited",
        until_at=1_722_470_500.0,
        now=1_722_470_400.0,
    )

    blocked = TurnRouterBudgetLedger(db_path=db_path, weekly_limit=2).reserve(
        turn_id="turn-blocked",
        route_id="grok-review",
        cooldown_scope="grok",
        now=1_722_470_450.0,
    )
    allowed = TurnRouterBudgetLedger(db_path=db_path, weekly_limit=2).reserve(
        turn_id="turn-after",
        route_id="grok-review",
        cooldown_scope="grok",
        now=1_722_470_501.0,
    )

    assert blocked.allowed is False
    assert blocked.reason_code == "provider_rate_limited"
    assert allowed.allowed is True


def test_status_reports_only_active_cooldown_in_same_budget_snapshot(tmp_path):
    ledger = TurnRouterBudgetLedger(db_path=tmp_path / "turn-router-budget.db", weekly_limit=2)
    ledger.set_cooldown(
        scope="grok",
        reason_code="provider_rate_limited",
        until_at=1_722_470_500.0,
        now=1_722_470_400.0,
    )

    active = ledger.status(now=1_722_470_450.0, cooldown_scope="grok")
    expired = ledger.status(now=1_722_470_501.0, cooldown_scope="grok")

    assert active.cooldown_scope == "grok"
    assert active.cooldown_reason_code == "provider_rate_limited"
    assert active.cooldown_until_at == 1_722_470_500.0
    assert expired.cooldown_scope == "grok"
    assert expired.cooldown_reason_code is None
    assert expired.cooldown_until_at is None


def test_append_only_audit_records_safe_state_transitions_and_rejects_secrets(tmp_path):
    ledger = TurnRouterBudgetLedger(
        db_path=tmp_path / "turn-router-budget.db",
        weekly_limit=1,
    )
    reservation = ledger.reserve(
        turn_id="turn-audit",
        route_id="grok-review",
        now=1_722_470_400.0,
    )
    assert reservation.reservation_id is not None

    with pytest.raises(ValueError, match="safe identifier"):
        ledger.commit(
            reservation.reservation_id,
            provider_submission_id="Bearer sk-secret-value",
            now=1_722_470_401.0,
        )
    with pytest.raises(ValueError, match="safe reason code"):
        ledger.release(
            reservation.reservation_id,
            reason_code="provider failed: sk-secret-value",
            now=1_722_470_401.0,
        )

    ledger.commit(
        reservation.reservation_id,
        provider_submission_id="safe-submission-1",
        now=1_722_470_402.0,
    )
    ledger.refund(
        reservation.reservation_id,
        reason_code="provider_explicitly_not_billed",
        now=1_722_470_403.0,
    )
    ledger.refund(
        reservation.reservation_id,
        reason_code="provider_explicitly_not_billed",
        now=1_722_470_404.0,
    )

    rows = ledger.audit_rows(reservation_id=reservation.reservation_id)
    assert [row.state for row in rows] == ["reserved", "committed", "refunded"]
    assert [row.reason_code for row in rows] == [
        "reserved",
        "provider_accepted",
        "provider_explicitly_not_billed",
    ]
    assert rows[1].provider_submission_id == "safe-submission-1"
    assert all(row.turn_id == "turn-audit" for row in rows)
    assert all(row.route_id == "grok-review" for row in rows)
    assert all(row.slots == 1 for row in rows)
    serialized = repr(rows)
    assert "sk-secret-value" not in serialized
    assert "Bearer" not in serialized


@pytest.mark.parametrize(
    ("stored_version", "reason_code"),
    [
        ("1", "budget_schema_migration_required"),
        ("3", "budget_schema_newer_than_runtime"),
        ("invalid", "budget_schema_invalid"),
    ],
)
def test_existing_unknown_budget_schema_fails_closed(
    tmp_path, stored_version, reason_code
):
    db_path = tmp_path / "turn-router-budget.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE budget_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO budget_meta(key, value) VALUES('schema_version', ?)",
            (stored_version,),
        )

    with pytest.raises(BudgetInvariantError, match=reason_code):
        TurnRouterBudgetLedger(db_path=db_path, weekly_limit=1)

    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT value FROM budget_meta WHERE key='schema_version'"
        ).fetchone()[0] == stored_version


def test_existing_budget_tables_without_schema_version_fail_closed(tmp_path):
    db_path = tmp_path / "turn-router-budget.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE budget_reservations (reservation_id TEXT)")

    with pytest.raises(BudgetInvariantError, match="budget_schema_version_missing"):
        TurnRouterBudgetLedger(db_path=db_path, weekly_limit=1)


def test_process_concurrent_reservations_cannot_overspend(tmp_path):
    db_path = tmp_path / "turn-router-budget.db"
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(lambda index: _reserve_in_process(db_path, index), range(8))
        )

    assert sum(result["allowed"] for result in results) == 3
    status = TurnRouterBudgetLedger(db_path=db_path, weekly_limit=3).status(
        now=1_722_470_400.0
    )
    assert status.reserved_slots == 3
    assert status.available_slots == 0


def test_process_exit_leaves_reapable_lease_without_overspend(tmp_path):
    db_path = tmp_path / "turn-router-budget.db"
    first = _reserve_in_process(
        db_path,
        1,
        now=1_722_470_400.0,
        lease_seconds=1.0,
    )
    second = _reserve_in_process(
        db_path,
        2,
        now=1_722_470_402.0,
        lease_seconds=1.0,
    )

    assert first["allowed"] is True
    assert second["allowed"] is True
    status = TurnRouterBudgetLedger(db_path=db_path, weekly_limit=3).status(
        now=1_722_470_402.0
    )
    assert status.reserved_slots == 1
    assert status.available_slots == 2
