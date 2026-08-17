"""Shadow-first Stage-3 autonomy gate.

The gate only decides and records evidence. It never transitions lifecycle state,
merges, deploys, restarts a gateway, or creates the operator-owned sentinel.
"""


from devflow_delegation.allowlist import TargetConfig
from devflow_delegation.gate import GateRequest, evaluate_gate, record_shadow_decision
from devflow_delegation.ledger import DelegationLedger
from devflow_delegation.risk import RiskInput


def _target(**overrides):
    values = dict(
        repo="fixture",
        checkout_path="/fixture",
        allowed_globs=("src/**",),
        denied_globs=("**/.env", "**/secrets/**"),
        risk_ceiling="medium",
        max_autonomous_action="create_pr",
        live_gateway_imports=False,
    )
    values.update(overrides)
    return TargetConfig(**values)


def _request(**overrides):
    values = dict(
        request_id="dwr_gate_fixture",
        action="merge",
        risk_input=RiskInput(
            changed_paths=("src/feature.py",),
            changed_lines=10,
            reversible=True,
            severity="low",
            source_kind="explicit",
        ),
    )
    values.update(overrides)
    return GateRequest(**values)


def test_absent_sentinel_records_shadow_eligible_decision(tmp_path):
    sentinel = tmp_path / ".autonomy_enabled"

    decision = evaluate_gate(_target(), _request(), sentinel_path=sentinel)

    assert decision.mode == "shadow"
    assert decision.eligible is True
    assert decision.authorized is False
    assert decision.reason == "shadow_mode"
    assert not sentinel.exists()


def test_present_sentinel_still_never_authorizes_merge(tmp_path):
    sentinel = tmp_path / ".autonomy_enabled"
    sentinel.write_text("operator-owned\n", encoding="utf-8")

    decision = evaluate_gate(_target(), _request(), sentinel_path=sentinel)

    assert decision.mode == "enabled"
    assert decision.eligible is True
    assert decision.authorized is False
    assert decision.reason == "merge_authority_not_implemented"


def test_present_sentinel_still_never_authorizes_deploy(tmp_path):
    sentinel = tmp_path / ".autonomy_enabled"
    sentinel.write_text("operator-owned\n", encoding="utf-8")

    decision = evaluate_gate(
        _target(), _request(action="deploy"), sentinel_path=sentinel
    )

    assert decision.mode == "enabled"
    assert decision.authorized is False
    assert decision.reason == "deploy_authority_not_implemented"


def test_live_gateway_target_is_hard_denied_even_when_sentinel_exists(tmp_path):
    sentinel = tmp_path / ".autonomy_enabled"
    sentinel.write_text("operator-owned\n", encoding="utf-8")

    decision = evaluate_gate(
        _target(live_gateway_imports=True), _request(), sentinel_path=sentinel
    )

    assert decision.eligible is False
    assert decision.authorized is False
    assert decision.reason == "live_gateway_target"


def test_risk_ceiling_and_window_budget_block_decisions(tmp_path):
    sentinel = tmp_path / ".autonomy_enabled"
    sentinel.write_text("operator-owned\n", encoding="utf-8")
    high_risk = _request(
        risk_input=RiskInput(
            changed_paths=("src/feature.py",),
            changed_lines=801,
            reversible=True,
            severity="high",
            source_kind="explicit",
        )
    )

    ceiling = evaluate_gate(_target(risk_ceiling="low"), high_risk, sentinel_path=sentinel)
    budget = evaluate_gate(
        _target(), _request(), sentinel_path=sentinel, actions_used=2, max_actions=2
    )

    assert ceiling.eligible is False
    assert ceiling.reason == "risk_above_ceiling"
    assert budget.eligible is False
    assert budget.reason == "window_budget_exhausted"


def test_shadow_decision_is_recorded_as_an_idempotent_ledger_artifact(tmp_path):
    ledger = DelegationLedger(tmp_path / "delegation_ledger.db")
    decision = evaluate_gate(
        _target(), _request(), sentinel_path=tmp_path / ".autonomy_enabled"
    )

    artifact = record_shadow_decision(ledger, decision)
    repeated = record_shadow_decision(ledger, decision)

    assert artifact["kind"] == "autonomy_gate"
    assert artifact["request_id"] == "dwr_gate_fixture"
    assert artifact["ref"].startswith("shadow:merge:low:shadow_mode:")
    assert repeated == artifact
    assert len(ledger.artifacts_for("dwr_gate_fixture")) == 1


def test_shadow_decision_record_never_creates_the_autonomy_sentinel(tmp_path):
    sentinel = tmp_path / ".autonomy_enabled"
    ledger = DelegationLedger(tmp_path / "delegation_ledger.db")
    decision = evaluate_gate(_target(), _request(), sentinel_path=sentinel)

    record_shadow_decision(ledger, decision)

    assert not sentinel.exists()
