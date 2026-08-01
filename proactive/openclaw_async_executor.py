"""Real zero-effect asynchronous OpenClaw execution through ClawOps."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from hermes_cli import kanban_db as kb
from plugins.openclaw_bridge.tools import (
    delegate_zero_effect_async_to_openclaw,
)
from proactive.execution_backends import (
    ExecutionRequirements,
    route_execution_backend,
)
from proactive.loop_contract import contract_fingerprint, validate_loop_contract


ZERO_EFFECT_AGENT = "missioncrew-browser-readonly"
ZERO_EFFECT_WORKSPACE = (
    Path.home()
    / "my_agent_team"
    / "openclaw-workspace"
    / "agents"
    / ZERO_EFFECT_AGENT
)
ZERO_EFFECT_RESULT_TEXT = (
    '{"result":"zero-effect async completed","sideEffectsPerformed":false}'
)


def _ambiguous_transport_result(result: Mapping[str, Any]) -> bool:
    errors = {
        str(error).strip().lower()
        for error in (result.get("errors") or [])
    }
    return (
        result.get("identity_correlated") is not True
        and (
            bool(errors & {"connection_failed", "timeout"})
            or any(error.startswith("http_5") for error in errors)
        )
    )


def _ambiguous_transport_exception(exc: Exception) -> bool:
    return isinstance(exc, (TimeoutError, ConnectionError, OSError))


def _digest(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _terminal_evidence_digest(result: Mapping[str, Any]) -> str:
    """Hash only immutable terminal identity and backend evidence."""
    return _digest(
        {
            "status": result.get("status"),
            "protocol_version": result.get("protocol_version"),
            "delegation_id": result.get("delegation_id"),
            "attempt_id": result.get("attempt_id"),
            "contract_fingerprint": result.get("contract_fingerprint"),
            "backend_run_id": result.get("backend_run_id"),
            "backend_agent_id": result.get("backend_agent_id"),
            "backend_session_key": result.get("backend_session_key"),
            "artifacts": result.get("artifacts"),
            "requires_human_review": result.get("requires_human_review"),
        }
    )


def _identity_matches(
    result: Mapping[str, Any],
    *,
    delegation_id: str,
    attempt_id: str,
    fingerprint: str,
) -> bool:
    return (
        result.get("identity_correlated") is True
        and result.get("delegation_id") == delegation_id
        and result.get("attempt_id") == attempt_id
        and result.get("contract_fingerprint") == fingerprint
    )


def _delegation_args(
    run: kb.Run,
    *,
    openclaw_task_id: str,
    idempotency_key: str,
    objective: str,
    start_idempotency_key: Optional[str] = None,
) -> dict[str, Any]:
    metadata = run.metadata or {}
    args: dict[str, Any] = {
        "task_id": run.task_id,
        "objective": objective,
        "context_refs": [f"kanban:{run.task_id}"],
        "allowed_tools": [],
        "denied_tools": ["*"],
        "risk_level": "low",
        "requires_confirmation": False,
        "max_runtime_seconds": int(run.max_runtime_seconds or 300),
        "output_format": "json",
        "audit_required": True,
        "requested_by": "hermes",
        "protocol_version": "2.0",
        "delegation_id": str(metadata["delegation_id"]),
        "attempt_id": str(metadata["attempt_id"]),
        "contract_fingerprint": str(metadata["contract_fingerprint"]),
        "project": str(metadata["project"]),
        "topic_id": str(metadata["topic_id"]),
        "executor_backend": "openclaw",
        "executor_profile": "zero-effect-async",
        "backend_agent_id": ZERO_EFFECT_AGENT,
        "external_effect_budget": 0,
        "workspace_policy": "dedicated",
        "session_policy": "ephemeral",
        "credential_refs": [],
        "idempotency_key": idempotency_key,
        "openclaw_task_id": openclaw_task_id,
        "dry_run": False,
    }
    if start_idempotency_key:
        args.update(
            {
                "start_idempotency_key": start_idempotency_key,
                "backend_run_id": str(run.backend_run_id or ""),
            }
        )
    return args


def start_zero_effect_async_acceptance(
    *,
    contract: Mapping[str, Any],
    board: Optional[str] = None,
    transport: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None,
    policy_path: Optional[str] = None,
) -> dict[str, Any]:
    """Create, route, and admit one zero-effect OpenClaw async acceptance run."""
    normalized_contract = validate_loop_contract(contract)
    fingerprint = contract_fingerprint(normalized_contract)
    identity = normalized_contract["identity"]
    replaying_admission = False
    run: kb.Run | None = None
    circuit_generation: int | None = None
    task_idempotency_key = (
        f"openclaw-zero-effect:{identity['project']}:"
        f"{identity['request_instance_id']}:{fingerprint}"
    )
    with kb.connect_closing(board=board) as conn:
        existing_row = conn.execute(
            """
            SELECT id
              FROM tasks
             WHERE idempotency_key = ?
               AND status != 'archived'
             ORDER BY created_at DESC
             LIMIT 1
            """,
            (task_idempotency_key,),
        ).fetchone()
        if existing_row is not None:
            existing_task_id = str(existing_row["id"])
            existing = kb.get_task(conn, existing_task_id)
            existing_run = kb.latest_run(conn, existing_task_id)
            review_row = conn.execute(
                """
                SELECT id
                  FROM tasks
                 WHERE idempotency_key = ?
                   AND status != 'archived'
                 ORDER BY created_at DESC
                 LIMIT 1
                """,
                (f"{existing_task_id}:zero-effect-review",),
            ).fetchone()
            existing_review_task_id = (
                str(review_row["id"]) if review_row is not None else ""
            )
            metadata = existing_run.metadata if existing_run else {}
            if (
                existing is not None
                and existing.status == "running"
                and existing_run is not None
                and existing_run.backend_status
                in {"succeeded", "failed", "blocked"}
                and isinstance(metadata, Mapping)
                and isinstance(
                    metadata.get("backend_terminal_observation"),
                    Mapping,
                )
            ):
                observation = dict(
                    metadata["backend_terminal_observation"]
                )
                observation["result_digest"] = existing_run.result_digest
                observation["circuit_generation"] = metadata.get(
                    "circuit_generation"
                )
                handled = make_zero_effect_async_terminal_handler(
                    board=board
                )(existing_run, observation)
                return {
                    "execution_task_id": existing_task_id,
                    "review_task_id": existing_review_task_id,
                    "run_id": existing_run.id,
                    "status": (
                        "succeeded"
                        if handled.get("accepted") is True
                        else "blocked"
                    ),
                    "backend_run_id": existing_run.backend_run_id,
                    "routing_decision": existing_run.routing_decision,
                    "deduplicated": True,
                }
            can_replay_admission = (
                existing is not None
                and existing.status == "running"
                and existing_run is not None
                and (
                    existing_run.backend_status is None
                    or (
                        existing_run.backend_status == "queued"
                        and not existing_run.backend_run_id
                        and metadata.get("admission_ambiguous") is True
                    )
                )
                and isinstance(metadata, Mapping)
                and isinstance(metadata.get("circuit_generation"), int)
                and not isinstance(metadata.get("circuit_generation"), bool)
                and all(
                    str(metadata.get(key) or "").strip()
                    for key in (
                        "delegation_id",
                        "attempt_id",
                        "contract_fingerprint",
                        "start_idempotency_key",
                    )
                )
            )
            if existing is not None and existing.status not in {"ready", "todo"}:
                if can_replay_admission:
                    run = existing_run
                    run_id = existing_run.id
                    delegation_id = str(metadata["delegation_id"])
                    attempt_id = str(metadata["attempt_id"])
                    start_idempotency_key = str(metadata["start_idempotency_key"])
                    circuit_generation = int(metadata["circuit_generation"])
                    replaying_admission = True
                else:
                    return {
                        "execution_task_id": existing_task_id,
                        "review_task_id": existing_review_task_id,
                        "run_id": existing_run.id if existing_run else None,
                        "status": (
                            existing_run.backend_status
                            if (
                                existing_run is not None
                                and existing_run.backend_status
                                in {
                                    "queued",
                                    "running",
                                    "succeeded",
                                    "failed",
                                    "blocked",
                                }
                            )
                            else (
                                "succeeded"
                                if existing.status == "done"
                                else "blocked"
                            )
                        ),
                        "backend_run_id": (
                            existing_run.backend_run_id if existing_run else None
                        ),
                        "routing_decision": (
                            existing_run.routing_decision
                            if existing_run and existing_run.routing_decision
                            else None
                        ),
                        "deduplicated": True,
                    }
        circuit_states = (
            {}
            if replaying_admission
            else kb.backend_circuit_states(conn)
        )
        if (
            not replaying_admission
            and circuit_states.get("openclaw") == "half_open"
        ):
            if kb.claim_backend_circuit_probe(
                conn,
                "openclaw",
                lease_seconds=(
                    int(
                        normalized_contract["stop_rules"][
                            "max_runtime_seconds"
                        ]
                    )
                    + 30
                ),
            ):
                circuit_states["openclaw"] = "closed"
        routing_decision = route_execution_backend(
            ExecutionRequirements.build(
                capabilities=("isolated_session", "long_running"),
                semantic_class="isolated_long_running",
                risk_level="low",
                credential_policy="agent_scoped",
                workspace_policy="dedicated",
                session_policy="ephemeral",
                max_runtime_seconds=int(
                    normalized_contract["stop_rules"]["max_runtime_seconds"]
                ),
                preferred_backend="openclaw",
            ),
            circuit_states=circuit_states,
        )
        if routing_decision["selected_backend"] != "openclaw":
            raise RuntimeError(routing_decision["selection_reason"])
        with kb.write_txn(conn):
            task_id = kb.create_task(
                conn,
                title="OpenClaw zero-effect asynchronous acceptance",
                body=json.dumps(normalized_contract, ensure_ascii=False, indent=2),
                assignee="clawops-ops",
                created_by="grace",
                workspace_kind="dir",
                workspace_path=str(ZERO_EFFECT_WORKSPACE),
                max_runtime_seconds=int(
                    normalized_contract["stop_rules"]["max_runtime_seconds"]
                ),
                idempotency_key=task_idempotency_key,
                executor_backend="openclaw",
                executor_profile="zero-effect-async",
                project_namespace=str(identity["project"]),
                routing_decision=routing_decision,
            )
            review_task_id = kb.create_task(
                conn,
                title="Grace review: OpenClaw zero-effect asynchronous acceptance",
                body=(
                    "Verify exact backend identity, terminal transcript, zero tools, "
                    "zero external effects, and ephemeral-session cleanup."
                ),
                assignee="clawops-review",
                created_by="grace",
                parents=[task_id],
                idempotency_key=f"{task_id}:zero-effect-review",
                executor_backend="hermes",
                executor_profile="grace-policy-review",
                project_namespace=str(identity["project"]),
            )
            existing = kb.get_task(conn, task_id)
            if existing is not None and existing.status not in {"ready", "todo"}:
                existing_run = kb.latest_run(conn, task_id)
                metadata = existing_run.metadata if existing_run else {}
                can_replay_admission = (
                    existing.status == "running"
                    and existing_run is not None
                    and existing_run.backend_status is None
                    and isinstance(metadata, Mapping)
                    and isinstance(metadata.get("circuit_generation"), int)
                    and not isinstance(
                        metadata.get("circuit_generation"), bool
                    )
                    and all(
                        str(metadata.get(key) or "").strip()
                        for key in (
                            "delegation_id",
                            "attempt_id",
                            "contract_fingerprint",
                            "start_idempotency_key",
                        )
                    )
                )
                if can_replay_admission:
                    run = existing_run
                    run_id = existing_run.id
                    delegation_id = str(metadata["delegation_id"])
                    attempt_id = str(metadata["attempt_id"])
                    start_idempotency_key = str(metadata["start_idempotency_key"])
                    circuit_generation = int(metadata["circuit_generation"])
                    replaying_admission = True
                else:
                    return {
                        "execution_task_id": task_id,
                        "review_task_id": review_task_id,
                        "run_id": existing_run.id if existing_run else None,
                        "status": (
                            existing_run.backend_status
                            if (
                                existing_run is not None
                                and existing_run.backend_status
                                in {
                                    "queued",
                                    "running",
                                    "succeeded",
                                    "failed",
                                    "blocked",
                                }
                            )
                            else (
                                "succeeded"
                                if existing.status == "done"
                                else "blocked"
                            )
                        ),
                        "backend_run_id": (
                            existing_run.backend_run_id if existing_run else None
                        ),
                        "routing_decision": (
                            existing_run.routing_decision
                            if existing_run and existing_run.routing_decision
                            else routing_decision
                        ),
                        "deduplicated": True,
                    }
            if run is None:
                claimed = kb.claim_task(
                    conn, task_id, claimer="clawops-openclaw-router"
                )
                if claimed is None or claimed.current_run_id is None:
                    raise RuntimeError("Zero-effect OpenClaw task could not be claimed.")
                run_id = int(claimed.current_run_id)
                delegation_id = f"grace:{task_id}"
                attempt_id = f"{task_id}:run:{run_id}"
                start_idempotency_key = f"{attempt_id}:async-start"
                circuit_generation = int(
                    kb.backend_circuit_snapshot(
                        conn,
                        "openclaw",
                    )["generation"]
                )
                if not kb.merge_active_run_metadata(
                    conn,
                    task_id,
                    expected_run_id=run_id,
                    metadata={
                        "delegation_id": delegation_id,
                        "attempt_id": attempt_id,
                        "contract_fingerprint": fingerprint,
                        "project": str(identity["project"]),
                        "topic_id": str(
                            identity.get("thread_id") or identity["topic_name"]
                        ),
                        "executor_profile": "zero-effect-async",
                        "start_idempotency_key": start_idempotency_key,
                        "review_task_id": review_task_id,
                        "circuit_generation": circuit_generation,
                        "external_effect_budget": 0,
                        "max_poll_iterations": int(
                            normalized_contract["stop_rules"]["max_iterations"]
                        ),
                        "no_progress_error_limit": (
                            2
                            if normalized_contract["stop_rules"]["no_progress"]
                            else None
                        ),
                    },
                ):
                    raise RuntimeError(
                        "Zero-effect OpenClaw run correlation could not be saved."
                    )
                run = kb.get_run(conn, run_id)
            if run is None:
                raise RuntimeError(
                    "Zero-effect OpenClaw run disappeared before admission."
                )
            if circuit_generation is None:
                raise RuntimeError(
                    "Zero-effect OpenClaw admission lacks a circuit generation."
                )

    try:
        result = delegate_zero_effect_async_to_openclaw(
            _delegation_args(
                run,
                openclaw_task_id="openclaw.agent.zero_effect_async_start",
                idempotency_key=start_idempotency_key,
                objective="Start the fixed zero-effect asynchronous acceptance task.",
            ),
            transport=transport,
            policy_path=policy_path,
        )
    except Exception as exc:
        with kb.connect_closing(board=board) as conn:
            with kb.write_txn(conn):
                kb.record_backend_circuit_outcome(
                    conn,
                    "openclaw",
                    succeeded=False,
                    error=f"OpenClaw async admission raised: {exc}",
                    expected_generation=circuit_generation,
                )
                if not _ambiguous_transport_exception(exc):
                    kb.block_task(
                        conn,
                        task_id,
                        reason=f"OpenClaw async admission raised: {exc}",
                        kind="capability",
                        expected_run_id=run_id,
                    )
                else:
                    kb.merge_active_run_metadata(
                        conn,
                        task_id,
                        expected_run_id=run_id,
                        metadata={"admission_ambiguous": True},
                    )
                    kb.record_backend_lifecycle(
                        conn,
                        task_id,
                        expected_run_id=run_id,
                        status="queued",
                        protocol_version="2.0",
                        next_poll_seconds=2,
                    )
                    kb.renew_external_backend_claim(
                        conn,
                        task_id,
                        expected_run_id=run_id,
                    )
        return {
            "execution_task_id": task_id,
            "review_task_id": review_task_id,
            "status": (
                "retrying"
                if _ambiguous_transport_exception(exc)
                else "blocked"
            ),
            "deduplicated": replaying_admission,
            "review_errors": [f"OpenClaw async admission raised: {exc}"],
        }
    status = str(result.get("status") or "").strip().lower()
    identity_ok = _identity_matches(
        result,
        delegation_id=delegation_id,
        attempt_id=attempt_id,
        fingerprint=fingerprint,
    )
    backend_run_id = str(result.get("backend_run_id") or "").strip()
    backend_agent_id = str(result.get("backend_agent_id") or "").strip()
    backend_session_key = str(result.get("backend_session_key") or "").strip()
    protocol_version = str(result.get("protocol_version") or "").strip()
    with kb.connect_closing(board=board) as conn:
        if _ambiguous_transport_result(result):
            with kb.write_txn(conn):
                kb.record_backend_circuit_outcome(
                    conn,
                    "openclaw",
                    succeeded=False,
                    error=(
                        "OpenClaw async admission response was ambiguous; "
                        "retrying the same idempotency key."
                    ),
                    expected_generation=circuit_generation,
                )
                kb.merge_active_run_metadata(
                    conn,
                    task_id,
                    expected_run_id=run_id,
                    metadata={"admission_ambiguous": True},
                )
                kb.record_backend_lifecycle(
                    conn,
                    task_id,
                    expected_run_id=run_id,
                    status="queued",
                    protocol_version="2.0",
                    next_poll_seconds=2,
                )
                kb.renew_external_backend_claim(
                    conn,
                    task_id,
                    expected_run_id=run_id,
                )
            return {
                "execution_task_id": task_id,
                "review_task_id": review_task_id,
                "run_id": run_id,
                "status": "retrying",
                "routing_decision": routing_decision,
                "delegated_result": result,
                "deduplicated": replaying_admission,
            }
        admission_output = next(
            (
                artifact.get("value")
                for artifact in result.get("artifacts") or []
                if (
                    isinstance(artifact, Mapping)
                    and artifact.get("type") == "openclaw_result"
                    and isinstance(artifact.get("value"), Mapping)
                )
            ),
            None,
        )
        admission_evidence = (
            admission_output.get("evidence")
            if isinstance(admission_output, Mapping)
            else None
        )
        admission_pending = (
            status in {"queued", "running"}
            and identity_ok
            and not backend_run_id
            and protocol_version == "2.0"
            and result.get("protocol_correlated") is True
            and isinstance(admission_evidence, Mapping)
            and admission_evidence.get("admissionPending") is True
            and admission_evidence.get("terminal") is False
        )
        if admission_pending:
            claim_renewed = False
            with kb.write_txn(conn):
                persisted = kb.merge_active_run_metadata(
                    conn,
                    task_id,
                    expected_run_id=run_id,
                    metadata={"admission_ambiguous": True},
                ) and kb.record_backend_lifecycle(
                    conn,
                    task_id,
                    expected_run_id=run_id,
                    status="queued",
                    protocol_version="2.0",
                    next_poll_seconds=2,
                )
                if persisted:
                    claim_renewed = kb.renew_external_backend_claim(
                        conn,
                        task_id,
                        expected_run_id=run_id,
                    )
            if not persisted:
                kb.block_task(
                    conn,
                    task_id,
                    reason=(
                        "OpenClaw pending admission could not be durably "
                        "scheduled for reconciliation."
                    ),
                    kind="capability",
                    expected_run_id=run_id,
                )
                return {
                    "execution_task_id": task_id,
                    "review_task_id": review_task_id,
                    "status": "blocked",
                    "delegated_result": result,
                }
            return {
                "execution_task_id": task_id,
                "review_task_id": review_task_id,
                "run_id": run_id,
                "status": "retrying",
                "routing_decision": routing_decision,
                "delegated_result": result,
                "deduplicated": replaying_admission,
                "claim_renewed": claim_renewed,
            }
        if (
            status
            not in {"queued", "running", "succeeded", "failed", "blocked"}
            or not identity_ok
            or not backend_run_id
            or backend_agent_id != ZERO_EFFECT_AGENT
            or not backend_session_key
            or protocol_version != "2.0"
            or result.get("protocol_correlated") is not True
            or not kb.merge_active_run_metadata(
                conn,
                task_id,
                expected_run_id=run_id,
                metadata={"backend_session_key": backend_session_key},
            )
            or not kb.record_backend_lifecycle(
                conn,
                task_id,
                expected_run_id=run_id,
                status=status,
                backend_run_id=backend_run_id,
                backend_agent_id=backend_agent_id,
                protocol_version=protocol_version,
                workspace_ref=str(ZERO_EFFECT_WORKSPACE),
                result_digest=(
                    _terminal_evidence_digest(result)
                    if status in {"succeeded", "failed", "blocked"}
                    else _digest(result)
                ),
                next_poll_seconds=(
                    0 if status in {"queued", "running"} else None
                ),
                terminal_observation=(
                    {
                        "status": status,
                        "delegated_result": result,
                    }
                    if status in {"succeeded", "failed", "blocked"}
                    else None
                ),
                terminal_handler_pending=(
                    status in {"succeeded", "failed", "blocked"}
                ),
            )
        ):
            kb.record_backend_circuit_outcome(
                conn,
                "openclaw",
                succeeded=False,
                error=(
                    "OpenClaw async admission did not return exact correlated "
                    "active evidence."
                ),
                expected_generation=circuit_generation,
            )
            kb.block_task(
                conn,
                task_id,
                reason="OpenClaw async admission did not return exact correlated active evidence.",
                kind="capability",
                expected_run_id=run_id,
            )
            return {
                "execution_task_id": task_id,
                "review_task_id": review_task_id,
                "status": "blocked",
                "delegated_result": result,
            }
        if status in {"queued", "running"} and not kb.renew_external_backend_claim(
            conn,
            task_id,
            expected_run_id=run_id,
        ):
            kb.block_task(
                conn,
                task_id,
                reason="OpenClaw async run could not renew its Kanban claim.",
                kind="capability",
                expected_run_id=run_id,
            )
            return {
                "execution_task_id": task_id,
                "review_task_id": review_task_id,
                "status": "blocked",
                "delegated_result": result,
            }
        if status in {"queued", "running"}:
            kb.record_backend_circuit_outcome(
                conn,
                "openclaw",
                succeeded=True,
                expected_generation=circuit_generation,
            )
        if status in {"succeeded", "failed", "blocked"}:
            terminal_run = kb.get_run(conn, run_id)
            if terminal_run is None:
                raise RuntimeError(
                    "Immediate terminal async run disappeared before review."
                )
            terminal_handler = make_zero_effect_async_terminal_handler(
                board=board
            )
            accepted = terminal_handler(
                terminal_run,
                {
                    "status": status,
                    "result_digest": _terminal_evidence_digest(result),
                    "delegated_result": result,
                    "circuit_generation": circuit_generation,
                },
            )
            return {
                "execution_task_id": task_id,
                "review_task_id": review_task_id,
                "run_id": run_id,
                "status": (
                    "succeeded"
                    if accepted.get("accepted") is True
                    else "blocked"
                ),
                "backend_run_id": backend_run_id,
                "routing_decision": routing_decision,
                "delegated_result": result,
                "deduplicated": replaying_admission,
            }
    return {
        "execution_task_id": task_id,
        "review_task_id": review_task_id,
        "run_id": run_id,
        "status": status,
        "backend_run_id": backend_run_id,
        "routing_decision": routing_decision,
        "delegated_result": result,
        "deduplicated": replaying_admission,
    }


def make_zero_effect_async_poll_adapter(
    *,
    transport: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None,
    policy_path: Optional[str] = None,
) -> Callable[[kb.Run], Mapping[str, Any]]:
    """Build a restart-safe poll adapter from correlation saved on the run."""

    def poll(run: kb.Run) -> Mapping[str, Any]:
        metadata = run.metadata or {}
        stop_cleanup_pending = bool(
            metadata.get("stop_rule_cleanup_pending")
        )
        start_key = str(metadata.get("start_idempotency_key") or "").strip()
        if not start_key:
            raise ValueError("Async run is missing durable start correlation.")
        if not run.backend_run_id and metadata.get("admission_ambiguous") is True:
            result = delegate_zero_effect_async_to_openclaw(
                _delegation_args(
                    run,
                    openclaw_task_id=(
                        "openclaw.agent.zero_effect_async_start"
                    ),
                    idempotency_key=start_key,
                    objective=(
                        "Reconcile the ambiguous zero-effect asynchronous "
                        "OpenClaw admission."
                    ),
                ),
                transport=transport,
                policy_path=policy_path,
            )
            if _ambiguous_transport_result(result):
                raise TimeoutError(
                    "OpenClaw async admission remains ambiguous."
                )
            status = str(result.get("status") or "").strip().lower()
            if status not in {
                "queued",
                "running",
                "succeeded",
                "failed",
                "blocked",
            }:
                raise ValueError(
                    f"Unexpected OpenClaw admission status={status!r}."
                )
            if not _identity_matches(
                result,
                delegation_id=str(metadata.get("delegation_id") or ""),
                attempt_id=str(metadata.get("attempt_id") or ""),
                fingerprint=str(
                    metadata.get("contract_fingerprint") or ""
                ),
            ):
                raise ValueError(
                    "OpenClaw admission replay identity did not match."
                )
            backend_run_id = str(
                result.get("backend_run_id") or ""
            ).strip()
            backend_agent_id = str(
                result.get("backend_agent_id") or ""
            ).strip()
            backend_session_key = str(
                result.get("backend_session_key") or ""
            ).strip()
            admission_output = next(
                (
                    artifact.get("value")
                    for artifact in result.get("artifacts") or []
                    if (
                        isinstance(artifact, Mapping)
                        and artifact.get("type") == "openclaw_result"
                        and isinstance(artifact.get("value"), Mapping)
                    )
                ),
                None,
            )
            admission_evidence = (
                admission_output.get("evidence")
                if isinstance(admission_output, Mapping)
                else None
            )
            if (
                status in {"queued", "running"}
                and not backend_run_id
                and result.get("protocol_version") == "2.0"
                and result.get("protocol_correlated") is True
                and isinstance(admission_evidence, Mapping)
                and admission_evidence.get("admissionPending") is True
                and admission_evidence.get("terminal") is False
            ):
                return {
                    "status": "queued",
                    "protocol_version": "2.0",
                    "result_digest": _digest(result),
                    "delegated_result": result,
                }
            if (
                status in {"failed", "blocked"}
                and not backend_run_id
                and result.get("protocol_version") == "2.0"
                and result.get("protocol_correlated") is True
            ):
                return {
                    "status": status,
                    "protocol_version": "2.0",
                    "result_digest": _terminal_evidence_digest(result),
                    "delegated_result": result,
                }
            if (
                result.get("protocol_correlated") is not True
                or not backend_run_id
                or backend_agent_id != ZERO_EFFECT_AGENT
                or not backend_session_key
            ):
                raise ValueError(
                    "OpenClaw admission replay returned unauthorized or "
                    "incomplete backend evidence."
                )
            return {
                "status": status,
                "backend_run_id": backend_run_id,
                "backend_agent_id": backend_agent_id,
                "backend_session_key": backend_session_key,
                "protocol_version": result.get("protocol_version"),
                "result_digest": (
                    _terminal_evidence_digest(result)
                    if status in {"succeeded", "failed", "blocked"}
                    else _digest(result)
                ),
                "delegated_result": result,
            }
        if not run.backend_run_id:
            raise ValueError("Async run is missing durable backend run identity.")
        poll_key = f"{start_key}:poll:{run.backend_poll_count + 1}"
        result = delegate_zero_effect_async_to_openclaw(
            _delegation_args(
                run,
                openclaw_task_id=(
                    "openclaw.agent.zero_effect_async_cancel"
                    if stop_cleanup_pending
                    else "openclaw.agent.zero_effect_async_poll"
                ),
                idempotency_key=poll_key,
                objective=(
                    "Cancel and clean up the exact zero-effect asynchronous "
                    "OpenClaw run."
                    if stop_cleanup_pending
                    else (
                        "Poll the exact zero-effect asynchronous OpenClaw run."
                    )
                ),
                start_idempotency_key=start_key,
            ),
            transport=transport,
            policy_path=policy_path,
        )
        status = str(result.get("status") or "").strip().lower()
        if status not in {"queued", "running", "succeeded", "failed", "blocked"}:
            raise ValueError(f"Unexpected OpenClaw async poll status={status!r}.")
        if not _identity_matches(
            result,
            delegation_id=str(metadata.get("delegation_id") or ""),
            attempt_id=str(metadata.get("attempt_id") or ""),
            fingerprint=str(metadata.get("contract_fingerprint") or ""),
        ):
            raise ValueError("OpenClaw async poll identity did not match its Kanban run.")
        returned_backend_run_id = str(result.get("backend_run_id") or "")
        returned_session_key = str(result.get("backend_session_key") or "")
        expected_session_key = str(metadata.get("backend_session_key") or "")
        if (
            returned_backend_run_id
            and returned_backend_run_id != run.backend_run_id
        ) or (
            status in {"queued", "running", "succeeded"}
            and returned_backend_run_id != run.backend_run_id
        ):
            raise ValueError("OpenClaw async poll returned a different backend run.")
        if (
            returned_session_key
            and returned_session_key != expected_session_key
        ) or (
            status in {"queued", "running", "succeeded"}
            and returned_session_key != expected_session_key
        ):
            raise ValueError("OpenClaw async poll returned a different backend session.")
        return {
            "status": status,
            "backend_run_id": run.backend_run_id,
            "backend_agent_id": result.get("backend_agent_id"),
            "protocol_version": result.get("protocol_version"),
            "result_digest": (
                _terminal_evidence_digest(result)
                if status in {"succeeded", "failed", "blocked"}
                else _digest(result)
            ),
            "delegated_result": result,
            "stop_cleanup_pending": stop_cleanup_pending,
        }

    return poll


def make_zero_effect_async_terminal_handler(
    *,
    board: Optional[str] = None,
) -> Callable[[kb.Run, Mapping[str, Any]], Mapping[str, Any]]:
    """Build the evidence gate that closes execution and Grace review tasks."""

    def handle(run: kb.Run, observation: Mapping[str, Any]) -> Mapping[str, Any]:
        circuit_generation_value = observation.get("circuit_generation")
        circuit_generation = (
            int(circuit_generation_value)
            if circuit_generation_value is not None
            else None
        )
        result = observation.get("delegated_result")
        if not isinstance(result, Mapping):
            raise ValueError("Terminal observation is missing delegated result evidence.")
        output = next(
            (
                artifact.get("value")
                for artifact in result.get("artifacts") or []
                if (
                    isinstance(artifact, Mapping)
                    and artifact.get("type") == "openclaw_result"
                    and isinstance(artifact.get("value"), Mapping)
                )
            ),
            None,
        )
        evidence = output.get("evidence") if isinstance(output, Mapping) else None
        valid = (
            observation.get("status") == "succeeded"
            and result.get("errors") in (None, [])
            and result.get("requires_human_review") is False
            and result.get("protocol_correlated") is True
            and result.get("backend_session_key")
            == (run.metadata or {}).get("backend_session_key")
            and isinstance(evidence, Mapping)
            and evidence.get("externalEffectBudget") == 0
            and evidence.get("sideEffectsPerformed") is False
            and evidence.get("toolsAllowed") == []
            and evidence.get("terminal") is True
            and evidence.get("sessionCleaned") is True
            and output.get("resultText") == ZERO_EFFECT_RESULT_TEXT
        )
        metadata = run.metadata or {}
        review_task_id = str(metadata.get("review_task_id") or "")
        with kb.connect_closing(board=board) as conn:
            with kb.write_txn(conn):
                current_run = kb.get_run(conn, run.id)
                if current_run is None:
                    raise RuntimeError(
                        "Async execution run disappeared before terminal review."
                    )
                current_metadata = current_run.metadata or metadata
                if not valid:
                    kb.record_backend_circuit_outcome(
                        conn,
                        "openclaw",
                        succeeded=False,
                        error="Zero-effect async terminal evidence failed review.",
                        expected_generation=circuit_generation,
                    )
                    kb.block_task(
                        conn,
                        run.task_id,
                        reason=(
                            "Zero-effect async terminal evidence failed Grace review."
                        ),
                        kind="capability",
                        expected_run_id=run.id,
                    )
                    return {"accepted": False}
                completed = kb.complete_task(
                    conn,
                    run.task_id,
                    result="OpenClaw zero-effect asynchronous acceptance passed.",
                    summary=(
                        "OpenClaw progressed asynchronously to terminal, returned "
                        "zero-tool/zero-effect evidence, and cleaned its session."
                    ),
                    metadata={
                        **current_metadata,
                        "backend_run_id": run.backend_run_id,
                        "result_digest": observation.get("result_digest"),
                        "side_effects_performed": False,
                        "terminal": True,
                    },
                    expected_run_id=run.id,
                )
                if not completed:
                    raise RuntimeError(
                        "Async execution changed before terminal completion."
                    )
                review = kb.claim_task(
                    conn,
                    review_task_id,
                    claimer="grace-policy-review",
                )
                if review is None or review.current_run_id is None:
                    raise RuntimeError("Grace async review task could not be claimed.")
                if not kb.complete_task(
                    conn,
                    review_task_id,
                    result="accepted",
                    summary=(
                        "Grace accepted exact backend identity, zero tools, zero "
                        "external effects, terminal evidence, and cleanup."
                    ),
                    metadata={
                        "reviewed_execution_task_id": run.task_id,
                        "backend_run_id": run.backend_run_id,
                        "checks": [
                            "backend_identity",
                            "zero_tools",
                            "zero_external_effects",
                            "terminal_transcript",
                            "ephemeral_cleanup",
                        ],
                    },
                    expected_run_id=int(review.current_run_id),
                ):
                    raise RuntimeError("Grace async review changed before completion.")
                kb.record_backend_circuit_outcome(
                    conn,
                    "openclaw",
                    succeeded=True,
                    expected_generation=circuit_generation,
                )
        return {"accepted": True, "review_task_id": review_task_id}

    return handle
