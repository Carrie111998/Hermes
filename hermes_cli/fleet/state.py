"""SQLite-backed fleet pins, leases, rotation, cooldowns, and audit."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Sequence

from .policy import evaluate_lane, select_lane
from .types import (
    AdapterKind,
    CapacityRead,
    LaneInputs,
    LeaseHandle,
    ReasonCode,
    TaskPin,
    TaskSpec,
)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks(
  task_id TEXT PRIMARY KEY,
  lane_id TEXT NOT NULL,
  adapter_kind TEXT NOT NULL,
  provider_id TEXT NOT NULL,
  model_id TEXT NOT NULL,
  effort TEXT NOT NULL,
  fast_mode INTEGER NOT NULL,
  cwd_fingerprint TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  terminal_at TEXT
);
CREATE TABLE IF NOT EXISTS leases(
  task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
  lane_id TEXT NOT NULL,
  owner_uuid TEXT NOT NULL,
  generation INTEGER NOT NULL,
  reserved_pct TEXT NOT NULL,
  acquired_at TEXT NOT NULL,
  heartbeat_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  released_at TEXT
);
CREATE TABLE IF NOT EXISTS lane_state(
  lane_id TEXT PRIMARY KEY,
  rotation_selected_at TEXT,
  cooldown_until TEXT,
  cooldown_reason TEXT,
  qualification_json TEXT,
  qualification_expires_at TEXT,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rotation(
  policy_id TEXT PRIMARY KEY,
  next_lane_index INTEGER NOT NULL,
  generation INTEGER NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events(
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_uuid TEXT UNIQUE NOT NULL,
  task_id TEXT,
  lane_id TEXT,
  at TEXT NOT NULL,
  event_type TEXT NOT NULL,
  reason_code TEXT NOT NULL,
  decision_json TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Acquisition:
    reason: ReasonCode
    pin: TaskPin | None
    lease: LeaseHandle | None
    evaluations: tuple = ()


def _at(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _at(value).isoformat()


def _datetime(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)


class FleetStore:
    """Dedicated state store; read methods never create the database."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.path, timeout=15, isolation_level=None, check_same_thread=False
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=15000")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(_SCHEMA)
        return connection

    def _connect_existing(self) -> sqlite3.Connection | None:
        if not self.path.is_file():
            return None
        uri = f"{self.path.resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _pin(row: sqlite3.Row) -> TaskPin:
        return TaskPin(
            task_id=row["task_id"],
            lane_id=row["lane_id"],
            adapter_kind=AdapterKind(row["adapter_kind"]),
            provider_id=row["provider_id"],
            model_id=row["model_id"],
            effort=row["effort"],
            fast_mode=bool(row["fast_mode"]),
            cwd_fingerprint=row["cwd_fingerprint"],
            status=row["status"],
        )

    @staticmethod
    def _lease(row: sqlite3.Row) -> LeaseHandle:
        return LeaseHandle(
            task_id=row["task_id"],
            lane_id=row["lane_id"],
            owner_uuid=row["owner_uuid"],
            generation=int(row["generation"]),
            reserved_pct=Decimal(row["reserved_pct"]),
            expires_at=_datetime(row["expires_at"]),
        )

    def read_pin(self, task_id: str) -> TaskPin | None:
        connection = self._connect_existing()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT * FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            return self._pin(row) if row is not None else None
        finally:
            connection.close()

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        *,
        at: datetime,
        event_type: str,
        reason: str,
        task_id: str | None = None,
        lane_id: str | None = None,
        decision: dict | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO audit_events(
              event_uuid, task_id, lane_id, at, event_type, reason_code,
              decision_json
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (
                str(uuid.uuid4()),
                task_id,
                lane_id,
                _iso(at),
                event_type,
                reason,
                json.dumps(decision or {}, sort_keys=True, separators=(",", ":")),
            ),
        )

    @staticmethod
    def _reap(connection: sqlite3.Connection, now: datetime) -> int:
        rows = connection.execute(
            """
            SELECT task_id, lane_id FROM leases
            WHERE released_at IS NULL AND expires_at <= ?
            """,
            (_iso(now),),
        ).fetchall()
        for row in rows:
            connection.execute(
                "UPDATE leases SET released_at=? WHERE task_id=? AND released_at IS NULL",
                (_iso(now), row["task_id"]),
            )
            FleetStore._audit(
                connection,
                at=now,
                task_id=row["task_id"],
                lane_id=row["lane_id"],
                event_type="LEASE_EXPIRED",
                reason="LEASE_TTL_EXPIRED",
            )
        return len(rows)

    @staticmethod
    def _lane_usage(
        connection: sqlite3.Connection, lane_id: str, now: datetime
    ) -> tuple[int, Decimal]:
        row = connection.execute(
            """
            SELECT COUNT(*) AS active_count,
                   COALESCE(SUM(CAST(reserved_pct AS REAL)), 0) AS reserved
            FROM leases
            WHERE lane_id=? AND released_at IS NULL AND expires_at > ?
            """,
            (lane_id, _iso(now)),
        ).fetchone()
        return int(row["active_count"]), Decimal(str(row["reserved"]))

    @staticmethod
    def _cooldown_in_txn(
        connection: sqlite3.Connection, lane_id: str, now: datetime
    ) -> datetime | None:
        row = connection.execute(
            "SELECT cooldown_until FROM lane_state WHERE lane_id=?", (lane_id,)
        ).fetchone()
        if row is None or not row["cooldown_until"]:
            return None
        until = _datetime(row["cooldown_until"])
        return until if until > now else None

    def _live_inputs(
        self,
        connection: sqlite3.Connection,
        candidates: Sequence[LaneInputs],
        now: datetime,
    ) -> tuple[LaneInputs, ...]:
        live: list[LaneInputs] = []
        for candidate in candidates:
            count, reserved = self._lane_usage(
                connection, candidate.profile.lane_id, now
            )
            capacity = candidate.capacity
            if capacity.snapshot is not None:
                remaining = capacity.snapshot.remaining_pct
                snapshot = replace(
                    capacity.snapshot,
                    reserved_pct=reserved.quantize(Decimal("0.001")),
                    effective_remaining_pct=max(
                        Decimal("0"), remaining - reserved
                    ).quantize(Decimal("0.001")),
                )
                capacity = CapacityRead(snapshot, capacity.reason, capacity.detail)
            live.append(
                replace(
                    candidate,
                    capacity=capacity,
                    active_leases=count,
                    active_reserved_pct=reserved,
                    cooldown_until=self._cooldown_in_txn(
                        connection, candidate.profile.lane_id, now
                    ),
                )
            )
        return tuple(live)

    @staticmethod
    def _rotation(connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT next_lane_index FROM rotation WHERE policy_id='fleet-v1'"
        ).fetchone()
        return int(row["next_lane_index"]) if row is not None else 0

    def rotation_cursor(self) -> int:
        connection = self._connect_existing()
        if connection is None:
            return 0
        try:
            return self._rotation(connection)
        finally:
            connection.close()

    def acquire(
        self,
        task: TaskSpec,
        candidates: Sequence[LaneInputs],
        *,
        owner_uuid: str,
        ttl_seconds: int,
        now: datetime,
        inject_failure: bool = False,
    ) -> Acquisition:
        at = _at(now)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._reap(connection, at)
            existing_row = connection.execute(
                "SELECT * FROM tasks WHERE task_id=?", (task.task_id,)
            ).fetchone()
            live_inputs = self._live_inputs(connection, candidates, at)
            evaluations = tuple(
                evaluate_lane(candidate, task, now=at) for candidate in live_inputs
            )

            if existing_row is not None:
                pin = self._pin(existing_row)
                evaluation = next(
                    (item for item in evaluations if item.lane_id == pin.lane_id),
                    None,
                )
                current = connection.execute(
                    """
                    SELECT * FROM leases
                    WHERE task_id=? AND released_at IS NULL AND expires_at > ?
                    """,
                    (task.task_id, _iso(at)),
                ).fetchone()
                if current is not None:
                    connection.commit()
                    return Acquisition(
                        ReasonCode.LEASE_CONFLICT,
                        pin,
                        self._lease(current),
                        evaluations,
                    )
                if evaluation is None or not (
                    evaluation.eligible or evaluation.fallback_eligible
                ):
                    self._audit(
                        connection,
                        at=at,
                        task_id=task.task_id,
                        lane_id=pin.lane_id,
                        event_type="ROUTE_DENIED",
                        reason=ReasonCode.PINNED_LANE_UNAVAILABLE.value,
                        decision={
                            "reasons": [
                                reason.value
                                for reason in (
                                    evaluation.reasons if evaluation else ()
                                )
                            ]
                        },
                    )
                    connection.commit()
                    return Acquisition(
                        ReasonCode.PINNED_LANE_UNAVAILABLE,
                        pin,
                        None,
                        evaluations,
                    )
                previous = connection.execute(
                    "SELECT generation FROM leases WHERE task_id=?", (task.task_id,)
                ).fetchone()
                generation = int(previous["generation"]) + 1 if previous else 1
                expires = at + timedelta(seconds=ttl_seconds)
                connection.execute(
                    """
                    INSERT INTO leases(
                      task_id,lane_id,owner_uuid,generation,reserved_pct,
                      acquired_at,heartbeat_at,expires_at,released_at
                    ) VALUES(?,?,?,?,?,?,?,?,NULL)
                    ON CONFLICT(task_id) DO UPDATE SET
                      lane_id=excluded.lane_id,
                      owner_uuid=excluded.owner_uuid,
                      generation=excluded.generation,
                      reserved_pct=excluded.reserved_pct,
                      acquired_at=excluded.acquired_at,
                      heartbeat_at=excluded.heartbeat_at,
                      expires_at=excluded.expires_at,
                      released_at=NULL
                    """,
                    (
                        task.task_id,
                        pin.lane_id,
                        owner_uuid,
                        generation,
                        str(task.reservation_pct),
                        _iso(at),
                        _iso(at),
                        _iso(expires),
                    ),
                )
                connection.execute(
                    "UPDATE tasks SET status='running',updated_at=?,terminal_at=NULL WHERE task_id=?",
                    (_iso(at), task.task_id),
                )
                lease = LeaseHandle(
                    task.task_id,
                    pin.lane_id,
                    owner_uuid,
                    generation,
                    task.reservation_pct,
                    expires,
                )
                self._audit(
                    connection,
                    at=at,
                    task_id=task.task_id,
                    lane_id=pin.lane_id,
                    event_type="LEASE_ACQUIRED",
                    reason=ReasonCode.MET.value,
                    decision={"generation": generation},
                )
                connection.commit()
                return Acquisition(ReasonCode.MET, pin, lease, evaluations)

            cursor = self._rotation(connection)
            decision = select_lane(evaluations, rotation_index=cursor)
            if decision.lane_id is None:
                self._audit(
                    connection,
                    at=at,
                    task_id=task.task_id,
                    event_type="ROUTE_DENIED",
                    reason=ReasonCode.NO_ELIGIBLE_LANE.value,
                    decision={
                        "lanes": {
                            item.lane_id: [reason.value for reason in item.reasons]
                            for item in evaluations
                        }
                    },
                )
                connection.commit()
                return Acquisition(
                    ReasonCode.NO_ELIGIBLE_LANE, None, None, evaluations
                )

            winner = next(
                item for item in evaluations if item.lane_id == decision.lane_id
            )
            assert winner.selected_model is not None
            assert winner.selected_effort is not None
            profile = winner.profile
            cwd_fingerprint = hashlib.sha256(
                str(task.cwd).encode("utf-8")
            ).hexdigest()
            connection.execute(
                """
                INSERT INTO tasks(
                  task_id,lane_id,adapter_kind,provider_id,model_id,effort,
                  fast_mode,cwd_fingerprint,status,created_at,updated_at,terminal_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL)
                """,
                (
                    task.task_id,
                    profile.lane_id,
                    profile.adapter_kind.value,
                    profile.provider_id,
                    winner.selected_model,
                    winner.selected_effort,
                    0,
                    cwd_fingerprint,
                    "running",
                    _iso(at),
                    _iso(at),
                ),
            )
            expires = at + timedelta(seconds=ttl_seconds)
            connection.execute(
                """
                INSERT INTO leases(
                  task_id,lane_id,owner_uuid,generation,reserved_pct,
                  acquired_at,heartbeat_at,expires_at,released_at
                ) VALUES(?,?,?,?,?,?,?,?,NULL)
                """,
                (
                    task.task_id,
                    profile.lane_id,
                    owner_uuid,
                    1,
                    str(task.reservation_pct),
                    _iso(at),
                    _iso(at),
                    _iso(expires),
                ),
            )
            connection.execute(
                """
                INSERT INTO rotation(policy_id,next_lane_index,generation,updated_at)
                VALUES('fleet-v1',?,?,?)
                ON CONFLICT(policy_id) DO UPDATE SET
                  next_lane_index=excluded.next_lane_index,
                  generation=rotation.generation+1,
                  updated_at=excluded.updated_at
                """,
                (decision.rotation_index, 1, _iso(at)),
            )
            self._audit(
                connection,
                at=at,
                task_id=task.task_id,
                lane_id=profile.lane_id,
                event_type="ROUTE_SELECTED",
                reason=decision.reason.value,
                decision={
                    "adapter_kind": profile.adapter_kind.value,
                    "provider_id": profile.provider_id,
                    "model_id": winner.selected_model,
                    "effort": winner.selected_effort,
                    "fast_mode": False,
                    "selection_reason": decision.reason.value,
                    "capacity_source": (
                        winner.capacity.source_id
                        if winner.capacity
                        and decision.reason is ReasonCode.MET
                        else None
                    ),
                    "qualification": winner.qualification_evidence_id,
                },
            )
            if inject_failure:
                raise RuntimeError("injected transaction failure")
            connection.commit()
            pin = TaskPin(
                task.task_id,
                profile.lane_id,
                profile.adapter_kind,
                profile.provider_id,
                winner.selected_model,
                winner.selected_effort,
                False,
                cwd_fingerprint,
                "running",
            )
            lease = LeaseHandle(
                task.task_id,
                profile.lane_id,
                owner_uuid,
                1,
                task.reservation_pct,
                expires,
            )
            return Acquisition(ReasonCode.MET, pin, lease, evaluations)
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def active_reserved_pct(self, lane_id: str, *, now: datetime) -> Decimal:
        connection = self._connect_existing()
        if connection is None:
            return Decimal("0")
        try:
            return self._lane_usage(connection, lane_id, _at(now))[1]
        finally:
            connection.close()

    def lane_usage(self, lane_id: str, *, now: datetime) -> tuple[int, Decimal]:
        connection = self._connect_existing()
        if connection is None:
            return 0, Decimal("0")
        try:
            return self._lane_usage(connection, lane_id, _at(now))
        finally:
            connection.close()

    def read_live_lease(
        self, task_id: str, *, now: datetime
    ) -> LeaseHandle | None:
        connection = self._connect_existing()
        if connection is None:
            return None
        try:
            row = connection.execute(
                """
                SELECT * FROM leases
                WHERE task_id=? AND released_at IS NULL AND expires_at > ?
                """,
                (task_id, _iso(now)),
            ).fetchone()
            return self._lease(row) if row is not None else None
        finally:
            connection.close()

    def record_event(
        self,
        *,
        at: datetime,
        event_type: str,
        reason: str,
        task_id: str | None = None,
        lane_id: str | None = None,
        decision: dict | None = None,
    ) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._audit(
                connection,
                at=_at(at),
                event_type=event_type,
                reason=reason,
                task_id=task_id,
                lane_id=lane_id,
                decision=decision,
            )
            connection.commit()
        finally:
            connection.close()

    def heartbeat(
        self,
        lease: LeaseHandle,
        *,
        ttl_seconds: int,
        now: datetime,
    ) -> LeaseHandle | None:
        connection = self._connect()
        at = _at(now)
        expires = at + timedelta(seconds=ttl_seconds)
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE leases SET heartbeat_at=?,expires_at=?
                WHERE task_id=? AND owner_uuid=? AND generation=?
                  AND released_at IS NULL AND expires_at > ?
                """,
                (
                    _iso(at),
                    _iso(expires),
                    lease.task_id,
                    lease.owner_uuid,
                    lease.generation,
                    _iso(at),
                ),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return None
            self._audit(
                connection,
                at=at,
                task_id=lease.task_id,
                lane_id=lease.lane_id,
                event_type="LEASE_HEARTBEAT",
                reason=ReasonCode.MET.value,
                decision={"generation": lease.generation},
            )
            connection.commit()
            return replace(lease, expires_at=expires)
        finally:
            connection.close()

    def release(
        self, lease: LeaseHandle, *, outcome: str, now: datetime
    ) -> bool:
        connection = self._connect()
        at = _at(now)
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE leases SET released_at=?
                WHERE task_id=? AND owner_uuid=? AND generation=?
                  AND released_at IS NULL
                """,
                (
                    _iso(at),
                    lease.task_id,
                    lease.owner_uuid,
                    lease.generation,
                ),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return False
            connection.execute(
                """
                UPDATE tasks SET status=?,updated_at=?,terminal_at=?
                WHERE task_id=?
                """,
                (outcome, _iso(at), _iso(at), lease.task_id),
            )
            self._audit(
                connection,
                at=at,
                task_id=lease.task_id,
                lane_id=lease.lane_id,
                event_type="LEASE_RELEASED",
                reason=ReasonCode.RELEASED.value,
                decision={"generation": lease.generation, "outcome": outcome},
            )
            connection.commit()
            return True
        finally:
            connection.close()

    def reap_expired(self, *, now: datetime) -> int:
        if not self.path.exists():
            return 0
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            count = self._reap(connection, _at(now))
            connection.commit()
            return count
        finally:
            connection.close()

    def set_cooldown(
        self,
        lane_id: str,
        *,
        until: datetime,
        reason: str,
        now: datetime,
    ) -> None:
        connection = self._connect()
        at = _at(now)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO lane_state(
                  lane_id,cooldown_until,cooldown_reason,updated_at
                ) VALUES(?,?,?,?)
                ON CONFLICT(lane_id) DO UPDATE SET
                  cooldown_until=excluded.cooldown_until,
                  cooldown_reason=excluded.cooldown_reason,
                  updated_at=excluded.updated_at
                """,
                (lane_id, _iso(until), reason, _iso(at)),
            )
            self._audit(
                connection,
                at=at,
                lane_id=lane_id,
                event_type="COOLDOWN_SET",
                reason=reason,
                decision={"until": _iso(until)},
            )
            connection.commit()
        finally:
            connection.close()

    def cooldown(
        self, lane_id: str, *, now: datetime
    ) -> tuple[datetime, str] | None:
        connection = self._connect_existing()
        if connection is None:
            return None
        try:
            row = connection.execute(
                """
                SELECT cooldown_until,cooldown_reason FROM lane_state
                WHERE lane_id=?
                """,
                (lane_id,),
            ).fetchone()
            if row is None or not row["cooldown_until"]:
                return None
            until = _datetime(row["cooldown_until"])
            if until <= _at(now):
                return None
            return until, row["cooldown_reason"]
        finally:
            connection.close()

    def audit(
        self,
        *,
        task_id: str | None = None,
        reason: str | None = None,
    ) -> list[dict]:
        connection = self._connect_existing()
        if connection is None:
            return []
        try:
            clauses: list[str] = []
            params: list[str] = []
            if task_id is not None:
                clauses.append("task_id=?")
                params.append(task_id)
            if reason is not None:
                clauses.append("reason_code=?")
                params.append(reason)
            where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
            rows = connection.execute(
                f"SELECT * FROM audit_events{where} ORDER BY event_id", params
            ).fetchall()
            return [
                {
                    "event_id": row["event_id"],
                    "event_uuid": row["event_uuid"],
                    "task_id": row["task_id"],
                    "lane_id": row["lane_id"],
                    "at": row["at"],
                    "event_type": row["event_type"],
                    "reason_code": row["reason_code"],
                    "decision": json.loads(row["decision_json"]),
                }
                for row in rows
            ]
        finally:
            connection.close()
