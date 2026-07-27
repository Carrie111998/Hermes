from __future__ import annotations

import json
import time
from types import SimpleNamespace

from hermes_cli import (
    agentmail_events,
    authority_integrity,
    finance_db,
    objective_triggers,
    objective_worker,
    objectives_db,
    organization_db,
)


def _planner_response(value: dict) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(value))
            )
        ],
        model="deterministic-e2e-planner",
        usage=SimpleNamespace(prompt_tokens=50, completion_tokens=25),
    )


def test_authenticated_event_survives_worker_restart_to_verified_outcome(
    tmp_path, monkeypatch
):
    root = tmp_path / "hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    db_path = root / "objectives.db"
    charter = {
        "enabled": True,
        "operating_mode": "autonomous",
        "operator_role": "advisor",
        "policy_version": "e2e-charter-v1",
        "runtime_host": "standalone",
        "runtime_interval_seconds": 1,
        "event_claim_ttl_seconds": 30,
        "operating_cadence": {"enabled": False},
        "allowed_capabilities": ["commitments.manage"],
        "forbidden_capabilities": [],
        "allowed_systems": ["commitments"],
        "approval_required_capabilities": [],
        "max_autonomous_risk": "low",
        "allow_irreversible": True,
        "max_action_spend_minor": 0,
        "permit_ttl_seconds": 300,
        "resource_lease_ttl_seconds": 30,
        "authority_store": {
            "backend": "sqlite",
            "deployment_scope": "single_host",
        },
        "recovery": {"enabled": False},
        "resource_limits": {
            "max_cycles_per_objective": 10,
            "max_actions_per_cycle": 1,
            "max_actions_per_objective": 10,
                "max_input_tokens_per_objective": 100_000,
                "max_output_tokens_per_objective": 10_000,
                "max_compute_cost_minor_per_objective": 1_000,
                "planner_call_compute_reservation_minor": 10,
        },
        "retry_policy": {
            "max_attempts": 3,
            "base_backoff_seconds": 1,
            "max_backoff_seconds": 2,
        },
        "security": {
            "enforce_isolated_execution": True,
            "require_external_secret_manager": True,
            "require_idempotency_key_for_external_actions": True,
            "require_fresh_state_for_external_actions": False,
            "require_compensation_for_reversible_actions": False,
            "quarantine_untrusted_instructions": True,
            "circuit_breaker_failure_threshold": 3,
            "circuit_breaker_cooldown_seconds": 60,
        },
        "organization": {"max_active_objectives": 10},
        "finance": {
            "base_currency": "USD",
            "payments": {
                "custody_model": "non_custodial",
                "store_raw_financial_credentials": False,
            },
        },
        "compliance": {
            "require_action_context": False,
            "fail_closed_on_unknown_applicability": True,
        },
    }
    config = {
        "agentic": charter,
        "terminal": {"backend": "docker"},
        "secrets": {"bitwarden": {"enabled": True}},
        "security": {"redact_secrets": True},
        "auxiliary": {"objective_planner": {"timeout": 10}},
    }
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: config)

    with objectives_db.connect_closing(db_path) as conn:
        organization_id, _ = organization_db.bootstrap_solo_founder(
            conn,
            organization_name="Restart-Safe Company",
            purpose="Honor authenticated customer commitments",
            profile_name="default",
            charter=charter,
        )
        authority_integrity.accept_policy_baseline(
            conn,
            organization_id=organization_id,
            policy=charter,
            actor="human_operator:setup",
            reason="process-level integration charter",
        )
        account_id = finance_db.create_treasury_account(
            conn, organization_id=organization_id, currency="USD"
        )
        finance_db.seed_initial_capital(
            conn,
            account_id=account_id,
            amount_minor=1_000,
            currency="USD",
            actor="human_operator",
        )
        objective = objectives_db.create_objective(
            conn,
            organization_id=organization_id,
            desired_outcome="Record and verify the customer delivery promise",
            originator="employee:ceo",
            permitted_systems=["commitments"],
            success_criteria=[
                {"verifier": "accounting.books_balanced", "params": {}}
            ],
        )
        objectives_db.transition_objective(
            conn, objective.id, "accepted", actor="employee:ceo"
        )
        objective_triggers.subscribe(
            conn,
            organization_id=organization_id,
            objective_id=objective.id,
            source_type="agentmail",
            event_type="message.received",
        )
        event_ids = agentmail_events.route_authenticated_event(
            conn,
            organization_id=organization_id,
            expected_inbox_id="ceo@agentmail.to",
            payload={
                "event_type": "message.received",
                "event_id": "evt_restart_e2e",
                "message": {
                    "inbox_id": "ceo@agentmail.to",
                    "message_id": "msg_restart_e2e",
                    "from": "customer@example.com",
                    "to": ["ceo@agentmail.to"],
                    "subject": "Delivery date",
                    "text": "Please confirm delivery next week.",
                },
            },
            svix_id="delivery_restart_e2e",
            svix_timestamp=str(int(time.time())),
        )
        assert len(event_ids) == 1

    due_at = int(time.time()) + 86_400
    proposals = iter(
        [
            {
                "assumptions": [],
                "tasks": [{"step": "record the external promise"}],
                "dependencies": [],
                "risks": [],
                "objective_complete_when_verified": False,
                "actions": [
                    {
                        "action_type": "commitments.create",
                        "payload": {
                            "system": "commitments",
                            "target_resource": "customer:customer@example.com",
                            "idempotency_key": "commitment-restart-e2e-0001",
                            "kind": "customer_delivery",
                            "title": "Deliver revised proposal",
                            "description": "Send the revised proposal next week.",
                            "counterparty_type": "customer",
                            "counterparty_reference": "customer@example.com",
                            "source_system": "agentmail",
                            "source_reference": "evt_restart_e2e",
                            "due_at": due_at,
                            "grace_seconds": 3_600,
                            "required_verifier": "commitment.record.readback",
                            "financial_exposure_minor": 0,
                        },
                        "expected_outcome": "commitment is durably recorded",
                        "required_capability": "commitments.manage",
                        "verification_method": "commitment.record.readback",
                        "risk_class": "low",
                        "reversible": False,
                        "rationale": "A dated customer promise must be state.",
                        "estimated_cost_minor": 0,
                    }
                ],
            },
            {
                "assumptions": [],
                "tasks": [],
                "dependencies": [],
                "risks": [],
                "objective_complete_when_verified": True,
                "actions": [],
            },
        ]
    )
    monkeypatch.setattr(
        "agent.auxiliary_client.call_llm",
        lambda **kwargs: _planner_response(next(proposals)),
    )

    assert objective_worker.run_forever(
        db_path=db_path, interval_seconds=0.01, max_cycles=1
    ) == 0
    with objectives_db.connect_closing(db_path) as conn:
        assert (
            objectives_db.get_objective(conn, objective.id).status
            == "planned"
        ), objective_worker.worker_health(conn)
        assert conn.execute(
            "SELECT COUNT(*) FROM business_commitments"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM verification_records WHERE verdict='pass'"
        ).fetchone()[0] == 1

    # A fresh supervised worker process lifecycle resumes the durable wake event.
    assert objective_worker.run_forever(
        db_path=db_path, interval_seconds=0.01, max_cycles=1
    ) == 0
    with objectives_db.connect_closing(db_path) as conn:
        assert objectives_db.get_objective(conn, objective.id).status == "verified"
        assert conn.execute(
            "SELECT COUNT(*) FROM external_event_receipts"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM business_commitments"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM objective_workers"
        ).fetchone()[0] == 2
        assert {
            row["status"]
            for row in conn.execute("SELECT status FROM objective_workers")
        } == {"stopped"}
        assert conn.execute(
            "SELECT COUNT(*) FROM objective_inbox WHERE status='completed'"
        ).fetchone()[0] == 2
