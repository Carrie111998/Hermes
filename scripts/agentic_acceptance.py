#!/usr/bin/env python3
"""Bounded current-tree Founder/CEO acceptance scenario.

Phases are separate so the caller can restart the container between run and
recover while retaining one durable state volume. The provider is a
deterministic file-backed test adapter, never a live rail.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from hermes_cli import (
    business,
    config as config_module,
    objective_triggers,
    objective_worker,
    objectives_db,
    operational_control,
)
from hermes_cli.objective_runtime import (
    ActionProposal,
    ExecutionOutcome,
    ObjectiveRuntime,
    PlanProposal,
    VerificationOutcome,
)
from hermes_cli import verification_evidence
from hermes_constants import get_hermes_home


def _db() -> Path:
    return objectives_db.objectives_db_path()


def _provider_file() -> Path:
    return get_hermes_home().resolve() / "acceptance-provider.json"


def _metadata_file() -> Path:
    return get_hermes_home().resolve() / "acceptance-objective.json"


class AcceptancePlanner:
    identity = "employee:ceo"

    def propose(self, snapshot, event):
        if not snapshot["execution_results"]:
            return PlanProposal(
                assumptions=[],
                tasks=[{"step": "publish bounded acceptance offer"}],
                dependencies=[],
                risks=[],
                actions=(
                    ActionProposal(
                        action_type="market.publish",
                        payload={
                            "system": "market",
                            "target_resource": "offer:acceptance",
                            "idempotency_key": "agentic-acceptance-offer-0001",
                            "observed_state_at": int(time.time()),
                            "max_state_age_seconds": 300,
                            "state_evidence": {
                                "reference": "acceptance-provider:preflight"
                            },
                            "compliance_context": {
                                "jurisdictions": [],
                                "activities": [],
                                "data_classes": [],
                                "entity_attributes": [],
                            },
                        },
                        expected_outcome="offer is externally visible",
                        required_capability="market.publish",
                        verification_method="market.readback",
                        risk_class="low",
                        reversible=True,
                        compensation={
                            "action_type": "market.withdraw",
                            "payload": {
                                "system": "market",
                                "target_resource": "offer:acceptance",
                                "idempotency_key": "agentic-acceptance-withdraw-0001",
                            },
                            "required_capability": "market.publish",
                            "verification_method": "market.readback",
                        },
                    ),
                ),
                objective_complete_when_verified=False,
            )
        return PlanProposal(
            assumptions=["provider read-back remains authoritative"],
            tasks=[],
            dependencies=[],
            risks=[],
            actions=(),
            objective_complete_when_verified=True,
        )


class AcceptanceExecutor:
    identity = "employee:ceo"

    def execute(self, action_type, payload):
        path = _provider_file()
        current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        key = str(payload["idempotency_key"])
        current.setdefault("effects", {})
        current["effects"].setdefault(
            key,
            {
                "target_resource": payload["target_resource"],
                "created_at": int(time.time()),
            },
        )
        path.write_text(json.dumps(current, sort_keys=True) + "\n", encoding="utf-8")
        return ExecutionOutcome(
            "succeeded",
            {"provider_readback": key in current["effects"]},
            external_reference=f"acceptance-provider:{key}",
        )


class AcceptanceVerifier:
    identity = "control:acceptance-verifier"

    def verify(self, action, execution):
        path = _provider_file()
        current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        key = str(action.payload["idempotency_key"])
        visible = key in current.get("effects", {})
        return VerificationOutcome(
            "pass" if visible else "fail",
            verification_evidence.build(
                observer=self.identity,
                source_kind="provider_readback",
                source_reference=str(execution.external_reference),
                facts={"visible": visible, "idempotency_key": key},
            ),
        )

    def verify_objective(self, snapshot, plan, action_verifications):
        path = _provider_file()
        current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        visible = "agentic-acceptance-offer-0001" in current.get("effects", {})
        return VerificationOutcome(
            "pass" if visible else "fail",
            verification_evidence.build(
                observer=self.identity,
                source_kind="provider_readback",
                source_reference="acceptance-provider:agentic-acceptance-offer-0001",
                facts={"visible": visible},
            ),
        )


def _runtime(conn):
    charter = config_module.load_config().get("agentic") or {}
    return ObjectiveRuntime(
        conn,
        planner=AcceptancePlanner(),
        executor=AcceptanceExecutor(),
        verifier=AcceptanceVerifier(),
        charter=charter,
        policy_version=str(charter.get("policy_version") or "acceptance"),
        runtime_id="runtime:agentic-acceptance",
    )


def prepare() -> None:
    """Bootstrap, prove the initial block, satisfy controls, and enqueue work."""
    conn = objectives_db.connect(_db())
    try:
        initial = business.build_business_readiness(conn)
        assert initial["state"] == "unconfigured", initial
        print(json.dumps({"phase": "prepare", "initial_readiness": "blocked"}))
    finally:
        conn.close()

    charter = json.loads(
        Path("/opt/hermes/examples/agentic-charter.json").read_text(encoding="utf-8")
    )
    from hermes_cli.setup import _bootstrap_agentic_business

    organization_id, _ = _bootstrap_agentic_business(charter)
    config = config_module.load_config()
    agentic = config.setdefault("agentic", {})
    agentic.update(charter)
    agentic.setdefault("operating_cadence", {})["enabled"] = False
    agentic["runtime_host"] = "standalone"
    agentic.setdefault("security", {}).update(
        {
            "enforce_isolated_execution": True,
            "require_external_secret_manager": True,
        }
    )
    agentic.setdefault("finance", {}).setdefault("payments", {})[
        "custody_model"
    ] = "non_custodial"
    config.setdefault("terminal", {})["backend"] = "docker"
    config.setdefault("secrets", {}).setdefault("bitwarden", {})["enabled"] = True
    config_module.save_config(config, merge_existing=True)

    conn = objectives_db.connect(_db())
    try:
        ready = business.build_business_readiness(conn)
        assert ready["ready"] is True, ready
        assert ready["runtime_active"] is False, ready
        objective = objectives_db.create_objective(
            conn,
            organization_id=organization_id,
            desired_outcome="Publish and verify the bounded acceptance offer",
            originator="employee:ceo",
            permitted_systems=["market"],
            success_criteria=[
                {
                    "verifier": "market.external_offer_visible",
                    "params": {"offer": "acceptance"},
                }
            ],
        )
        objectives_db.transition_objective(conn, objective.id, "accepted", actor="employee:ceo")
        objective_triggers.subscribe(
            conn,
            organization_id=organization_id,
            objective_id=objective.id,
            source_type="acceptance-webhook",
            event_type="objective.requested",
        )
        routed = objective_triggers.route_external_event(
            conn,
            organization_id=organization_id,
            source_type="acceptance-webhook",
            event_type="objective.requested",
            source_reference="acceptance-request-0001",
            payload={"source": "current-tree-acceptance"},
            authentication_evidence={
                "method": "acceptance-hmac",
                "key_id": "acceptance-key",
                "signature_validated": True,
                "signed_timestamp": int(time.time()),
            },
        )
        assert len(routed) == 1, routed
        objective_triggers.create_schedule(
            conn,
            organization_id=organization_id,
            objective_id=objective.id,
            event_type="objective.scheduled-review",
            interval_seconds=3600,
            next_fire_at=int(time.time()) - 1,
            payload={"source": "current-tree-schedule"},
            idempotency_key="agentic-acceptance-schedule-0001",
        )
        _metadata_file().write_text(
            json.dumps(
                {"organization_id": organization_id, "objective_id": objective.id}
            )
            + "\n",
            encoding="utf-8",
        )
        print(json.dumps({"phase": "prepare", "ready": True, "runtime_active": False}))
    finally:
        conn.close()


def run_worker() -> None:
    metadata = json.loads(_metadata_file().read_text(encoding="utf-8"))
    conn = objectives_db.connect(_db())
    try:
        def tick_with_schedule_dispatch():
            objective_triggers.dispatch_due(conn)
            return _runtime(conn).tick()

        result = objective_worker.run_forever(
            db_path=_db(), tick=tick_with_schedule_dispatch, max_cycles=4,
            interval_seconds=1,
        )
        assert result == 0, result
        row = conn.execute(
            "SELECT status FROM objectives WHERE id=?", (metadata["objective_id"],)
        ).fetchone()
        if row["status"] != "verified":
            raise AssertionError(
                {
                    "status": dict(row),
                    "snapshot": objectives_db.objective_snapshot(
                        conn, metadata["objective_id"]
                    ),
                }
            )
        effects = json.loads(_provider_file().read_text(encoding="utf-8"))["effects"]
        assert list(effects) == ["agentic-acceptance-offer-0001"], effects
        scheduled = conn.execute(
            """SELECT COUNT(*) AS n FROM objective_inbox
               WHERE objective_id=? AND event_type='objective.scheduled-review'
                 AND status='completed'""",
            (metadata["objective_id"],),
        ).fetchone()["n"]
        assert scheduled == 1, scheduled
        plan_versions = conn.execute(
            "SELECT COUNT(*) AS n FROM plans WHERE objective_id=?",
            (metadata["objective_id"],),
        ).fetchone()["n"]
        assert plan_versions >= 2, plan_versions
        print(json.dumps({
            "phase": "run", "objective": "verified", "effects": 1,
            "scheduled_events": scheduled, "plan_versions": plan_versions,
        }))
    finally:
        conn.close()


def recover() -> None:
    metadata = json.loads(_metadata_file().read_text(encoding="utf-8"))
    conn = objectives_db.connect(_db())
    try:
        before = json.loads(_provider_file().read_text(encoding="utf-8"))
        row = conn.execute(
            "SELECT status FROM objectives WHERE id=?", (metadata["objective_id"],)
        ).fetchone()
        assert row["status"] == "verified", dict(row)
        result = objective_worker.run_forever(
            db_path=_db(), tick=_runtime(conn).tick, max_cycles=1, interval_seconds=1
        )
        assert result == 0, result
        after = json.loads(_provider_file().read_text(encoding="utf-8"))
        assert after == before, {"before": before, "after": after}
        print(
            json.dumps(
                {"phase": "recover", "durable_state": "verified", "duplicate_effects": 0}
            )
        )
    finally:
        conn.close()


def stop() -> None:
    """Revoke autonomy and prove the worker stops before another effect."""
    conn = objectives_db.connect(_db())
    try:
        before = json.loads(_provider_file().read_text(encoding="utf-8"))
        generation = operational_control.set_autonomy_mode(
            conn,
            mode="paused",
            actor="advisor:acceptance",
            reason="master stop acceptance",
        )
        result = objective_worker.run_forever(
            db_path=_db(), tick=_runtime(conn).tick, max_cycles=1, interval_seconds=1
        )
        assert result == 0, result
        assert operational_control.autonomy_state(conn)["mode"] == "paused"
        after = json.loads(_provider_file().read_text(encoding="utf-8"))
        assert after == before, {"before": before, "after": after}
        print(
            json.dumps(
                {
                    "phase": "stop",
                    "autonomy": "paused",
                    "generation": generation,
                    "duplicate_effects": 0,
                }
            )
        )
    finally:
        conn.close()


if __name__ == "__main__":
    phases = {
        "prepare": prepare,
        "run": run_worker,
        "recover": recover,
        "stop": stop,
    }
    try:
        phases[sys.argv[1]]()
    except (AssertionError, KeyError, IndexError) as exc:
        print(f"acceptance phase failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
