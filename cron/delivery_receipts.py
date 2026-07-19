"""Immutable, append-only receipt ledger for cron platform delivery.

The ledger records evidence about a send attempt; it is intentionally not a retry
queue. A process-wide file lock enforces one receipt per execution/target key.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:  # pragma: no cover - Windows fallback is exercised by platform integration.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


def _target_key(receipt: dict[str, Any]) -> tuple[str, str, str]:
    target = receipt.get("target")
    if not isinstance(target, dict):
        raise ValueError("receipt.target must be a mapping")
    platform = str(target.get("platform") or "").strip().lower()
    chat_id = str(target.get("chat_id") or "").strip()
    thread_id = str(target.get("thread_id") or "").strip()
    if not platform or not chat_id:
        raise ValueError("receipt target requires platform and chat_id")
    return platform, chat_id, thread_id


def _receipt_key(receipt: dict[str, Any]) -> tuple[str, str, str, str]:
    execution_id = str(receipt.get("execution_id") or "").strip()
    if not execution_id:
        raise ValueError("receipt requires execution_id")
    return (execution_id, *_target_key(receipt))


def _recorded_key(record: dict[str, Any]) -> tuple[str, str, str, str] | None:
    try:
        return _receipt_key(record)
    except ValueError:
        return None


def append_receipt(path: Path, receipt: dict[str, Any]) -> dict[str, Any]:
    """Append one receipt unless that execution/target was already recorded.

    Malformed existing lines fail closed. Silently ignoring them could make a
    duplicate send look absent and break the at-most-once audit invariant.
    """
    key = _receipt_key(receipt)
    path.parent.mkdir(parents=True, exist_ok=True)
    created = not path.exists()
    with path.open("a+", encoding="utf-8") as handle:
        if created:
            os.chmod(path, 0o600)
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.seek(0)
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    existing = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"malformed receipt ledger at line {line_number}") from exc
                if isinstance(existing, dict) and _recorded_key(existing) == key:
                    return {"written": False, "reason": "duplicate_execution_target"}

            record = dict(receipt)
            record.setdefault("recorded_at", datetime.now(timezone.utc).isoformat())
            handle.seek(0, os.SEEK_END)
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            return {"written": True, "reason": None}
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def aggregate_execution_state(path: Path, execution_id: str) -> str | None:
    """Return a deterministic execution summary from target-level receipts.

    The receipt ledger remains the source of truth.  An execution-level state is
    only a searchable summary, so an ambiguous or failed target can never be
    masked by a later accepted target in a fan-out.
    """
    if not path.exists():
        return None
    states: set[str] = set()
    with path.open("r", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        try:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    receipt = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"malformed receipt ledger at line {line_number}") from exc
                if not isinstance(receipt, dict):
                    raise ValueError(f"malformed receipt ledger at line {line_number}")
                if str(receipt.get("execution_id") or "") == execution_id:
                    state = receipt.get("state")
                    if isinstance(state, str):
                        states.add(state)
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    for state in ("uncertain_in_flight", "failed", "accepted", "suppressed", "generated"):
        if state in states:
            return state
    return None


def get_delivery_trace(
    receipt_path: Path,
    execution_id: str,
) -> list[dict[str, Any]]:
    """Return the full delivery trace for an execution.

    Each entry is a receipt record with execution_id, state, target,
    message_id (if captured), and recorded_at.  Empty list means no
    receipts found for this execution.

    The returned list is ordered by insertion order (ledger append order).
    """
    result: list[dict[str, Any]] = []
    if not receipt_path.exists():
        return result

    with receipt_path.open("r", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        try:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    receipt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if str(receipt.get("execution_id") or "") == execution_id:
                    result.append(receipt)
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    return result
