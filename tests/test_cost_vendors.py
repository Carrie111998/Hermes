"""CS-02c all-vendor cost metering acceptance tests."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli.cost import caps, ledger, ratecards, recorders, vendors
from hermes_cli.cost.kill_switch import KillSwitchTripped, PerTaskCapExceeded
from hermes_cli.verdict import schema as verdict_schema


@pytest.fixture
def cost_db(tmp_path: Path, monkeypatch) -> Path:
    db_path = tmp_path / "kanban.db"
    monkeypatch.setattr(ledger, "DB_PATH", db_path)
    monkeypatch.setattr(verdict_schema, "DB_PATH", db_path)
    ledger._MIGRATED_PATHS.discard(str(db_path.resolve()))
    verdict_schema._MIGRATED_PATHS.discard(str(db_path.resolve()))
    ledger.migrate(db_path)
    return db_path


def _rows(db_path: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM cost_ledger ORDER BY id").fetchall()
    finally:
        conn.close()


def test_all_vendors_have_valid_kind():
    assert vendors.VENDORS
    assert all(
        item.kind in vendors.VALID_VENDOR_KINDS
        for item in vendors.VENDORS.values()
    )


def test_openrouter_has_surcharge():
    assert vendors.get_vendor("openrouter").surcharge_pct == 5.5


def test_get_vendor_unknown_raises():
    with pytest.raises(ValueError, match="Unknown vendor"):
        vendors.get_vendor("unregistered")


def test_validate_lane_accepts_all_six_lanes():
    for lane in vendors.ALLOWED_LANES:
        vendors.validate_lane(lane)
    assert len(vendors.ALLOWED_LANES) == 6


def test_validate_lane_rejects_unknown():
    with pytest.raises(ValueError, match="Unknown lane"):
        vendors.validate_lane("mystery")


def test_retell_rate_frozen():
    assert ratecards.RETELL.usd_per_minute == 0.31


def test_retell_usd_conversion():
    assert ratecards.retell_usd(3) == pytest.approx(0.93)


def test_perplexity_small_tier_math():
    assert ratecards.perplexity_usd(1_000, 500) == pytest.approx(0.0015)


def test_perplexity_large_tier_math():
    assert ratecards.perplexity_usd(10_000, 500) == pytest.approx(0.0525)


def test_record_call_openrouter_applies_surcharge(cost_db):
    entry = ledger.record_call(
        task_id="t-openrouter",
        lane="platform",
        vendor="openrouter",
        model="openrouter/test",
        reported_usd=1.0,
        db_path=cost_db,
    )
    assert entry.surcharge_applied == pytest.approx(0.055)
    assert entry.aud_amount == pytest.approx(1.6036)


def test_record_call_retell_uses_voice_minutes_and_rate(cost_db):
    entry = ledger.record_call(
        task_id="t-retell",
        lane="green_captains",
        vendor="retell",
        voice_minutes=3,
        db_path=cost_db,
    )
    assert entry.vendor_kind == "voice_metered"
    assert entry.voice_minutes == 3
    assert entry.usd_amount == pytest.approx(0.93)
    assert entry.aud_amount == pytest.approx(1.4136)


def test_record_call_perplexity_uses_token_math(cost_db):
    entry = ledger.record_call(
        task_id="t-pplx",
        lane="platform",
        vendor="perplexity",
        input_tokens=1_000,
        output_tokens=500,
        db_path=cost_db,
    )
    assert entry.vendor_kind == "search_metered"
    assert entry.usd_amount == pytest.approx(0.0015)
    assert entry.aud_amount == pytest.approx(0.00228)


def _assert_free_vendor(
    cost_db, vendor: str, kind: str
):
    entry = ledger.record_call(
        task_id=f"t-{vendor}",
        lane="platform",
        vendor=vendor,
        api_call_kind=kind,
        db_path=cost_db,
    )
    assert entry.aud_amount == 0.0
    assert entry.is_free_tier is True
    assert entry.api_call_kind == kind


def test_record_call_apple_is_free_tier_zero_aud(cost_db):
    _assert_free_vendor(cost_db, "apple", "appstore_read")


def test_record_call_meta_is_free_tier_zero_aud(cost_db):
    _assert_free_vendor(cost_db, "meta", "leadgen_read")


def test_record_call_github_is_free_tier_zero_aud(cost_db):
    _assert_free_vendor(cost_db, "github", "repo_read")


def test_record_call_openai_codex_is_subscription_bridge_zero_aud(cost_db):
    entry = ledger.record_call(
        task_id="t-codex",
        lane="platform",
        vendor="openai-codex",
        model="openai-codex/gpt-5.6-sol",
        db_path=cost_db,
    )
    assert entry.aud_amount == 0.0
    assert entry.is_subscription_bridge is True


def test_record_call_rejects_unknown_vendor(cost_db):
    with pytest.raises(ValueError, match="Unknown vendor"):
        ledger.record_call(
            task_id="t-x",
            lane="platform",
            vendor="unknown",
            reported_usd=1,
            db_path=cost_db,
        )


def test_record_call_rejects_missing_lane_raises_typeerror():
    with pytest.raises(TypeError):
        ledger.record_call(
            task_id="t-x",
            vendor="retell",
            voice_minutes=1,
        )


def test_record_call_rejects_unknown_lane_valueerror(cost_db):
    with pytest.raises(ValueError, match="Unknown lane"):
        ledger.record_call(
            task_id="t-x",
            lane="unknown",
            vendor="retell",
            voice_minutes=1,
            db_path=cost_db,
        )


def _insert_attribution_row(
    db_path: Path,
    *,
    task_id: str = "t-free",
    lane: str = "platform",
    aud_amount: float = 99.0,
    free: int = 1,
    subscription: int = 0,
) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO cost_ledger (
                ts, task_id, lane, vendor, model_slug, escalation,
                usd_amount, aud_amount, fx_rate, surcharge_applied,
                vendor_kind, is_free_tier, is_subscription_bridge
            ) VALUES (?, ?, ?, 'apple', 'apple/api', 0, 0, ?, 1.52, 0,
                      'free_tier_attributed', ?, ?)
            """,
            (ledger.utc_now(), task_id, lane, aud_amount, free, subscription),
        )


def test_daily_spend_billable_excludes_free_tier(cost_db):
    _insert_attribution_row(cost_db)
    assert caps.daily_spend_aud() == 99.0
    assert caps.daily_spend_aud_billable() == 0.0


def test_daily_spend_billable_excludes_subscription_bridge(cost_db):
    _insert_attribution_row(cost_db, free=0, subscription=1)
    assert caps.daily_spend_aud() == 99.0
    assert caps.daily_spend_aud_billable() == 0.0


def test_lane_spend_billable_excludes_free_tier(cost_db):
    _insert_attribution_row(cost_db, lane="green_captains")
    assert caps.lane_spend_aud("green_captains") == 99.0
    assert caps.lane_spend_aud_billable("green_captains") == 0.0


def test_cap_check_uses_billable_not_gross(cost_db):
    _insert_attribution_row(cost_db, aud_amount=999.0)
    assert caps.check_all_caps("t-free", "platform", False) == (False, None)


def test_migration_adds_new_columns_idempotently(tmp_path):
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE cost_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                task_id TEXT,
                lane TEXT NOT NULL,
                vendor TEXT NOT NULL,
                model_slug TEXT NOT NULL,
                attempt_number INTEGER,
                rung_id TEXT,
                escalation BOOLEAN NOT NULL DEFAULT 0,
                input_tokens INTEGER,
                output_tokens INTEGER,
                cached_input_tokens INTEGER DEFAULT 0,
                usd_amount REAL NOT NULL,
                aud_amount REAL NOT NULL,
                fx_rate REAL NOT NULL,
                surcharge_applied REAL DEFAULT 0.0,
                latency_ms INTEGER,
                request_id TEXT,
                raw_response_meta TEXT
            );
            """
        )
    ledger.migrate(db_path)
    ledger.migrate(db_path)
    with sqlite3.connect(db_path) as conn:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(cost_ledger)")
        }
    assert {
        "vendor_kind",
        "voice_minutes",
        "api_call_kind",
        "is_free_tier",
        "is_subscription_bridge",
    } <= columns


def test_migration_leaves_existing_rows_null_vendor_kind(tmp_path):
    db_path = tmp_path / "legacy-row.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE cost_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL, task_id TEXT, lane TEXT NOT NULL,
                vendor TEXT NOT NULL, model_slug TEXT NOT NULL,
                attempt_number INTEGER, rung_id TEXT,
                escalation BOOLEAN NOT NULL DEFAULT 0,
                input_tokens INTEGER, output_tokens INTEGER,
                cached_input_tokens INTEGER DEFAULT 0,
                usd_amount REAL NOT NULL, aud_amount REAL NOT NULL,
                fx_rate REAL NOT NULL, surcharge_applied REAL DEFAULT 0.0,
                latency_ms INTEGER, request_id TEXT, raw_response_meta TEXT
            );
            INSERT INTO cost_ledger (
                ts, task_id, lane, vendor, model_slug, usd_amount,
                aud_amount, fx_rate
            ) VALUES (
                '2026-01-01T00:00:00Z', 'legacy', 'platform',
                'openrouter', 'openrouter/legacy', 1.0, 1.6, 1.52
            );
            """
        )
    ledger.migrate(db_path)
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT id, task_id, vendor_kind FROM cost_ledger"
        ).fetchone()
    assert row == (1, "legacy", None)


def test_new_indexes_exist(cost_db):
    with sqlite3.connect(cost_db) as conn:
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND tbl_name='cost_ledger'"
            )
        }
    assert {
        "idx_cost_ledger_vendor_kind",
        "idx_cost_ledger_lane_ts",
    } <= indexes


def test_record_retell_call_helper(cost_db):
    row_id = recorders.record_retell_call(
        "t-retell", "green_captains", 2, cost_db
    )
    assert row_id == 1
    assert _rows(cost_db)[0]["vendor"] == "retell"


def test_record_perplexity_call_helper(cost_db):
    row_id = recorders.record_perplexity_call(
        "t-pplx", "platform", 1_200, 400, cost_db
    )
    assert row_id == 1
    assert _rows(cost_db)[0]["vendor"] == "perplexity"


def test_record_apple_api_call_helper(cost_db):
    row_id = recorders.record_apple_api_call(
        "t-apple", "tihna", "appstore_read", cost_db
    )
    assert row_id == 1
    assert _rows(cost_db)[0]["is_free_tier"] == 1


def test_record_meta_api_call_helper(cost_db):
    row_id = recorders.record_meta_api_call(
        "t-meta", "green_captains", "leadgen_read", cost_db
    )
    assert row_id == 1
    assert _rows(cost_db)[0]["vendor"] == "meta"


def test_record_github_api_call_helper(cost_db):
    row_id = recorders.record_github_api_call(
        "t-github", "dayroute", "repo_read", cost_db
    )
    assert row_id == 1
    assert _rows(cost_db)[0]["vendor"] == "github"


class _SessionUsage:
    def __init__(self):
        self.calls = 0

    def record_auxiliary_usage(self, *args, **kwargs):
        self.calls += 1


def _aux_response():
    return SimpleNamespace(
        model="perplexity/sonar",
        usage=SimpleNamespace(prompt_tokens=1_200, completion_tokens=400),
    )


def test_aux_perplexity_call_writes_ledger_row_when_wired(cost_db):
    from agent.aux_accounting import (
        record_aux_usage,
        reset_accounting_context,
        set_accounting_context,
    )

    session = _SessionUsage()
    token = set_accounting_context(
        session,
        "session-1",
        task_id="t-aux",
        lane="dayroute",
    )
    try:
        record_aux_usage(
            _aux_response(),
            "web_search",
            provider="perplexity",
            base_url="https://api.perplexity.ai",
        )
    finally:
        reset_accounting_context(token)
    row = _rows(cost_db)[0]
    assert session.calls == 1
    assert (row["vendor"], row["lane"]) == ("perplexity", "dayroute")


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("ledger unavailable"),
        PerTaskCapExceeded(
            task_id="t-aux",
            current_total=0.9,
            projected_total=1.1,
            cap=1.0,
        ),
    ],
    ids=["accounting-error", "deprecated-cap-exception"],
)
def test_aux_perplexity_call_ledger_failure_is_non_fatal(
    cost_db, monkeypatch, caplog, failure
):
    from agent.aux_accounting import (
        record_aux_usage,
        reset_accounting_context,
        set_accounting_context,
    )

    def fail(**kwargs):
        raise failure

    monkeypatch.setattr(recorders, "record_perplexity_call", fail)
    session = _SessionUsage()
    token = set_accounting_context(
        session,
        "session-1",
        task_id="t-aux",
        lane="platform",
    )
    try:
        with caplog.at_level(logging.WARNING):
            record_aux_usage(
                _aux_response(),
                "web_search",
                provider="perplexity",
                base_url="https://api.perplexity.ai",
            )
    finally:
        reset_accounting_context(token)
    assert session.calls == 1
    assert f"{type(failure).__name__}: {failure}" in caplog.text


def test_aux_perplexity_call_operator_kill_is_fatal(cost_db, monkeypatch):
    from agent.aux_accounting import (
        record_aux_usage,
        reset_accounting_context,
        set_accounting_context,
    )

    failure = KillSwitchTripped(task_id="t-aux", reason="operator")

    def fail(**kwargs):
        raise failure

    monkeypatch.setattr(recorders, "record_perplexity_call", fail)
    session = _SessionUsage()
    token = set_accounting_context(
        session,
        "session-1",
        task_id="t-aux",
        lane="platform",
    )
    try:
        with pytest.raises(KillSwitchTripped) as raised:
            record_aux_usage(
                _aux_response(),
                "web_search",
                provider="perplexity",
                base_url="https://api.perplexity.ai",
            )
    finally:
        reset_accounting_context(token)
    assert raised.value is failure
