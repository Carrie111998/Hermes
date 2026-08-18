from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import hermes_cli.kanban_summary as summary
from hermes_cli.config import DEFAULT_CONFIG
from scripts.install_kanban_summary import write_user_units


def _usage(**overrides):
    value = {
        "schema": "hermes.worker.v1",
        "provider": "grok",
        "model": "grok-build-0.1",
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
        "source": "worker_reported",
    }
    value.update(overrides)
    return value


def test_token_usage_is_all_or_nothing_and_bounds_strings():
    valid, reason = summary.validate_token_usage(_usage(cache_read_tokens=2))
    assert reason is None
    assert valid["cache_read_tokens"] == 2

    invalid, reason = summary.validate_token_usage(_usage(output_tokens="5"))
    assert invalid is None
    assert reason == "output_tokens"

    invalid, reason = summary.validate_token_usage(_usage(provider="grok\nleak"))
    assert invalid is None
    assert reason == "identity"


def test_rollup_counts_invalid_records_and_accounting_anomalies():
    rows = [
        {"metadata": {"token_usage": _usage(cache_read_tokens=2, total_tokens=17)}},
        {"metadata": {"token_usage": _usage(output_tokens="bad")}},
        {"metadata": {"other": "ignored"}},
    ]
    rollup = summary._run_token_rollup(rows, truncated=False)
    assert rollup["valid_records"] == 1
    assert rollup["invalid_token_usage_count"] == 1
    assert rollup["accounting_anomaly_count"] == 0
    assert rollup["input_tokens"] == 10
    assert rollup["cache_read_tokens"] == 2


def test_rollup_keeps_anomalous_record_and_sums_usd_micros():
    rows = [{"metadata": {"token_usage": _usage(
        input_tokens=1,
        output_tokens=1,
        total_tokens=5000,
        estimated_cost_usd="1.25",
    )}}]
    rollup = summary._run_token_rollup(rows, truncated=False)
    assert rollup["valid_records"] == 1
    assert rollup["accounting_anomaly_count"] == 1
    assert rollup["total_tokens"] == 5000
    assert rollup["estimated_cost_usd_micros_by_provider_model"] == {
        "grok/grok-build-0.1": 1_250_000
    }


def test_provider_and_model_length_bounds_are_rejected():
    invalid, reason = summary.validate_token_usage(_usage(provider="p" * 129))
    assert invalid is None and reason == "identity"


def test_target_is_read_from_non_secret_config_section():
    assert summary._target_from_config({
        "kanban": {"summary": {"telegram_target": "telegram:-123"}}
    }) == "telegram:-123"
    assert summary.validate_target(summary._target_from_config({"kanban": {"summary": {"telegram_target": ""}}})) is None
    assert DEFAULT_CONFIG["kanban"]["summary"]["telegram_target"] == ""


def test_fingerprint_is_canonical_and_zero_valid_rollup_is_empty():
    assert summary.summary_fingerprint({"b": 2, "a": 1}) == summary.summary_fingerprint({"a": 1, "b": 2})
    rollup = summary._run_token_rollup([{"metadata": {}}, {"metadata": None}], truncated=False)
    assert rollup["valid_records"] == 0
    assert rollup["invalid_token_usage_count"] == 0
    assert rollup["input_tokens"] == rollup["total_tokens"] == 0


def test_invalid_schema_and_cost_are_rejected():
    invalid, reason = summary.validate_token_usage(_usage(schema="hermes.worker.v0"))
    assert invalid is None and reason == "schema"
    invalid, reason = summary.validate_token_usage(_usage(estimated_cost_usd=-1))
    assert invalid is None and reason == "estimated_cost_usd"
    invalid, reason = summary.validate_token_usage(_usage(estimated_cost_usd="not-a-number"))
    assert invalid is None and reason == "estimated_cost_usd"
    invalid, reason = summary.validate_token_usage(_usage(estimated_cost_usd=10**12))
    assert invalid is None and reason == "estimated_cost_usd"


def test_recent_run_reader_is_bounded_and_reports_truncation(monkeypatch, tmp_path: Path):
    db_path = tmp_path / "kanban.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT)")
    conn.execute("CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, metadata TEXT, ended_at INTEGER)")
    conn.execute("INSERT INTO tasks VALUES ('task-opaque', 'done')")
    conn.executemany(
        "INSERT INTO task_runs(task_id, metadata, ended_at) VALUES (?, ?, ?)",
        [("task-opaque", json.dumps({}), 1_000_000) for _ in range(summary.MAX_RUNS + 1)],
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(summary, "kanban_db_path", lambda board: db_path)
    rows, truncated = summary.load_recent_run_metadata("default", now=1_000_000)
    assert len(rows) == summary.MAX_RUNS
    assert truncated is True


def test_corrupt_json_state_fails_closed(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(summary, "build_summary", lambda board, now=None: {
        "schema": summary.SUMMARY_SCHEMA, "board": "default", "queue_counts": {},
        "diagnostics": [], "token_rollup": {
            "window_days": 90, "max_runs": 10000, "scanned_runs": 0,
            "truncated": False, "valid_records": 0,
            "invalid_token_usage_count": 0, "accounting_anomaly_count": 0,
            "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
            "cache_read_tokens": 0, "cache_write_tokens": 0,
            "by_provider_model": {},
        },
    })
    state_path, _lock_path = summary._state_paths(tmp_path, "default", "telegram:-123")
    state_path.parent.mkdir(exist_ok=True)
    state_path.write_text("{", encoding="utf-8")
    monkeypatch.setattr(summary, "_send", lambda target, message: (_ for _ in ()).throw(AssertionError("send")))
    assert summary.run_once(board="default", target="telegram:-123", state_dir=tmp_path) == "state_unreadable"


def test_unit_renderer_fills_all_placeholders_without_enabling(tmp_path: Path):
    outputs = write_user_units(
        Path("/home/cwliao/.hermes"),
        Path("/home/cwliao/.hermes/releases/test"),
        Path("/home/cwliao/.hermes/hermes-agent/venv/bin/python"),
        "default",
        unit_dir=tmp_path,
    )
    assert {path.name for path in outputs} == {
        "hermes-kanban-summary.service", "hermes-kanban-summary.timer"
    }
    rendered = (tmp_path / "hermes-kanban-summary.service").read_text(encoding="utf-8")
    assert "@" not in rendered
    assert "systemctl" not in rendered
    assert "WorkingDirectory=" in rendered
    invalid, reason = summary.validate_token_usage(_usage(model="😀" * 129))
    assert invalid is None and reason == "identity"


def test_build_summary_projects_only_metadata_safe_diagnostics(monkeypatch):
    def fake_json(kind, board):
        assert board == "default"
        if kind == "stats":
            return {"by_status": {"ready": 2, "running": 1}}
        return [
            {
                "task_id": "task-opaque",
                "title": "must not escape",
                "diagnostics": [
                    {"kind": "repeated_failures", "severity": "error", "detail": "secret"},
                    {"kind": "ignored", "severity": "debug"},
                ],
            }
        ]

    monkeypatch.setattr(summary, "_run_hermes_json", fake_json)
    monkeypatch.setattr(summary, "load_recent_run_metadata", lambda board, now=None: ([], False))
    result = summary.build_summary("default", now=1)
    assert result["queue_counts"] == {"ready": 2, "running": 1}
    assert result["diagnostics"] == [
        {"task_id": "task-opaque", "severity": "error", "rule": "repeated_failures"}
    ]
    assert "secret" not in json.dumps(result)


def test_run_once_sends_once_then_deduplicates(monkeypatch, tmp_path: Path):
    payload = {
        "schema": summary.SUMMARY_SCHEMA,
        "board": "default",
        "queue_counts": {"ready": 1},
        "diagnostics": [],
        "token_rollup": {
            "window_days": 90,
            "max_runs": 10000,
            "scanned_runs": 0,
            "truncated": False,
            "valid_records": 0,
            "invalid_token_usage_count": 0,
            "accounting_anomaly_count": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "by_provider_model": {},
        },
    }
    monkeypatch.setattr(summary, "build_summary", lambda board, now=None: payload)
    sent = []
    monkeypatch.setattr(summary, "_send", lambda target, message: sent.append((target, message)) or True)
    assert summary.run_once(board="default", target="telegram:-123", state_dir=tmp_path, now=100) == "sent"
    assert summary.run_once(board="default", target="telegram:-123", state_dir=tmp_path, now=101) == "unchanged"
    assert len(sent) == 1
    state_files = list(tmp_path.glob("*.json"))
    assert len(state_files) == 1
    if summary.os.name != "nt":
        assert state_files[0].stat().st_mode & 0o777 == 0o600


def test_send_failure_does_not_advance_state(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(summary, "build_summary", lambda board, now=None: {
        "schema": summary.SUMMARY_SCHEMA,
        "board": "default",
        "queue_counts": {},
        "diagnostics": [],
        "token_rollup": {
            "window_days": 90, "max_runs": 10000, "scanned_runs": 0,
            "truncated": False, "valid_records": 0,
            "invalid_token_usage_count": 0, "accounting_anomaly_count": 0,
            "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
            "cache_read_tokens": 0, "cache_write_tokens": 0,
            "by_provider_model": {},
        },
    })
    monkeypatch.setattr(summary, "_send", lambda target, message: False)
    assert summary.run_once(board="default", target="telegram:-123", state_dir=tmp_path) == "send_failed"
    assert not list(tmp_path.glob("*.json"))


def test_send_failure_is_retryable_on_next_invocation(monkeypatch, tmp_path: Path):
    payload = {
        "schema": summary.SUMMARY_SCHEMA, "board": "default",
        "queue_counts": {"ready": 1}, "diagnostics": [],
        "token_rollup": {
            "window_days": 90, "max_runs": 10000, "scanned_runs": 0,
            "truncated": False, "valid_records": 0,
            "invalid_token_usage_count": 0, "accounting_anomaly_count": 0,
            "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
            "cache_read_tokens": 0, "cache_write_tokens": 0,
            "by_provider_model": {},
        },
    }
    monkeypatch.setattr(summary, "build_summary", lambda board, now=None: payload)
    outcomes = iter((False, True))
    monkeypatch.setattr(summary, "_send", lambda target, message: next(outcomes))
    assert summary.run_once(board="default", target="telegram:-123", state_dir=tmp_path) == "send_failed"
    assert summary.run_once(board="default", target="telegram:-123", state_dir=tmp_path) == "sent"


def test_invalid_target_fails_closed_without_state_or_send(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(summary, "_send", lambda target, message: (_ for _ in ()).throw(AssertionError("send")))
    assert summary.run_once(board="default", target="discord:123", state_dir=tmp_path) == "invalid_target_or_board"
    assert not list(tmp_path.glob("*.json"))


def test_lock_held_skips_without_sending(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(summary, "_send", lambda target, message: (_ for _ in ()).throw(AssertionError("send")))
    _state, lock = summary._state_paths(tmp_path, "default", "telegram:-123")
    with summary._instance_lock(lock) as locked:
        assert locked
        assert summary.run_once(board="default", target="telegram:-123", state_dir=tmp_path) == "lock_held"


def test_corrupt_or_oversized_state_fails_closed(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(summary, "build_summary", lambda board, now=None: {"same": True})
    target = "telegram:-123"
    state_path, _ = summary._state_paths(tmp_path, "default", target)
    state_path.parent.mkdir(exist_ok=True)
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(summary, "_send", lambda target, message: (_ for _ in ()).throw(AssertionError("send")))
    assert summary.run_once(board="default", target=target, state_dir=tmp_path) == "state_schema"
    state_path.write_bytes(b"x" * (summary.MAX_STATE_BYTES + 1))
    assert summary.run_once(board="default", target=target, state_dir=tmp_path) == "state_oversized"


def test_instance_key_separates_board_and_target():
    assert summary._instance_key("a", "telegram:1") != summary._instance_key("b", "telegram:1")
    assert summary._instance_key("a", "telegram:1") != summary._instance_key("a", "telegram:2")
