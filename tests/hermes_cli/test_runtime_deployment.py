from __future__ import annotations

import os

import pytest

from hermes_cli import objectives_db, objective_worker, runtime_deployment


def test_selected_runtime_requires_matching_live_supervisor_process(tmp_path):
    conn = objectives_db.connect(tmp_path / "authority.db")
    charter = {"runtime_host": "gateway"}

    with pytest.raises(
        runtime_deployment.RuntimeDeploymentError,
        match="selected supervised host",
    ):
        runtime_deployment.assert_current_runtime(conn, charter)

    objective_worker.register_worker(
        conn,
        role="objective-runtime",
        worker_id="standalone",
    )
    with pytest.raises(runtime_deployment.RuntimeDeploymentError):
        runtime_deployment.assert_current_runtime(conn, charter)

    gateway_id = objective_worker.register_worker(
        conn,
        role="gateway-objective-runtime",
        worker_id="gateway",
    )
    runtime_deployment.assert_current_runtime(conn, charter)
    result = runtime_deployment.posture(conn, charter)
    assert result["ready"] is True
    assert result["healthy_worker_ids"] == [gateway_id]


def test_stale_or_foreign_process_does_not_prove_runtime_readiness(tmp_path):
    conn = objectives_db.connect(tmp_path / "authority.db")
    worker_id = objective_worker.register_worker(
        conn,
        role="objective-runtime",
        worker_id="stale-worker",
    )
    conn.execute(
        """UPDATE objective_workers SET pid=?,heartbeat_at=1 WHERE id=?""",
        (os.getpid() + 10_000, worker_id),
    )
    conn.commit()
    charter = {"runtime_host": "standalone"}

    posture = runtime_deployment.posture(
        conn, charter, stale_after_seconds=10
    )
    assert posture["ready"] is False
    assert posture["matching_workers"][0]["effective_status"] == "stale"
    with pytest.raises(runtime_deployment.RuntimeDeploymentError):
        runtime_deployment.assert_current_runtime(conn, charter)


def test_host_role_contract_rejects_wrong_entrypoint():
    runtime_deployment.validate_worker_role(
        {"runtime_host": "standalone"}, "objective-runtime"
    )
    with pytest.raises(
        runtime_deployment.RuntimeDeploymentError, match="does not admit"
    ):
        runtime_deployment.validate_worker_role(
            {"runtime_host": "gateway"}, "objective-runtime"
        )


def test_legacy_charter_remains_inspectable_but_not_reported_configured(tmp_path):
    conn = objectives_db.connect(tmp_path / "authority.db")
    runtime_deployment.validate_worker_role({}, "objective-runtime")
    runtime_deployment.assert_current_runtime(conn, {})
    posture = runtime_deployment.posture(conn, {})
    assert posture["configured"] is False
    assert posture["ready"] is False
