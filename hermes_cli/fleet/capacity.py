"""Read-only capacity evidence adapters and normalization."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .types import CapacityRead, CapacitySnapshot, Confidence, Freshness, ReasonCode


_QUANTUM = Decimal("0.001")
_TOTAL = Decimal("100.000")


class _DuplicateKey(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(key)
        result[key] = value
    return result


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _percentage(value: object) -> Decimal:
    # JSON numbers become int/float; string-only percentages prevent binary
    # float ambiguity and make non-finite values explicit failures.
    if not isinstance(value, str):
        raise ValueError("percentage must be a decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("invalid decimal") from exc
    if not parsed.is_finite() or parsed < 0 or parsed > 100:
        raise ValueError("percentage outside [0, 100]")
    return parsed.quantize(_QUANTUM)


class BridgeUsageAdapter:
    """Normalize an optional HermesBridge JSON file without mutating it."""

    def __init__(
        self,
        path: str | Path = "C:/HermesBridge/usage-weekly.json",
        *,
        max_age: timedelta = timedelta(hours=2),
        max_bytes: int = 256 * 1024,
        future_tolerance: timedelta = timedelta(seconds=30),
    ) -> None:
        self.path = Path(path)
        self.max_age = max_age
        self.max_bytes = max_bytes
        self.future_tolerance = future_tolerance

    def read(
        self,
        lane_id: str,
        *,
        now: datetime | None = None,
        reserved_pct: Decimal = Decimal("0"),
    ) -> CapacityRead:
        read_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        try:
            with self.path.open("rb") as handle:
                before = handle.seek(0, 2)
                if before > self.max_bytes:
                    raise ValueError("document exceeds size limit")
                handle.seek(0)
                raw = handle.read(self.max_bytes + 1)
                if len(raw) > self.max_bytes:
                    raise ValueError("document exceeds size limit")
        except FileNotFoundError:
            return CapacityRead(
                None, ReasonCode.CAPACITY_MISSING, f"{lane_id}: bridge file absent"
            )
        except (OSError, ValueError) as exc:
            return CapacityRead(None, ReasonCode.CAPACITY_INVALID, str(exc))

        try:
            document = json.loads(raw, object_pairs_hook=_unique_object)
            if not isinstance(document, dict) or document.get("schema_version") != "1":
                raise ValueError("unsupported bridge schema")
            captured_at = _parse_timestamp(document.get("captured_at"))
            if captured_at > read_at + self.future_tolerance:
                raise ValueError("capture timestamp is in the future")
            lanes = document.get("lanes")
            if not isinstance(lanes, dict):
                raise ValueError("lanes must be an object")
            lane = lanes.get(lane_id)
            if lane is None:
                return CapacityRead(
                    None,
                    ReasonCode.CAPACITY_MISSING,
                    f"{lane_id}: no bridge evidence",
                )
            if not isinstance(lane, dict):
                raise ValueError("lane evidence must be an object")
            used = _percentage(lane.get("used_pct"))
            remaining = _percentage(lane.get("remaining_pct"))
            if abs((used + remaining) - _TOTAL) > _QUANTUM:
                raise ValueError("used and remaining percentages disagree")
            confidence = Confidence(lane.get("confidence"))
            overage_disabled = lane.get("overage_disabled")
            if not isinstance(overage_disabled, bool):
                raise ValueError("overage_disabled must be explicit")
            reserved = _percentage(str(reserved_pct))
            effective = max(Decimal("0"), remaining - reserved).quantize(_QUANTUM)
            expires_at = captured_at + self.max_age
            freshness = (
                Freshness.FRESH if read_at <= expires_at else Freshness.STALE
            )
            digest = hashlib.sha256(raw).hexdigest()
            snapshot = CapacitySnapshot(
                lane_id=lane_id,
                used_pct=used,
                remaining_pct=remaining,
                reserved_pct=reserved,
                effective_remaining_pct=effective,
                source_kind="bridge_file",
                source_id=f"bridge_file:{self.path}#{digest}",
                captured_at=captured_at,
                read_at=read_at,
                expires_at=expires_at,
                freshness=freshness,
                confidence=confidence,
                schema_version="1",
                overage_disabled=overage_disabled,
            )
            reason = (
                None if freshness is Freshness.FRESH else ReasonCode.CAPACITY_STALE
            )
            return CapacityRead(snapshot, reason)
        except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
            return CapacityRead(None, ReasonCode.CAPACITY_INVALID, str(exc))
