import pytest

from hermes_cli import objectives_db, operational_control, runtime_drift


def _charter(**security):
    return {
        "enabled": True,
        "operating_mode": "autonomous",
        "security": {"require_runtime_baseline": True, **security},
    }


def test_human_baseline_is_stable_and_detects_charter_drift(tmp_path):
    conn = objectives_db.connect(tmp_path / "authority.db")
    charter = _charter()
    baseline_id = runtime_drift.accept_baseline(
        conn,
        organization_id="org_1",
        charter=charter,
        actor="human:advisor",
        reason="reviewed deployment",
    )
    assert baseline_id.startswith("runtime_baseline_")
    posture = runtime_drift.check(
        conn,
        organization_id="org_1",
        charter=charter,
        require_baseline=True,
    )
    assert posture.status == "ready"
    assert posture.ready

    changed = _charter(require_idempotency_key_for_external_actions=False)
    drifted = runtime_drift.check(
        conn,
        organization_id="org_1",
        charter=changed,
        require_baseline=True,
    )
    assert drifted.status == "drifted"
    assert "charter" in drifted.differences


def test_rebaseline_requires_human_reason_and_does_not_mutate_history(tmp_path):
    conn = objectives_db.connect(tmp_path / "authority.db")
    charter = _charter()
    first = runtime_drift.accept_baseline(
        conn,
        organization_id="org_1",
        charter=charter,
        actor="human:setup",
        reason="initial",
    )
    with pytest.raises(runtime_drift.RuntimeDriftError):
        runtime_drift.accept_baseline(
            conn,
            organization_id="org_1",
            charter=charter,
            actor="employee:ceo",
            reason="model requested it",
        )
    with pytest.raises(runtime_drift.RuntimeDriftError):
        runtime_drift.accept_baseline(
            conn,
            organization_id="org_1",
            charter=charter,
            actor="human:advisor",
            reason="",
        )
    second = runtime_drift.accept_baseline(
        conn,
        organization_id="org_1",
        charter=_charter(max_autonomous_risk="high"),
        actor="human:advisor",
        reason="reviewed runtime upgrade",
    )
    assert second != first
    assert conn.execute(
        "SELECT COUNT(*) FROM runtime_drift_baselines WHERE organization_id='org_1'"
    ).fetchone()[0] == 2


def test_required_missing_baseline_pauses_autonomy_and_escalates(tmp_path):
    conn = objectives_db.connect(tmp_path / "authority.db")
    operational_control.set_autonomy_mode(
        conn, mode="autonomous", actor="human:advisor", reason="start"
    )
    posture = runtime_drift.enforce(
        conn,
        organization_id="org_1",
        charter=_charter(),
    )
    assert posture.status == "missing"
    assert not posture.ready
    assert operational_control.autonomy_state(conn)["mode"] == "paused"
    interventions = operational_control.list_interventions(conn)
    assert interventions[0]["category"] == "runtime_drift_detected"

