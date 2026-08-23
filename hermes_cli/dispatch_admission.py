"""Fail-closed admission receipts for autonomous Kanban dispatch."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional


SCHEMA = "aos.dispatch_admission.v1"
REQUIRED_GATES = (
    "hook_health",
    "hermes_canary",
    "source_installed_hashes",
    "router_acceptance",
    "telemetry_coverage",
    "github_broker_readback",
    "quota_state",
    "worker_count",
)
VALID_CLASSES = {"cloud_priority", "local_only"}


class DispatchAdmissionError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class DispatchAdmission:
    receipt_id: str
    allowed_classes: frozenset[str]
    max_workers: int
    cloud_concurrency: int
    running_workers: int
    running_cloud_workers: int
    expires_at: datetime


def _timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise DispatchAdmissionError(f"missing_{field}")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise DispatchAdmissionError(f"invalid_{field}") from exc
    if parsed.tzinfo is None:
        raise DispatchAdmissionError(f"invalid_{field}")
    return parsed.astimezone(timezone.utc)


def _positive_int(value: Any, field: str, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if type(value) is not int or value < minimum:
        raise DispatchAdmissionError(f"invalid_{field}")
    return value


def validate_dispatch_admission(
    value: Any,
    *,
    now: Optional[datetime] = None,
    maximum_workers: int = 5,
) -> DispatchAdmission:
    if not isinstance(value, Mapping) or value.get("schema") != SCHEMA:
        raise DispatchAdmissionError("invalid_schema")
    if value.get("status") != "pass":
        raise DispatchAdmissionError("gate_failed")
    receipt_id = str(value.get("receipt_id") or "").strip()
    if not receipt_id or len(receipt_id) > 160:
        raise DispatchAdmissionError("invalid_receipt_id")

    issued_at = _timestamp(value.get("issued_at"), "issued_at")
    expires_at = _timestamp(value.get("expires_at"), "expires_at")
    current = now or datetime.now(timezone.utc)
    if expires_at <= issued_at or expires_at <= current:
        raise DispatchAdmissionError("stale_receipt")
    if (expires_at - issued_at).total_seconds() > 300:
        raise DispatchAdmissionError("ttl_exceeds_five_minutes")
    if issued_at > current and (issued_at - current).total_seconds() > 60:
        raise DispatchAdmissionError("future_receipt")

    gates = value.get("gates")
    if not isinstance(gates, Mapping):
        raise DispatchAdmissionError("missing_gates")
    for gate in REQUIRED_GATES:
        state = gates.get(gate)
        passed = state is True or (
            isinstance(state, Mapping) and state.get("status") == "pass"
        )
        if not passed:
            raise DispatchAdmissionError(f"gate_failed_{gate}")

    classes_raw = value.get("allowed_classes")
    if not isinstance(classes_raw, list):
        raise DispatchAdmissionError("invalid_allowed_classes")
    allowed = frozenset(str(item) for item in classes_raw)
    if not allowed or allowed - VALID_CLASSES:
        raise DispatchAdmissionError("invalid_allowed_classes")

    max_workers = _positive_int(value.get("max_workers"), "max_workers")
    if max_workers > maximum_workers:
        raise DispatchAdmissionError("max_workers_exceeds_policy")
    cloud_concurrency = _positive_int(
        value.get("cloud_concurrency"), "cloud_concurrency", allow_zero=True
    )
    if cloud_concurrency > max_workers:
        raise DispatchAdmissionError("cloud_concurrency_exceeds_workers")
    running_workers = _positive_int(
        value.get("running_workers"), "running_workers", allow_zero=True
    )
    running_cloud_workers = _positive_int(
        value.get("running_cloud_workers"),
        "running_cloud_workers",
        allow_zero=True,
    )
    if running_cloud_workers > running_workers:
        raise DispatchAdmissionError("cloud_workers_exceed_total")

    return DispatchAdmission(
        receipt_id=receipt_id,
        allowed_classes=allowed,
        max_workers=max_workers,
        cloud_concurrency=cloud_concurrency,
        running_workers=running_workers,
        running_cloud_workers=running_cloud_workers,
        expires_at=expires_at,
    )


def load_dispatch_admission(
    path: Optional[str], *, maximum_workers: int = 5
) -> DispatchAdmission:
    if not path:
        raise DispatchAdmissionError("receipt_path_missing")
    receipt_path = Path(path).expanduser()
    try:
        value = json.loads(receipt_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DispatchAdmissionError("receipt_missing") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise DispatchAdmissionError("receipt_unreadable") from exc
    return validate_dispatch_admission(value, maximum_workers=maximum_workers)
