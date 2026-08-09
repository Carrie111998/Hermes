from datetime import datetime, timezone

import pytest

from plugins.agentops.control.config import load_agentops_config
from plugins.agentops.control.observer_models import (
    CollectionBatch,
    CollectorHealth,
    LogCursor,
    RawSignal,
    TargetSnapshot,
)
from plugins.agentops.control.observer_store import ObserverStoreError, observer_database_path, open_observer_store
from plugins.agentops.control.redaction import redact_signal


def _batch():
    signal = redact_signal(
        RawSignal(
            target_id="hermes:profile:default:gateway",
            collector="test.collector",
            signal_type="signal.test",
            observed_at=datetime(2026, 8, 9, tzinfo=timezone.utc),
            payload={"message": "ok"},
        )
    )
    return CollectionBatch(
        target_id=signal.target_id,
        collector=signal.collector,
        collected_at=signal.observed_at,
        signals=(signal,),
        health=CollectorHealth(healthy=True),
        next_cursor=LogCursor(inode=99, offset=12, source_id="sha256:" + "9" * 64),
        source_id="sha256:" + "9" * 64,
    )


def test_observer_store_is_fixed_inside_agentops_state_and_commits_cursor_with_batch(write_config):
    config = load_agentops_config(write_config())
    store = open_observer_store(config)
    try:
        store.commit_collection(_batch())
        assert store.path == config.state_dir / "observer.db"
        assert observer_database_path(config) == store.path
        assert store.journal_mode() == "wal"
        assert store.signal_count() == 1
        assert store.get_cursor("hermes:profile:default:gateway", "test.collector", "sha256:" + "9" * 64) == LogCursor(99, 12, "sha256:" + "9" * 64)
    finally:
        store.close()


def test_observer_store_rejects_config_that_has_not_passed_state_validation(tmp_path):
    config = load_agentops_config(tmp_path / "missing.yaml")

    with pytest.raises(ObserverStoreError):
        open_observer_store(config)


def test_observer_store_reapplies_redaction_to_signals_and_target_snapshots(write_config):
    config = load_agentops_config(write_config())
    store = open_observer_store(config)
    try:
        batch = _batch()
        altered = batch.signals[0]
        object.__setattr__(altered, "payload", {"token": "sk-test-canary-secret-123456"})
        store.commit_collection(batch)
        store.record_target_snapshot(
            TargetSnapshot(
                target_id=batch.target_id,
                observed_at=datetime(2026, 8, 9, tzinfo=timezone.utc),
                facts={"cookie": "session=example-cookie-value", "message": "useful context"},
            )
        )
        stored_signal = store._connection.execute("SELECT payload_json FROM signals").fetchone()[0]
        stored_snapshot = store._connection.execute("SELECT facts_json FROM target_snapshots").fetchone()[0]
        assert "sk-test-canary-secret-123456" not in stored_signal + stored_snapshot
        assert "example-cookie-value" not in stored_signal + stored_snapshot
        assert "useful context" in stored_snapshot
    finally:
        store.close()
