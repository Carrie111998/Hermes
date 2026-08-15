from hermes_cli.resource_guard import ProcessMemoryGuard, ResourceGuardSettings


def test_hard_limit_captures_evidence_and_calls_restart_once(monkeypatch):
    snapshot = {
        "rss_bytes": 9 * 1024 * 1024,
        "descendant_rss_bytes": 0,
        "descendant_count": 0,
    }
    callbacks = []
    evidence = []
    monkeypatch.setattr(
        "hermes_cli.resource_guard.collect_process_memory_snapshot",
        lambda _metrics: dict(snapshot),
    )
    monkeypatch.setattr(
        "hermes_cli.resource_guard._append_telemetry", lambda row: None
    )
    monkeypatch.setattr(
        "hermes_cli.resource_guard._write_evidence_snapshot",
        lambda row, reason: evidence.append(reason) or None,
    )
    guard = ProcessMemoryGuard(
        component="test",
        settings=ResourceGuardSettings(
            poll_seconds=15,
            telemetry_seconds=60,
            warn_rss_mb=2,
            snapshot_rss_mb=4,
            hard_rss_mb=8,
            descendant_warn_rss_mb=8,
            descendant_hard_rss_mb=24,
            snapshot_cooldown_seconds=300,
        ),
        on_hard_limit=lambda row: callbacks.append(row),
    )

    guard._sample()
    guard._sample()

    assert callbacks == [callbacks[0]]
    assert "hard-parent" in evidence


def test_descendant_hard_limit_is_independent_of_parent_rss(monkeypatch):
    snapshot = {
        "rss_bytes": 1 * 1024 * 1024,
        "descendant_rss_bytes": 25 * 1024 * 1024,
        "descendant_count": 2,
    }
    callbacks = []
    monkeypatch.setattr(
        "hermes_cli.resource_guard.collect_process_memory_snapshot",
        lambda _metrics: dict(snapshot),
    )
    monkeypatch.setattr(
        "hermes_cli.resource_guard._append_telemetry", lambda row: None
    )
    monkeypatch.setattr(
        "hermes_cli.resource_guard._write_evidence_snapshot", lambda row, reason: None
    )
    guard = ProcessMemoryGuard(
        component="test",
        settings=ResourceGuardSettings(
            warn_rss_mb=2,
            snapshot_rss_mb=4,
            hard_rss_mb=8,
            descendant_warn_rss_mb=8,
            descendant_hard_rss_mb=24,
        ),
        on_hard_limit=lambda row: callbacks.append(row),
    )

    guard._sample()

    assert len(callbacks) == 1
