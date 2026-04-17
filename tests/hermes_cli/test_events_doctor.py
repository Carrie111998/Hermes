"""Tests for hermes events doctor CLI diagnostic."""
import json
import sqlite3

from hermes_cli.events_doctor import run_doctor


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
