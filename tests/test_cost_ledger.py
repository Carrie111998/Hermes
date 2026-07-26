"""CS-02 synchronous cost-ledger acceptance tests."""

from __future__ import annotations

import sqlite3

import pytest

from hermes_cli.cost import config, ledger


@pytest.fixture
def cost_db(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setattr(ledger, "DB_PATH", db_path)
    ledger.migrate()
    return db_path


def _record(**overrides):
    values = {
        "task_id": "t_1",
        "lane": "platform",
        "vendor": "anthropic",
        "model_slug": "anthropic/claude-opus-5",
        "attempt_number": 1,
        "rung_id": None,
        "escalation": False,
        "input_tokens": 100,
        "output_tokens": 25,
        "cached_input_tokens": 10,
        "usd_amount": 1.00,
        "latency_ms": 250,
        "request_id": "req_1",
        "raw_response_meta": {"finish_reason": "stop"},
    }
    values.update(overrides)
    return ledger.record_call(**values)


def test_migration_creates_cost_ledger_table(cost_db):
    with sqlite3.connect(cost_db) as conn:
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='cost_ledger'"
        ).fetchone()
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='cost_ledger'"
            )
        }
    assert table == ("cost_ledger",)
    assert "idx_cost_ledger_ts_lane" in indexes
    assert "idx_cost_ledger_task" in indexes


def test_record_call_writes_row(cost_db):
    entry = _record()
    with sqlite3.connect(cost_db) as conn:
        row = conn.execute("SELECT * FROM cost_ledger").fetchone()
    assert row is not None
    assert entry.id == row[0]
    assert entry.task_id == "t_1"
    assert entry.lane == "platform"
    assert entry.vendor == "anthropic"
    assert entry.model_slug == "anthropic/claude-opus-5"
    assert entry.input_tokens == 100
    assert entry.output_tokens == 25
    assert entry.cached_input_tokens == 10
    assert entry.request_id == "req_1"


def test_openrouter_surcharge_applied(cost_db):
    entry = _record(vendor="openrouter", usd_amount=1.00)
    assert entry.surcharge_applied == pytest.approx(0.055)
    assert entry.aud_amount == pytest.approx(1.6036)


def test_anthropic_no_surcharge(cost_db):
    entry = _record(vendor="anthropic", usd_amount=1.00)
    assert entry.surcharge_applied == 0.0
    assert entry.aud_amount == pytest.approx(1.52)


def test_openai_no_surcharge(cost_db):
    entry = _record(
        vendor="openai",
        model_slug="openai/gpt-5.6-sol",
        usd_amount=1.00,
    )
    assert entry.surcharge_applied == 0.0
    assert entry.aud_amount == pytest.approx(1.52)


def test_aud_computed_from_configured_fx(cost_db, monkeypatch):
    monkeypatch.setattr(config, "FX_RATE", 1.60)
    entry = _record(vendor="anthropic", usd_amount=1.00)
    assert entry.fx_rate == pytest.approx(1.60)
    assert entry.aud_amount == pytest.approx(1.60)


def test_ledger_row_has_timestamp_z_suffix(cost_db):
    assert _record().ts.endswith("Z")


def test_atomic_write(cost_db, monkeypatch):
    def fail_after_insert(_row):
        raise RuntimeError("forced post-insert failure")

    monkeypatch.setattr(ledger, "_row_to_entry", fail_after_insert)
    with pytest.raises(RuntimeError, match="forced post-insert failure"):
        _record()
    with sqlite3.connect(cost_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM cost_ledger").fetchone()[0] == 0
