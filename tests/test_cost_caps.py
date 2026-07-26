"""CS-02 cost-cap and programme-integration acceptance tests."""

from __future__ import annotations

import sqlite3

import pytest

from hermes_cli.cost import caps, ledger, telegram_alert
from hermes_cli.cost import gate_integration
from hermes_cli.cost.gate_integration import on_call_complete
from hermes_cli.programme import gate as programme_gate
from hermes_cli.programme import init as programme_init


@pytest.fixture
def cost_env(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    halt_path = tmp_path / "halt"
    monkeypatch.setattr(ledger, "DB_PATH", db_path)
    monkeypatch.setattr(programme_init, "DB_PATH", db_path)
    monkeypatch.setattr(programme_gate, "HALT_SIGNAL_PATH", halt_path)
    programme_init.migrate()
    ledger.migrate()
    return db_path


def _insert_aud(
    db_path,
    amounts,
    *,
    lane="platform",
    task_id="t_1",
    escalation=False,
):
    rows = [
        (
            ledger.utc_now(),
            task_id,
            lane,
            "anthropic",
            "anthropic/claude-opus-5",
            int(bool(escalation)),
            float(amount),
        )
        for amount in amounts
    ]
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO cost_ledger (
                ts, task_id, lane, vendor, model_slug, escalation,
                usd_amount, aud_amount, fx_rate, surcharge_applied
            ) VALUES (?, ?, ?, ?, ?, ?, 0.0, ?, 1.52, 0.0)
            """,
            rows,
        )


def _breaching_call(
    task_id="t_breach",
    *,
    enforce_programme_cap=False,
):
    on_call_complete(
        task_id=task_id,
        lane="platform",
        vendor="anthropic",
        model_slug="anthropic/claude-opus-5",
        attempt_number=1,
        rung_id=None,
        escalation=False,
        input_tokens=100,
        output_tokens=25,
        cached_input_tokens=0,
        usd_amount=2.51 / 1.52,
        latency_ms=250,
        request_id="req_breach",
        raw_response_meta=None,
        enforce_programme_cap=enforce_programme_cap,
    )


def test_daily_spend_aud_zero_when_empty(cost_env):
    assert caps.daily_spend_aud() == 0.0


def test_cost_gate_has_no_task_cap_kill_alert_api(cost_env):
    assert not hasattr(gate_integration, "send_task_cap_kill_alert")


def test_daily_spend_aud_sums_today(cost_env):
    _insert_aud(cost_env, [1.00, 2.00, 2.25])
    assert caps.daily_spend_aud() == pytest.approx(5.25)


def test_daily_spend_aud_filters_by_lane(cost_env):
    _insert_aud(cost_env, [1.00, 2.00], lane="green_captains")
    _insert_aud(cost_env, [3.00], lane="dayroute")
    _insert_aud(cost_env, [4.00, 5.00], lane="reserve")
    assert caps.daily_spend_aud("green_captains") == pytest.approx(3.00)


def test_task_spend_aud_sums_task(cost_env):
    _insert_aud(cost_env, [0.25, 0.50, 1.00, 1.25], task_id="t_1")
    _insert_aud(cost_env, [4.00], task_id="t_other")
    assert caps.task_spend_aud("t_1") == pytest.approx(3.00)


def test_check_all_caps_no_breach(cost_env):
    _insert_aud(cost_env, [0.25], task_id="t_small")
    assert caps.check_all_caps("t_small", "platform", False) == (False, None)


def test_check_all_caps_global_breach(cost_env):
    _insert_aud(cost_env, [6.01], lane="green_captains", task_id="g1")
    _insert_aud(cost_env, [5.00], lane="dayroute", task_id="g2")
    _insert_aud(cost_env, [4.00], lane="tihna", task_id="g3")
    _insert_aud(cost_env, [2.00], lane="platform", task_id="g4")
    _insert_aud(cost_env, [3.00], lane="reserve", task_id="g5")
    assert caps.check_all_caps("g5", "reserve", False) == (True, "global")


def test_check_all_caps_per_task_breach(cost_env):
    _insert_aud(cost_env, [0.75, 0.80, 0.96], task_id="t_x")
    assert caps.check_all_caps("t_x", "platform", False) == (
        True,
        "per_task",
    )


def test_check_all_caps_per_lane_breach(cost_env):
    _insert_aud(
        cost_env,
        [2.00, 2.00, 2.01],
        lane="green_captains",
        task_id=None,
    )
    assert caps.check_all_caps(None, "green_captains", False) == (
        True,
        "per_lane_green_captains",
    )


def test_check_all_caps_escalation_breach(cost_env):
    _insert_aud(
        cost_env,
        [1.00, 1.00, 1.01],
        lane="green_captains",
        task_id=None,
        escalation=True,
    )
    assert caps.check_all_caps(None, "green_captains", True) == (
        True,
        "escalation_envelope",
    )


def test_breach_is_advisory_and_programme_keeps_running(
    cost_env,
    monkeypatch,
):
    messages = []
    monkeypatch.setattr(
        telegram_alert,
        "send_bridge_alert",
        lambda message: messages.append(message),
    )
    _breaching_call()
    state = programme_gate.get_state()
    assert state.state == "RUNNING"
    assert len(messages) == 1
    assert "TASK SPEND ADVISORY" in messages[0]
    assert "programme_paused: no" in messages[0]


def test_advisory_sent_once_per_task_per_utc_day(cost_env, monkeypatch):
    calls = []
    monkeypatch.setattr(
        telegram_alert,
        "send_bridge_alert",
        lambda message: calls.append(message),
    )
    _breaching_call()
    _breaching_call()
    assert len(calls) == 1


def test_legacy_programme_enforcement_kwarg_remains_advisory(
    cost_env,
    monkeypatch,
):
    advisory_calls = []
    hard_pause_calls = []
    monkeypatch.setattr(
        telegram_alert,
        "send_cost_alert",
        lambda *args: hard_pause_calls.append(args),
    )
    monkeypatch.setattr(
        telegram_alert,
        "send_bridge_alert",
        lambda message: advisory_calls.append(message),
    )
    _breaching_call(enforce_programme_cap=True)
    state = programme_gate.get_state()
    assert state.state == "RUNNING"
    assert len(advisory_calls) == 1
    assert "advisory_only: yes" in advisory_calls[0]
    assert hard_pause_calls == []
