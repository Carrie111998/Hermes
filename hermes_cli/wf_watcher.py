"""Gateway workflow watcher: timers, state probes, and intake recovery.

The watcher is deliberately tenant-neutral.  Tenant integrations register a
read-only probe which receives immutable snapshots of open workflow instances
and returns structured observations.  The shared frame owns event ingestion,
matching, application, timer catch-up, and sweep recovery.
"""

from __future__ import annotations

import inspect
import sqlite3
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping

from hermes_cli import wf_engine


@dataclass(frozen=True)
class ProbeTarget:
    """Immutable input handed to a tenant state probe."""

    task_id: str
    tenant: str
    template_id: str
    step_key: str
    entity_key: str
    corr: Mapping[str, Any]
    vars: Mapping[str, Any]
    parked_since: int | None


@dataclass(frozen=True)
class ProbeObservation:
    """One structured, deduplicable observation returned by a probe."""

    external_id: str
    event_type: str
    corr: Mapping[str, Any]
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WatchTickResult:
    """Countable output of one watcher tick."""

    timers_fired: tuple[int, ...] = ()
    timer_results: tuple[str, ...] = ()
    poll_events: tuple[int, ...] = ()
    poll_duplicates: int = 0
    sweep_processed: int = 0
    applied_events: tuple[int, ...] = ()


StateProbe = Callable[
    [tuple[ProbeTarget, ...]],
    Iterable[ProbeObservation | Mapping[str, Any]],
]

_STATE_PROBES: dict[str, StateProbe] = {}


def register_state_probe(
    tenant: str,
    probe: StateProbe,
    *,
    read_only: bool,
) -> None:
    """Register one tenant probe under the E7 read-only contract.

    The callback receives values only: no SQLite connection, engine handle, or
    mutation function.  ``read_only=True`` is an explicit capability
    declaration and registrations that do not make it are refused.
    """

    normalized = str(tenant).strip()
    if not normalized:
        raise ValueError("workflow state probe tenant is required")
    if not callable(probe):
        raise TypeError("workflow state probe must be callable")
    if read_only is not True:
        raise ValueError("workflow state probes must declare read_only=True")
    parameters = tuple(inspect.signature(probe).parameters.values())
    if len(parameters) != 1:
        raise TypeError("workflow state probe must accept exactly one targets argument")
    _STATE_PROBES[normalized] = probe


def unregister_state_probe(tenant: str) -> None:
    """Remove a tenant probe, primarily for plugin unload and tests."""

    _STATE_PROBES.pop(str(tenant).strip(), None)


def registered_state_probes() -> tuple[str, ...]:
    """Return registered tenants without exposing callbacks."""

    return tuple(sorted(_STATE_PROBES))


def _json_object(raw: str | None) -> dict[str, Any]:
    value = wf_engine._load_json(raw, {}) or {}
    return value if isinstance(value, dict) else {}


def _probe_targets(conn: sqlite3.Connection) -> dict[str, tuple[ProbeTarget, ...]]:
    rows = conn.execute(
        """
        SELECT i.task_id, i.entity_key, i.template_id, i.corr, i.vars,
               i.parked_since, t.tenant, t.current_step_key
          FROM wf_instance i
          JOIN tasks t ON t.id = i.task_id
         WHERE i.state != 'done'
           AND t.current_step_key IS NOT NULL
         ORDER BY t.tenant, i.task_id
        """
    ).fetchall()
    grouped: dict[str, list[ProbeTarget]] = {}
    for row in rows:
        tenant = str(row["tenant"] or "").strip()
        if tenant not in _STATE_PROBES:
            continue
        grouped.setdefault(tenant, []).append(
            ProbeTarget(
                task_id=row["task_id"],
                tenant=tenant,
                template_id=row["template_id"],
                step_key=row["current_step_key"],
                entity_key=row["entity_key"],
                corr=MappingProxyType(_json_object(row["corr"])),
                vars=MappingProxyType(_json_object(row["vars"])),
                parked_since=(
                    int(row["parked_since"])
                    if row["parked_since"] is not None
                    else None
                ),
            )
        )
    return {tenant: tuple(targets) for tenant, targets in grouped.items()}


def _observation(value: ProbeObservation | Mapping[str, Any]) -> ProbeObservation:
    if isinstance(value, ProbeObservation):
        result = value
    elif isinstance(value, Mapping):
        result = ProbeObservation(
            external_id=str(value.get("external_id") or ""),
            event_type=str(value.get("event_type") or ""),
            corr=value.get("corr") or {},
            payload=value.get("payload") or {},
        )
    else:
        raise TypeError("workflow state probe observations must be mappings")
    if not result.external_id or not result.event_type:
        raise ValueError("workflow state probe observation needs external_id and event_type")
    if not isinstance(result.corr, Mapping) or not isinstance(result.payload, Mapping):
        raise TypeError("workflow state probe corr and payload must be mappings")
    return result


def _drive_matched_event(
    conn: sqlite3.Connection,
    event_id: int,
) -> bool:
    result = wf_engine.match_event(conn, int(event_id))
    if result.kind != "matched" or not result.task_id:
        return False
    task = conn.execute(
        "SELECT current_step_key FROM tasks WHERE id = ?", (result.task_id,)
    ).fetchone()
    if task is None or not task["current_step_key"]:
        return False
    applied = wf_engine.apply_event(
        conn,
        int(event_id),
        result.task_id,
        expected_step=task["current_step_key"],
    )
    return applied.kind == "applied"


def run_tick(conn: sqlite3.Connection, now: int) -> WatchTickResult:
    """Run one complete timer/probe/sweeper cycle against one board."""

    timers = tuple(wf_engine.fire_due_timers(conn, int(now)))
    timer_results: list[str] = []
    applied: list[int] = []
    for event_id in timers:
        result = wf_engine.process_timer_event(conn, event_id)
        timer_results.append(result.kind)
        if result.kind in {"applied", "chase", "exception"}:
            applied.append(event_id)

    poll_events: list[int] = []
    poll_duplicates = 0
    # Probe calls happen outside a write transaction and receive immutable
    # values only.  A slow tenant probe cannot hold the SQLite writer lock.
    for tenant, targets in _probe_targets(conn).items():
        observations = _STATE_PROBES[tenant](targets)
        for raw in observations or ():
            observation = _observation(raw)
            event_id = wf_engine.ingest_event(
                conn,
                source="state_poll",
                external_id=observation.external_id,
                payload=dict(observation.payload),
                corr=dict(observation.corr),
                event_type=observation.event_type,
            )
            if event_id is None:
                poll_duplicates += 1
                continue
            poll_events.append(event_id)
            if _drive_matched_event(conn, event_id):
                applied.append(event_id)

    swept = wf_engine.sweep(conn, int(now))
    for event_id in swept.matched_ids:
        if _drive_matched_event(conn, event_id):
            applied.append(event_id)
    return WatchTickResult(
        timers_fired=timers,
        timer_results=tuple(timer_results),
        poll_events=tuple(poll_events),
        poll_duplicates=poll_duplicates,
        sweep_processed=swept.processed,
        applied_events=tuple(dict.fromkeys(applied)),
    )


__all__ = [
    "ProbeObservation",
    "ProbeTarget",
    "StateProbe",
    "WatchTickResult",
    "register_state_probe",
    "registered_state_probes",
    "run_tick",
    "unregister_state_probe",
]
