from __future__ import annotations

import hashlib
import json
import os
import plistlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from plugins.agentops.control.collectors.base import failed_batch
from plugins.agentops.control.observer_models import Criticality, Signal, Target, TargetKind, TargetSpec, utc_now
from plugins.agentops.control.observation import DefaultObservationLoop, ObservationBoundaryError, ObservationLedger, ObservationRunbook
from plugins.agentops.control.registry import FleetRegistry
from plugins.agentops.control.redaction import contains_secret


def _trusted_registry(tmp_path: Path) -> tuple[FleetRegistry, Path, Path, Path]:
    logs = tmp_path / "logs"
    logs.mkdir()
    log = logs / "gateway.log"
    log.write_text('{"state":"failed","message":"gateway down"}\n', encoding="utf-8")
    plist = tmp_path / "ai.hermes.gateway.plist"
    command = ["/Users/molly/Desktop/Hermes/venv/bin/python", "-m", "hermes_cli.main", "gateway", "run", "--replace"]
    plist.write_bytes(plistlib.dumps({"Label": "ai.hermes.gateway", "ProgramArguments": command}))
    os.chmod(plist, 0o600)
    fingerprint = "sha256:" + hashlib.sha256("\x00".join(command).encode()).hexdigest()
    cron = tmp_path / "cron.json"
    now = datetime.now(timezone.utc).isoformat()
    cron.write_text(json.dumps({
        "execution": {"job_id": "default", "observed_at": now, "exit_code": 0, "completed": True},
        "assertions": [
            {"name": "cron_execution_completed", "passed": True, "observed_at": now},
            {"name": "cron_business_assertion_fresh", "passed": True, "observed_at": now},
        ],
    }), encoding="utf-8")
    spec = TargetSpec(
        target_id="hermes:profile:default:gateway", profile="default", kind=TargetKind.GATEWAY,
        criticality=Criticality.CRITICAL, observed_paths=(str(logs), str(plist), str(cron)),
        labels={"service_label": "ai.hermes.gateway", "profile": "default", "g2_scope": "core",
                "process_marker": "default", "command_fingerprint": fingerprint,
                "process_observation": "enabled", "process_command_label_optional": "true",
                "process_marker_optional": "true", "process_name_contains": "python3.11"},
    )
    return FleetRegistry((spec,)), log, plist, cron


class _Process:
    pid = 42
    def name(self): return "python3.11"
    def cmdline(self): return ["/Users/molly/Desktop/Hermes/venv/bin/python", "-m", "hermes_cli.main", "gateway", "run", "--replace"]
    def uids(self):
        class Uids: real = os.getuid()
        return Uids()


def test_ledger_is_append_only_detached_and_bounded(tmp_path):
    registry, log, _, _ = _trusted_registry(tmp_path)
    target = registry.get_target("hermes:profile:default:gateway")
    batch = failed_batch(target, "logs", "collector_failed", source_id="sha256:" + "a" * 64)
    ledger = ObservationLedger(max_runs=1, max_signals=1, max_bytes=10_000)
    first_id = ledger.append(batch)
    assert first_id == batch.observation_id and ledger.batches()[0]["observation_id"] == batch.observation_id
    with pytest.raises(ObservationBoundaryError, match="budget"):
        ledger.append(batch)
    signal = Signal("sha256:" + "b" * 64, target.target_id, "logs", "log.line", utc_now(), {"message": "safe"})
    poisoned = failed_batch(target, "logs", "collector_failed", source_id="sha256:" + "c" * 64)
    object.__setattr__(poisoned, "signals", (signal,))
    object.__setattr__(signal, "payload", {"password": "hunter2"})
    with pytest.raises(ObservationBoundaryError):
        ObservationLedger().append(poisoned)


def test_ledger_authority_record_survives_source_mutation(tmp_path):
    registry, log, _, _ = _trusted_registry(tmp_path)
    loop = DefaultObservationLoop.create(registry=registry, log_path=log, process_iter=lambda: [])
    batches = loop.collect_once()
    original = next(batch for batch in batches if batch.collector == "launchd")
    assert original.signals
    object.__setattr__(original.signals[0], "payload", {"password": "injected"})
    stored = loop.ledger.batches()
    launchd = next(item for item in stored if item["collector"] == "launchd")
    assert "password" not in str(launchd) and "injected" not in str(launchd)
    assert not contains_secret(launchd)


def test_daily_summary_and_terra_input_are_bounded_and_no_actions(tmp_path):
    registry, _, _, _ = _trusted_registry(tmp_path)
    target = registry.get_target("hermes:profile:default:gateway")
    ledger = ObservationLedger(max_runs=10, max_signals=10, max_bytes=100_000)
    ledger.append(failed_batch(target, "processes", "process_binding_no_match", source_id="sha256:" + "a" * 64))
    day = datetime.now(timezone.utc)
    summary = ledger.daily_summary(day)
    assert summary["unhealthy_runs"] == 1 and summary["automatic_repair"] is False
    handoff = ledger.terra_input(day, max_items=1, max_bytes=4096)
    assert handoff["actions"] == [] and len(handoff["evidence"]) == 0
    with pytest.raises(ObservationBoundaryError, match="Terra input budget"):
        ledger.terra_input(day, max_items=1, max_bytes=1)


def test_daily_summary_uses_utc_day_label(tmp_path):
    registry, _, _, _ = _trusted_registry(tmp_path)
    target = registry.get_target("hermes:profile:default:gateway")
    batch = failed_batch(target, "logs", "collector_failed", source_id="sha256:" + "e" * 64)
    local_time = datetime(2026, 1, 1, 0, 30, tzinfo=timezone(timedelta(hours=2)))
    object.__setattr__(batch, "collected_at", local_time)
    ledger = ObservationLedger()
    ledger.append(batch)
    assert ledger.daily_summary("2025-12-31")["run_count"] == 1
    assert ledger.daily_summary(local_time)["day"] == "2025-12-31"


def test_default_loop_collects_read_only_process_launchd_logs_and_cron(tmp_path):
    registry, log, plist, cron = _trusted_registry(tmp_path)
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (log, plist, cron)}
    loop = DefaultObservationLoop.create(registry=registry, log_path=log, cron_source_path=cron, process_iter=lambda: [_Process()])
    assert loop.collector_names == ("processes", "launchd", "logs", "cron")
    batches = loop.collect_once()
    assert {batch.collector for batch in batches} == set(loop.collector_names)
    assert any(batch.collector == "processes" and batch.health.healthy for batch in batches)
    assert any(batch.collector == "launchd" and batch.health.healthy for batch in batches)
    assert any(batch.collector == "cron" for batch in batches)
    assert before == {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (log, plist, cron)}
    assert registry.get_snapshot("hermes:profile:default:gateway") is not None


def test_default_loop_rejects_tampered_or_disabled_binding(tmp_path):
    registry, _, _, _ = _trusted_registry(tmp_path)
    target = registry.get_target("hermes:profile:default:gateway")
    labels = dict(target.spec.labels)
    labels["command_fingerprint"] = "sha256:" + "0" * 64
    bad = TargetSpec(target_id=target.target_id, profile=target.spec.profile, kind=target.spec.kind, criticality=target.spec.criticality, observed_paths=target.spec.observed_paths, labels=labels)
    with pytest.raises(ObservationBoundaryError):
        DefaultObservationLoop.create(registry=FleetRegistry((bad,)))
    labels["process_marker"] = "untrusted"
    bad_marker = TargetSpec(target_id=target.target_id, profile=target.spec.profile, kind=target.spec.kind, criticality=target.spec.criticality, observed_paths=target.spec.observed_paths, labels=labels)
    with pytest.raises(ObservationBoundaryError):
        DefaultObservationLoop.create(registry=FleetRegistry((bad_marker,)))


def test_launchd_asset_replacement_between_passes_fails_closed(tmp_path):
    registry, _, plist, _ = _trusted_registry(tmp_path)
    loop = DefaultObservationLoop.create(registry=registry)
    plist.write_bytes(plistlib.dumps({"Label": "ai.hermes.gateway", "ProgramArguments": ["/bin/evil"]}))
    launchd = next(item for item in loop.collectors if item.name == "launchd")
    batch = launchd.collect(loop.target)
    assert batch.health.healthy is False
    assert batch.health.reason in {"plist_command_fingerprint_mismatch", "plist_identity_rejected"}


def test_default_loop_does_not_expose_sqlite_or_lifecycle_surface(tmp_path):
    registry, _, _, _ = _trusted_registry(tmp_path)
    loop = DefaultObservationLoop.create(registry=registry)
    assert not hasattr(loop, "repair") and not hasattr(loop, "restart") and not hasattr(loop, "launchctl")
    assert not list(tmp_path.glob("observer.db*"))


def test_runbook_backlog_drain_reaches_tail_without_counting_observation_day(tmp_path):
    registry, log, _, _ = _trusted_registry(tmp_path)
    loop = DefaultObservationLoop.create(registry=registry, log_path=log, process_iter=lambda: [])
    for collector in loop.collectors:
        if hasattr(collector, "min_interval_seconds"):
            collector.min_interval_seconds = 0
    runbook = ObservationRunbook(loop)
    report = runbook.drain_backlog(max_passes=2)
    assert report["status"] == "tail_reached"
    assert report["observation_day_counted"] is False
    assert report["stop_reason"] == "tail_reached"


def test_runbook_daily_rotation_requires_export_and_preserves_cursor(tmp_path):
    registry, log, _, _ = _trusted_registry(tmp_path)
    loop = DefaultObservationLoop.create(registry=registry, log_path=log, process_iter=lambda: [])
    for collector in loop.collectors:
        if hasattr(collector, "min_interval_seconds"):
            collector.min_interval_seconds = 0
    runbook = ObservationRunbook(loop)
    assert runbook.drain_backlog(max_passes=1)["tail_reached"] is True
    day = datetime.now(timezone.utc).date()
    summary = loop.ledger.daily_summary(day)
    terra = loop.ledger.terra_input(day, max_items=500, max_bytes=8 * 1024 * 1024)
    receipt = hashlib.sha256(json.dumps({"summary": summary, "terra_input": terra}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    runbook.finalize_backlog_export(utc_day=day, summary=summary, terra_input=terra, export_verified=True, export_receipt_sha256=receipt)
    runbook._next_observation_utc_day = day
    loop.collect_once()
    summary = loop.ledger.daily_summary(day)
    terra = loop.ledger.terra_input(day, max_items=500, max_bytes=8 * 1024 * 1024)
    receipt = hashlib.sha256(json.dumps({"summary": summary, "terra_input": terra}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    cursor_keys = tuple(sorted(loop._cursors))
    rotated = runbook.rotate_after_daily_export(utc_day=day, summary=summary, terra_input=terra, export_verified=True, export_receipt_sha256=receipt)
    assert rotated["status"] == "rotated" and rotated["cursor_keys_preserved"] is True
    assert tuple(sorted(loop._cursors)) == cursor_keys and len(loop.ledger.batches()) == 0
    missed = runbook.record_missed_slot(datetime.now(timezone.utc) - timedelta(hours=8))
    assert missed["catch_up"] is False and missed["status"] == "missed"
    with pytest.raises(ObservationBoundaryError):
        runbook.rotate_after_daily_export(utc_day=day, summary=summary, terra_input=terra, export_verified=False)


def test_runbook_export_receipt_is_bound_to_current_ledger(tmp_path):
    registry, log, _, _ = _trusted_registry(tmp_path)
    loop = DefaultObservationLoop.create(registry=registry, log_path=log, process_iter=lambda: [])
    for collector in loop.collectors:
        if hasattr(collector, "min_interval_seconds"):
            collector.min_interval_seconds = 0
    runbook = ObservationRunbook(loop)
    assert runbook.drain_backlog(max_passes=1)["tail_reached"] is True
    day = datetime.now(timezone.utc).date()
    summary = loop.ledger.daily_summary(day)
    terra = loop.ledger.terra_input(day, max_items=500, max_bytes=8 * 1024 * 1024)
    receipt = hashlib.sha256(json.dumps({"summary": summary, "terra_input": terra}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    runbook.finalize_backlog_export(utc_day=day, summary=summary, terra_input=terra, export_verified=True, export_receipt_sha256=receipt)
    runbook._next_observation_utc_day = day
    loop.collect_once()
    old_summary = loop.ledger.daily_summary(day)
    old_terra = loop.ledger.terra_input(day, max_items=500, max_bytes=8 * 1024 * 1024)
    old_receipt = hashlib.sha256(json.dumps({"summary": old_summary, "terra_input": old_terra}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    loop.collect_once()
    before = loop.ledger.batches()
    with pytest.raises(ObservationBoundaryError, match="current ledger"):
        runbook.rotate_after_daily_export(utc_day=day, summary=old_summary, terra_input=old_terra, export_verified=True, export_receipt_sha256=old_receipt)
    assert loop.ledger.batches() == before


def test_runbook_rate_limit_is_safe_stop_not_catch_up(tmp_path):
    registry, log, _, _ = _trusted_registry(tmp_path)
    log.write_text("{\"state\":\"failed\",\"message\":\"gateway down\"}\n" * 500, encoding="utf-8")
    loop = DefaultObservationLoop.create(registry=registry, log_path=log, process_iter=lambda: [])
    report = ObservationRunbook(loop).drain_backlog(max_passes=2)
    assert report["status"] == "safe_stop"
    assert report["passes"] == 1
    assert report["stop_reason"] == "next_eligible_not_reached"
    assert report["next_eligible_seconds"] >= 60
    second = ObservationRunbook(loop).drain_backlog(max_passes=2)
    assert second["passes"] == 0 and second["stop_reason"] == "next_eligible_not_reached"


def test_runbook_backlog_stops_on_ledger_budget(tmp_path):
    registry, log, _, _ = _trusted_registry(tmp_path)
    ledger = ObservationLedger(max_runs=1, max_signals=10000, max_bytes=1000000)
    loop = DefaultObservationLoop.create(registry=registry, ledger=ledger, log_path=log, process_iter=lambda: [])
    for collector in loop.collectors:
        if hasattr(collector, "min_interval_seconds"):
            collector.min_interval_seconds = 0
    report = ObservationRunbook(loop).drain_backlog(max_passes=1)
    assert report["status"] == "safe_stop" and report["stop_reason"] == "ledger_budget_exceeded"


def test_runbook_rejects_inode_changed_tail(tmp_path):
    registry, log, _, _ = _trusted_registry(tmp_path)
    loop = DefaultObservationLoop.create(registry=registry, log_path=log, process_iter=lambda: [])
    for collector in loop.collectors:
        if hasattr(collector, "min_interval_seconds"):
            collector.min_interval_seconds = 0
    logs = next(item for item in loop.collectors if item.name == "logs")
    batch = logs.collect(loop.target)
    object.__setattr__(batch, "next_cursor", type(batch.next_cursor)(inode=batch.next_cursor.inode + 1, offset=batch.next_cursor.offset, source_id=batch.next_cursor.source_id))
    loop.collect_once = lambda: (batch,)
    report = ObservationRunbook(loop).drain_backlog(max_passes=1)
    assert report["status"] == "safe_stop" and report["stop_reason"] == "log_source_changed"


def test_runbook_rejects_path_rotation_before_declaring_tail(tmp_path):
    registry, log, _, _ = _trusted_registry(tmp_path)
    loop = DefaultObservationLoop.create(registry=registry, log_path=log, process_iter=lambda: [])
    for collector in loop.collectors:
        if hasattr(collector, "min_interval_seconds"):
            collector.min_interval_seconds = 0
    original_collect = loop.collect_once

    def collect_then_rotate():
        batches = original_collect()
        replacement = log.with_name("gateway.log.replacement")
        replacement.write_text(log.read_text(encoding="utf-8"), encoding="utf-8")
        os.replace(replacement, log)
        return batches

    loop.collect_once = collect_then_rotate
    report = ObservationRunbook(loop).drain_backlog(max_passes=1)
    assert report["status"] == "safe_stop" and report["stop_reason"] == "log_source_changed"


def test_runbook_finalize_backlog_requires_verified_export_and_marks_day0(tmp_path):
    registry, log, _, _ = _trusted_registry(tmp_path)
    loop = DefaultObservationLoop.create(registry=registry, log_path=log, process_iter=lambda: [])
    for collector in loop.collectors:
        if hasattr(collector, "min_interval_seconds"):
            collector.min_interval_seconds = 0
    runbook = ObservationRunbook(loop)
    report = runbook.drain_backlog(max_passes=1)
    assert report["tail_reached"] is True
    day = datetime.now(timezone.utc).date()
    summary = loop.ledger.daily_summary(day)
    terra = loop.ledger.terra_input(day, max_items=500, max_bytes=8 * 1024 * 1024)
    receipt = hashlib.sha256(json.dumps({"summary": summary, "terra_input": terra}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    finalized = runbook.finalize_backlog_export(utc_day=day, summary=summary, terra_input=terra, export_verified=True, export_receipt_sha256=receipt)
    assert finalized["day1_cleanup"] is True and finalized["observation_day_counted"] is False
    assert finalized["next_observation_utc_day"] != day.isoformat() and len(loop.ledger.batches()) == 0
    with pytest.raises(ObservationBoundaryError, match="out of sequence"):
        runbook.rotate_after_daily_export(utc_day=day, summary=summary, terra_input=terra, export_verified=True, export_receipt_sha256=receipt)
    with pytest.raises(ObservationBoundaryError, match="out of sequence"):
        runbook.rotate_after_daily_export(utc_day=day + timedelta(days=7), summary=summary, terra_input=terra, export_verified=True, export_receipt_sha256=receipt)
    with pytest.raises(ObservationBoundaryError):
        runbook.finalize_backlog_export(utc_day=day, summary=summary, terra_input=terra, export_verified=True, export_receipt_sha256=receipt)


def test_runbook_rejects_mixed_utc_days_and_reserves_rotation_event(tmp_path):
    registry, log, _, _ = _trusted_registry(tmp_path)
    loop = DefaultObservationLoop.create(registry=registry, log_path=log, process_iter=lambda: [])
    for collector in loop.collectors:
        if hasattr(collector, "min_interval_seconds"):
            collector.min_interval_seconds = 0
    runbook = ObservationRunbook(loop, max_events=3)
    assert runbook.drain_backlog(max_passes=1)["tail_reached"] is True
    day = datetime.now(timezone.utc).date()
    summary = loop.ledger.daily_summary(day)
    terra = loop.ledger.terra_input(day, max_items=500, max_bytes=8 * 1024 * 1024)
    receipt = hashlib.sha256(json.dumps({"summary": summary, "terra_input": terra}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    runbook.finalize_backlog_export(utc_day=day, summary=summary, terra_input=terra, export_verified=True, export_receipt_sha256=receipt)
    runbook._next_observation_utc_day = day
    loop.collect_once()
    current = loop.ledger.batches()
    old = dict(current[0])
    old["collected_at"] = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    object.__setattr__(loop.ledger._records[0], "record", old)
    with pytest.raises(ObservationBoundaryError, match="mixed UTC days"):
        runbook.rotate_after_daily_export(utc_day=day, summary=loop.ledger.daily_summary(day), terra_input=loop.ledger.terra_input(day, max_items=500, max_bytes=8 * 1024 * 1024), export_verified=True, export_receipt_sha256="0" * 64)
    assert len(loop.ledger.batches()) == len(current)
    runbook._events.clear()
    runbook.max_events = 0
    with pytest.raises(ObservationBoundaryError, match="event budget"):
        runbook.rotate_after_daily_export(utc_day=day, summary=loop.ledger.daily_summary(day), terra_input=loop.ledger.terra_input(day, max_items=500, max_bytes=8 * 1024 * 1024), export_verified=True, export_receipt_sha256="0" * 64)
    assert len(loop.ledger.batches()) == len(current)


def test_runbook_capacity_and_slot_reservations_fail_before_mutation(tmp_path):
    registry, log, _, _ = _trusted_registry(tmp_path)
    loop = DefaultObservationLoop.create(registry=registry, log_path=log, process_iter=lambda: [])
    runbook = ObservationRunbook(loop, max_events=1)
    object.__setattr__(runbook, "max_events", 0)
    called = False
    def forbidden_collect():
        nonlocal called
        called = True
        raise AssertionError("collect must not run when event budget is full")
    loop.collect_once = forbidden_collect
    with pytest.raises(ObservationBoundaryError, match="event budget"):
        runbook.drain_backlog(max_passes=1)
    assert called is False and runbook.state == "DRAINING" and not runbook.metadata()

    runbook.max_events = 0
    scheduled = datetime.now(timezone.utc) - timedelta(hours=1)
    with pytest.raises(ObservationBoundaryError, match="event budget"):
        runbook.record_missed_slot(scheduled)
    runbook.max_events = 1
    retry = runbook.record_missed_slot(scheduled)
    assert retry["status"] == "missed"

    empty = ObservationRunbook(loop)
    empty._state = "READY"
    empty._backlog_tail_reached = True
    day = datetime.now(timezone.utc).date()
    summary = loop.ledger.daily_summary(day)
    terra = loop.ledger.terra_input(day, max_items=500, max_bytes=8 * 1024 * 1024)
    receipt = hashlib.sha256(json.dumps({"summary": summary, "terra_input": terra}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    with pytest.raises(ObservationBoundaryError, match="empty"):
        empty.finalize_backlog_export(utc_day=day, summary=summary, terra_input=terra, export_verified=True, export_receipt_sha256=receipt)


def test_runbook_rejects_future_duplicate_slots_and_metadata_mutation(tmp_path):
    registry, _, _, _ = _trusted_registry(tmp_path)
    loop = DefaultObservationLoop.create(registry=registry)
    runbook = ObservationRunbook(loop, max_events=2)
    with pytest.raises(ObservationBoundaryError):
        runbook.record_missed_slot(datetime.now(timezone.utc) + timedelta(hours=1))
    scheduled = datetime.now(timezone.utc) - timedelta(hours=1)
    first = runbook.record_missed_slot(scheduled)
    with pytest.raises(ObservationBoundaryError):
        runbook.record_missed_slot(scheduled)
    runbook.record_missed_slot(scheduled - timedelta(hours=1))
    with pytest.raises(ObservationBoundaryError, match="event budget"):
        runbook.record_missed_slot(scheduled - timedelta(hours=2))
    first["status"] = "mutated"
    assert runbook.metadata()[0]["status"] == "missed"
