from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from tools.openai_media_spend import (
    SpendPolicyError,
    cancel_reservation,
    gate,
    record,
)


@pytest.fixture
def spend_env(tmp_path, monkeypatch):
    ledger = tmp_path / "spend.sqlite"
    monkeypatch.setenv(
        "OPENAI_API_ALLOWED_OPERATIONS",
        "image_generation,transcription",
    )
    monkeypatch.setenv("HERMES_API_SPEND_CALLER", "pytest")
    monkeypatch.setenv("API_SPEND_LEDGER", str(ledger))
    monkeypatch.setenv("API_SPEND_DAILY_HARD_USD", "1.00")
    return ledger


def test_gate_fails_closed_when_operation_is_not_allowed(spend_env, monkeypatch):
    monkeypatch.setenv("OPENAI_API_ALLOWED_OPERATIONS", "transcription")

    with pytest.raises(SpendPolicyError, match="not allowed"):
        gate("image_generation", "gpt-image-2", 0.01)


def test_recorded_spend_counts_toward_daily_hard_cap(spend_env):
    row_id = record(
        "image_generation",
        "gpt-image-2",
        0.75,
        estimated=True,
    )

    assert row_id > 0
    with pytest.raises(SpendPolicyError, match="hard cap"):
        gate("image_generation", "gpt-image-2", 0.30)

    with sqlite3.connect(spend_env) as con:
        assert con.execute("SELECT COUNT(*) FROM api_spend_events").fetchone()[0] == 1


def test_gate_reservation_is_reconciled_in_place(spend_env):
    reservation = gate("image_generation", "gpt-image-2", 0.40)

    row_id = record(
        "image_generation",
        "gpt-image-2",
        0.25,
        estimated=False,
        reservation_id=reservation["reservation_id"],
    )

    assert row_id == reservation["reservation_id"]
    with sqlite3.connect(spend_env) as con:
        rows = con.execute(
            "SELECT estimated_usd, estimated, status FROM api_spend_events"
        ).fetchall()
    assert rows == [(0.25, 0, "recorded")]


def test_concurrent_gates_cannot_overbook_daily_hard_cap(spend_env):
    def attempt() -> str:
        try:
            gate("image_generation", "gpt-image-2", 0.60)
            return "reserved"
        except SpendPolicyError:
            return "blocked"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _index: attempt(), range(2)))

    assert sorted(outcomes) == ["blocked", "reserved"]
    with sqlite3.connect(spend_env) as con:
        rows = con.execute(
            "SELECT estimated_usd, status FROM api_spend_events"
        ).fetchall()
    assert rows == [(0.60, "reserved")]


def test_cancel_reservation_releases_pre_call_budget(spend_env):
    reservation = gate("transcription", "gpt-4o-transcribe", 0.40)

    cancel_reservation(reservation["reservation_id"], "client_initialization_failed")

    with sqlite3.connect(spend_env) as con:
        rows = con.execute(
            "SELECT estimated_usd, status, metadata_json FROM api_spend_events"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0:2] == (0.0, "cancelled")
    assert "client_initialization_failed" in rows[0][2]

    # The released amount must not consume the hard cap.
    next_reservation = gate("transcription", "gpt-4o-transcribe", 1.0)
    assert next_reservation["reservation_id"] != reservation["reservation_id"]
