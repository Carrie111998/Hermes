from __future__ import annotations

import pytest

from hermes_cli import objective_policy as policy


def _objective(**overrides):
    value = {
        "status": "planned",
        "expires_at": None,
        "permitted_systems": ["crm", "email"],
        "prohibited_actions": ["company.delete"],
        "max_spend_minor": 10_000,
    }
    value.update(overrides)
    return value


def _action(**overrides):
    value = {
        "action_type": "crm.update",
        "required_capability": "crm.write",
        "payload": {"system": "crm", "target_resource": "lead:123"},
        "risk_class": "medium",
        "reversible": 1,
        "estimated_cost_minor": 0,
    }
    value.update(overrides)
    return value


def _charter(**overrides):
    value = {
        "enabled": True,
        "operating_mode": "autonomous",
        "allowed_capabilities": ["crm.write", "email.send"],
        "forbidden_capabilities": ["company.delete"],
        "allowed_systems": ["crm", "email"],
        "max_autonomous_risk": "medium",
        "allow_irreversible": False,
        "max_action_spend_minor": 500,
        "approval_required_capabilities": [],
        "permit_ttl_seconds": 300,
    }
    value.update(overrides)
    return value


def test_autonomous_mode_permits_in_charter_action():
    decision = policy.evaluate_action(
        objective=_objective(), action=_action(), charter=_charter()
    )
    assert decision.verdict == "permit"
    assert decision.ttl_seconds == 300
    assert decision.constraints["target_resource"] == "lead:123"


def test_operator_is_not_default_approval_bottleneck():
    decision = policy.evaluate_action(
        objective=_objective(),
        action=_action(risk_class="low"),
        charter=_charter(operating_mode="autonomous"),
    )
    assert decision.verdict == "permit"


def test_charter_rejects_zero_or_unfundable_planner_reservation():
    with pytest.raises(ValueError, match="positive integer"):
        policy.validate_charter(
            _charter(
                resource_limits={
                    "planner_call_compute_reservation_minor": 0
                }
            )
        )
    with pytest.raises(ValueError, match="exceeds"):
        policy.validate_charter(
            _charter(
                resource_limits={
                    "max_compute_cost_minor_per_objective": 5,
                    "planner_call_compute_reservation_minor": 10,
                }
            )
        )


def test_out_of_scope_capability_escalates_instead_of_self_expanding():
    decision = policy.evaluate_action(
        objective=_objective(),
        action=_action(required_capability="bank.transfer"),
        charter=_charter(),
    )
    assert decision.verdict == "escalate"
    assert "outside the standing charter" in decision.reason


def test_irreversible_action_escalates():
    decision = policy.evaluate_action(
        objective=_objective(),
        action=_action(reversible=0),
        charter=_charter(),
    )
    assert decision.verdict == "escalate"


def test_explicit_prohibition_is_denied():
    decision = policy.evaluate_action(
        objective=_objective(),
        action=_action(action_type="company.delete"),
        charter=_charter(),
    )
    assert decision.verdict == "deny"


def test_setup_can_require_approval_for_every_action():
    decision = policy.evaluate_action(
        objective=_objective(),
        action=_action(),
        charter=_charter(operating_mode="approval_required"),
    )
    assert decision.verdict == "escalate"
    assert "every action" in decision.reason


def test_external_action_requires_authoritative_fresh_state_when_configured():
    charter = _charter(
        security={"require_fresh_state_for_external_actions": True}
    )
    missing = policy.evaluate_action(
        objective=_objective(), action=_action(), charter=charter
    )
    assert missing.verdict == "escalate"
    assert "fresh authoritative state" in missing.reason

    evidenced_action = _action(
        payload={
            "system": "crm",
            "target_resource": "lead:123",
            "observed_state_at": 1_800_000_000,
            "max_state_age_seconds": 300,
            "state_evidence": {
                "source": "crm.readback",
                "reference": "etag:abc",
            },
        }
    )
    permitted = policy.evaluate_action(
        objective=_objective(), action=evidenced_action, charter=charter
    )
    assert permitted.verdict == "permit"


def test_reversible_action_requires_exact_compensation_when_configured():
    decision = policy.evaluate_action(
        objective=_objective(),
        action=_action(reversible=1),
        charter=_charter(
            security={"require_compensation_for_reversible_actions": True}
        ),
    )
    assert decision.verdict == "escalate"
    assert "compensation contract" in decision.reason
