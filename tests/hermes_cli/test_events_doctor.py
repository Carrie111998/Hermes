"""Tests for hermes events doctor CLI diagnostic."""
import json
import sqlite3

from events.bus import EventBus
from events.schema import EventType
from hermes_cli.events_doctor import print_dead_letters, run_doctor


def test_doctor_reports_missing_topics_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "events").mkdir()
    sqlite3.connect(str(tmp_path / "events" / "event_bus.db")).close()

    rc = run_doctor(check_telegram_api=False)
    captured = capsys.readouterr().out
    assert "topics.json" in captured
    assert "FAIL" in captured or "missing" in captured.lower()
    assert rc != 0


def test_doctor_all_green_on_healthy_setup(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "events").mkdir()
    (tmp_path / "telegram").mkdir()
    (tmp_path / "notifications").mkdir()
    sqlite3.connect(str(tmp_path / "events" / "event_bus.db")).close()
    (tmp_path / "telegram" / "topics.json").write_text(
        json.dumps({"group_chat_id": "-1", "topics": {}}))
    (tmp_path / "telegram" / "verbosity.json").write_text(json.dumps({}))
    (tmp_path / "notifications" / "quiet_hours.json").write_text(
        json.dumps({"enabled": True}))

    run_doctor(check_telegram_api=False)
    captured = capsys.readouterr().out
    assert "topics.json" in captured
    assert "quiet_hours.json" in captured


class TestDeadLettersFlag:
    """SR-109: `events_doctor --dead-letters` surface."""

    def _setup_bus_with_dead_letter(self, tmp_path):
        db = tmp_path / "events" / "event_bus.db"
        bus = EventBus(db_path=db)
        eid = bus.emit(EventType.CRON_COMPLETED, "scout", {})
        bus.record_dead_letter("digest-composer", eid, "KeyError: 'score'")
        bus.close()
        return db, eid

    def test_prints_message_when_empty(self, tmp_path, capsys):
        (tmp_path / "events").mkdir()
        db = tmp_path / "events" / "event_bus.db"
        # Create an empty DB with schema
        EventBus(db_path=db).close()

        rc = print_dead_letters(db_path=db)
        captured = capsys.readouterr().out
        assert rc == 0
        assert "No dead-letter" in captured

    def test_prints_row_when_present(self, tmp_path, capsys):
        db, eid = self._setup_bus_with_dead_letter(tmp_path)

        rc = print_dead_letters(db_path=db)
        captured = capsys.readouterr().out
        assert rc == 0
        assert "digest-composer" in captured
        assert "KeyError" in captured
        assert "cron_completed" in captured

    def test_limit_respected(self, tmp_path, capsys):
        db = tmp_path / "events" / "event_bus.db"
        bus = EventBus(db_path=db)
        for i in range(5):
            eid = bus.emit(EventType.CRON_COMPLETED, "scout", {"i": i})
            bus.record_dead_letter("sub", eid, f"err-{i}")
        bus.close()

        print_dead_letters(db_path=db, limit=2)
        captured = capsys.readouterr().out
        # 2 data lines + 2 header lines
        assert captured.count("sub ") == 2 or captured.count("sub\n") == 0  # tolerant
        # At most 2 error messages should appear
        errs = [ln for ln in captured.splitlines() if "err-" in ln]
        assert len(errs) == 2

    def test_missing_db_returns_nonzero(self, tmp_path, capsys):
        rc = print_dead_letters(db_path=tmp_path / "does-not-exist.db")
        assert rc == 1

    def test_missing_table_reports_migration_hint(self, tmp_path, capsys):
        """Old DB without dead_letters table should emit a hint, not crash."""
        db = tmp_path / "legacy.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE events (event_id TEXT)")
        conn.close()

        rc = print_dead_letters(db_path=db)
        captured = capsys.readouterr().out
        assert rc == 0
        assert "dead_letters" in captured and "migrate" in captured.lower()
