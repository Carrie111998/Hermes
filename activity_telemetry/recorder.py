from collections.abc import Mapping
import logging
from pathlib import Path
import threading
from typing import Any

from .schema import (
    LogicalActivityStart,
    OutcomeLayers,
    RouteUsageDelta,
    ServedRoute,
)
from .store import ActivityStore

logger = logging.getLogger(__name__)

# Tool calls and retries are not served by any model, but the store keys usage
# by served route. They are booked to this one reserved route so per-model cost
# and quality reports can exclude it by symbol rather than by string literal.
NON_MODEL_ROUTE = ServedRoute("activity", "non-model")


class ActivityRecorder:
    def __init__(self, store: ActivityStore, run_id: str) -> None:
        self.store = store
        self.run_id = run_id
        self._lock = threading.Lock()

    @classmethod
    def open(cls, db_path: Path, **start_fields: Any) -> "ActivityRecorder":
        store = ActivityStore(db_path)
        store.start(LogicalActivityStart(**start_fields))
        return cls(store, start_fields["run_id"])

    def link_session(self, session_id: str) -> None:
        self._best_effort("link_session", self.store.link_session, self.run_id, session_id)

    def record_response(
        self,
        provider: str,
        model: str,
        usage: Mapping[str, Any],
    ) -> None:
        try:
            delta = RouteUsageDelta(
                turns=1,
                model_calls=int(usage.get("request_count") or 1),
                uncached_input_tokens=int(usage.get("input_tokens") or 0),
                cache_read_tokens=int(usage.get("cache_read_tokens") or 0),
                cache_write_tokens=int(usage.get("cache_write_tokens") or 0),
                output_tokens=int(usage.get("output_tokens") or 0),
                reasoning_tokens=int(usage.get("reasoning_tokens") or 0),
            )
            with self._lock:
                self.store.record_usage(
                    self.run_id,
                    ServedRoute(provider, model),
                    delta,
                )
        except Exception as exc:
            self._log_failure("record_response", exc)

    def record_tool_call(self, count: int = 1) -> None:
        self._record_counter("record_tool_call", RouteUsageDelta(tool_calls=count))

    def record_retry(self, count: int = 1) -> None:
        self._record_counter("record_retry", RouteUsageDelta(retries=count))

    def finish(
        self,
        layers: OutcomeLayers,
        evidence_refs: tuple[str, ...] = (),
        escalation_reason: str | None = None,
    ) -> str:
        try:
            with self._lock:
                return self.store.finish(
                    self.run_id,
                    layers,
                    evidence_refs,
                    escalation_reason,
                )
        except Exception as exc:
            self._log_failure("finish", exc)
            return "unknown"

    def _record_counter(self, operation: str, delta: RouteUsageDelta) -> None:
        try:
            with self._lock:
                self.store.record_usage(self.run_id, NON_MODEL_ROUTE, delta)
        except Exception as exc:
            self._log_failure(operation, exc)

    def _best_effort(self, operation: str, callback, *args: Any) -> None:
        try:
            with self._lock:
                callback(*args)
        except Exception as exc:
            self._log_failure(operation, exc)

    @staticmethod
    def _log_failure(operation: str, exc: Exception) -> None:
        logger.warning(
            "activity telemetry %s failed: %s",
            operation,
            type(exc).__name__,
        )
