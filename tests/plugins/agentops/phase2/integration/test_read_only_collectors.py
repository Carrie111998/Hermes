from datetime import datetime, timezone
import hashlib
import plistlib
import sqlite3

from plugins.agentops.control.collectors.cron import CronCollector
from plugins.agentops.control.collectors.git_state import GitStateCollector
from plugins.agentops.control.collectors.launchd import LaunchdCollector
from plugins.agentops.control.collectors.processes import ProcessCollector
from plugins.agentops.control.collectors.sqlite_health import SQLiteHealthCollector
from plugins.agentops.control.observer_models import BusinessAssertion, CronExecution
from plugins.agentops.control.registry import bootstrap_gateway_registry


def _target():
    return bootstrap_gateway_registry().list_targets()[0]


def test_zero_exit_with_failed_business_assertion_is_unhealthy():
    collector = CronCollector(
        lambda: (
            CronExecution("aivault-watch", datetime(2026, 8, 9, tzinfo=timezone.utc), 0, True),
            (BusinessAssertion("fresh-business-output", False, {"message": "missing"}),),
        )
    )

    batch = collector.collect(_target())

    assert batch.health.healthy is False
    assert any(signal.signal_type == "cron.business_assertion_failed" for signal in batch.signals)


def test_sqlite_collector_keeps_target_database_bytes_unchanged(tmp_path):
    database = tmp_path / "target.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE facts (value TEXT)")
    connection.execute("INSERT INTO facts VALUES ('one')")
    connection.commit()
    connection.close()
    before = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in tmp_path.iterdir()
        if path.is_file()
    }

    batch = SQLiteHealthCollector(database).collect(_target())

    after = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in tmp_path.iterdir()
        if path.is_file()
    }
    assert batch.health.healthy is True
    assert before == after


def test_plist_process_and_git_collectors_expose_read_only_fingerprints(tmp_path):
    plist = tmp_path / "gateway.plist"
    with plist.open("wb") as handle:
        plistlib.dump({"Label": "ai.hermes.gateway", "ProgramArguments": ["/bin/example", "--token", "secret"]}, handle)
    plist_batch = LaunchdCollector(plist).collect(_target())
    assert plist_batch.health.healthy is True
    assert "configuration_fingerprint" in plist_batch.signals[0].payload
    assert "secret" not in str(plist_batch.signals[0].payload)

    class Process:
        pid = 77

        def name(self):
            return "hermes-gateway"

        def cmdline(self):
            return ["hermes", "--token", "secret"]

    process_batch = ProcessCollector(process_iter=lambda: [Process()]).collect(_target())
    assert process_batch.signals[0].payload["command_fingerprint"].startswith("sha256:")

    git_dir = tmp_path / "repo" / ".git"
    (git_dir / "refs" / "heads").mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git_dir / "refs" / "heads" / "main").write_text("a" * 40 + "\n", encoding="utf-8")
    (git_dir / "config").write_text('[branch "main"]\nremote = origin\nmerge = refs/heads/main\n', encoding="utf-8")
    git_batch = GitStateCollector(git_dir.parent).collect(_target())
    assert git_batch.health.healthy is True
    assert git_batch.signals[0].payload["dirty"] == "unknown"
    assert git_batch.signals[0].payload["upstream"] == "origin:refs/heads/main"
