from __future__ import annotations

import hashlib
import json
import sqlite3
from decimal import Decimal
from types import SimpleNamespace

import pytest

from hermes_cli import objective_adapters
from hermes_cli import objectives_db
from hermes_cli import organization_db
from hermes_cli import planner_inferences
from hermes_cli import resource_budget


def _response(text: str):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        model="audited-planner-v1",
        usage=SimpleNamespace(prompt_tokens=321, completion_tokens=54),
    )


def _state(tmp_path):
    conn = objectives_db.connect(tmp_path / "authority.db")
    organization_id, _ = organization_db.bootstrap_solo_founder(
        conn,
        organization_name="Inference Audit Company",
        purpose="Preserve exact decision lineage",
        profile_name="audit",
        charter={},
    )
    objective = objectives_db.create_objective(
        conn,
        organization_id=organization_id,
        desired_outcome="Choose the next measured action",
        originator="employee:ceo",
        permitted_systems=["strategy"],
    )
    objectives_db.transition_objective(
        conn, objective.id, "accepted", actor="employee:ceo"
    )
    event_id = objectives_db.enqueue_objective_event(
        conn,
        objective_id=objective.id,
        event_type="ceo.operating_review",
        payload={"review": ["runway", "strategy"]},
    )
    return conn, organization_id, objective.id, event_id


def test_exact_planner_input_output_is_immutable_and_bound_to_plan(
    tmp_path, monkeypatch
):
    conn, organization_id, objective_id, event_id = _state(tmp_path)
    raw = (
        '{"assumptions":[],"tasks":[{"step":"review evidence"}],'
        '"dependencies":[],"risks":[],"objective_complete_when_verified":false,'
        '"actions":[]}'
    )
    monkeypatch.setattr(
        "agent.auxiliary_client.call_llm", lambda **kwargs: _response(raw)
    )
    planner = objective_adapters.AuxiliaryObjectivePlanner(
        action_contracts=[],
        authority_conn=conn,
        context_provider=lambda: {
            "finance": {"available_minor": 1000},
            "sensitive_data_included": False,
        },
    )

    proposal = planner.propose(
        objectives_db.objective_snapshot(conn, objective_id),
        dict(
            conn.execute(
                "SELECT * FROM objective_inbox WHERE id=?", (event_id,)
            ).fetchone()
        )
        | {"payload": {"review": ["runway", "strategy"]}},
    )
    plan_id = objectives_db.create_plan(
        conn,
        objective_id,
        assumptions=proposal.assumptions,
        tasks=proposal.tasks,
        dependencies=proposal.dependencies,
        risks=proposal.risks,
        created_by=planner.identity,
        inference_id=proposal.inference_id,
    )

    inference = conn.execute(
        "SELECT * FROM planner_inferences WHERE id=?", (proposal.inference_id,)
    ).fetchone()
    request = json.loads(inference["request_json"])
    assert inference["organization_id"] == organization_id
    assert inference["inbox_event_id"] == event_id
    assert inference["model"] == "audited-planner-v1"
    assert inference["response_text"] == raw
    assert inference["response_sha256"] == hashlib.sha256(raw.encode()).hexdigest()
    assert inference["input_tokens"] == 321
    assert inference["output_tokens"] == 54
    assert request["messages"][0]["content"] == (
        objective_adapters.PLANNER_SYSTEM_PROMPT
    )
    assert json.loads(request["messages"][1]["content"])[
        "operating_context"
    ]["sensitive_data_included"] is False
    plan = conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    assert plan["inference_id"] == proposal.inference_id
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(
            "UPDATE planner_inferences SET response_text='changed' WHERE id=?",
            (proposal.inference_id,),
        )


def test_invalid_and_failed_planner_calls_are_preserved_without_plan(
    tmp_path, monkeypatch
):
    conn, _, objective_id, event_id = _state(tmp_path)
    planner = objective_adapters.AuxiliaryObjectivePlanner(
        action_contracts=[], authority_conn=conn
    )
    event = dict(
        conn.execute("SELECT * FROM objective_inbox WHERE id=?", (event_id,)).fetchone()
    ) | {"payload": {}}
    snapshot = objectives_db.objective_snapshot(conn, objective_id)
    monkeypatch.setattr(
        "agent.auxiliary_client.call_llm",
        lambda **kwargs: _response("not valid JSON"),
    )
    with pytest.raises(ValueError, match="JSON object"):
        planner.propose(snapshot, event)
    invalid = conn.execute(
        """SELECT * FROM planner_inferences
            WHERE parse_status='invalid_response'"""
    ).fetchone()
    assert invalid["response_text"] == "not valid JSON"
    assert invalid["error"]

    def fail(**kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr("agent.auxiliary_client.call_llm", fail)
    with pytest.raises(RuntimeError, match="provider unavailable"):
        planner.propose(snapshot, event)
    failed = conn.execute(
        """SELECT * FROM planner_inferences WHERE parse_status='call_failed'"""
    ).fetchone()
    assert failed["response_text"] is None
    assert failed["error"] == "provider unavailable"
    assert conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0


def test_failed_planner_call_retains_conservative_resource_reservation(
    tmp_path, monkeypatch
):
    conn, _, objective_id, event_id = _state(tmp_path)
    limits = {
        **resource_budget.DEFAULT_LIMITS,
        "max_cycles_per_objective": 2,
        "max_input_tokens_per_objective": 100_000,
        "max_output_tokens_per_objective": 8_192,
        "max_compute_cost_minor_per_objective": 20,
    }
    planner = objective_adapters.AuxiliaryObjectivePlanner(
        action_contracts=[],
        authority_conn=conn,
        resource_limits=limits,
        planner_call_compute_reservation_minor=10,
    )

    def fail(**kwargs):
        raise RuntimeError("provider charged then disconnected")

    monkeypatch.setattr("agent.auxiliary_client.call_llm", fail)
    event = dict(
        conn.execute(
            "SELECT * FROM objective_inbox WHERE id=?", (event_id,)
        ).fetchone()
    ) | {"payload": {}}
    with pytest.raises(RuntimeError, match="charged then disconnected"):
        planner.propose(
            objectives_db.objective_snapshot(conn, objective_id), event
        )
    usage = resource_budget.usage(conn, objective_id)
    assert usage["cycles"] == 1
    assert usage["actions"] == 0
    assert usage["output_tokens"] == 4096
    assert usage["input_tokens"] > 0
    assert usage["estimated_compute_cost_minor"] == 10
    reservation = conn.execute(
        "SELECT * FROM planner_compute_reservations"
    ).fetchone()
    assert reservation["objective_id"] == objective_id
    assert reservation["inbox_event_id"] == event_id
    inference = conn.execute(
        "SELECT request_json FROM planner_inferences"
    ).fetchone()
    assert (
        json.loads(inference["request_json"])["compute_reservation_id"]
        == reservation["id"]
    )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(
            """UPDATE planner_compute_reservations
                  SET reserved_minor=0 WHERE id=?""",
            (reservation["id"],),
        )


def test_explicit_included_billing_route_reconciles_without_phantom_cost(
    tmp_path, monkeypatch
):
    from agent.usage_pricing import CostResult

    conn, organization_id, objective_id, event_id = _state(tmp_path)
    planner = objective_adapters.AuxiliaryObjectivePlanner(
        action_contracts=[],
        authority_conn=conn,
        resource_limits=resource_budget.DEFAULT_LIMITS,
        planner_call_compute_reservation_minor=10,
        billing_provider="subscription-provider",
    )
    monkeypatch.setattr(
        "agent.auxiliary_client.call_llm",
        lambda **kwargs: _response(
            '{"assumptions":[],"tasks":[],"dependencies":[],'
            '"risks":[],"objective_complete_when_verified":false,'
            '"actions":[]}'
        ),
    )
    monkeypatch.setattr(
        "agent.usage_pricing.estimate_usage_cost",
        lambda *args, **kwargs: CostResult(
            amount_usd=Decimal("0"),
            status="included",
            source="none",
            label="included",
            pricing_version="subscription-contract-v1",
        ),
    )
    event = dict(
        conn.execute(
            "SELECT * FROM objective_inbox WHERE id=?", (event_id,)
        ).fetchone()
    ) | {"payload": {}}
    proposal = planner.propose(
        objectives_db.objective_snapshot(conn, objective_id), event
    )
    assert proposal.inference_id
    assert resource_budget.compute_reservation_posture(
        conn, organization_id
    )["unreconciled_count"] == 0
    reconciliation = conn.execute(
        "SELECT * FROM planner_compute_reconciliations"
    ).fetchone()
    assert reconciliation["status"] == "included"
    assert reconciliation["actual_minor"] == 0


def test_plan_rejects_inference_from_another_objective(tmp_path):
    conn, _, objective_id, event_id = _state(tmp_path)
    other = objectives_db.create_objective(
        conn,
        organization_id=conn.execute(
            "SELECT organization_id FROM objectives WHERE id=?", (objective_id,)
        ).fetchone()["organization_id"],
        desired_outcome="Other decision",
        originator="employee:ceo",
    )
    objectives_db.transition_objective(
        conn, other.id, "accepted", actor="employee:ceo"
    )
    inference_id = planner_inferences.record(
        conn,
        objective_id=objective_id,
        inbox_event_id=event_id,
        planner_identity="employee:ceo",
        task="objective_planner",
        model="test",
        request={"messages": []},
        response_text="{}",
        parse_status="parsed",
        error=None,
        input_tokens=0,
        output_tokens=0,
        started_at=1,
        finished_at=2,
    )
    with pytest.raises(objectives_db.ObjectiveStateError, match="belongs elsewhere"):
        objectives_db.create_plan(
            conn,
            other.id,
            assumptions=[],
            tasks=[],
            dependencies=[],
            risks=[],
            created_by="employee:ceo",
            inference_id=inference_id,
        )
