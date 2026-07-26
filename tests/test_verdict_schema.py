"""CS-03 compact leaf-verdict and dispatch-envelope acceptance tests."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli.verdict import api, schema
from hermes_cli.verdict.types import DispatchEnvelope, LeafVerdict


@pytest.fixture
def verdict_db(tmp_path: Path) -> Path:
    path = tmp_path / "kanban.db"
    schema.migrate(path)
    return path


def _envelope(**overrides) -> DispatchEnvelope:
    values = {
        "task_id": "task-1",
        "attempt_number": 1,
        "rung_id": "r0_baseline",
        "model_slug": "test/model",
        "mode": "single",
        "strategy_payload": {
            "model": "test/model",
            "mode": "single",
            "prompt_hash": "abc",
        },
    }
    values.update(overrides)
    return DispatchEnvelope(**values)


def _verdict(**overrides) -> LeafVerdict:
    values = {
        "task_id": "task-1",
        "attempt_number": 1,
        "rung_id": "r0_baseline",
        "model_used": "test/model",
        "outcome": "success",
        "confidence": 1.0,
        "strategy_hash": _envelope().strategy_hash,
    }
    values.update(overrides)
    return LeafVerdict(**values)


def test_migration_creates_leaf_verdicts_and_dispatch_envelopes(verdict_db):
    with sqlite3.connect(verdict_db) as conn:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert {"leaf_verdicts", "dispatch_envelopes"} <= names


def test_all_indexes_exist(verdict_db):
    expected = {
        "idx_leaf_verdicts_task",
        "idx_leaf_verdicts_ts",
        "idx_leaf_verdicts_rung",
        "idx_leaf_verdicts_failure_class",
        "idx_dispatch_envelopes_task",
        "idx_dispatch_envelopes_ts",
        "idx_dispatch_envelopes_rung",
    }
    with sqlite3.connect(verdict_db) as conn:
        actual = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
    assert expected <= actual


def test_lazy_migration_on_isolated_home(tmp_path, monkeypatch):
    isolated = tmp_path / "isolated" / "kanban.db"
    monkeypatch.setattr(schema, "DB_PATH", isolated)
    schema._MIGRATED_PATHS.discard(str(isolated.resolve()))
    dispatch_id = api.record_dispatch(_envelope())
    assert dispatch_id == 1
    assert isolated.exists()


def test_reject_invalid_rung_id(verdict_db):
    with pytest.raises(ValueError, match="invalid rung_id"):
        api.record_dispatch(_envelope(rung_id="r9_magic"), verdict_db)


def test_reject_confidence_out_of_range(verdict_db):
    with pytest.raises(ValueError, match="confidence"):
        api.record_verdict(_verdict(confidence=1.01), verdict_db)


def test_success_verdict_must_not_carry_failure_class(verdict_db):
    with pytest.raises(ValueError, match="success verdict"):
        api.record_verdict(_verdict(failure_class="quality"), verdict_db)


def test_failure_verdict_must_carry_failure_class(verdict_db):
    with pytest.raises(ValueError, match="must carry failure_class"):
        api.record_verdict(_verdict(outcome="failure", confidence=0.0), verdict_db)


def test_budget_failure_cannot_recommend_escalation(verdict_db):
    with pytest.raises(ValueError, match="budget failure"):
        api.record_verdict(
            _verdict(
                outcome="failure",
                failure_class="budget",
                confidence=0.0,
                escalation_recommended=True,
            ),
            verdict_db,
        )


def test_infra_failure_cannot_recommend_escalation(verdict_db):
    with pytest.raises(ValueError, match="infra failure"):
        api.record_verdict(
            _verdict(
                outcome="failure",
                failure_class="infra",
                confidence=0.0,
                escalation_recommended=True,
            ),
            verdict_db,
        )


def test_reject_invalid_outcome(verdict_db):
    with pytest.raises(ValueError, match="invalid outcome"):
        api.record_verdict(_verdict(outcome="maybe"), verdict_db)


def test_reject_invalid_failure_class(verdict_db):
    with pytest.raises(ValueError, match="invalid failure_class"):
        api.record_verdict(
            _verdict(
                outcome="failure",
                failure_class="network",
                confidence=0.0,
            ),
            verdict_db,
        )


def test_record_dispatch_writes_row_and_returns_id(verdict_db):
    row_id = api.record_dispatch(_envelope(), verdict_db)
    assert row_id == 1
    with sqlite3.connect(verdict_db) as conn:
        assert conn.execute(
            "SELECT task_id FROM dispatch_envelopes WHERE id = ?", (row_id,)
        ).fetchone()[0] == "task-1"


def test_strategy_hash_is_deterministic_for_same_payload():
    assert _envelope().strategy_hash == _envelope().strategy_hash


def test_strategy_hash_differs_for_different_payload():
    changed = _envelope(
        strategy_payload={
            "model": "test/model",
            "mode": "single",
            "prompt_hash": "different",
        }
    )
    assert _envelope().strategy_hash != changed.strategy_hash


def test_get_dispatch_roundtrip(verdict_db):
    original = _envelope(expected_cost_aud=0.25)
    assert api.get_dispatch(
        api.record_dispatch(original, verdict_db), verdict_db
    ) == original


def test_parent_verdict_id_soft_fk(verdict_db):
    parent_id = api.record_verdict(_verdict(), verdict_db)
    child_id = api.record_dispatch(
        _envelope(parent_verdict_id=parent_id), verdict_db
    )
    assert api.get_dispatch(child_id, verdict_db).parent_verdict_id == parent_id


def test_record_verdict_writes_row_and_returns_id(verdict_db):
    row_id = api.record_verdict(_verdict(), verdict_db)
    assert row_id == 1
    with sqlite3.connect(verdict_db) as conn:
        assert conn.execute(
            "SELECT outcome FROM leaf_verdicts WHERE id = ?", (row_id,)
        ).fetchone()[0] == "success"


def test_get_verdict_roundtrip(verdict_db):
    original = _verdict(raw_meta={"request": "abc"})
    assert api.get_verdict(
        api.record_verdict(original, verdict_db), verdict_db
    ) == original


def test_list_verdicts_for_task_ordered_by_attempt(verdict_db):
    api.record_verdict(_verdict(attempt_number=2), verdict_db)
    api.record_verdict(_verdict(attempt_number=1), verdict_db)
    assert [
        item.attempt_number
        for item in api.list_verdicts_for_task("task-1", verdict_db)
    ] == [1, 2]


def test_attempts_at_current_rung_counts_correctly(verdict_db):
    api.record_verdict(_verdict(attempt_number=1), verdict_db)
    api.record_verdict(_verdict(attempt_number=2), verdict_db)
    api.record_verdict(
        _verdict(attempt_number=3, rung_id="r1_decompose"), verdict_db
    )
    assert api.attempts_at_current_rung(
        "task-1", "r0_baseline", verdict_db
    ) == 2


def test_has_strategy_changed_true_on_first_attempt(verdict_db):
    assert api.has_strategy_changed("task-1", "new-hash", verdict_db) is True


def test_has_strategy_changed_false_when_hash_seen_before(verdict_db):
    api.record_verdict(_verdict(strategy_hash="seen"), verdict_db)
    assert api.has_strategy_changed("task-1", "seen", verdict_db) is False


def test_verdict_side_effects_list_persisted_as_json(verdict_db):
    with sqlite3.connect(verdict_db) as conn:
        conn.execute("CREATE TABLE side_effects (id INTEGER PRIMARY KEY)")
        conn.executemany("INSERT INTO side_effects(id) VALUES (?)", [(2,), (7,)])
    row_id = api.record_verdict(_verdict(side_effects=[2, 7]), verdict_db)
    with sqlite3.connect(verdict_db) as conn:
        raw = conn.execute(
            "SELECT side_effects FROM leaf_verdicts WHERE id = ?", (row_id,)
        ).fetchone()[0]
    assert raw == "[2,7]"


def test_verdict_with_missing_side_effect_id_warns_but_persists(
    verdict_db, caplog
):
    with caplog.at_level(logging.WARNING):
        row_id = api.record_verdict(_verdict(side_effects=[999]), verdict_db)
    assert row_id == 1
    assert "missing side-effect ids" in caplog.text


def test_verdict_read_deserializes_side_effects_list(verdict_db):
    row_id = api.record_verdict(_verdict(side_effects=[3, 5]), verdict_db)
    assert api.get_verdict(row_id, verdict_db).side_effects == [3, 5]


def test_verdict_cost_aud_matches_last_cost_ledger_row_for_task(verdict_db):
    with sqlite3.connect(verdict_db) as conn:
        conn.execute(
            "CREATE TABLE cost_ledger "
            "(id INTEGER PRIMARY KEY, task_id TEXT, aud_amount REAL)"
        )
        conn.executemany(
            "INSERT INTO cost_ledger(task_id, aud_amount) VALUES (?, ?)",
            [("task-1", 0.2), ("task-1", 0.75)],
        )
    verdict = _verdict(cost_aud=api.last_cost_aud_for_task("task-1", verdict_db))
    row_id = api.record_verdict(verdict, verdict_db)
    assert api.get_verdict(row_id, verdict_db).cost_aud == pytest.approx(0.75)


def test_verdict_persists_even_when_cost_ledger_empty(verdict_db):
    verdict = _verdict(cost_aud=api.last_cost_aud_for_task("task-1", verdict_db))
    row_id = api.record_verdict(verdict, verdict_db)
    assert api.get_verdict(row_id, verdict_db).cost_aud == 0.0


def test_conversation_loop_success_writes_success_verdict(verdict_db):
    from agent.conversation_loop import _execute_recorded_leaf_call

    response = SimpleNamespace(
        model="response/model",
        usage=SimpleNamespace(prompt_tokens=11, completion_tokens=4),
    )
    returned = _execute_recorded_leaf_call(
        lambda: response,
        task_id="loop-success",
        attempt_number=1,
        rung_id="r0_baseline",
        model="request/model",
        prompt="hello",
        db_path=verdict_db,
    )
    verdict = api.list_verdicts_for_task("loop-success", verdict_db)[0]
    assert returned is response
    assert verdict.outcome == "success"
    assert verdict.model_used == "response/model"


def test_conversation_loop_exception_writes_infra_failure_verdict(verdict_db):
    from agent.conversation_loop import _execute_recorded_leaf_call

    def fail():
        raise RuntimeError("provider unavailable")

    with pytest.raises(RuntimeError, match="provider unavailable"):
        _execute_recorded_leaf_call(
            fail,
            task_id="loop-failure",
            attempt_number=1,
            rung_id="r0_baseline",
            model="request/model",
            prompt="hello",
            db_path=verdict_db,
        )
    verdict = api.list_verdicts_for_task("loop-failure", verdict_db)[0]
    assert verdict.failure_class == "infra"
    assert verdict.error_class == "RuntimeError"


def test_aux_call_without_task_id_skips_verdict_gracefully(caplog):
    from agent.aux_accounting import (
        record_aux_usage,
        reset_accounting_context,
        set_accounting_context,
    )

    class SessionUsage:
        def record_auxiliary_usage(self, *args, **kwargs):
            return None

    token = set_accounting_context(SessionUsage(), "session-1")
    response = SimpleNamespace(
        model="aux/model",
        usage=SimpleNamespace(prompt_tokens=5, completion_tokens=1),
    )
    try:
        with caplog.at_level(logging.INFO):
            record_aux_usage(response, "vision")
    finally:
        reset_accounting_context(token)
    assert "task_id unavailable" in caplog.text
