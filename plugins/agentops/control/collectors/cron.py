"""Cron evidence where execution completion is never business success alone."""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime
from pathlib import Path
import threading
import time

from plugins.agentops.control.collectors.base import failed_batch
from plugins.agentops.control.observer_models import (
    BusinessAssertion,
    CollectionBatch,
    CollectorHealth,
    CronExecution,
    CronObservation,
    LogCursor,
    RawSignal,
    Target,
    asset_source_id,
    target_allows_asset,
    thaw_value,
    utc_now,
)
from plugins.agentops.control.redaction import redact_signal


class CronCollector:
    name = "cron"

    def __init__(
        self,
        observation: CronObservation,
        *,
        source_path: Path,
        required_assertion_ids: tuple[str, ...] = (),
        max_assertions: int = 32,
        max_bytes: int = 64 * 1024,
        min_interval_seconds: float = 0.0,
    ) -> None:
        if not isinstance(observation, CronObservation):
            raise ValueError("cron collector requires a detached observation")
        if (
            not all(isinstance(item, str) and item for item in required_assertion_ids)
            or max_assertions <= 0
            or max_bytes <= 0
            or min_interval_seconds < 0
        ):
            raise ValueError("invalid cron assertion ids")
        self._observation = observation
        self.source_path = Path(source_path)
        self.source_id = asset_source_id(self.source_path)
        self.required_assertion_ids = frozenset(required_assertion_ids)
        self.max_assertions = max_assertions
        self.max_bytes = max_bytes
        self.min_interval_seconds = min_interval_seconds
        self._last_collection = 0.0
        self._rate_lock = threading.Lock()

    @classmethod
    def from_json_file(
        cls,
        source_path: Path,
        *,
        required_assertion_ids: tuple[str, ...],
        max_bytes: int = 64 * 1024,
    ) -> "CronCollector":
        """Create an observation from a bounded, regular JSON status file."""
        path = Path(source_path)
        try:
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) or metadata.st_size > max_bytes:
                raise ValueError("source rejected")
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                raw = os.read(descriptor, max_bytes + 1)
            finally:
                os.close(descriptor)
            data = json.loads(raw.decode("utf-8"))
            execution_data = data["execution"]
            assertions_data = data.get("assertions", [])
            execution = CronExecution(
                job_id=execution_data["job_id"],
                observed_at=datetime.fromisoformat(execution_data["observed_at"].replace("Z", "+00:00")),
                exit_code=execution_data.get("exit_code"),
                completed=execution_data["completed"],
            )
            assertions = tuple(
                BusinessAssertion(
                    name=item["name"],
                    passed=item["passed"],
                    evidence=item.get("evidence", {}),
                    observed_at=datetime.fromisoformat(item["observed_at"].replace("Z", "+00:00")),
                    max_age_seconds=item.get("max_age_seconds", 300),
                    mandatory=item.get("mandatory", True),
                    severity=item.get("severity", "warning"),
                )
                for item in assertions_data
            )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("invalid cron status source") from exc
        return cls(
            CronObservation(execution, assertions),
            source_path=path,
            required_assertion_ids=required_assertion_ids,
            max_bytes=max_bytes,
        )

    @staticmethod
    def _is_fresh(assertion: BusinessAssertion, observed_at: datetime) -> bool:
        if assertion.observed_at is None:
            return False
        age = (observed_at - assertion.observed_at).total_seconds()
        return 0 <= age <= assertion.max_age_seconds

    def collect(self, target: Target, cursor: LogCursor | None = None) -> CollectionBatch:
        if not target_allows_asset(target, self.source_path):
            return failed_batch(target, self.name, "asset_unbound", source_id=self.source_id)
        try:
            metadata = self.source_path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) or metadata.st_size > self.max_bytes:
                return failed_batch(target, self.name, "cron_source_rejected", source_id=self.source_id)
        except OSError:
            return failed_batch(target, self.name, "cron_source_rejected", source_id=self.source_id)
        with self._rate_lock:
            now = time.monotonic()
            if now - self._last_collection < self.min_interval_seconds:
                return failed_batch(target, self.name, "collector_rate_limited", source_id=self.source_id)
            self._last_collection = now
        observation = self._observation
        if len(observation.assertions) > self.max_assertions:
            return failed_batch(target, self.name, "cron_assertion_budget_exceeded", source_id=self.source_id)
        observed_at = utc_now()
        execution = observation.execution
        by_name = {assertion.name: assertion for assertion in observation.assertions}
        missing = sorted(self.required_assertion_ids.difference(by_name))
        execution_ok = execution.completed and execution.exit_code == 0
        signals = [
            redact_signal(
                RawSignal(
                    target_id=target.target_id,
                    collector=self.name,
                    signal_type="cron.execution",
                    observed_at=observed_at,
                    severity="info" if execution_ok else "warning",
                    payload={
                        "job_id": execution.job_id,
                        "completed": execution.completed,
                        "exit_code": execution.exit_code,
                        "execution_observed_at": execution.observed_at.isoformat(),
                    },
                )
            )
        ]
        failures = list(missing)
        if not self.required_assertion_ids:
            failures.append("required_assertions_not_configured")
            signals.append(
                redact_signal(
                    RawSignal(
                        target_id=target.target_id,
                        collector=self.name,
                        signal_type="cron.business_assertions_missing",
                        observed_at=observed_at,
                        severity="warning",
                        payload={"reason": "required_assertions_not_configured"},
                    )
                )
            )
        for assertion_id in self.required_assertion_ids:
            assertion = by_name.get(assertion_id)
            if assertion is None:
                signals.append(
                    redact_signal(
                        RawSignal(
                            target_id=target.target_id,
                            collector=self.name,
                            signal_type="cron.business_assertion_missing",
                            observed_at=observed_at,
                            severity="warning",
                            payload={"name": assertion_id},
                        )
                    )
                )
                continue
            fresh = self._is_fresh(assertion, observed_at)
            if assertion.passed and fresh:
                signals.append(
                    redact_signal(
                        RawSignal(
                            target_id=target.target_id,
                            collector=self.name,
                            signal_type="cron.business_assertion_passed",
                            observed_at=observed_at,
                            severity="info",
                            payload={
                                "name": assertion.name,
                                "assertion_observed_at": assertion.observed_at.isoformat(),
                                "evidence": thaw_value(assertion.evidence),
                            },
                        )
                    )
                )
            else:
                failures.append(assertion.name)
                reason = "failed" if not assertion.passed else "stale"
                signal_type = (
                    "cron.business_assertion_failed"
                    if reason == "failed"
                    else "cron.business_assertion_stale"
                )
                signals.append(
                    redact_signal(
                        RawSignal(
                            target_id=target.target_id,
                            collector=self.name,
                            signal_type=signal_type,
                            observed_at=observed_at,
                            severity=assertion.severity,
                            payload={
                                "name": assertion.name,
                                "state": reason,
                                "assertion_observed_at": None
                                if assertion.observed_at is None
                                else assertion.observed_at.isoformat(),
                                "evidence": thaw_value(assertion.evidence),
                            },
                        )
                    )
                )
        reason = None
        if not execution_ok:
            reason = "cron_execution_unhealthy"
        elif failures:
            reason = "cron_assertions_missing" if not self.required_assertion_ids else "cron_business_assertions_unhealthy"
        return CollectionBatch(
            target_id=target.target_id,
            collector=self.name,
            collected_at=observed_at,
            signals=tuple(signals),
            health=CollectorHealth(healthy=reason is None, reason=reason),
            source_id=self.source_id,
        )
