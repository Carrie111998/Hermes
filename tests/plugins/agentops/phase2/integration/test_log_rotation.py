from pathlib import Path

from plugins.agentops.control.collectors.base import collect_all
from plugins.agentops.control.collectors.logs import LogCollector
from plugins.agentops.control.observer_models import CursorResetReason
from plugins.agentops.control.observer_models import Criticality, Target, TargetKind, TargetSpec


def _target(path):
    return Target(
        TargetSpec(
            target_id="hermes:profile:test:gateway",
            profile="test",
            kind=TargetKind.GATEWAY,
            criticality=Criticality.NONCRITICAL,
            observed_paths=(str(path),),
            labels={"service_label": "ai.hermes.gateway-test"},
        )
    )


def _reason(batch):
    return next(signal.payload["reason"] for signal in batch.signals if signal.signal_type == "log.cursor_reset")


def test_log_cursor_recovers_after_rotation_and_truncation(tmp_path):
    path = tmp_path / "gateway.log"
    target = _target(tmp_path)
    path.write_text("2026-08-09T00:00:00Z initial failure\n", encoding="utf-8")
    collector = LogCollector("logs", path)

    first = collector.collect(target)
    assert _reason(first) == CursorResetReason.INITIAL.value

    path.rename(tmp_path / "gateway.log.1")
    path.write_text("2026-08-09T00:01:00Z rotated failure\n", encoding="utf-8")
    rotated = collector.collect(target, first.next_cursor)
    assert _reason(rotated) == CursorResetReason.ROTATED.value

    path.write_text("short\n", encoding="utf-8")
    truncated = collector.collect(target, rotated.next_cursor)
    assert _reason(truncated) == CursorResetReason.TRUNCATED.value
    assert truncated.next_cursor.offset <= path.stat().st_size


def test_same_log_error_from_two_files_is_a_single_signal_in_fan_out(tmp_path):
    first = tmp_path / "gateway.log"
    second = tmp_path / "errors.log"
    target = _target(tmp_path)
    first.write_text("2026-08-09T00:00:00Z pid=100 boom\n", encoding="utf-8")
    second.write_text("2026-08-09T01:00:00Z pid=200 boom\n", encoding="utf-8")

    batches = collect_all(target, (LogCollector("logs", first), LogCollector("logs", second)))
    lines = [signal for batch in batches for signal in batch.signals if signal.signal_type == "log.line"]

    assert len(lines) == 1
