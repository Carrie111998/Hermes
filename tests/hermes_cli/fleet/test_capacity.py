from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from hermes_cli.fleet.capacity import BridgeUsageAdapter
from hermes_cli.fleet.types import Freshness, ReasonCode


NOW = datetime(2026, 7, 24, 0, 0, tzinfo=timezone.utc)


def _payload(**lane_overrides: object) -> dict[str, object]:
    lane = {
        "used_pct": "25.000",
        "remaining_pct": "75.000",
        "confidence": "high",
        "overage_disabled": True,
    }
    lane.update(lane_overrides)
    return {
        "schema_version": "1",
        "captured_at": "2026-07-23T23:30:00Z",
        "lanes": {"chatgpt_codex": lane},
    }


def _write(path, payload: object) -> bytes:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    path.write_bytes(raw)
    return raw


def test_bridge_normalizes_attributable_per_lane_capacity_without_writing(tmp_path):
    path = tmp_path / "usage-weekly.json"
    raw = _write(path, _payload())
    before = hashlib.sha256(raw).hexdigest()

    result = BridgeUsageAdapter(path, max_age=timedelta(hours=2)).read(
        "chatgpt_codex", now=NOW, reserved_pct=Decimal("5")
    )

    assert result.reason is None
    assert result.snapshot is not None
    assert result.snapshot.used_pct == Decimal("25.000")
    assert result.snapshot.remaining_pct == Decimal("75.000")
    assert result.snapshot.reserved_pct == Decimal("5.000")
    assert result.snapshot.effective_remaining_pct == Decimal("70.000")
    assert result.snapshot.freshness is Freshness.FRESH
    assert result.snapshot.source_id.endswith(f"#{before}")
    assert result.snapshot.read_at == NOW
    assert result.snapshot.expires_at.isoformat() == "2026-07-24T01:30:00+00:00"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (None, ReasonCode.CAPACITY_MISSING),
        ("{", ReasonCode.CAPACITY_INVALID),
        ({"schema_version": "2", "lanes": {}}, ReasonCode.CAPACITY_INVALID),
        (_payload(used_pct=25.0), ReasonCode.CAPACITY_INVALID),
        (_payload(used_pct="NaN"), ReasonCode.CAPACITY_INVALID),
        (_payload(used_pct="-1"), ReasonCode.CAPACITY_INVALID),
        (_payload(remaining_pct="101"), ReasonCode.CAPACITY_INVALID),
        (_payload(remaining_pct="70"), ReasonCode.CAPACITY_INVALID),
        (_payload(confidence="unknown"), ReasonCode.CAPACITY_INVALID),
        (_payload(overage_disabled=None), ReasonCode.CAPACITY_INVALID),
    ],
)
def test_bridge_fails_closed_for_missing_or_malformed_lane_evidence(
    tmp_path, payload, reason
):
    path = tmp_path / "usage-weekly.json"
    if payload is None:
        pass
    elif payload == "{":
        path.write_text(payload, encoding="utf-8")
    else:
        _write(path, payload)

    result = BridgeUsageAdapter(path, max_age=timedelta(hours=2)).read(
        "chatgpt_codex", now=NOW
    )

    assert result.snapshot is None
    assert result.reason is reason


def test_bridge_reports_only_the_missing_lane(tmp_path):
    path = tmp_path / "usage-weekly.json"
    _write(path, _payload())

    result = BridgeUsageAdapter(path).read("claude_code", now=NOW)

    assert result.snapshot is None
    assert result.reason is ReasonCode.CAPACITY_MISSING
    assert "claude_code" in result.detail


def test_bridge_marks_expired_samples_stale_with_provenance(tmp_path):
    path = tmp_path / "usage-weekly.json"
    _write(
        path,
        {
            **_payload(),
            "captured_at": "2026-07-23T20:00:00Z",
        },
    )

    result = BridgeUsageAdapter(path, max_age=timedelta(hours=2)).read(
        "chatgpt_codex", now=NOW
    )

    assert result.snapshot is not None
    assert result.snapshot.freshness is Freshness.STALE
    assert result.reason is ReasonCode.CAPACITY_STALE


def test_bridge_rejects_future_duplicate_and_oversized_documents(tmp_path):
    future = tmp_path / "future.json"
    _write(future, {**_payload(), "captured_at": "2026-07-24T00:05:00Z"})
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(
        '{"schema_version":"1","captured_at":"2026-07-23T23:30:00Z",'
        '"lanes":{"chatgpt_codex":{"used_pct":"25","remaining_pct":"75",'
        '"confidence":"high","overage_disabled":true},'
        '"chatgpt_codex":{"used_pct":"25","remaining_pct":"75",'
        '"confidence":"high","overage_disabled":true}}}',
        encoding="utf-8",
    )
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b" " * 1025)

    assert (
        BridgeUsageAdapter(future).read("chatgpt_codex", now=NOW).reason
        is ReasonCode.CAPACITY_INVALID
    )
    assert (
        BridgeUsageAdapter(duplicate).read("chatgpt_codex", now=NOW).reason
        is ReasonCode.CAPACITY_INVALID
    )
    assert (
        BridgeUsageAdapter(oversized, max_bytes=1024)
        .read("chatgpt_codex", now=NOW)
        .reason
        is ReasonCode.CAPACITY_INVALID
    )


def test_capacity_snapshot_is_immutable(tmp_path):
    path = tmp_path / "usage-weekly.json"
    _write(path, _payload())
    snapshot = BridgeUsageAdapter(path).read("chatgpt_codex", now=NOW).snapshot

    with pytest.raises((AttributeError, TypeError)):
        snapshot.remaining_pct = Decimal("0")  # type: ignore[misc]
