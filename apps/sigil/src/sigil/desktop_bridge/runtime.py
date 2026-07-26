"""Durable, proposal-only paper runtime for the local Sigil desktop bridge."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 1
CYCLE_SECONDS = 5
CONTROL_ACTIONS = frozenset({"start", "pause", "stop"})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _digest(payload: object) -> str:
    return hashlib.sha256(_canonical(payload)).hexdigest()


def _state_directory() -> Path:
    raw = os.environ.get("SIGIL_DESKTOP_STATE_DIR")
    if not raw:
        raise RuntimeError("SIGIL_DESKTOP_STATE_DIR is required")
    directory = Path(raw)
    if not directory.is_absolute():
        raise RuntimeError("SIGIL_DESKTOP_STATE_DIR must be absolute")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink():
        raise RuntimeError("SIGIL_DESKTOP_STATE_DIR cannot be a symlink")
    return directory


def _initial_state(now: datetime) -> dict[str, Any]:
    timestamp = _timestamp(now)
    return {
        "schema_version": SCHEMA_VERSION,
        "revision": 1,
        "generated_at": timestamp,
        "connection": {
            "status": "connected",
            "last_refresh_at": timestamp,
            "degraded_services": [],
        },
        "environment": "paper",
        "simulation": True,
        "execution_authorized": False,
        "broker_submission_available": False,
        "balances": {"cash": "10000.00", "portfolio_value": "10842.16", "currency": "USD"},
        "positions": [
            {"symbol": "MSFT", "quantity": "1.25", "market_value": "566.00"},
            {"symbol": "NVDA", "quantity": "1.60", "market_value": "276.80"},
        ],
        "automation": {
            "state": "stopped",
            "cycle_count": 0,
            "last_cycle_at": None,
            "next_cycle_at": None,
            "proposal_only": True,
        },
        "proposals": [],
        "executions": [],
        "reconciliation": [],
        "audit": [
            {
                "id": "AUD-RUNTIME-0001",
                "timestamp": timestamp,
                "status": "ready",
                "proposal_id": "—",
                "order_id": "—",
                "evidence_reference": "RUNTIME-BOOT",
                "summary": "Durable paper runtime initialized",
                "details": {"broker_submission_attempted": False, "paper_only": True},
            }
        ],
    }


@contextmanager
def _locked_state() -> Iterator[tuple[Path, dict[str, Any]]]:
    directory = _state_directory()
    state_path = directory / "runtime-state.json"
    lock_path = directory / "runtime-state.lock"
    if state_path.is_symlink() or lock_path.is_symlink():
        raise RuntimeError("paper runtime state files cannot be symlinks")
    with lock_path.open("a+", encoding="utf-8") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if state_path.exists():
            envelope = json.loads(state_path.read_text(encoding="utf-8"))
            payload = envelope.get("payload")
            if (
                not isinstance(payload, dict)
                or envelope.get("sha256") != _digest(payload)
                or payload.get("schema_version") != SCHEMA_VERSION
            ):
                raise RuntimeError("paper runtime state integrity validation failed")
        else:
            payload = _initial_state(_now())
        yield state_path, payload


def _persist(state_path: Path, payload: dict[str, Any]) -> None:
    envelope = {"payload": payload, "sha256": _digest(payload)}
    descriptor, temporary = tempfile.mkstemp(prefix=".runtime-state.", dir=state_path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(_canonical(envelope))
            output.write(b"\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, state_path)
        directory_fd = os.open(state_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _run_due_cycle(state: dict[str, Any], now: datetime) -> None:
    automation = state["automation"]
    if automation["state"] != "running":
        return
    next_cycle = automation.get("next_cycle_at")
    if next_cycle and datetime.fromisoformat(next_cycle.replace("Z", "+00:00")) > now:
        return

    sequence = int(automation["cycle_count"]) + 1
    timestamp = _timestamp(now)
    proposal_id = f"PRP-PAPER-{sequence:06d}"
    evidence_id = f"HERMES-PAPER-{sequence:06d}"
    symbols = ("MSFT", "NVDA", "AAPL")
    symbol = symbols[(sequence - 1) % len(symbols)]
    state["proposals"].insert(
        0,
        {
            "id": proposal_id,
            "symbol": symbol,
            "side": "BUY",
            "quantity": 0.05,
            "estimated_notional": "25.00",
            "strategy": "Hermes governed paper analysis",
            "status": "paper-simulated",
            "evidence_references": [evidence_id],
            "risk_results": ["Paper cap passed", "Broker execution disabled"],
        },
    )
    cash = Decimal(state["balances"]["cash"]) - Decimal("25.00")
    state["balances"]["cash"] = f"{cash:.2f}"
    position = next((item for item in state["positions"] if item["symbol"] == symbol), None)
    if position:
        position["quantity"] = f"{Decimal(position['quantity']) + Decimal('0.05'):.2f}"
        position["market_value"] = f"{Decimal(position['market_value']) + Decimal('25.00'):.2f}"
    else:
        state["positions"].append({"symbol": symbol, "quantity": "0.05", "market_value": "25.00"})
    order_id = f"PAPER-ORD-{sequence:06d}"
    state["executions"].insert(
        0,
        {
            "id": f"PAPER-RCT-{sequence:06d}",
            "order_id": order_id,
            "proposal_id": proposal_id,
            "symbol": symbol,
            "status": "local-paper-fill",
            "state": "simulated",
            "timestamp": timestamp,
            "broker_submission_attempted": False,
        },
    )
    state["reconciliation"].insert(
        0,
        {
            "order_id": order_id,
            "status": "reconciled-local-paper",
            "required": False,
            "automatic_retry_allowed": False,
            "timestamp": timestamp,
        },
    )
    state["audit"].insert(
        0,
        {
            "id": f"AUD-PAPER-{sequence:06d}",
            "timestamp": timestamp,
            "status": "proposed",
            "proposal_id": proposal_id,
            "order_id": "—",
            "evidence_reference": evidence_id,
            "summary": "Hermes analysis produced a governed paper proposal",
            "details": {
                "analysis_only": True,
                "approval_created": False,
                "local_paper_fill": True,
                "broker_submission_attempted": False,
            },
        },
    )
    automation.update(
        {
            "cycle_count": sequence,
            "last_cycle_at": timestamp,
            "next_cycle_at": _timestamp(
                now.replace(microsecond=0) + timedelta(seconds=CYCLE_SECONDS)
            ),
        }
    )


def runtime_snapshot(*, now: datetime | None = None) -> dict[str, Any]:
    observed_at = now or _now()
    with _locked_state() as (state_path, state):
        _run_due_cycle(state, observed_at)
        state["revision"] = int(state["revision"]) + 1
        state["generated_at"] = _timestamp(observed_at)
        state["connection"]["last_refresh_at"] = _timestamp(observed_at)
        _persist(state_path, state)
        return json.loads(json.dumps(state))


def control_paper_cycle(action: object, *, now: datetime | None = None) -> dict[str, Any]:
    if action not in CONTROL_ACTIONS:
        raise ValueError("paper automation action must be start, pause, or stop")
    observed_at = now or _now()
    with _locked_state() as (state_path, state):
        automation = state["automation"]
        if action == "start":
            automation["state"] = "running"
            automation["next_cycle_at"] = _timestamp(observed_at)
        elif action == "pause":
            automation["state"] = "paused"
            automation["next_cycle_at"] = None
        else:
            automation.update({"state": "stopped", "next_cycle_at": None})
        state["revision"] = int(state["revision"]) + 1
        state["generated_at"] = _timestamp(observed_at)
        state["audit"].insert(
            0,
            {
                "id": f"AUD-CONTROL-{state['revision']:06d}",
                "timestamp": _timestamp(observed_at),
                "status": str(action),
                "proposal_id": "—",
                "order_id": "—",
                "evidence_reference": "PAPER-AUTOMATION",
                "summary": f"Paper automation {action} recorded",
                "details": {"broker_submission_attempted": False, "proposal_only": True},
            },
        )
        _persist(state_path, state)
        return json.loads(json.dumps(state))
