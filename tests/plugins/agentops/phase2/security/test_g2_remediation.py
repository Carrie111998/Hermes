"""Negative regressions for every blocking finding from Sol's G2 review."""

from __future__ import annotations

import hashlib
import sqlite3
import plistlib
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from plugins.agentops.bridge import BoundedBridgeBuffer
from plugins.agentops.control.collectors.base import collect_all
from plugins.agentops.control.collectors.cron import CronCollector
from plugins.agentops.control.collectors.git_state import GitStateCollector
from plugins.agentops.control.collectors.logs import LogCollector
from plugins.agentops.control.collectors.launchd import LaunchdCollector
from plugins.agentops.control.collectors.processes import ProcessCollector
from plugins.agentops.control.collectors.sqlite_health import SQLiteHealthCollector
from plugins.agentops.control.config import load_agentops_config
from plugins.agentops.control.observer_models import (
    BusinessAssertion,
    CollectionBatch,
    CollectorHealth,
    Criticality,
    CronExecution,
    CronObservation,
    LogCursor,
    RawSignal,
    Target,
    TargetKind,
    TargetSnapshot,
    TargetSpec,
)
from plugins.agentops.control.observer_store import ObserverStoreError, open_observer_store
from plugins.agentops.control.redaction import redact_signal
from plugins.agentops.control.review_pack import ManifestValidationError, build_collector, load_review_pack


def _target(path: Path) -> Target:
    return Target(
        TargetSpec(
            target_id="hermes:profile:g2test:gateway",
            profile="g2test",
            kind=TargetKind.GATEWAY,
            criticality=Criticality.NONCRITICAL,
            observed_paths=(str(path),),
            labels={"service_label": "ai.hermes.gateway-g2test"},
        )
    )


def _hashes(paths: list[Path]) -> dict[str, str]:
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths if path.exists()}


def test_live_wal_target_files_are_never_opened_or_changed(tmp_path):
    database = tmp_path / "writer.db"
    writer = sqlite3.connect(database)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE facts (value TEXT)")
    writer.execute("INSERT INTO facts VALUES ('one')")
    writer.commit()
    related = [database, database.with_name("writer.db-wal"), database.with_name("writer.db-shm")]
    before = _hashes(related)
    assert {"writer.db", "writer.db-wal", "writer.db-shm"}.issubset(before)

    batch = SQLiteHealthCollector(database).collect(_target(tmp_path))

    assert batch.health.healthy is False
    assert batch.signals[0].payload["integrity"] == "unknown"
    assert _hashes(related) == before
    writer.close()


def test_json_and_quoted_password_canaries_do_not_cross_log_store_boundary(tmp_path, write_config):
    log = tmp_path / "gateway.log"
    log.write_text(
        '{"password":"hunter2","nested":{"token":"sk-canary-secret-123456"},"message":"safe"}\n'
        'password="hunter2" cookie=example-cookie-value message=safe\n',
        encoding="utf-8",
    )
    batch = LogCollector("logs", log).collect(_target(tmp_path))
    store = open_observer_store(load_agentops_config(write_config()))
    try:
        store.commit_collection(batch)
        stored = "".join(path.read_bytes().decode("latin1") for path in store.path.parent.glob("observer.db*"))
        payloads = "".join(str(signal.to_dict()) for signal in batch.signals)
        for canary in ("hunter2", "sk-canary-secret-123456", "example-cookie-value"):
            assert canary not in stored
            assert canary not in payloads
    finally:
        store.close()


def test_log_cursor_only_advances_consumed_lines_and_source_cursors_do_not_collide(tmp_path, write_config):
    first = tmp_path / "first.log"
    second = tmp_path / "second.log"
    first.write_text("one\ntwo\nthree\n", encoding="utf-8")
    second.write_text("alpha\nbeta\n", encoding="utf-8")
    target = _target(tmp_path)
    first_collector = LogCollector("logs", first, max_bytes=1024, max_lines=1)
    second_collector = LogCollector("logs", second, max_bytes=1024, max_lines=1)

    first_batch = first_collector.collect(target)
    assert first_batch.next_cursor.offset == len(b"one\n")
    second_batch = first_collector.collect(target, first_batch.next_cursor)
    assert second_batch.next_cursor.offset == len(b"one\ntwo\n")
    assert second_batch.signals[0].payload["message"] == "two"

    store = open_observer_store(load_agentops_config(write_config()))
    try:
        store.commit_collection(first_batch)
        other_batch = second_collector.collect(target)
        store.commit_collection(other_batch)
        assert store.get_cursor(target.target_id, "logs", first_collector.source_id) == first_batch.next_cursor
        assert store.get_cursor(target.target_id, "logs", second_collector.source_id) == other_batch.next_cursor
    finally:
        store.close()


def test_cron_missing_and_stale_assertions_are_unhealthy_and_runs_record_recurrence(tmp_path, write_config):
    source = tmp_path / "cron-observation.json"
    source.write_text("{}", encoding="utf-8")
    now = datetime.now(timezone.utc)
    target = _target(tmp_path)
    missing = CronCollector(
        CronObservation(CronExecution("job", now, 0, True), ()), source_path=source, required_assertion_ids=()
    ).collect(target)
    stale = CronCollector(
        CronObservation(
            CronExecution("job", now, 0, True),
            (BusinessAssertion("fresh", True, {"status": "old"}, now - timedelta(seconds=301), max_age_seconds=300),),
        ),
        source_path=source,
        required_assertion_ids=("fresh",),
    ).collect(target)
    assert missing.health.reason == "cron_assertions_missing"
    assert stale.health.reason == "cron_business_assertions_unhealthy"
    assert any(signal.signal_type == "cron.business_assertion_stale" for signal in stale.signals)

    signal = redact_signal(
        RawSignal(target.target_id, "test.collector", "signal.repeat", now, {"message": "same"})
    )
    first = CollectionBatch(target.target_id, "test.collector", now, (signal,), CollectorHealth(True), source_id="sha256:" + "2" * 64)
    second = CollectionBatch(target.target_id, "test.collector", now, (signal,), CollectorHealth(False, "probe_failed"), source_id="sha256:" + "2" * 64)
    store = open_observer_store(load_agentops_config(write_config()))
    try:
        store.commit_collection(first)
        store.commit_collection(second)
        assert store.collection_run_count() == 2
        assert store.occurrence_count(signal.signal_id) == 2
        rows = store._connection.execute("SELECT healthy, reason FROM collection_runs ORDER BY rowid").fetchall()
        assert rows == [(1, None), (0, "probe_failed")]
    finally:
        store.close()


def test_cron_json_status_source_is_bounded_read_only_evidence(tmp_path):
    source = tmp_path / "cron-status.json"
    now = datetime.now(timezone.utc).isoformat()
    source.write_text(
        '{"execution":{"job_id":"job","observed_at":"' + now + '","exit_code":0,"completed":true},'
        '"assertions":[{"name":"cron_business_assertion_fresh","passed":true,"observed_at":"' + now + '","evidence":{"status":"ok"}}]}',
        encoding="utf-8",
    )
    before = source.read_bytes()
    batch = CronCollector.from_json_file(source, required_assertion_ids=("cron_business_assertion_fresh",)).collect(_target(tmp_path))
    assert batch.health.healthy is True
    assert any(signal.signal_type == "cron.business_assertion_passed" for signal in batch.signals)
    assert source.read_bytes() == before


def test_bridge_copies_nested_payload_revalidates_and_remains_capacity_bounded(make_event):
    payload = {"nested": {"message": "safe"}}
    event = make_event("evt-g2-1", payload=payload)
    bridge = BoundedBridgeBuffer(capacity=3)

    result = bridge.publish(event, lambda _: (_ for _ in ()).throw(ConnectionError("closed")))
    payload["nested"]["token"] = "sk-canary-secret-123456"
    observed = []
    assert result.queued is True
    assert bridge.drain(observed.append) == 1
    assert observed[0].payload["nested"] == {"message": "safe"}

    workers = [
        threading.Thread(target=lambda index=index: bridge.publish(make_event(f"evt-g2-{index + 2}"), lambda _: (_ for _ in ()).throw(ConnectionError())))
        for index in range(12)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert bridge.depth == 3
    assert bridge.dropped >= 9


def test_git_ref_traversal_is_rejected_and_standard_worktree_packed_refs_are_read(tmp_path):
    outside = tmp_path / "outside-secret"
    outside.write_text("a" * 40, encoding="utf-8")
    bad_repo = tmp_path / "bad-repo"
    (bad_repo / ".git").mkdir(parents=True)
    (bad_repo / ".git" / "HEAD").write_text("ref: refs/../../outside-secret\n", encoding="utf-8")
    bad = GitStateCollector(bad_repo).collect(_target(tmp_path))
    assert bad.health.reason == "git_read_failed"

    repo = tmp_path / "worktree"
    git_dir = tmp_path / "metadata" / "worktree-git"
    common = tmp_path / "metadata" / "common"
    repo.mkdir()
    git_dir.mkdir(parents=True)
    common.mkdir(parents=True)
    (repo / ".git").write_text("gitdir: ../metadata/worktree-git\n", encoding="utf-8")
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git_dir / "commondir").write_text("../common\n", encoding="utf-8")
    (common / "packed-refs").write_text("# pack\n" + "b" * 40 + " refs/heads/main\n", encoding="utf-8")
    (common / "config").write_text('[branch "main"]\nremote = origin\nmerge = refs/heads/main\n', encoding="utf-8")
    good = GitStateCollector(repo).collect(_target(tmp_path))
    assert good.health.healthy is True
    assert good.signals[0].payload["head"] == "b" * 40
    assert good.signals[0].payload["upstream"] == "origin:refs/heads/main"


def test_asset_binding_deadline_and_snapshot_deep_freeze(tmp_path):
    target = _target(tmp_path)
    unbound = LogCollector("logs", tmp_path.parent / "outside.log")
    assert unbound.collect(target).health.reason == "asset_unbound"

    class SlowCollector:
        name = "slow"
        source_id = "sha256:" + "a" * 64

        def collect(self, target, cursor=None):
            time.sleep(0.2)
            return CollectionBatch(target.target_id, self.name, datetime.now(timezone.utc), (), CollectorHealth(True))

    class FastCollector:
        name = "fast"
        source_id = "sha256:" + "b" * 64

        def collect(self, target, cursor=None):
            return CollectionBatch(target.target_id, self.name, datetime.now(timezone.utc), (), CollectorHealth(True))

    started = time.monotonic()
    batches = collect_all(target, (SlowCollector(), FastCollector()), deadline_seconds=0.01)
    assert time.monotonic() - started < 0.15
    assert batches[0].health.reason == "collector_timeout"
    assert batches[1].health.healthy is True

    facts = {"nested": {"value": "before"}}
    snapshot = TargetSnapshot(target.target_id, datetime.now(timezone.utc), facts)
    facts["nested"]["value"] = "after"
    assert snapshot.facts["nested"]["value"] == "before"


def test_process_plist_and_cron_collectors_enforce_item_or_byte_budgets(tmp_path):
    target = _target(tmp_path)

    class Process:
        def __init__(self, pid):
            self.pid = pid

        def name(self):
            return "hermes-gateway"

        def cmdline(self):
            return ["hermes", "ai.hermes.gateway-g2test", "g2test"]

        def uids(self):
            class Uids: real = __import__("os").getuid()
            return Uids()

    command = Process(1).cmdline()
    fingerprint = "sha256:" + hashlib.sha256("\x00".join(command).encode()).hexdigest()
    bound = Target(TargetSpec(target_id=target.target_id, profile=target.spec.profile, kind=target.spec.kind, criticality=target.spec.criticality, observed_paths=target.spec.observed_paths, labels={"service_label": "ai.hermes.gateway-g2test", "process_marker": "g2test", "command_fingerprint": fingerprint}))
    process_batch = ProcessCollector(process_iter=lambda: [Process(1), Process(2)], max_items=1).collect(bound)
    assert len(process_batch.signals) == 1

    plist = tmp_path / "too-large.plist"
    plist.write_bytes(b"x" * 32)
    assert LaunchdCollector(plist, max_bytes=1).collect(target).health.reason == "plist_path_rejected"

    source = tmp_path / "cron.json"
    source.write_text("{}", encoding="utf-8")
    now = datetime.now(timezone.utc)
    assertions = tuple(BusinessAssertion(f"a{index}", True, {}, now) for index in range(2))
    cron_batch = CronCollector(
        CronObservation(CronExecution("job", now, 0, True), assertions),
        source_path=source,
        required_assertion_ids=("a0",),
        max_assertions=1,
    ).collect(target)
    assert cron_batch.health.reason == "cron_assertion_budget_exceeded"


def test_unrelated_existing_observer_database_is_unchanged_before_preflight(write_config):
    config = load_agentops_config(write_config())
    unrelated = config.state_dir / "observer.db"
    connection = sqlite3.connect(unrelated)
    connection.execute("CREATE TABLE unrelated (value TEXT)")
    connection.execute("INSERT INTO unrelated VALUES ('preserve')")
    connection.commit()
    journal_before = connection.execute("PRAGMA journal_mode").fetchone()[0]
    connection.close()
    unrelated.chmod(0o600)
    before = unrelated.read_bytes()

    with pytest.raises(ObserverStoreError):
        open_observer_store(config)

    check = sqlite3.connect(unrelated)
    try:
        assert check.execute("PRAGMA journal_mode").fetchone()[0] == journal_before
    finally:
        check.close()
    assert unrelated.read_bytes() == before


def test_same_named_incompatible_observer_schema_is_rejected_without_wal_or_bytes_change(write_config):
    config = load_agentops_config(write_config())
    path = config.state_dir / "observer.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY); INSERT INTO schema_migrations VALUES (1); CREATE TABLE target_snapshots(target_id TEXT PRIMARY KEY); CREATE TABLE signals(signal_id TEXT PRIMARY KEY); CREATE TABLE collector_cursors(target_id TEXT, collector TEXT, inode INTEGER, offset INTEGER, PRIMARY KEY(target_id,collector));")
    db.commit(); db.close(); path.chmod(0o600)
    before = path.read_bytes()
    with pytest.raises(ObserverStoreError):
        open_observer_store(config)
    assert path.read_bytes() == before
    assert not path.with_name("observer.db-wal").exists()


def test_all_persisted_record_strings_are_redacted_and_occurrences_cursors_are_monotonic(tmp_path, write_config):
    target = _target(tmp_path)
    now = datetime.now(timezone.utc)
    signal = redact_signal(RawSignal(target.target_id, "test.collector", "signal.record", now, {"ok": True}))
    store = open_observer_store(load_agentops_config(write_config()))
    try:
        newer = CollectionBatch(target.target_id, "test.collector", now, (signal,), CollectorHealth(False, "password=hunter2"), next_cursor=LogCursor(1, 100, "sha256:" + "1" * 64), source_id="sha256:" + "1" * 64)
        store.commit_collection(newer)
        older = CollectionBatch(target.target_id, "test.collector", now - timedelta(seconds=5), (signal,), CollectorHealth(True), next_cursor=LogCursor(1, 1, "sha256:" + "1" * 64), source_id="sha256:" + "1" * 64)
        store.commit_collection(older)
        raw = b"".join(path.read_bytes() for path in store.path.parent.glob("observer.db*"))
        assert b"hunter2" not in raw
        assert store.get_cursor(target.target_id, "test.collector", "sha256:" + "1" * 64).offset == 100
        row = store._connection.execute("SELECT first_seen,last_seen FROM signal_occurrences WHERE signal_id=?", (signal.signal_id,)).fetchone()
        assert row[0] <= row[1]
    finally:
        store.close()


def test_cron_unknown_mandatory_and_stale_execution_are_unhealthy(tmp_path):
    source = tmp_path / "status.json"; source.write_text("{}")
    old = datetime.now(timezone.utc) - timedelta(hours=1)
    observation = CronObservation(CronExecution("job", old, 0, True, max_age_seconds=10), (BusinessAssertion("not-in-pack", True, {}, datetime.now(timezone.utc), mandatory=True),))
    batch = CronCollector(observation, source_path=source, required_assertion_ids=("not-in-pack",)).collect(_target(tmp_path))
    assert not batch.health.healthy
    assert any(signal.signal_type == "cron.business_assertion_unknown" for signal in batch.signals)
    assert any(signal.signal_type == "cron.execution_stale" for signal in batch.signals)


def test_bridge_concurrent_drain_claims_each_event_once(make_event):
    bridge = BoundedBridgeBuffer(capacity=8)
    for index in range(4):
        bridge.publish(make_event(f"evt-drain-{index}"), lambda _: (_ for _ in ()).throw(ConnectionError()))
    seen = []; lock = threading.Lock()
    def deliver(event):
        time.sleep(0.01)
        with lock: seen.append(event.event_id)
    threads = [threading.Thread(target=lambda: bridge.drain(deliver)) for _ in range(3)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert len(seen) == len(set(seen)) == 4


def test_manifest_loader_executes_entry_capability_and_budget_validation(tmp_path):
    pack = load_review_pack()
    assert pack.validate_collector("logs", TargetKind.GATEWAY).entry.endswith(":LogCollector")
    bad = tmp_path / "manifest.yaml"
    bad.write_text("schema_version: 2\nauthority_mode: observe_only\nexecution: {no_write: true, action_execution: disabled}\npack: {id: x, version: 1}\ntarget_kinds: [gateway]\ninputs: {retention_days: 1, collectors: [{id: logs, entry: plugins.agentops.control.collectors.logs:Missing, capabilities: [read], target_kinds: [gateway], max_bytes: 99999999, max_items: 1, deadline_seconds: 1, rate_limit_seconds: 1}]}\nassertions: [{id: a, severity: warning, mandatory: true}]\n")
    with pytest.raises(ManifestValidationError): load_review_pack(bad)


def test_store_rejects_legacy_trigger_object_before_migration(write_config):
    config = load_agentops_config(write_config())
    path = config.state_dir / "observer.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY); INSERT INTO schema_migrations VALUES (1); CREATE TABLE target_snapshots(target_id TEXT PRIMARY KEY, observed_at TEXT NOT NULL, facts_json TEXT NOT NULL, collector_version TEXT NOT NULL); CREATE TABLE signals(signal_id TEXT PRIMARY KEY, target_id TEXT NOT NULL, collector TEXT NOT NULL, signal_type TEXT NOT NULL, observed_at TEXT NOT NULL, severity TEXT NOT NULL, redaction_version INTEGER NOT NULL, payload_json TEXT NOT NULL); CREATE TABLE collector_cursors(target_id TEXT NOT NULL, collector TEXT NOT NULL, inode INTEGER NOT NULL, offset INTEGER NOT NULL, PRIMARY KEY(target_id,collector)); CREATE TRIGGER extra AFTER INSERT ON signals BEGIN SELECT 1; END;")
    db.commit(); db.close(); path.chmod(0o600)
    before = path.read_bytes()
    with pytest.raises(ObserverStoreError): open_observer_store(config)
    assert path.read_bytes() == before


def test_cron_file_is_reparsed_and_duplicate_names_rejected(tmp_path):
    source = tmp_path / "cron.json"
    now = datetime.now(timezone.utc).isoformat()
    source.write_text('{"execution":{"job_id":"j","observed_at":"'+now+'","exit_code":0,"completed":true},"assertions":[{"name":"cron_business_assertion_fresh","passed":true,"observed_at":"'+now+'"}]}')
    collector = CronCollector.from_json_file(source, required_assertion_ids=("cron_business_assertion_fresh",))
    assert collector.collect(_target(tmp_path)).health.healthy
    source.write_text("not-json")
    assert collector.collect(_target(tmp_path)).health.reason == "cron_source_invalid"
    with pytest.raises(ValueError):
        CronObservation(CronExecution("j", datetime.now(timezone.utc), 0, True), (BusinessAssertion("x", True), BusinessAssertion("x", True)))


def test_process_zero_match_and_launchd_label_mismatch_are_unhealthy(tmp_path):
    target = _target(tmp_path)
    labels = dict(target.spec.labels); labels.update(process_marker="other", command_fingerprint="sha256:"+"0"*64)
    bound = Target(TargetSpec(target.spec.target_id, target.spec.profile, target.spec.kind, target.spec.criticality, target.spec.observed_paths, labels))
    class P:
        pid=1
        def name(self): return "hermes"
        def cmdline(self): return ["hermes", "wrong"]
        def uids(self):
            class U: real=__import__("os").getuid()
            return U()
    assert ProcessCollector(process_iter=lambda:[P()]).collect(bound).health.reason == "process_binding_no_match"
    plist = tmp_path / "mismatch.plist"; plistlib.dump({"Label":"wrong"}, plist.open("wb"))
    assert LaunchdCollector(plist).collect(target).health.reason == "plist_label_mismatch"


def test_review_pack_factory_applies_runtime_target_and_budget(tmp_path):
    pack = load_review_pack()
    with pytest.raises(ManifestValidationError): build_collector("logs", target_kind=TargetKind.CRON, pack=pack, name="logs", path=tmp_path/"x", max_bytes=pack.collectors["logs"].max_bytes+1)


def test_legacy_v1_secret_is_rejected_before_migration(write_config):
    config = load_agentops_config(write_config()); path = config.state_dir / "observer.db"; db = sqlite3.connect(path)
    db.executescript('''CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY); INSERT INTO schema_migrations VALUES (1); CREATE TABLE target_snapshots(target_id TEXT PRIMARY KEY, observed_at TEXT NOT NULL, facts_json TEXT NOT NULL, collector_version TEXT NOT NULL); CREATE TABLE signals(signal_id TEXT PRIMARY KEY, target_id TEXT NOT NULL, collector TEXT NOT NULL, signal_type TEXT NOT NULL, observed_at TEXT NOT NULL, severity TEXT NOT NULL, redaction_version INTEGER NOT NULL, payload_json TEXT NOT NULL); CREATE TABLE collector_cursors(target_id TEXT NOT NULL, collector TEXT NOT NULL, inode INTEGER NOT NULL, offset INTEGER NOT NULL, PRIMARY KEY(target_id,collector)); INSERT INTO signals VALUES ('sha256:x','hermes:profile:x:gateway','c','t','2026-01-01T00:00:00+00:00','warning',1,'{"password":"hunter2"}');''')
    db.commit(); db.close(); path.chmod(0o600)
    with pytest.raises(ObserverStoreError): open_observer_store(config)


def test_cursor_truncate_reset_and_cross_source_independence(tmp_path, write_config):
    target = _target(tmp_path); now = datetime.now(timezone.utc); store = open_observer_store(load_agentops_config(write_config()))
    try:
        def batch(source, offset, at):
            return CollectionBatch(target.target_id, "logs", at, (), CollectorHealth(True), LogCursor(7, offset, source), source_id=source)
        a = "sha256:"+"a"*64; b = "sha256:"+"b"*64
        store.commit_collection(batch(a, 10, now)); store.commit_collection(batch(b, 15, now + timedelta(seconds=1))); store.commit_collection(batch(a, 4, now + timedelta(seconds=2)))
        assert store.get_cursor(target.target_id, "logs", a).offset == 4
        assert store.get_cursor(target.target_id, "logs", b).offset == 15
    finally: store.close()


def test_cron_execution_json_max_age_is_enforced(tmp_path):
    source = tmp_path / "cron.json"; old = (datetime.now(timezone.utc)-timedelta(seconds=30)).isoformat()
    source.write_text('{"execution":{"job_id":"j","observed_at":"'+old+'","exit_code":0,"completed":true,"max_age_seconds":1},"assertions":[]}')
    batch = CronCollector.from_json_file(source, required_assertion_ids=("cron_business_assertion_fresh",)).collect(_target(tmp_path))
    assert batch.health.reason == "cron_execution_stale"
