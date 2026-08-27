"""Phase B contracts for per-run Kanban token and cost accounting."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from agent.usage_pricing import CostResult
from hermes_cli import kanban_db as kb
from tests.conftest import write_valid_model_routing_config


@pytest.fixture
def routed_conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    write_valid_model_routing_config(home)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    yield conn
    conn.close()


def _claimed_run(conn):
    task_id = kb.create_task(conn, title="account this", assignee="worker")
    task = kb.claim_task(conn, task_id)
    assert task is not None and task.current_run_id is not None
    return task_id, task.current_run_id


def test_schema_exposes_complete_per_run_accounting(routed_conn):
    cols = {r["name"] for r in routed_conn.execute("PRAGMA table_info(task_runs)")}
    assert {
        "attempt_number", "run_kind", "retry_of_run_id", "escalation_of_run_id",
        "provider", "model", "service_tier", "routing_complexity",
        "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
        "reasoning_tokens", "api_call_count", "estimated_cost_usd", "actual_cost_usd",
        "usage_status", "cost_status", "cost_source", "policy_version",
        "registry_version", "duration_ms",
    } <= cols


def test_record_accounting_uses_pricing_registry_and_is_idempotent(routed_conn, monkeypatch):
    task_id, run_id = _claimed_run(routed_conn)
    calls = []

    def estimate(model, usage, *, provider=None, **_kwargs):
        calls.append((model, provider, usage))
        return CostResult(
            amount_usd=Decimal("0.0125"), status="estimated", source="test-registry",
            label="$0.0125", pricing_version="registry-v1",
        )

    monkeypatch.setattr("agent.usage_pricing.estimate_usage_cost", estimate)
    accounting = kb.RunAccounting(
        provider="nous", model="nemotron", service_tier="T1",
        input_tokens=1000, output_tokens=500, cache_read_tokens=50,
        cache_write_tokens=25, reasoning_tokens=100, api_call_count=2,
    )
    assert kb.record_run_accounting(routed_conn, task_id=task_id, run_id=run_id, accounting=accounting)
    assert kb.record_run_accounting(routed_conn, task_id=task_id, run_id=run_id, accounting=accounting)
    row = routed_conn.execute("SELECT * FROM task_runs WHERE id=?", (run_id,)).fetchone()
    assert len(calls) == 2
    assert row["input_tokens"] == 1000
    assert row["output_tokens"] == 500
    assert row["cache_read_tokens"] == 50
    assert row["cache_write_tokens"] == 25
    assert row["estimated_cost_usd"] == pytest.approx(0.0125)
    assert row["actual_cost_usd"] is None
    assert row["usage_status"] == "reported"
    assert row["cost_status"] == "estimated"
    assert row["cost_source"] == "test-registry"
    assert row["registry_version"] == "registry-v1"


def test_missing_usage_is_explicit_and_never_fabricates_zero(routed_conn):
    task_id, run_id = _claimed_run(routed_conn)
    assert kb.record_run_accounting(
        routed_conn, task_id=task_id, run_id=run_id,
        accounting=kb.RunAccounting(provider="nous", model="unknown", usage_status="unavailable"),
    )
    row = routed_conn.execute("SELECT * FROM task_runs WHERE id=?", (run_id,)).fetchone()
    assert row["usage_status"] == "unavailable"
    assert row["input_tokens"] is None
    assert row["output_tokens"] is None
    assert row["estimated_cost_usd"] is None
    assert row["actual_cost_usd"] is None


def test_actual_cost_is_stored_separately_and_wrong_run_is_rejected(routed_conn, monkeypatch):
    task_id, run_id = _claimed_run(routed_conn)
    monkeypatch.setattr(
        "agent.usage_pricing.estimate_usage_cost",
        lambda *_a, **_kw: CostResult(Decimal("0.5"), "estimated", "registry", "$0.5"),
    )
    accounting = kb.RunAccounting(
        provider="nous", model="nemotron", input_tokens=1, output_tokens=2,
        actual_cost_usd=Decimal("0.7"),
    )
    assert not kb.record_run_accounting(routed_conn, task_id="wrong", run_id=run_id, accounting=accounting)
    assert kb.record_run_accounting(routed_conn, task_id=task_id, run_id=run_id, accounting=accounting)
    row = routed_conn.execute("SELECT estimated_cost_usd, actual_cost_usd FROM task_runs WHERE id=?", (run_id,)).fetchone()
    assert row["estimated_cost_usd"] == pytest.approx(0.5)
    assert row["actual_cost_usd"] == pytest.approx(0.7)


def test_retry_is_a_separate_linked_attempt(routed_conn):
    task_id, first_run = _claimed_run(routed_conn)
    assert kb.block_task(routed_conn, task_id, reason="retry later", kind="transient", expected_run_id=first_run)
    assert kb.unblock_task(routed_conn, task_id)
    task = kb.claim_task(routed_conn, task_id)
    assert task is not None and task.current_run_id is not None
    rows = routed_conn.execute(
        "SELECT id, attempt_number, run_kind, retry_of_run_id FROM task_runs WHERE task_id=? ORDER BY id",
        (task_id,),
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["attempt_number"] == 1
    assert rows[1]["attempt_number"] == 2
    assert rows[1]["run_kind"] == "retry"
    assert rows[1]["retry_of_run_id"] == first_run
