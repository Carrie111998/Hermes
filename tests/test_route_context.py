from __future__ import annotations

import json
import os
from contextlib import contextmanager

import pytest

from hermes_cli.routing import facade, route_context, schema


def _payload(decision_row_id: int = 1) -> dict:
    return {
        "schema_version": 1,
        "decision_row_id": decision_row_id,
        "task_id": "atlas-t-route-context",
        "session_id": "atlas-s-route-context",
        "matched_rule_id": 2,
        "primary_provider": "openai-codex",
        "primary_model": "gpt-5-6-sol",
        "fallback_chain": [
            {"provider": "openrouter", "model": "fallback/model"}
        ],
        "nonce": "smoke-nonce",
    }


@pytest.fixture(autouse=True)
def reset_context(monkeypatch):
    monkeypatch.delenv("HERMES_ROUTE_CONTEXT_JSON", raising=False)
    route_context._reset_for_tests()
    yield
    route_context._reset_for_tests()


def _decision(db_path) -> int:
    result = facade.route_for_turn(
        lane="test",
        rung="default",
        complexity="default",
        caller_provider="openai-codex",
        caller_model="gpt-5-6-sol",
        task_id="atlas-t-route-context",
        session_id="atlas-s-route-context",
        use_doctrine_reader=False,
        db_path=db_path,
    )
    return int(result["decision_row_id"])


def _install(monkeypatch, payload: dict) -> None:
    monkeypatch.setenv(
        "HERMES_ROUTE_CONTEXT_JSON",
        json.dumps(payload),
    )
    assert route_context.get_route_context() == payload


def _row(db_path, query: str):
    conn = schema.connect(db_path)
    try:
        return conn.execute(query).fetchone()
    finally:
        conn.close()


def test_get_route_context_returns_None_when_env_absent():
    assert route_context.get_route_context() is None


def test_get_route_context_returns_None_when_env_invalid_json(monkeypatch):
    monkeypatch.setenv("HERMES_ROUTE_CONTEXT_JSON", "{not json")
    assert route_context.get_route_context() is None


def test_get_route_context_returns_None_when_schema_version_mismatch(
    monkeypatch,
):
    value = _payload()
    value["schema_version"] = 2
    monkeypatch.setenv("HERMES_ROUTE_CONTEXT_JSON", json.dumps(value))
    assert route_context.get_route_context() is None


def test_get_route_context_returns_context_when_env_valid(monkeypatch):
    value = _payload()
    monkeypatch.setenv("HERMES_ROUTE_CONTEXT_JSON", json.dumps(value))
    assert route_context.get_route_context() == value


def test_get_route_context_reads_env_only_once(monkeypatch):
    first = _payload(10)
    second = _payload(20)
    monkeypatch.setenv("HERMES_ROUTE_CONTEXT_JSON", json.dumps(first))
    assert route_context.get_route_context() == first
    monkeypatch.setenv("HERMES_ROUTE_CONTEXT_JSON", json.dumps(second))
    assert route_context.get_route_context() == first


def test_get_route_context_clears_env_after_read(monkeypatch):
    monkeypatch.setenv("HERMES_ROUTE_CONTEXT_JSON", json.dumps(_payload()))
    route_context.get_route_context()
    assert "HERMES_ROUTE_CONTEXT_JSON" not in os.environ


def test_append_failure_records_all_fields(monkeypatch):
    _install(monkeypatch, _payload())
    route_context.append_failure(
        provider="openai-codex",
        model="gpt-5-6-sol",
        failure_class="timeout",
        latency_ms=1234,
        error_repr="TimeoutError: mock",
        transition_reason="provider_switch",
    )
    entry = route_context._failure_history[0]
    assert {
        "provider",
        "model",
        "failure_class",
        "latency_ms",
        "attempt_ts",
        "error_repr",
        "transition_reason",
    } == set(entry)
    assert entry["latency_ms"] == 1234


def test_append_failure_truncates_error_repr(monkeypatch):
    _install(monkeypatch, _payload())
    route_context.append_failure(
        provider="p",
        model="m",
        failure_class="timeout",
        latency_ms=1,
        error_repr="x" * 900,
        transition_reason="provider_switch",
    )
    assert len(route_context._failure_history[0]["error_repr"]) == 500


def test_flush_to_db_updates_routing_decisions_row(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    decision_id = _decision(db_path)
    _install(monkeypatch, _payload(decision_id))
    route_context.append_failure(
        provider="openai-codex",
        model="gpt-5-6-sol",
        failure_class="timeout",
        latency_ms=50,
        error_repr="mock",
        transition_reason="provider_switch",
    )
    assert route_context.flush_to_db(
        chosen_provider="openrouter",
        chosen_model="fallback/model",
        outcome="success",
        db_path=db_path,
    )
    row = _row(
        db_path,
        "SELECT chosen_provider, chosen_model, failure_history_json "
        "FROM routing_decisions",
    )
    assert tuple(row[:2]) == ("openrouter", "fallback/model")
    assert json.loads(row["failure_history_json"])[0]["failure_class"] == (
        "timeout"
    )


def test_flush_to_db_writes_sentinel_when_all_failed(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "kanban.db"
    decision_id = _decision(db_path)
    _install(monkeypatch, _payload(decision_id))
    route_context.append_failure(
        provider="openai-codex",
        model="gpt-5-6-sol",
        failure_class="timeout",
        latency_ms=50,
        error_repr="mock",
        transition_reason="provider_switch",
    )
    route_context.mark_cascade_exhausted("timeout")
    route_context.flush_to_db(
        chosen_provider="ignored",
        chosen_model="ignored",
        outcome="failure",
        db_path=db_path,
    )
    row = _row(
        db_path,
        "SELECT chosen_provider, chosen_model FROM routing_decisions",
    )
    assert tuple(row) == ("__all_failed__", "__none__")
    verdict = _row(
        db_path,
        "SELECT outcome, failure_class, raw_meta FROM leaf_verdicts "
        "ORDER BY id DESC LIMIT 1",
    )
    assert tuple(verdict[:2]) == ("failure", "infra")
    assert json.loads(verdict["raw_meta"])["cascade_exhausted"] is True


def test_flush_to_db_is_idempotent(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    decision_id = _decision(db_path)
    _install(monkeypatch, _payload(decision_id))
    assert route_context.flush_to_db(
        chosen_provider="p2",
        chosen_model="m2",
        outcome="success",
        db_path=db_path,
    )
    assert not route_context.flush_to_db(
        chosen_provider="p3",
        chosen_model="m3",
        outcome="success",
        db_path=db_path,
    )
    row = _row(
        db_path,
        "SELECT chosen_provider, chosen_model FROM routing_decisions",
    )
    assert tuple(row) == ("p2", "m2")


def test_flush_to_db_uses_retrying_write_txn(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "kanban.db"
    decision_id = _decision(db_path)
    _install(monkeypatch, _payload(decision_id))
    observed = []
    original = facade.retrying_write_txn

    @contextmanager
    def tracking(conn, *args, **kwargs):
        observed.append(conn)
        with original(conn, *args, **kwargs):
            yield conn

    monkeypatch.setattr(facade, "retrying_write_txn", tracking)
    route_context.flush_to_db(
        chosen_provider="p2",
        chosen_model="m2",
        outcome="success",
        db_path=db_path,
    )
    assert observed
