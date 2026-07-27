"""Bounded fresh-worker handoff for dispatcher-spawned Kanban tasks.

The control-plane chat stays small by delegating implementation work to Kanban
workers.  This module keeps those workers small too: when an enabled worker
reaches its soft iteration limit, the conversation loop stops before the hard
budget, writes a bounded checkpoint, and continues through a fresh child task.

The feature is deliberately disabled by default and is a no-op outside a
dispatcher worker (``HERMES_KANBAN_TASK`` is absent).
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
import re
import threading
import time
from typing import Any, Callable, Mapping, Optional


AUTO_HANDOFF_EXIT_REASON = "kanban_auto_handoff_requested"
POLICY_SNAPSHOT_ENV = "HERMES_KANBAN_SHORT_TASK_HANDOFF_POLICY"
POLICY_HOME_ENV = "HERMES_KANBAN_SHORT_TASK_POLICY_HOME"
PENDING_CONTROL_ENV = "HERMES_KANBAN_HANDOFF_CONTROL_PENDING"
_IDEMPOTENCY_PREFIX = "kanban-auto-handoff:"
_TITLE_SUFFIX_RE = re.compile(r"\s*[·-]\s*自动接力\s*\d+\s*$")
_log = logging.getLogger(__name__)


# A control accepted after a task has installed its worker-exit gate must never
# disappear merely because SQLite is temporarily unavailable.  Keep the exact
# receipt in this process and keep one non-daemon retry thread alive until every
# receipt is confirmed.  The CLI exit watchdog consults the same registry, so
# neither normal interpreter shutdown nor its hard-exit backstop can erase the
# only remaining copy while releasing the task's exit gate.
_PENDING_CONTROL_CONDITION = threading.Condition(threading.RLock())
_PENDING_HANDOFF_CONTROLS: dict[str, dict[str, Any]] = {}
_PENDING_CONTROL_START_LOCK = threading.Lock()
_PENDING_CONTROL_WAKE = threading.Event()
_PENDING_CONTROL_SUPERVISOR: threading.Thread | None = None
_HARD_EXIT_COMMITTED = False


@dataclass(frozen=True)
class AutoHandoffPolicy:
    enabled: bool = False
    soft_iteration_limit: int = 36
    max_handoffs: int = 8
    validation_error: str | None = None


def _strict_int(value: Any, *, default: int) -> int:
    """Accept a real integer only; bool/string/float config is invalid."""
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("value must be an integer")
    return value


def _raw_feature_opt_in(config: Mapping[str, Any] | None) -> bool:
    """Return True only for the literal opt-in boolean in config."""
    try:
        raw = (((config or {}).get("kanban") or {}).get("short_task_handoff") or {})
    except Exception:
        return False
    return isinstance(raw, dict) and raw.get("enabled") is True


def resolve_config_policy(
    config: Mapping[str, Any] | None,
    *,
    max_iterations: int,
) -> AutoHandoffPolicy:
    """Validate the feature config independently of worker environment.

    The same resolved object drives both the control-plane prompt and worker
    execution. Invalid opt-in values therefore fail closed everywhere instead
    of producing a prompt/runtime half-enabled state.
    """
    if not _raw_feature_opt_in(config):
        return AutoHandoffPolicy()

    try:
        raw = (((config or {}).get("kanban") or {}).get("short_task_handoff") or {})
        soft_limit = _strict_int(raw.get("soft_iteration_limit"), default=36)
        max_handoffs = _strict_int(raw.get("max_handoffs"), default=8)
        hard_limit = _strict_int(max_iterations, default=90)
    except (AttributeError, TypeError, ValueError):
        return AutoHandoffPolicy(validation_error="settings must be integers")

    if soft_limit < 2:
        return AutoHandoffPolicy(validation_error="soft_iteration_limit must be at least 2")
    if soft_limit >= hard_limit:
        return AutoHandoffPolicy(
            validation_error="soft_iteration_limit must be below agent.max_iterations"
        )
    if max_handoffs < 1:
        return AutoHandoffPolicy(validation_error="max_handoffs must be at least 1")
    return AutoHandoffPolicy(True, soft_limit, max_handoffs)


def configured_feature_enabled(
    config: Mapping[str, Any] | None,
    *,
    max_iterations: int = 90,
) -> bool:
    """Return whether the complete opt-in configuration is valid."""
    return resolve_config_policy(config, max_iterations=max_iterations).enabled


def build_dispatcher_policy_snapshot(
    config: Mapping[str, Any] | None,
    *,
    failure_limit: int | None = None,
) -> dict[str, Any]:
    """Build the one policy snapshot shared by dispatcher and worker.

    The dispatcher owns the board-level policy. It freezes both the soft and
    hard iteration limits into the worker environment so switching to an
    assignee profile cannot silently read a different feature setting.
    """
    hard_limit_error: str | None = None
    try:
        hard_limit = _strict_int(
            ((config or {}).get("agent") or {}).get("max_turns"),
            default=90,
        )
        if hard_limit < 1:
            raise ValueError("agent.max_turns must be at least 1")
    except (AttributeError, TypeError, ValueError):
        # Preserve a valid worker hard-limit value in the snapshot, but never
        # silently turn an explicitly enabled feature on with malformed agent
        # configuration.  A string/bool/float here used to be coerced by int().
        hard_limit = 90
        hard_limit_error = "agent.max_turns must be a positive integer"
    policy = resolve_config_policy(config, max_iterations=hard_limit)
    allowlist_error: str | None = None
    workspace_allowlist_error: str | None = None
    allowed_workspace_roots: list[str] = []
    if _raw_feature_opt_in(config):
        try:
            from agent.kanban_handoff_scope import (
                normalize_short_task_allowed_origins,
                normalize_short_task_allowed_workspace_roots,
            )

            allowed_origins, allowlist_error = (
                normalize_short_task_allowed_origins(config)
            )
            if allowlist_error is None and not allowed_origins:
                allowlist_error = (
                    "short-task handoff requires at least one allowed_origins entry"
                )
            (
                allowed_workspace_roots,
                workspace_allowlist_error,
            ) = normalize_short_task_allowed_workspace_roots(config)
        except Exception:
            allowlist_error = "short-task allowed_origins could not be validated"
            workspace_allowlist_error = (
                "short-task allowed_workspace_roots could not be validated"
            )
    raw_failure_limit = (
        failure_limit
        if failure_limit is not None
        else (((config or {}).get("kanban") or {}).get("failure_limit", 2))
    )
    try:
        if isinstance(raw_failure_limit, bool):
            raise ValueError("boolean is not a failure limit")
        resolved_failure_limit = int(raw_failure_limit)
        if resolved_failure_limit < 1:
            raise ValueError("failure limit must be positive")
    except (TypeError, ValueError):
        # Match the dispatcher: malformed configuration falls back to the
        # historical circuit-breaker default instead of disabling dispatch.
        resolved_failure_limit = 2
    platform_error = (
        "short-task handoff phase 1 requires a POSIX host"
        if _raw_feature_opt_in(config) and os.name == "nt"
        else None
    )
    configured_terminal = (config or {}).get("terminal") or {}
    if not isinstance(configured_terminal, Mapping):
        configured_terminal = {}
    explicit_terminal = os.environ.get("TERMINAL_ENV")
    effective_terminal = str(
        (
            explicit_terminal
            if explicit_terminal is not None
            else configured_terminal.get("backend")
            or (config or {}).get("backend")
            or "local"
        )
    ).strip().lower() or "local"
    terminal_error = (
        "short-task handoff phase 1 requires terminal.backend=local"
        if _raw_feature_opt_in(config) and effective_terminal != "local"
        else None
    )
    validation_error = (
        allowlist_error
        or workspace_allowlist_error
        or platform_error
        or terminal_error
        or hard_limit_error
        or policy.validation_error
    )
    return {
        "schema": 2,
        "enabled": policy.enabled and validation_error is None,
        "soft_iteration_limit": policy.soft_iteration_limit,
        "max_handoffs": policy.max_handoffs,
        "max_iterations": hard_limit,
        "failure_limit": resolved_failure_limit,
        "allowed_workspace_roots": list(allowed_workspace_roots),
        "validation_error": validation_error,
    }


def encode_dispatcher_policy_snapshot(
    config: Mapping[str, Any] | None,
) -> str:
    """Return a compact deterministic environment representation."""
    return json.dumps(
        build_dispatcher_policy_snapshot(config),
        sort_keys=True,
        separators=(",", ":"),
    )


def load_current_dispatcher_policy_snapshot(
    *,
    policy_home: str | None = None,
    failure_limit: int | None = None,
) -> dict[str, Any]:
    """Read one current, effective policy from the dispatcher process home.

    Request/profile ContextVars must not choose this policy: a multiplexed
    gateway, its embedded dispatcher, and every spawned worker all belong to
    the process-level dispatcher home.  The strict config loader applies the
    same defaults, legacy normalization, environment expansion, and managed
    overlay as normal config loading, but never accepts stale cached state when
    the current user or managed file is unreadable.
    """
    try:
        from hermes_constants import (
            get_process_hermes_home,
            reset_hermes_home_override,
            set_hermes_home_override,
        )
        from hermes_cli.config import load_config_current_strict

        resolved_home = (
            str(policy_home).strip()
            if policy_home is not None
            else str(get_process_hermes_home())
        )
        if not resolved_home:
            raise ValueError("dispatcher policy home is required")
        token = set_hermes_home_override(resolved_home)
        try:
            return build_dispatcher_policy_snapshot(
                load_config_current_strict(),
                failure_limit=failure_limit,
            )
        finally:
            reset_hermes_home_override(token)
    except Exception:
        # Keep the shape deterministic so every caller can fail closed without
        # exposing filesystem/parser details to a chat response.
        snapshot = build_dispatcher_policy_snapshot(
            None,
            failure_limit=failure_limit,
        )
        snapshot["enabled"] = False
        snapshot["validation_error"] = (
            "dispatcher policy could not be read from current configuration"
        )
        return snapshot


def worker_failure_limit(*, strict: bool = False) -> int:
    """Return the dispatcher-frozen failure threshold for this worker.

    Newly spawned workers always receive this field.  ``strict=True`` is used
    by Phase-1 managed terminal accounting, where silently substituting a
    different threshold would violate the dispatcher's frozen policy.  Legacy
    workers keep the historical default for upgrade compatibility.
    """
    raw_snapshot = os.environ.get(POLICY_SNAPSHOT_ENV)
    try:
        snapshot = json.loads(raw_snapshot or "")
        if not isinstance(snapshot, dict) or snapshot.get("schema") != 2:
            raise ValueError("unsupported snapshot")
        value = snapshot["failure_limit"]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("invalid failure limit")
        return value
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        if strict:
            raise RuntimeError(
                "dispatcher policy snapshot has no valid failure limit"
            )
        return 2


def _resolve_dispatcher_snapshot(
    raw_snapshot: str,
    *,
    max_iterations: int,
) -> AutoHandoffPolicy:
    """Validate a dispatcher-owned worker policy snapshot fail-closed."""
    try:
        snapshot = json.loads(raw_snapshot)
        if not isinstance(snapshot, dict) or snapshot.get("schema") != 2:
            raise ValueError("unsupported schema")
        snapshot_hard = _strict_int(snapshot["max_iterations"], default=90)
        snapshot_failure_limit = snapshot["failure_limit"]
        if (
            isinstance(snapshot_failure_limit, bool)
            or not isinstance(snapshot_failure_limit, int)
            or snapshot_failure_limit < 1
        ):
            raise ValueError("invalid failure limit")
        worker_hard = _strict_int(max_iterations, default=90)
        if snapshot_hard != worker_hard:
            return AutoHandoffPolicy(
                validation_error=(
                    "dispatcher policy max_iterations does not match the worker limit"
                )
            )
        if snapshot.get("validation_error"):
            return AutoHandoffPolicy(validation_error=str(snapshot["validation_error"]))
        if snapshot.get("enabled") is not True:
            return AutoHandoffPolicy()
        return resolve_config_policy(
            {
                "kanban": {
                    "short_task_handoff": {
                        "enabled": True,
                        "soft_iteration_limit": snapshot["soft_iteration_limit"],
                        "max_handoffs": snapshot["max_handoffs"],
                    }
                }
            },
            max_iterations=max_iterations,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return AutoHandoffPolicy(
            validation_error="dispatcher policy snapshot is invalid"
        )


def resolve_policy(
    config: Mapping[str, Any] | None,
    *,
    max_iterations: int,
    task_id: str | None = None,
) -> AutoHandoffPolicy:
    """Resolve the immutable policy for one worker process.

    Invalid values fail closed.  The soft limit must leave at least one normal
    model call before the hard limit; the final checkpoint itself is made by a
    separate, tool-less request in the turn finalizer.
    """
    resolved_task_id = (
        task_id if task_id is not None else os.environ.get("HERMES_KANBAN_TASK") or ""
    ).strip()
    if not resolved_task_id:
        return AutoHandoffPolicy()

    snapshot = os.environ.get(POLICY_SNAPSHOT_ENV)
    if snapshot is None:
        return AutoHandoffPolicy(
            validation_error="dispatcher policy snapshot is required"
        )
    return _resolve_dispatcher_snapshot(
        snapshot,
        max_iterations=max_iterations,
    )


def load_policy(*, max_iterations: int) -> AutoHandoffPolicy:
    """Compatibility wrapper used by isolated callers and tests.

    Production agents resolve this once during initialization and keep the
    resulting dataclass for their whole lifetime.  A config toggle therefore
    takes effect only for newly started workers, which makes rollback timing
    explicit and keeps the system prompt byte-stable.
    """
    try:
        from hermes_cli.config import load_config

        config = load_config()
    except Exception:
        config = None
    return resolve_policy(config, max_iterations=max_iterations)


def live_dispatcher_policy_enabled() -> bool:
    """Re-read the dispatcher-owned policy immediately before a handoff.

    Workers run under an assignee-specific ``HERMES_HOME``. The dispatcher
    therefore pins its own policy home separately so switching the feature off
    can stop successor creation without waiting for an immutable worker
    snapshot to expire. Missing or unreadable state fails closed.
    """
    policy_home = (os.environ.get(POLICY_HOME_ENV) or "").strip()
    if not policy_home:
        return False
    snapshot = load_current_dispatcher_policy_snapshot(policy_home=policy_home)
    return bool(
        snapshot.get("enabled") is True
        and not snapshot.get("validation_error")
    )


def should_request_handoff(
    *,
    policy: AutoHandoffPolicy,
    api_call_count: int,
    messages: list[dict],
    eligibility_check: Optional[Callable[[], bool]] = None,
) -> bool:
    """True when an enabled worker should checkpoint before another API call."""
    if (
        not isinstance(policy, AutoHandoffPolicy)
        or not policy.enabled
        or api_call_count < policy.soft_iteration_limit
    ):
        return False

    # A worker that already called kanban_complete/kanban_block has reached a
    # durable terminal state.  Never manufacture a continuation after that.
    try:
        from agent.kanban_stop import (
            session_called_kanban_terminal,
            session_called_kanban_waiting_tool,
        )

        if (
            session_called_kanban_terminal(messages)
            or session_called_kanban_waiting_tool(messages)
        ):
            return False
    except Exception:
        return False
    check = eligibility_check or worker_task_is_handoff_eligible
    try:
        return bool(check())
    except Exception:
        return False


def worker_task_is_handoff_eligible() -> bool:
    """Fail-closed preflight before spending a model call on a checkpoint.

    Phase 1 only supports a leaf task in a persistent directory/worktree whose
    active DB run belongs to this process. Scratch/default tasks and tasks with
    existing downstream dependents keep the historical hard-limit behavior;
    they never enter the automatic checkpoint path and therefore cannot fail
    after generating an unusable summary.
    """
    task_id = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    run_text = (os.environ.get("HERMES_KANBAN_RUN_ID") or "").strip()
    if (
        not task_id
        or not run_text.isdigit()
        or os.name == "nt"
        or os.environ.get("HERMES_KANBAN_PROCESS_CLEANUP_UNSAFE")
    ):
        return False
    try:
        from tools.process_registry import process_registry

        if process_registry.has_any_active():
            return False
    except Exception:
        # Unknown process ownership is not sufficient proof for a shared-
        # checkout handoff. Fail closed rather than overlap writers.
        return False
    from hermes_cli import kanban_db as kb

    conn = kb.connect()
    try:
        task = kb.get_task(conn, task_id)
        run = conn.execute(
            "SELECT * FROM task_runs WHERE id = ? AND task_id = ?",
            (int(run_text), task_id),
        ).fetchone()
        identity = kb._capture_handoff_worker_identity(os.getpid())
        durable_identity_matches = bool(
            run is not None
            and identity is not None
            and run["status"] == "running"
            and run["ended_at"] is None
            and bool(run["handoff_safety_required"])
            and str(run["owner_node_id"] or "") == identity["owner_node_id"]
            and str(run["owner_boot_id"] or "") == identity["owner_boot_id"]
            and str(run["worker_start_token"] or "")
            == identity["worker_start_token"]
            and int(run["worker_pgid"] or 0) == identity["worker_pgid"]
            and not run["process_cleanup_unsafe"]
        )
        return bool(
            task is not None
            and task.status == "running"
            and task.current_run_id == int(run_text)
            and task.worker_pid == os.getpid()
            and task.workspace_kind in {"dir", "worktree"}
            and task.workspace_path
            and not task.goal_mode
            and not kb.child_ids(conn, task_id)
            and durable_identity_matches
        )
    finally:
        conn.close()


def _handoff_generation(conn, task_id: str) -> int:
    """Return the next generation from the task's immutable creation proof.

    Dependency links are operator-editable and ancestors may be archived or
    deleted, so the safety counter must never be reconstructed from the live
    graph. Every automatic successor carries its own generation in its
    append-only ``created`` event. Missing/corrupt proof fails closed instead
    of resetting the cap.
    """
    from hermes_cli import kanban_db as kb

    current_task = kb.get_task(conn, task_id)
    if current_task is None:
        raise RuntimeError(f"unknown Kanban task {task_id}")
    if not (current_task.idempotency_key or "").startswith(_IDEMPOTENCY_PREFIX):
        return 1
    created = conn.execute(
        "SELECT payload FROM task_events "
        "WHERE task_id = ? AND kind = 'created' "
        "ORDER BY id ASC LIMIT 1",
        (task_id,),
    ).fetchone()
    try:
        payload = json.loads(created["payload"]) if created and created["payload"] else {}
        generation = payload.get("handoff_generation")
    except (TypeError, ValueError, json.JSONDecodeError):
        generation = None
    if (
        payload.get("auto_handoff") is not True
        or isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 1
    ):
        raise RuntimeError(
            "automatic handoff generation proof is missing or invalid; "
            "the chain is paused instead of resetting its safety limit"
        )
    return generation + 1


def create_successor_and_close(
    *,
    policy: AutoHandoffPolicy,
    summary: str,
    api_call_count: int,
    max_iterations: int,
) -> dict[str, Any]:
    """Create one idempotent successor and close the current worker segment.

    The successor inherits the resolved persistent workspace kind/path (and
    branch for a worktree). This deliberately continues in the same physical
    checkout while still starting a fresh Hermes process/session. Phase 1
    rejects ephemeral scratch workspaces because their deliverable ledger is
    not yet durable across a multi-process chain.
    """
    task_id = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    if not task_id:
        raise RuntimeError("automatic handoff requires HERMES_KANBAN_TASK")

    if not isinstance(policy, AutoHandoffPolicy) or not policy.enabled:
        raise RuntimeError("automatic handoff is not enabled")
    if not live_dispatcher_policy_enabled():
        raise RuntimeError(
            "automatic handoff stopped because the dispatcher policy is now disabled"
        )
    if os.environ.get("HERMES_KANBAN_PROCESS_CLEANUP_UNSAFE"):
        raise RuntimeError(
            "automatic handoff refused because subprocess cleanup is unsafe"
        )

    # Repeat the managed-process proof at the commit boundary. The earlier
    # eligibility check is advisory and a tool could have spawned work between
    # it and summary generation; no successor may start while such work lives.
    try:
        from tools.process_registry import process_registry

        if process_registry.has_any_active():
            raise RuntimeError(
                "automatic handoff refused while a managed background process is active"
            )
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(
            "automatic handoff could not prove background-process quiescence"
        ) from exc

    from hermes_cli import kanban_db as kb

    conn = kb.connect()
    try:
        task = kb.get_task(conn, task_id)
        if task is None:
            raise RuntimeError(f"unknown Kanban task {task_id}")
        run_id_text = (os.environ.get("HERMES_KANBAN_RUN_ID") or "").strip()
        if not run_id_text.isdigit():
            raise RuntimeError("automatic handoff requires HERMES_KANBAN_RUN_ID")
        run_id = int(run_id_text)
        if task.status not in {"running", "done"}:
            raise RuntimeError(
                f"current Kanban task cannot hand off (status={task.status})"
            )
        if not task.workspace_path:
            raise RuntimeError("current Kanban workspace has not been resolved")
        if task.workspace_kind == "scratch":
            raise RuntimeError(
                "automatic handoff does not support ephemeral scratch workspaces in phase 1"
            )

        generation = _handoff_generation(conn, task_id)
        if task.status == "running" and generation > policy.max_handoffs:
            blocked = kb.pause_task_at_handoff_limit(
                conn,
                task_id,
                reason=(
                    f"Automatic short-task handoff reached its safety limit "
                    f"({policy.max_handoffs}). Review the task objective before continuing."
                ),
                expected_run_id=run_id,
                expected_worker_pid=os.getpid(),
            )
            if not blocked:
                raise RuntimeError(
                    "current Kanban task changed before the safety limit could pause it"
                )
            return {
                "status": "safety_limit",
                "task_id": task_id,
                "generation": generation,
            }

        base_title = _TITLE_SUFFIX_RE.sub("", task.title).strip()
        metadata = {
            "auto_handoff": {
                "generation": generation,
                "reason": "soft_iteration_limit",
                "budget_used": api_call_count,
                "budget_max": max_iterations,
            }
        }
        result = kb.handoff_task(
            conn,
            task_id,
            title=f"{base_title} · 自动接力 {generation}",
            idempotency_key=f"{_IDEMPOTENCY_PREFIX}{task_id}",
            summary=summary,
            metadata=metadata,
            expected_run_id=run_id,
            expected_worker_pid=os.getpid(),
        )
        if result.get("status") not in {"handed_off", "already_handed_off"}:
            raise RuntimeError(result.get("error") or "atomic Kanban handoff failed")
        successor_id = result["successor_task_id"]
        return {
            "status": "handed_off",
            "task_id": task_id,
            "successor_task_id": successor_id,
            "generation": generation,
            "idempotent_replay": result.get("status") == "already_handed_off",
        }
    finally:
        conn.close()


def _normalized_pending_handoff_control(
    control: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the immutable semantics used by retries and deduplication."""
    normalized = {
        "control_id": str(control.get("control_id") or "").strip(),
        "source_task_id": str(control.get("source_task_id") or "").strip(),
        "target_task_id": str(
            control.get("target_task_id") or control.get("source_task_id") or ""
        ).strip(),
        "kind": str(control.get("kind") or "").strip(),
        "message": str(control.get("message") or ""),
        "phase": str(control.get("phase") or "before_commit").strip(),
    }
    if not all(
        normalized[key]
        for key in ("control_id", "source_task_id", "target_task_id")
    ):
        raise ValueError("handoff control identity is incomplete")
    if normalized["kind"] not in {"stop", "redirect", "steer"}:
        raise ValueError("handoff control kind is invalid")
    if normalized["phase"] not in {
        "before_commit",
        "after_commit",
        "after_terminal",
        "before_start",
        "superseded",
    }:
        raise ValueError("handoff control phase is invalid")
    if normalized["phase"] == "before_commit":
        try:
            normalized["expected_run_id"] = int(control.get("expected_run_id"))
            normalized["expected_worker_pid"] = int(
                control.get("expected_worker_pid")
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("handoff control has invalid worker identity") from exc
        if (
            normalized["expected_run_id"] < 1
            or normalized["expected_worker_pid"] < 1
        ):
            raise ValueError(
                "handoff control worker identity must be positive"
            )
    if normalized["phase"] == "superseded":
        superseded_by = str(
            control.get("superseded_by_control_id") or ""
        ).strip()
        if not superseded_by or superseded_by == normalized["control_id"]:
            raise ValueError("superseded control requires a distinct winner")
        normalized["superseded_by_control_id"] = superseded_by
    return normalized


def _pending_control_semantics(control: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple(
        control.get(key)
        for key in (
            "control_id",
            "source_task_id",
            "target_task_id",
            "kind",
            "message",
            "phase",
            "expected_run_id",
            "expected_worker_pid",
            "superseded_by_control_id",
        )
    )


def _refresh_pending_control_env_locked() -> None:
    if _PENDING_HANDOFF_CONTROLS:
        os.environ[PENDING_CONTROL_ENV] = "1"
    else:
        os.environ.pop(PENDING_CONTROL_ENV, None)


def try_commit_handoff_control_hard_exit() -> bool:
    """Atomically close admission only when no accepted receipt can be lost."""
    global _HARD_EXIT_COMMITTED
    with _PENDING_CONTROL_CONDITION:
        if _PENDING_HANDOFF_CONTROLS:
            return False
        _HARD_EXIT_COMMITTED = True
        return True


def _resolve_supervised_handoff_control(
    control: Mapping[str, Any],
) -> bool:
    """Remove one pending receipt only when its exact semantics still match."""
    control_id = str(control.get("control_id") or "")
    with _PENDING_CONTROL_CONDITION:
        pending = _PENDING_HANDOFF_CONTROLS.get(control_id)
        if pending is None:
            return False
        if _pending_control_semantics(pending["control"]) != _pending_control_semantics(
            control
        ):
            return False
        del _PENDING_HANDOFF_CONTROLS[control_id]
        _refresh_pending_control_env_locked()
        _PENDING_CONTROL_CONDITION.notify_all()
    _PENDING_CONTROL_WAKE.set()
    return True


def confirm_pending_handoff_control(control: Mapping[str, Any]) -> bool:
    """Release an exact staged receipt after direct durable confirmation."""
    return _resolve_supervised_handoff_control(control)


def _handoff_control_supervisor_loop() -> None:
    """Replay exact receipts forever, with bounded backoff, until confirmed."""
    global _PENDING_CONTROL_SUPERVISOR
    delay = 0.1
    while True:
        with _PENDING_CONTROL_CONDITION:
            if not _PENDING_HANDOFF_CONTROLS:
                _PENDING_CONTROL_SUPERVISOR = None
                _PENDING_CONTROL_CONDITION.notify_all()
                return
            pending = [
                (control_id, dict(entry["control"]))
                for control_id, entry in _PENDING_HANDOFF_CONTROLS.items()
            ]

        made_progress = False
        for control_id, control in pending:
            try:
                outcome = persist_worker_handoff_control(control, attempts=1)
            except Exception as exc:  # validation/import failures stay fail-closed
                outcome = {
                    "status": "failed",
                    "control_id": control_id,
                    "error": str(exc) or exc.__class__.__name__,
                }
            if outcome.get("status") in {"recorded", "already_recorded"}:
                if _resolve_supervised_handoff_control(control):
                    made_progress = True
                    _log.info(
                        "Confirmed deferred handoff control %s; worker exit may proceed",
                        control_id,
                    )
                continue
            failure_log: tuple[int, str] | None = None
            with _PENDING_CONTROL_CONDITION:
                current = _PENDING_HANDOFF_CONTROLS.get(control_id)
                if current is not None and _pending_control_semantics(
                    current["control"]
                ) == _pending_control_semantics(control):
                    current["attempts"] = int(current.get("attempts", 0)) + 1
                    current["error"] = str(
                        outcome.get("error") or "handoff control is not confirmed"
                    )
                    attempts = int(current["attempts"])
                    _PENDING_CONTROL_CONDITION.notify_all()
                    if attempts == 1 or attempts % 10 == 0:
                        failure_log = (attempts, str(current["error"]))
            if failure_log is not None:
                attempts, failure_error = failure_log
                _log.error(
                    "Deferred handoff control %s remains unconfirmed "
                    "(attempt %d); keeping the managed worker alive: %s",
                    control_id,
                    attempts,
                    failure_error,
                )

        delay = 0.1 if made_progress else min(delay * 2.0, 5.0)
        _PENDING_CONTROL_WAKE.wait(delay)
        _PENDING_CONTROL_WAKE.clear()


def stage_pending_handoff_control(
    control: Mapping[str, Any],
    *,
    error: str = "",
) -> dict[str, Any]:
    """Stage one exact receipt and start the single fail-closed supervisor."""
    global _PENDING_CONTROL_SUPERVISOR
    normalized = _normalized_pending_handoff_control(control)
    managed_task_id = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    if not managed_task_id:
        raise RuntimeError("handoff control admission requires a managed worker")
    if normalized["source_task_id"] != managed_task_id:
        raise RuntimeError("handoff control admission source does not own this worker")
    control_id = normalized["control_id"]
    thread_to_start: threading.Thread | None = None
    # Serialize only supervisor publication/start.  The agent control lock,
    # when present, is the outer lock; no DB, logging, join, or sleep occurs while
    # the receipt registry Condition is held.  The ``finally`` is deliberate:
    # a Python signal may raise into this frame immediately after publication,
    # and the exact receipt must still gain an independent replay owner.
    with _PENDING_CONTROL_START_LOCK:
        try:
            with _PENDING_CONTROL_CONDITION:
                if _HARD_EXIT_COMMITTED:
                    raise RuntimeError(
                        "managed worker hard exit is already committed; "
                        "control was not accepted"
                    )
                if (
                    _PENDING_CONTROL_SUPERVISOR is None
                    or (
                        not _PENDING_CONTROL_SUPERVISOR.is_alive()
                        and _PENDING_CONTROL_SUPERVISOR.ident is not None
                    )
                ):
                    _PENDING_CONTROL_SUPERVISOR = threading.Thread(
                        target=_handoff_control_supervisor_loop,
                        daemon=False,
                        name="kanban-handoff-control-supervisor",
                    )
                    thread_to_start = _PENDING_CONTROL_SUPERVISOR
                elif (
                    not _PENDING_CONTROL_SUPERVISOR.is_alive()
                    and _PENDING_CONTROL_SUPERVISOR.ident is None
                ):
                    thread_to_start = _PENDING_CONTROL_SUPERVISOR
                existing = _PENDING_HANDOFF_CONTROLS.get(control_id)
                if existing is not None and _pending_control_semantics(
                    existing["control"]
                ) != _pending_control_semantics(normalized):
                    raise ValueError(
                        "control id was staged with different semantics"
                    )
                if existing is None:
                    _PENDING_HANDOFF_CONTROLS[control_id] = {
                        "control": normalized,
                        "attempts": 0,
                        "error": str(
                            error or "handoff control is not confirmed"
                        ),
                    }
                elif error:
                    existing["error"] = str(error)
                _refresh_pending_control_env_locked()
                _PENDING_CONTROL_CONDITION.notify_all()
        finally:
            if thread_to_start is not None and not thread_to_start.is_alive():
                try:
                    thread_to_start.start()
                except RuntimeError:
                    if not thread_to_start.is_alive():
                        raise
    _PENDING_CONTROL_WAKE.set()
    return {
        **normalized,
        "state": "pending_supervisor_retry",
        "error": str(error or "handoff control is not confirmed"),
    }


def pending_handoff_control_count() -> int:
    """Return the number of exact receipts that still veto worker exit."""
    with _PENDING_CONTROL_CONDITION:
        return len(_PENDING_HANDOFF_CONTROLS)


def pending_handoff_controls_snapshot() -> list[dict[str, Any]]:
    with _PENDING_CONTROL_CONDITION:
        return [
            {
                **dict(entry["control"]),
                "state": "pending_supervisor_retry",
                "attempts": int(entry.get("attempts", 0)),
                "error": str(entry.get("error") or ""),
            }
            for entry in _PENDING_HANDOFF_CONTROLS.values()
        ]


def wait_for_pending_handoff_controls(timeout: float | None = None) -> bool:
    """Wait without polling; return True only when every receipt is confirmed."""
    deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
    with _PENDING_CONTROL_CONDITION:
        while _PENDING_HANDOFF_CONTROLS:
            remaining = (
                None if deadline is None else max(0.0, deadline - time.monotonic())
            )
            if remaining == 0.0:
                return False
            _PENDING_CONTROL_CONDITION.wait(remaining)
        return True


def persist_worker_handoff_control(
    control: Mapping[str, Any],
    *,
    attempts: int = 2,
) -> dict[str, Any]:
    """Durably save one worker control with bounded idempotent replay.

    The same helper is used by soft checkpoints, the hard-budget backstop, and
    their gateway/CLI supervisors. Connection creation, persistence, and close
    all belong to the retry boundary. A close failure after a successful write
    therefore replays the same ``control_id`` *and original phase* to confirm
    ``already_recorded`` instead of creating a semantic conflict.
    """
    from hermes_cli import kanban_db as kb

    normalized = _normalized_pending_handoff_control(control)
    control_id = normalized["control_id"]
    source_task_id = normalized["source_task_id"]
    target_task_id = normalized["target_task_id"]
    kind = normalized["kind"]
    message = normalized["message"]
    phase = normalized["phase"]
    try:
        bounded_attempts = max(1, int(attempts))
    except (TypeError, ValueError) as exc:
        raise ValueError("handoff control attempts must be an integer") from exc
    identity: dict[str, Any] = {}
    if phase == "before_commit":
        identity = {
            "expected_run_id": int(normalized["expected_run_id"]),
            "expected_worker_pid": int(normalized["expected_worker_pid"]),
        }

    last_error = "handoff control could not be persisted"
    last_result: dict[str, Any] | None = None
    for attempt_index in range(bounded_attempts):
        conn = None
        result: dict[str, Any] | None = None
        close_error: Exception | None = None
        try:
            conn = kb.connect()
            if phase == "superseded":
                result = kb.persist_superseded_handoff_control(
                    conn,
                    control_id=control_id,
                    source_task_id=source_task_id,
                    target_task_id=target_task_id,
                    kind=kind,
                    message=message,
                    superseded_by_control_id=normalized[
                        "superseded_by_control_id"
                    ],
                )
            else:
                result = kb.persist_handoff_control(
                    conn,
                    control_id=control_id,
                    source_task_id=source_task_id,
                    target_task_id=target_task_id,
                    kind=kind,
                    message=message,
                    phase=phase,
                    **identity,
                )
            last_result = dict(result)
            if result.get("error"):
                last_error = str(result["error"])
        except Exception as exc:
            last_error = str(exc) or exc.__class__.__name__
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception as exc:
                    close_error = exc
                    last_error = str(exc) or exc.__class__.__name__

        if result is not None and result.get("status") in {
            "recorded",
            "already_recorded",
        }:
            if close_error is not None:
                # A successful SQLite return followed by a close error is not a
                # confirmed receipt. Re-open and replay the *same phase* and id;
                # changing before_commit to after_terminal would conflict with
                # the receipt that may already have committed.
                continue
            confirmed = dict(result)
            confirmed["attempts"] = attempt_index + 1
            return confirmed

    return {
        "status": "failed",
        "control_id": control_id,
        "error": last_error,
        "attempts": bounded_attempts,
        "last_status": (
            last_result.get("status") if last_result is not None else None
        ),
    }


def recover_pending_handoff_control(
    result: dict[str, Any] | None,
    *,
    attempts: int = 2,
) -> dict[str, Any] | None:
    """Let a caller/supervisor replay a finalizer's structured control receipt."""
    if not isinstance(result, dict):
        return None
    control = result.get("pending_handoff_control")
    if not isinstance(control, Mapping):
        return None
    recovery = persist_worker_handoff_control(control, attempts=attempts)
    result["handoff_control_recovery"] = recovery
    if recovery.get("status") in {"recorded", "already_recorded"}:
        result.pop("pending_handoff_control", None)
        _resolve_supervised_handoff_control(control)
    else:
        stage_pending_handoff_control(
            control,
            error=str(recovery.get("error") or "handoff control is not confirmed"),
        )
    return recovery


__all__ = [
    "AUTO_HANDOFF_EXIT_REASON",
    "POLICY_HOME_ENV",
    "PENDING_CONTROL_ENV",
    "POLICY_SNAPSHOT_ENV",
    "AutoHandoffPolicy",
    "build_dispatcher_policy_snapshot",
    "confirm_pending_handoff_control",
    "configured_feature_enabled",
    "create_successor_and_close",
    "encode_dispatcher_policy_snapshot",
    "live_dispatcher_policy_enabled",
    "load_current_dispatcher_policy_snapshot",
    "load_policy",
    "pending_handoff_control_count",
    "pending_handoff_controls_snapshot",
    "persist_worker_handoff_control",
    "resolve_config_policy",
    "resolve_policy",
    "recover_pending_handoff_control",
    "stage_pending_handoff_control",
    "should_request_handoff",
    "wait_for_pending_handoff_controls",
    "try_commit_handoff_control_hard_exit",
    "worker_failure_limit",
    "worker_task_is_handoff_eligible",
]
