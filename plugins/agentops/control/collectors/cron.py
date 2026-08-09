"""Cron observation where execution completion is not business success."""

from __future__ import annotations

from collections.abc import Callable

from plugins.agentops.control.observer_models import (
    BusinessAssertion,
    CollectionBatch,
    CollectorHealth,
    CronExecution,
    LogCursor,
    RawSignal,
    Target,
    utc_now,
)
from plugins.agentops.control.redaction import redact_signal


class CronCollector:
    name = "cron"

    def __init__(self, observation: Callable[[], tuple[CronExecution, tuple[BusinessAssertion, ...]]]) -> None:
        self._observation = observation

    def collect(self, target: Target, cursor: LogCursor | None = None) -> CollectionBatch:
        observed_at = utc_now()
        try:
            execution, assertions = self._observation()
            if not isinstance(execution, CronExecution) or not all(
                isinstance(assertion, BusinessAssertion) for assertion in assertions
            ):
                raise ValueError("invalid cron observation")
        except Exception:
            return CollectionBatch(
                target_id=target.target_id,
                collector=self.name,
                collected_at=observed_at,
                signals=(),
                health=CollectorHealth(healthy=False, reason="cron_observation_failed"),
            )
        execution_ok = execution.completed and execution.exit_code == 0
        assertions_ok = all(assertion.passed for assertion in assertions)
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
                    },
                )
            )
        ]
        for assertion in assertions:
            if not assertion.passed:
                signals.append(
                    redact_signal(
                        RawSignal(
                            target_id=target.target_id,
                            collector=self.name,
                            signal_type="cron.business_assertion_failed",
                            observed_at=observed_at,
                            severity="warning",
                            payload={"name": assertion.name, "evidence": dict(assertion.evidence)},
                        )
                    )
                )
        reason = None if execution_ok and assertions_ok else "cron_execution_or_business_assertion_unhealthy"
        return CollectionBatch(
            target_id=target.target_id,
            collector=self.name,
            collected_at=observed_at,
            signals=tuple(signals),
            health=CollectorHealth(healthy=reason is None, reason=reason),
        )
