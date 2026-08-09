from datetime import datetime, timezone

from plugins.agentops.control.collectors.base import collect_all
from plugins.agentops.control.observer_models import CollectionBatch, CollectorHealth, RawSignal
from plugins.agentops.control.redaction import redact_signal
from plugins.agentops.control.registry import bootstrap_gateway_registry


class _WorkingCollector:
    name = "logs"

    def __init__(self, message="same observation"):
        self.message = message

    def collect(self, target, cursor=None):
        observed_at = datetime(2026, 8, 9, tzinfo=timezone.utc)
        signal = redact_signal(
            RawSignal(
                target_id=target.target_id,
                collector=self.name,
                signal_type="log.line",
                observed_at=observed_at,
                payload={"message": self.message},
            )
        )
        return CollectionBatch(target.target_id, self.name, observed_at, (signal,), CollectorHealth(True))


class _BrokenCollector:
    name = "broken"

    def collect(self, target, cursor=None):
        raise TimeoutError()


def test_collector_failure_is_isolated_and_duplicate_signals_are_suppressed():
    target = bootstrap_gateway_registry().list_targets()[0]

    batches = collect_all(target, (_WorkingCollector(), _WorkingCollector(), _BrokenCollector()))

    assert [len(batch.signals) for batch in batches] == [1, 0, 0]
    assert batches[-1].health.healthy is False
    assert batches[-1].health.reason == "collector_timeout"
