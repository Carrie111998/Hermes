"""Gateway-facing service lifecycle for governed objective processing."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from hermes_cli import objectives_db as db
from hermes_cli import resource_budget
from hermes_cli.objective_adapters import (
    ActionExecutorRegistry,
    AuxiliaryObjectivePlanner,
    IndependentVerifierRegistry,
    register_accounting_adapters,
    register_commitment_adapters,
    register_kanban_adapters,
    register_email_adapters,
    register_payment_adapters,
    register_portfolio_adapters,
    register_procurement_adapters,
    register_strategy_adapters,
    register_workforce_adapters,
    organization_planning_context,
)
from hermes_cli.objective_runtime import CycleOutcome, ObjectiveRuntime

logger = logging.getLogger(__name__)


def _raise_readiness_intervention(
    conn,
    *,
    category: str,
    summary: str,
    context: dict[str, Any],
    options: list[dict[str, str]],
    organization_id: str = "__unscoped__",
    objective_id: Optional[str] = None,
    dedupe_key: Optional[str] = None,
) -> None:
    """Persist a bounded advisor handoff for a deterministic readiness stop."""
    from hermes_cli import operational_control

    operational_control.raise_intervention(
        conn,
        organization_id=organization_id,
        objective_id=objective_id,
        category=category,
        summary=summary,
        context=context,
        options=options,
        dedupe_key=dedupe_key,
    )


def build_runtime(
    conn,
    config: dict[str, Any],
    *,
    board: Optional[str] = None,
) -> ObjectiveRuntime:
    charter = config.get("agentic") or {}
    from hermes_cli import organization_db

    ceo = organization_db.active_ceo(conn)
    identity = f"employee:{ceo['id']}" if ceo is not None else "employee:ceo"
    security = charter.get("security") or {}
    executor = ActionExecutorRegistry(
        identity=identity,
        authority_conn=conn,
        failure_threshold=int(
            security.get("circuit_breaker_failure_threshold", 3)
        ),
        cooldown_seconds=int(
            security.get("circuit_breaker_cooldown_seconds", 900)
        ),
    )
    verifier = IndependentVerifierRegistry(identity="control:verification")
    register_kanban_adapters(
        executor,
        verifier,
        board=board,
        authority_conn=conn,
        manager_employee_id=ceo["id"] if ceo is not None else None,
    )
    register_payment_adapters(executor, verifier, authority_conn=conn)
    register_accounting_adapters(executor, verifier, authority_conn=conn)
    register_procurement_adapters(
        executor, verifier, authority_conn=conn
    )
    register_email_adapters(
        executor,
        verifier,
        authority_conn=conn,
        config=config,
    )
    register_workforce_adapters(
        executor,
        verifier,
        authority_conn=conn,
        config=config,
    )
    register_portfolio_adapters(
        executor,
        verifier,
        authority_conn=conn,
        config=config,
    )
    register_strategy_adapters(
        executor,
        verifier,
        authority_conn=conn,
    )
    register_commitment_adapters(
        executor,
        verifier,
        authority_conn=conn,
    )
    capabilities = list(charter.get("allowed_capabilities") or [])
    systems = list(charter.get("allowed_systems") or [])
    action_contracts = [
        contract
        for contract in executor.authority_contracts
        if contract.required_capability in capabilities
        and contract.target_system in systems
        and contract.verification_method in verifier.action_methods
    ]
    resource_limits = {
        **resource_budget.DEFAULT_LIMITS,
        **(charter.get("resource_limits") or {}),
    }
    planner_config = (
        (config.get("auxiliary") or {}).get("objective_planner") or {}
    )
    planner = AuxiliaryObjectivePlanner(
        action_contracts=action_contracts,
        context_provider=(
            lambda: organization_planning_context(
                conn, str(ceo["organization_id"])
            )
            if ceo is not None
            else {}
        ),
        authority_conn=conn,
        resource_limits=resource_limits,
        planner_call_compute_reservation_minor=int(
            resource_limits.get(
                "planner_call_compute_reservation_minor", 10
            )
        ),
        enforce_treasury_budget=True,
        billing_provider=planner_config.get("provider"),
        billing_base_url=planner_config.get("base_url"),
        timeout=float(
            planner_config.get("timeout", 120)
            or 120
        ),
    )
    runtime = ObjectiveRuntime(
        conn,
        planner=planner,
        executor=executor,
        verifier=verifier,
        charter=charter,
        policy_version=str(charter.get("policy_version") or "charter-v1"),
    )
    represented = {
        contract.required_capability for contract in action_contracts
    }
    runtime.unavailable_allowed_capabilities = tuple(
        sorted(set(capabilities) - represented)
    )
    unreachable = []
    terminal = (
        "verified", "closed", "cancelled", "expired", "abandoned", "superseded"
    )
    rows = (
        conn.execute(
            f"""SELECT id,success_criteria_json FROM objectives
                WHERE organization_id=?
                  AND status NOT IN ({','.join('?' for _ in terminal)})
                ORDER BY created_at,id""",
            (str(ceo["organization_id"]), *terminal),
        ).fetchall()
        if ceo is not None
        else []
    )
    objective_methods = set(verifier.objective_methods)
    for row in rows:
        criteria = json.loads(row["success_criteria_json"])
        invalid = [
            criterion
            for criterion in criteria
            if not isinstance(criterion, dict)
            or not str(criterion.get("verifier") or "")
            or not isinstance(criterion.get("params", {}), dict)
            or str(criterion.get("verifier") or "") not in objective_methods
        ]
        if not criteria or invalid:
            unreachable.append(
                {
                    "objective_id": str(row["id"]),
                    "reason": (
                        "missing success criteria"
                        if not criteria
                        else "unregistered or malformed success verifier contract"
                    ),
                }
            )
    runtime.unreachable_objectives = tuple(unreachable)
    return runtime


def tick_once(
    *,
    board: Optional[str] = None,
    db_path=None,
) -> CycleOutcome:
    """Process at most one due event for the active profile."""
    from hermes_cli.config import load_config

    config = load_config()
    charter = config.get("agentic") or {}
    if not bool(charter.get("enabled", False)):
        return CycleOutcome(None, None, "disabled", "agentic operation is disabled")
    from hermes_cli.business_security import evaluate_security_readiness

    readiness = evaluate_security_readiness(config)
    if not readiness.ready:
        return CycleOutcome(
            None,
            None,
            "security_blocked",
            "; ".join(readiness.violations),
        )
    with db.connect_closing(db_path) as conn:
        from hermes_cli import authority_integrity
        from hermes_cli import operational_control
        from hermes_cli import objective_maintenance
        from hermes_cli import objective_triggers

        from hermes_cli import organization_db

        ceo = organization_db.active_ceo(conn)
        if ceo is None:
            _raise_readiness_intervention(
                conn,
                category="ceo_authority_missing",
                summary="Autonomous operation has no active Founder/CEO authority record",
                context={
                    "reason": "no active CEO authority record is configured",
                    "required_role": "founder_ceo",
                },
                options=[
                    {"id": "bootstrap", "label": "Complete Founder/CEO bootstrap"},
                    {"id": "repair", "label": "Repair the authority record"},
                    {"id": "manual", "label": "Remain in manual operation"},
                ],
                dedupe_key="readiness:ceo-authority-missing",
            )
            return CycleOutcome(
                None,
                None,
                "configuration_blocked",
                "no active CEO authority record is configured",
            )
        from hermes_cli import runtime_deployment

        try:
            runtime_deployment.assert_current_runtime(conn, charter)
        except runtime_deployment.RuntimeDeploymentError as exc:
            _raise_readiness_intervention(
                conn,
                organization_id=str(ceo["organization_id"]),
                category="runtime_host_mismatch",
                summary="The configured runtime host cannot execute this worker",
                context={"error": str(exc), "authority_boundary": "No action was attempted"},
                options=[
                    {"id": "select_host", "label": "Select an admissible runtime host"},
                    {"id": "restart", "label": "Restart under the configured host"},
                    {"id": "manual", "label": "Remain in manual operation"},
                ],
                dedupe_key=f"readiness:runtime-host-mismatch:{ceo['organization_id']}",
            )
            return CycleOutcome(None, None, "runtime_host_blocked", str(exc))
        integrity = authority_integrity.enforce_preflight(
            conn,
            organization_id=str(ceo["organization_id"]),
            policy=charter,
        )
        if not integrity.ready:
            return CycleOutcome(
                None,
                None,
                "integrity_blocked",
                "authority integrity preflight failed: "
                + ", ".join(integrity.checks["failed_checks"]),
            )
        from hermes_cli import runtime_drift

        drift = runtime_drift.enforce(
            conn,
            organization_id=str(ceo["organization_id"]),
            charter=charter,
        )
        if not drift.ready:
            return CycleOutcome(
                None,
                None,
                "runtime_drift_blocked",
                "runtime baseline " + drift.status,
            )
        from hermes_cli import authority_recovery

        recovery_config = charter.get("recovery") or {}
        if bool(recovery_config.get("enabled", True)):
            try:
                authority_recovery.maybe_snapshot(
                    conn,
                    organization_id=str(ceo["organization_id"]),
                    interval_seconds=max(
                        60,
                        int(
                            recovery_config.get(
                                "snapshot_interval_seconds", 86400
                            )
                        ),
                    ),
                    retention_count=max(
                        1, int(recovery_config.get("retention_count", 7))
                    ),
                    root=(
                        Path(db_path).expanduser().resolve().parent
                        / "business-recovery"
                        if db_path is not None
                        else None
                    ),
                )
            except Exception as exc:
                reason = f"known-good authority snapshot failed: {exc}"
                state = operational_control.autonomy_state(conn)
                if state["mode"] == "autonomous":
                    operational_control.set_autonomy_mode(
                        conn,
                        mode="paused",
                        actor="control:recovery",
                        reason=reason,
                    )
                operational_control.raise_intervention(
                    conn,
                    organization_id=str(ceo["organization_id"]),
                    category="authority_recovery_unavailable",
                    summary="Known-good authority recovery snapshot failed",
                    context={"error": str(exc)},
                    options=[
                        {"id": "repair", "label": "Repair recovery storage"},
                        {"id": "manual", "label": "Remain in manual operation"},
                    ],
                    dedupe_key=f"authority-recovery:{ceo['organization_id']}",
                )
                return CycleOutcome(None, None, "recovery_blocked", reason)
        objective_maintenance.run_housekeeping(
            conn,
            compute_reconciliation_grace_seconds=max(
                1,
                int(
                    (charter.get("resource_limits") or {}).get(
                        "compute_reconciliation_grace_seconds",
                        86_400,
                    )
                ),
            ),
        )
        recovered = operational_control.recover_incomplete_executions(conn)
        if recovered:
            logger.warning(
                "objective runtime recovered %d uncertain external effects",
                len(recovered),
            )
        objective_triggers.dispatch_due(conn)
        sync_kanban_events(conn, board=board)
        from hermes_cli import compliance_deadlines

        compliance_config = charter.get("compliance") or {}
        compliance_deadlines.dispatch_deadlines(
            conn,
            organization_id=str(ceo["organization_id"]),
            horizon_seconds=max(
                0,
                int(compliance_config.get("deadline_horizon_days", 30)) * 86400,
            ),
        )
        from hermes_cli import business_commitments

        business_commitments.dispatch_due(
            conn,
            organization_id=str(ceo["organization_id"]),
            horizon_seconds=max(
                0,
                int(
                    (charter.get("commitments") or {}).get(
                        "deadline_horizon_days", 7
                    )
                )
                * 86400,
            ),
        )
        from hermes_cli import business_metrics

        business_metrics.sync_financial_observations(
            conn,
            organization_id=str(ceo["organization_id"]),
        )
        business_metrics.dispatch_reviews(
            conn,
            organization_id=str(ceo["organization_id"]),
        )
        from hermes_cli import outcome_attribution

        outcome_attribution.sync_authoritative_links(
            conn, str(ceo["organization_id"])
        )
        runtime = build_runtime(conn, config, board=board)
        if runtime.unavailable_allowed_capabilities:
            _raise_readiness_intervention(
                conn,
                organization_id=str(ceo["organization_id"]),
                category="runtime_capability_unavailable",
                summary="The charter allows capabilities with no governed executor",
                context={
                    "capabilities": list(runtime.unavailable_allowed_capabilities),
                    "authority_boundary": "No external action was attempted",
                },
                options=[
                    {"id": "configure", "label": "Configure a governed executor"},
                    {"id": "narrow_charter", "label": "Narrow the allowed capabilities"},
                    {"id": "manual", "label": "Remain in manual operation"},
                ],
                dedupe_key=(
                    "readiness:runtime-capability-unavailable:"
                    + str(ceo["organization_id"])
                ),
            )
            return CycleOutcome(
                None,
                None,
                "configuration_blocked",
                "allowed capabilities have no configured governed executor: "
                + ", ".join(runtime.unavailable_allowed_capabilities),
            )
        if runtime.unreachable_objectives:
            item = runtime.unreachable_objectives[0]
            objective_id = str(item["objective_id"])
            objective = db.get_objective(conn, objective_id)
            if objective.status in {"accepted", "planned"}:
                db.transition_objective(
                    conn,
                    objective_id,
                    "blocked",
                    actor="control:readiness",
                    reason=str(item["reason"]),
                )
            _raise_readiness_intervention(
                conn,
                organization_id=str(ceo["organization_id"]),
                objective_id=objective_id,
                category="objective_unreachable",
                summary="An active objective has no admissible success verifier",
                context={
                    "reason": str(item["reason"]),
                    "objective_id": objective_id,
                    "authority_boundary": "No plan or external action was attempted",
                },
                options=[
                    {"id": "configure_verifier", "label": "Configure a verifier contract"},
                    {"id": "redefine_success", "label": "Redefine success criteria"},
                    {"id": "abandon", "label": "Abandon the objective"},
                ],
                dedupe_key=f"readiness:objective-unreachable:{objective_id}",
            )
            return CycleOutcome(
                None,
                objective_id,
                "configuration_blocked",
                str(item["reason"]),
            )
        if not runtime.planner.action_types:
            _raise_readiness_intervention(
                conn,
                organization_id=str(ceo["organization_id"]),
                category="runtime_no_action_contract",
                summary="The charter admits no registered governed action contract",
                context={"authority_boundary": "No plan or external action was attempted"},
                options=[
                    {"id": "configure", "label": "Configure a governed action contract"},
                    {"id": "narrow_charter", "label": "Narrow or disable autonomous operation"},
                    {"id": "manual", "label": "Remain in manual operation"},
                ],
                dedupe_key=f"readiness:no-action-contract:{ceo['organization_id']}",
            )
            return CycleOutcome(
                None,
                None,
                "configuration_blocked",
                "charter admits no registered action contract",
            )
        return runtime.tick()


def sync_kanban_events(conn, *, board: Optional[str] = None) -> int:
    """Project delegated Kanban task state changes into the objective inbox."""
    from hermes_cli import kanban_db as kb

    rows = conn.execute(
        """
        SELECT a.objective_id, o.organization_id, r.external_reference
          FROM execution_results r
          JOIN candidate_actions a ON a.id = r.action_id
          JOIN objectives o ON o.id = a.objective_id
         WHERE a.action_type = 'kanban.create_task'
           AND r.external_reference IS NOT NULL
           AND o.status NOT IN (
               'closed','cancelled','expired','abandoned','superseded','verified'
           )
        """
    ).fetchall()
    enqueued = 0
    if not rows:
        return enqueued
    with kb.connect_closing(board=board) as kanban:
        for row in rows:
            task_id = str(row["external_reference"])
            task = kb.get_task(kanban, task_id)
            if task is None:
                state = "missing"
                state_version = "missing"
                payload = {"task_id": task_id, "status": state}
            else:
                state = task.status
                state_version = str(
                    task.completed_at
                    or task.started_at
                    or task.last_heartbeat_at
                    or task.created_at
                )
                payload = {
                    "task_id": task_id,
                    "status": task.status,
                    "result": task.result,
                    "assignee": task.assignee,
                }
            if state not in {"done", "blocked", "failed", "cancelled", "missing"}:
                continue
            from hermes_cli import operational_control, workforce_delegation

            try:
                workforce_delegation.validate_task_result_authority(
                    conn, task_id
                )
            except workforce_delegation.DelegationError as exc:
                operational_control.raise_intervention(
                    conn,
                    organization_id=str(row["organization_id"]),
                    objective_id=str(row["objective_id"]),
                    category="delegated_result_authority_invalid",
                    summary="Delegated worker result cannot enter CEO planning",
                    context={
                        "task_id": task_id,
                        "task_status": state,
                        "reason": str(exc),
                    },
                    options=[
                        {
                            "id": "reconcile",
                            "label": "Reconcile employee and mandate evidence",
                        },
                        {"id": "reassign", "label": "Reassign under a new grant"},
                        {"id": "abandon", "label": "Abandon delegated work"},
                    ],
                    dedupe_key=f"delegated-result-authority:{task_id}:{state}",
                )
                continue
            before = conn.execute(
                "SELECT COUNT(*) AS n FROM objective_inbox WHERE dedupe_key = ?",
                (f"kanban:{task_id}:{state}:{state_version}",),
            ).fetchone()["n"]
            db.enqueue_objective_event(
                conn,
                objective_id=row["objective_id"],
                event_type=f"kanban.task.{state}",
                payload=payload,
                dedupe_key=f"kanban:{task_id}:{state}:{state_version}",
            )
            if not before:
                enqueued += 1
    return enqueued


async def gateway_watcher(runner) -> None:
    """Tick objectives without blocking the gateway event loop."""
    import asyncio

    from hermes_cli import objective_worker
    from hermes_cli.config import load_config

    with db.connect_closing() as health_conn:
        from hermes_cli import runtime_deployment

        config = load_config()
        charter = config.get("agentic") or {}
        try:
            runtime_deployment.validate_worker_role(
                charter, "gateway-objective-runtime"
            )
        except runtime_deployment.RuntimeDeploymentError as exc:
            logger.info("objective runtime: %s", exc)
            return
        startup_interval = max(
            1.0,
            float(charter.get("runtime_interval_seconds", 15) or 15),
        )
        objective_worker.reconcile_stale_workers(
            health_conn,
            stale_after_seconds=max(60, int(startup_interval * 3)),
        )
        worker_id = objective_worker.register_worker(
            health_conn, role="gateway-objective-runtime"
        )
        try:
            with objective_worker.WorkerHeartbeatKeeper(
                health_conn, worker_id
            ) as heartbeat_keeper:
                while runner._running:
                    try:
                        heartbeat_keeper.assert_healthy()
                    except RuntimeError:
                        logger.error(
                            "objective runtime heartbeat lease lost; stopping gateway worker"
                        )
                        return
                    try:
                        config = load_config()
                        charter = config.get("agentic") or {}
                        if not bool(charter.get("enabled", False)):
                            logger.info("objective runtime: disabled by setup charter")
                            return
                        interval = max(
                            1.0,
                            float(charter.get("runtime_interval_seconds", 15) or 15),
                        )
                        security = charter.get("security") or {}
                        failure_threshold = max(
                            1,
                            int(
                                security.get(
                                    "circuit_breaker_failure_threshold", 3
                                )
                                or 3
                            ),
                        )
                        retry = charter.get("retry_policy") or {}
                        max_backoff = max(
                            interval,
                            float(retry.get("max_backoff_seconds", 900) or 900),
                        )
                    except Exception as exc:
                        objective_worker.heartbeat(
                            health_conn,
                            worker_id,
                            cycle_status="configuration_error",
                            error=str(exc),
                        )
                        objective_worker.open_worker_circuit(
                            health_conn,
                            worker_id,
                            role="gateway-objective-runtime",
                            error=str(exc),
                            failure_threshold=1,
                        )
                        logger.warning(
                            "objective runtime: invalid config; stopping (%s)", exc
                        )
                        return
                    consecutive_failures = 0
                    try:
                        outcome = await asyncio.to_thread(tick_once)
                        heartbeat_keeper.assert_healthy()
                        consecutive_failures = objective_worker.heartbeat(
                            health_conn, worker_id, cycle_status=outcome.status
                        )
                        if outcome.status in objective_worker.FAIL_CLOSED_STATUSES:
                            logger.warning(
                                "objective runtime safety stop: status=%s reason=%s",
                                outcome.status,
                                outcome.reason,
                            )
                            return
                        if outcome.status not in {"idle", "disabled"}:
                            logger.info(
                                "objective runtime: objective=%s status=%s reason=%s",
                                outcome.objective_id,
                                outcome.status,
                                outcome.reason,
                            )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        try:
                            consecutive_failures = objective_worker.heartbeat(
                                health_conn,
                                worker_id,
                                cycle_status="worker_error",
                                error=str(exc),
                            )
                        except RuntimeError:
                            logger.error(
                                "objective runtime heartbeat lease lost after tick; stopping gateway worker"
                            )
                            return
                        logger.exception("objective runtime tick failed")
                        if consecutive_failures >= failure_threshold:
                            objective_worker.open_worker_circuit(
                                health_conn,
                                worker_id,
                                role="gateway-objective-runtime",
                                error=str(exc),
                                failure_threshold=failure_threshold,
                            )
                            logger.error(
                                "objective runtime circuit opened after %d failures",
                                consecutive_failures,
                            )
                            return
                    delay = min(
                        max_backoff,
                        interval * (2 ** max(0, consecutive_failures - 1)),
                    )
                    await asyncio.sleep(delay)
        finally:
            objective_worker.stop_worker(health_conn, worker_id)
