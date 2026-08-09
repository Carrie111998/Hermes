"""Default-profile read-only observation loop and bounded memory evidence sink.

This module intentionally has no scheduler, SQLite writer, lifecycle API, or
repair path.  An operator (or an already-approved external cadence) invokes
``collect_once`` and decides how to retain the resulting offline evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import stat
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping

from plugins.agentops.control.collectors.base import collect_all, failed_batch
from plugins.agentops.control.collectors.cron import CronCollector
from plugins.agentops.control.collectors.launchd import LaunchdCollector
from plugins.agentops.control.collectors.logs import LogCollector
from plugins.agentops.control.collectors.processes import ProcessCollector
from plugins.agentops.control.observer_models import (
    CollectionBatch,
    CollectorHealth,
    Target,
    TargetSnapshot,
    Signal,
    LogCursor,
    asset_source_id,
    thaw_value,
    utc_now,
)
from plugins.agentops.control.redaction import RedactionError, contains_secret, verify_redacted_signal
from plugins.agentops.control.registry import FleetRegistry, bootstrap_gateway_registry
from plugins.agentops.control.review_pack import ReviewPack, load_review_pack


_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_DEFAULT_TARGET = "hermes:profile:default:gateway"


class ObservationBoundaryError(ValueError):
    """Raised when untrusted evidence or an untrusted deployment binding appears."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _detached_batch(batch: CollectionBatch) -> dict[str, Any]:
    return {
        "observation_id": batch.observation_id,
        "target_id": batch.target_id,
        "collector": batch.collector,
        "collected_at": batch.collected_at.isoformat(),
        "source_id": batch.source_id,
        "healthy": batch.health.healthy,
        "reason": batch.health.reason,
        "worker_detached": batch.health.worker_detached,
        "signal_count": len(batch.signals),
        "signals": [signal.to_dict() for signal in batch.signals],
    }


@dataclass(frozen=True)
class _StoredBatch:
    batch: CollectionBatch
    record: Mapping[str, Any]
    size_bytes: int


class ObservationLedger:
    """Append-only bounded memory sink for detached, redacted collection runs."""

    def __init__(self, *, max_runs: int = 7 * 24 * 60, max_signals: int = 20_000, max_bytes: int = 32 * 1024 * 1024) -> None:
        if not all(isinstance(value, int) and value > 0 for value in (max_runs, max_signals, max_bytes)):
            raise ValueError("invalid observation ledger budget")
        self.max_runs = max_runs
        self.max_signals = max_signals
        self.max_bytes = max_bytes
        self._records: list[_StoredBatch] = []
        self._signal_count = 0
        self._bytes = 0

    @property
    def bytes_used(self) -> int:
        return self._bytes

    @property
    def signal_count(self) -> int:
        return self._signal_count

    def append(self, batch: CollectionBatch) -> str:
        if not isinstance(batch, CollectionBatch) or not batch.source_id:
            raise ObservationBoundaryError("observation batch source is required")
        try:
            for signal in batch.signals:
                verify_redacted_signal(signal)
            record = _detached_batch(batch)
            encoded = _canonical_json(record)
            if contains_secret(record):
                raise RedactionError("observation evidence contains secret")
        except (RedactionError, TypeError, ValueError, UnicodeError) as exc:
            raise ObservationBoundaryError("observation evidence rejected") from exc
        size = len(encoded)
        if len(self._records) >= self.max_runs or self._signal_count + len(batch.signals) > self.max_signals or self._bytes + size > self.max_bytes:
            raise ObservationBoundaryError("observation ledger budget exceeded")
        # The record is detached before this point; appending cannot be
        # affected by later mutation of a collector's input mapping.
        self._records.append(_StoredBatch(batch=batch, record=MappingProxyType(record), size_bytes=size))
        self._signal_count += len(batch.signals)
        self._bytes += size
        return batch.observation_id

    def batches(self) -> tuple[CollectionBatch, ...]:
        return tuple(item.batch for item in self._records)

    def _records_for_day(self, day: date | datetime | str) -> tuple[_StoredBatch, ...]:
        if isinstance(day, datetime):
            selected = day.astimezone(timezone.utc).date()
        elif isinstance(day, date):
            selected = day
        else:
            selected = date.fromisoformat(day)
        return tuple(item for item in self._records if item.batch.collected_at.astimezone(timezone.utc).date() == selected)

    def daily_summary(self, day: date | datetime | str) -> dict[str, Any]:
        records = self._records_for_day(day)
        healthy = sum(1 for item in records if item.batch.health.healthy)
        reasons: dict[str, int] = {}
        collectors: dict[str, dict[str, int]] = {}
        for item in records:
            batch = item.batch
            if batch.health.reason:
                reasons[batch.health.reason] = reasons.get(batch.health.reason, 0) + 1
            bucket = collectors.setdefault(batch.collector, {"runs": 0, "healthy": 0, "signals": 0})
            bucket["runs"] += 1
            bucket["healthy"] += int(batch.health.healthy)
            bucket["signals"] += len(batch.signals)
        return {
            "day": (day.date() if isinstance(day, datetime) else day).isoformat() if not isinstance(day, str) else day,
            "run_count": len(records),
            "healthy_runs": healthy,
            "unhealthy_runs": len(records) - healthy,
            "signal_count": sum(len(item.batch.signals) for item in records),
            "reasons": dict(sorted(reasons.items())),
            "collectors": {key: collectors[key] for key in sorted(collectors)},
            "authority_mode": "observe_only",
            "automatic_repair": False,
        }

    def terra_input(self, day: date | datetime | str, *, max_items: int = 500, max_bytes: int = 256 * 1024) -> dict[str, Any]:
        if not isinstance(max_items, int) or max_items <= 0 or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError("invalid Terra input budget")
        records: list[dict[str, Any]] = []
        used = 0
        for stored in self._records_for_day(day):
            for signal in stored.batch.signals:
                if len(records) >= max_items:
                    break
                item = {
                    "observation_id": stored.batch.observation_id,
                    "target_id": stored.batch.target_id,
                    "collector": stored.batch.collector,
                    "collected_at": stored.batch.collected_at.isoformat(),
                    "health": stored.batch.health.healthy,
                    "reason": stored.batch.health.reason,
                    "signal": signal.to_dict(),
                }
                size = len(_canonical_json(item))
                if used + size > max_bytes:
                    break
                records.append(item)
                used += size
            if len(records) >= max_items or used >= max_bytes:
                break
        result = {
            "schema_version": 1,
            "purpose": "offline_observation_analysis",
            "authority_mode": "observe_only",
            "actions": [],
            "summary": self.daily_summary(day),
            "evidence": records,
        }
        if contains_secret(result):
            raise ObservationBoundaryError("Terra input redaction gate failed")
        return result


def _asset_is_trusted(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode) and metadata.st_uid == os.getuid() and metadata.st_nlink == 1 and stat.S_IMODE(metadata.st_mode) in {0o600, 0o644}


def _read_trusted_asset(path: Path, *, max_bytes: int = 1024 * 1024) -> bytes:
    """Read one fixed asset through an O_NOFOLLOW descriptor and recheck identity."""
    before = path.lstat()
    if not _asset_is_trusted(path) or before.st_size > max_bytes:
        raise ObservationBoundaryError("deployment asset identity rejected")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.getuid() or opened.st_nlink != 1 or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ObservationBoundaryError("deployment asset identity changed")
        raw = os.read(descriptor, max_bytes + 1)
    finally:
        os.close(descriptor)
    after = path.lstat()
    if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino) or len(raw) > max_bytes:
        raise ObservationBoundaryError("deployment asset changed during read")
    return raw


def _trusted_default_target(registry: FleetRegistry, *, fixed_asset: Path | None = None) -> tuple[Target, Path]:
    target = next((item for item in registry.list_targets() if item.target_id == _DEFAULT_TARGET), None)
    if target is None:
        raise ObservationBoundaryError("default target is not registered")
    labels = target.spec.labels
    if (
        labels.get("g2_scope") != "core"
        or labels.get("profile") != "default"
        or labels.get("service_label") != "ai.hermes.gateway"
        or labels.get("process_marker") != "default"
        or labels.get("process_name_contains") != "python3.11"
        or labels.get("process_observation") != "enabled"
        or not _SHA256.fullmatch(labels.get("command_fingerprint", ""))
    ):
        raise ObservationBoundaryError("default deployment binding is not trusted")
    plist = fixed_asset or next((Path(path) for path in target.spec.observed_paths if path.endswith("ai.hermes.gateway.plist")), None)
    if plist is None or not _asset_is_trusted(plist):
        raise ObservationBoundaryError("default deployment asset is not trusted")
    try:
        raw = _read_trusted_asset(plist)
        try:
            data = plistlib.loads(raw)
            args = data.get("ProgramArguments") if isinstance(data, dict) else None
            label = data.get("Label") if isinstance(data, dict) else None
        except (plistlib.InvalidFileException, ValueError, TypeError):
            decoded = json.loads(raw.decode("utf-8"))
            args = decoded.get("ProgramArguments") if isinstance(decoded, dict) else decoded
            label = decoded.get("Label") if isinstance(decoded, dict) else "ai.hermes.gateway"
        if label != "ai.hermes.gateway" or not isinstance(args, list) or not all(isinstance(item, str) for item in args):
            raise ValueError("deployment asset shape rejected")
        digest = "sha256:" + hashlib.sha256("\x00".join(args).encode("utf-8")).hexdigest()
        if digest != labels.get("command_fingerprint"):
            raise ValueError("deployment fingerprint mismatch")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, plistlib.InvalidFileException, TypeError, ValueError) as exc:
        raise ObservationBoundaryError("default deployment asset contents are not trusted") from exc
    return target, plist


class DefaultObservationLoop:
    """One explicit, bounded observation pass for the default core profile."""

    def __init__(self, *, registry: FleetRegistry, target: Target, ledger: ObservationLedger, collectors: tuple[Any, ...], deadline_seconds: float = 1.0) -> None:
        self.registry = registry
        self.target = target
        self.ledger = ledger
        self.collectors = collectors
        self.deadline_seconds = deadline_seconds
        self._cursors: dict[tuple[str, str], Any] = {}

    @classmethod
    def create(
        cls,
        *,
        registry: FleetRegistry | None = None,
        ledger: ObservationLedger | None = None,
        log_path: Path | None = None,
        cron_source_path: Path | None = None,
        process_iter: Callable[[], Iterable[Any]] | None = None,
        review_pack: ReviewPack | None = None,
    ) -> "DefaultObservationLoop":
        active_pack = review_pack or load_review_pack()
        using_bootstrap = registry is None
        active_registry = registry or bootstrap_gateway_registry()
        fixed_plist = Path("~/Library/LaunchAgents/ai.hermes.gateway.plist").expanduser() if using_bootstrap else None
        target, plist_path = _trusted_default_target(active_registry, fixed_asset=fixed_plist)
        logs_root = Path("~/.hermes/logs").expanduser() if using_bootstrap else Path(target.spec.observed_paths[0])
        selected_log = Path(log_path) if log_path is not None else logs_root / "gateway.log"
        log_spec = active_pack.collectors["logs"]
        process_spec = active_pack.collectors["processes"]
        launchd_spec = active_pack.collectors["launchd"]
        collectors: list[Any] = [
            ProcessCollector(max_items=process_spec.max_items, min_interval_seconds=process_spec.rate_limit_seconds, process_iter=process_iter),
            LaunchdCollector(plist_path, max_bytes=launchd_spec.max_bytes, min_interval_seconds=launchd_spec.rate_limit_seconds),
            LogCollector("logs", selected_log, max_bytes=log_spec.max_bytes, max_lines=log_spec.max_items, min_interval_seconds=log_spec.rate_limit_seconds),
        ]
        if cron_source_path is not None:
            cron_spec = active_pack.collectors["cron"]
            collectors.append(CronCollector.from_json_file(
                Path(cron_source_path),
                required_assertion_ids=tuple(sorted(active_pack.cron_mandatory_assertion_ids)),
                max_bytes=cron_spec.max_bytes,
            ))
        else:
            collectors.append(_UnavailableCronCollector(Path("~/.hermes/cron/status.json").expanduser()))
        return cls(registry=active_registry, target=target, ledger=ledger or ObservationLedger(), collectors=tuple(collectors), deadline_seconds=min(spec.deadline_seconds for spec in active_pack.collectors.values()))

    @property
    def collector_names(self) -> tuple[str, ...]:
        return tuple(collector.name for collector in self.collectors)

    def collect_once(self) -> tuple[CollectionBatch, ...]:
        batches = collect_all(self.target, self.collectors, self._cursors, deadline_seconds=self.deadline_seconds)
        facts: dict[str, Any] = {"collectors": {}}
        for batch in batches:
            self.ledger.append(batch)
            if batch.next_cursor is not None:
                self._cursors[(batch.collector, batch.source_id)] = batch.next_cursor
            if batch.collector == "processes":
                self.registry.record_process_result(batch)
            facts["collectors"][batch.collector] = {"healthy": batch.health.healthy, "reason": batch.health.reason, "signal_count": len(batch.signals)}
        observed_at = max((batch.collected_at for batch in batches), default=utc_now())
        self.registry.record_target_snapshot(TargetSnapshot(target_id=self.target.target_id, observed_at=observed_at, facts=facts))
        return batches

    run_once = collect_once


class _UnavailableCronCollector:
    """Explicit unhealthy evidence when no authorized Cron status asset exists."""

    name = "cron"

    def __init__(self, source_path: Path) -> None:
        self.source_path = source_path
        self.source_id = asset_source_id(source_path)

    def collect(self, target: Target, cursor: LogCursor | None = None) -> CollectionBatch:
        return failed_batch(target, self.name, "cron_source_unconfigured", source_id=self.source_id)
