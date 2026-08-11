"""Pure deterministic Stage-3 risk scoring."""

from devflow_delegation.allowlist import TargetConfig
from devflow_delegation.risk import RiskInput, assess_risk


def _target(**overrides):
    values = dict(
        repo="fixture",
        checkout_path="/fixture",
        allowed_globs=("src/**",),
        denied_globs=("**/.env", "**/secrets/**"),
        risk_ceiling="medium",
        live_gateway_imports=False,
    )
    values.update(overrides)
    return TargetConfig(**values)


def test_small_explicit_reversible_scoped_change_is_low_risk():
    assessment = assess_risk(
        _target(),
        RiskInput(
            changed_paths=("src/feature.py",),
            changed_lines=12,
            reversible=True,
            severity="low",
            source_kind="explicit",
        ),
    )

    assert assessment.score == 0
    assert assessment.tier == "low"
    assert assessment.blocked is False


def test_out_of_scope_change_is_a_critical_hard_block():
    assessment = assess_risk(
        _target(),
        RiskInput(
            changed_paths=("scripts/restart_gateway.py",),
            changed_lines=1,
            reversible=True,
            severity="low",
            source_kind="explicit",
        ),
    )

    assert assessment.tier == "critical"
    assert assessment.blocked is True
    assert "out_of_scope_path" in assessment.reasons


def test_large_nonreversible_unknown_source_change_is_critical():
    assessment = assess_risk(
        _target(),
        RiskInput(
            changed_paths=("src/large_change.py",),
            changed_lines=1501,
            reversible=False,
            severity="critical",
            source_kind="unknown-source",
        ),
    )

    assert assessment.score >= 9
    assert assessment.tier == "critical"
    assert assessment.blocked is False
    assert {"large_diff", "not_reversible", "unknown_source"} <= set(assessment.reasons)


def test_missing_diff_evidence_fails_closed():
    assessment = assess_risk(
        _target(),
        RiskInput(
            changed_paths=(),
            changed_lines=None,
            reversible=None,
            severity="low",
            source_kind="explicit",
        ),
    )

    assert assessment.tier == "critical"
    assert assessment.blocked is True
    assert {"missing_changed_paths", "missing_diff_size", "unknown_reversibility"} <= set(
        assessment.reasons
    )
