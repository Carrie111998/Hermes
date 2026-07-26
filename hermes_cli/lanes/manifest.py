"""Validated YAML lane manifest with canonical hash audit."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from hermes_constants import get_default_hermes_root
from hermes_cli.lanes import schema
from hermes_cli.sqlite_util import retrying_write_txn


class LaneManifestError(ValueError):
    pass


@dataclass(frozen=True)
class LaneConfig:
    lane_id: str
    enabled: bool
    module: str
    approval_channel: str
    approval_timeout_hours: int
    per_lane_daily_cost_cap_aud: float
    per_lane_daily_task_cap: int
    per_lane_hourly_ingest_cap: int
    publish_enabled: bool
    description: str


@dataclass(frozen=True)
class LaneManifest:
    lanes: tuple[LaneConfig, ...]
    canonical_json: str
    manifest_hash: str

    def by_id(self) -> dict[str, LaneConfig]:
        return {lane.lane_id: lane for lane in self.lanes}


def default_path() -> Path:
    return get_default_hermes_root() / "lane_manifest.yaml"


def validate_manifest(raw: Any) -> LaneManifest:
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise LaneManifestError("lane manifest schema_version must be 1")
    items = raw.get("lanes")
    if not isinstance(items, list) or not items:
        raise LaneManifestError("lanes must be a non-empty list")
    lanes = []
    seen = set()
    canonical = []
    for item in items:
        if not isinstance(item, dict):
            raise LaneManifestError("each lane must be a mapping")
        lane_id = str(item.get("lane_id") or "").strip().lower()
        if not lane_id or lane_id in seen:
            raise LaneManifestError(f"invalid or duplicate lane_id: {lane_id}")
        seen.add(lane_id)
        channel = str(item.get("approval_channel") or "")
        if channel not in {"telegram", "dashboard"}:
            raise LaneManifestError(f"unsupported approval channel: {channel}")
        values = {
            "lane_id": lane_id,
            "enabled": bool(item.get("enabled", False)),
            "module": str(item.get("module") or "").strip(),
            "approval_channel": channel,
            "approval_timeout_hours": int(item["approval_timeout_hours"]),
            "per_lane_daily_cost_cap_aud": float(
                item["per_lane_daily_cost_cap_aud"]
            ),
            "per_lane_daily_task_cap": int(item["per_lane_daily_task_cap"]),
            "per_lane_hourly_ingest_cap": int(
                item["per_lane_hourly_ingest_cap"]
            ),
            "publish_enabled": bool(item.get("publish_enabled", False)),
            "description": str(item.get("description") or ""),
        }
        if not values["module"] or any(
            values[key] <= 0
            for key in (
                "approval_timeout_hours",
                "per_lane_daily_cost_cap_aud",
                "per_lane_daily_task_cap",
                "per_lane_hourly_ingest_cap",
            )
        ):
            raise LaneManifestError(f"invalid lane limits/module: {lane_id}")
        canonical.append(values)
        lanes.append(LaneConfig(**values))
    encoded = json.dumps(
        {"schema_version": 1, "lanes": canonical},
        sort_keys=True,
        separators=(",", ":"),
    )
    return LaneManifest(
        tuple(lanes),
        encoded,
        hashlib.sha256(encoded.encode()).hexdigest(),
    )


def _record(
    manifest: LaneManifest,
    *,
    db_path: str | Path | None,
    applied_by: str,
) -> None:
    schema.ensure_migrated(db_path)
    conn = schema.connect(db_path)
    try:
        with retrying_write_txn(conn):
            active = conn.execute(
                "SELECT id,manifest_hash FROM lane_manifest_state "
                "WHERE is_active=1"
            ).fetchone()
            if active and active["manifest_hash"] == manifest.manifest_hash:
                return
            version = conn.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM lane_manifest_state"
            ).fetchone()[0]
            conn.execute(
                "UPDATE lane_manifest_state SET is_active=0 WHERE is_active=1"
            )
            conn.execute(
                """INSERT INTO lane_manifest_state(
                  version,applied_at,applied_by,manifest_hash,manifest_json,
                  is_active) VALUES(?,?,?,?,?,1)""",
                (
                    version,
                    datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    applied_by,
                    manifest.manifest_hash,
                    manifest.canonical_json,
                ),
            )
    finally:
        conn.close()


def load_manifest(
    path: str | Path | None = None,
    *,
    db_path: str | Path | None = None,
    record_state: bool = True,
    applied_by: str = "hermes lanes",
) -> LaneManifest:
    source = Path(path).expanduser() if path else default_path()
    try:
        result = validate_manifest(yaml.safe_load(source.read_text()))
    except (OSError, yaml.YAMLError) as exc:
        raise LaneManifestError(f"cannot load lane manifest: {exc}") from exc
    if record_state:
        _record(result, db_path=db_path, applied_by=applied_by)
    return result


__all__ = [
    "LaneConfig",
    "LaneManifest",
    "LaneManifestError",
    "default_path",
    "load_manifest",
    "validate_manifest",
]
