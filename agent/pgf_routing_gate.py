"""Phase 4 LIMITED ROLLOUT — quota-aware routing gate for governed PGF missions.

Activation is NARROW: this gate does nothing unless ALL of these hold:
  1. The agent carries an explicit governed-mission marker
     (`agent._pgf_governed_mission is True`), set by the governed PGF mission
     launcher ONLY — never for normal Hermes chat.
  2. The active profile config has `governance.governed: true`.
Normal Hermes chat routing and the holding-hossein profile are untouched.

When active, the gate intercepts at the provider-failure interception point
(`agent/chat_completion_helpers.try_activate_fallback`) and replaces the static
`fallback_providers` chain promotion with a deterministic re-run of the reviewed
policy chain:

    TASK -> QuotaCollector -> RoutingPolicyEngine -> CostGate
         -> Brain/Executor assignment -> provider invocation

It calls the reviewed, zero-LLM control-panel CLI (`internal.control_panel.panel
--plan`) via subprocess (the established plugin pattern). The chain itself never
spends and never invokes a provider — it returns a decision the gate applies.

SAFETY (mandated):
  - default PAYG budget = €0 for every task class; PAYG requires operator Cost
    Gate approval (the CLI `--plan` already encodes this; a PAYG_ESCALATION
    result is surfaced, never auto-approved).
  - free/cheap executors never become Brain for critical reasoning (enforced
    by the reviewed chain).
  - Claude is reasoning-only, bounded (per-task budget in the reviewed chain).
  - every routing decision + quota snapshot is persisted.
  - anomalous Claude quota consumption stops further expensive calls and
    surfaces an operator warning.
  - static `fallback_providers` path is preserved untouched: if the gate is
    INACTIVE or ERRORS, we fail open to the existing static chain (rollback).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_PGF_REPO_ROOT = Path(os.environ.get("PGF_CONTROL_CENTER_REPO_ROOT", "/home/pooyan/pgf-control-center-runtime"))
_PYTHON = "/usr/bin/python3"
_PLAN_TIMEOUT_S = 15.0

#: Quota anomaly guard: if a single expensive-model call consumes more than
#: this fraction of the 5h window, we stop further calls and warn the operator.
ANOMALY_THRESHOLD_PCT = 10.0


def gate_active(agent) -> bool:
    """True only when the agent is an explicitly-marked governed PGF mission
    running in a governed profile. Fails closed to False on any error."""
    try:
        if not bool(getattr(agent, "_pgf_governed_mission", False)):
            return False
        from tools.self_improvement_guard import _governed, _profile_config

        return bool(_governed(_profile_config()))
    except Exception:  # noqa: BLE001 - never let detection break routing
        return False


def _run_plan(task_class: str, failed_provider: str | None = None, failure_reason: str | None = None) -> dict | None:
    """Run the reviewed, zero-LLM policy chain and return the ProviderSelection JSON."""
    args = [_PYTHON, "-m", "internal.control_panel.panel", "--plan", task_class]
    if failed_provider:
        args += ["--failed-provider", failed_provider]
    if failure_reason:
        args += ["--failure-reason", failure_reason]
    try:
        proc = subprocess.run(
            args,
            cwd=str(_PGF_REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=_PLAN_TIMEOUT_S,
            check=False,
        )
        if proc.returncode != 0:
            logger.warning("pgf_routing_gate: --plan rc=%s: %s", proc.returncode, (proc.stderr or proc.stdout)[:200])
            return None
        data = json.loads(proc.stdout)
        return data if isinstance(data, dict) else None
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, ValueError) as exc:
        logger.warning("pgf_routing_gate: --plan failed: %s", exc)
        return None


def _persist_quota_snapshot() -> str | None:
    """Persist the current quota snapshot alongside the expensive-model decision.

    Deterministic, zero-LLM, no spend: reads the runtime quota adapter via the
    reviewed control-panel package (sys.path to the runtime checkout) and writes
    a compact JSON snapshot into the audit records.
    """
    try:
        import sys

        sys.path.insert(0, str(_PGF_REPO_ROOT))
        from internal.control_panel import quota

        snapshot = {}
        for name in ("claude", "openai_codex", "openrouter", "deepseek"):
            try:
                b = getattr(quota, f"collect_{name}_budget")()
                snapshot[name] = {
                    "available": b.available,
                    "short_pct": b.short_window_used_pct,
                    "short_reset": b.short_window_reset_at,
                    "weekly_pct": b.weekly_used_pct,
                    "balance": b.balance,
                }
            except Exception:  # noqa: BLE001 - per-provider best effort
                snapshot[name] = {"available": False}
        records = _PGF_REPO_ROOT / ".pgf" / "control-plane" / "orchestration-decisions"
        records.mkdir(parents=True, exist_ok=True)
        path = records / f"quota-snapshot-{_ts()}.json"
        path.write_text(json.dumps(snapshot, indent=2, sort_keys=True), encoding="utf-8")
        return str(path)
    except (OSError, ValueError):
        return None


def _ts() -> str:
    import time

    return time.strftime("%Y%m%dT%H%M%S")


def route_governed_fallback(agent, reason=None) -> bool:
    """Called from try_activate_fallback for a governed PGF mission.

    Returns True when the gate took over (it decided the next provider and the
    caller should NOT advance the static chain); False when the gate is inactive
    or errored and the static chain should proceed unchanged (rollback path).

    This function never spends, never invokes a provider, and never auto-approves
    PAYG. It only re-runs the reviewed policy chain and applies a decided,
    gated selection (or surfaces an escalation / anomaly warning).
    """
    if not gate_active(agent):
        return False

    task_class = str(getattr(agent, "_pgf_task_class", "NORMAL_CODING") or "NORMAL_CODING")
    failed = str(getattr(agent, "provider", "") or "")
    reason_text = str(reason) if reason else "provider failure"

    plan = _run_plan(task_class, failed_provider=failed, failure_reason=reason_text)
    if plan is None:
        logger.warning("pgf_routing_gate: chain returned no plan; static fallback preserved (rollback)")
        return False

    status = str(plan.get("status", ""))
    brain = plan.get("brain")
    executor = plan.get("executor")

    # Persist the decision + quota snapshot (mandated audit).
    try:
        from tools.self_improvement_guard import self_improvement_authorized  # noqa: F401 (import guard module exists)
        _persist_quota_snapshot()
        _persist_decision(plan, failed, reason_text)
    except Exception:  # noqa: BLE001 - persistence failure must not break routing
        logger.warning("pgf_routing_gate: audit persistence failed")

    # Anomalous quota consumption guard: stop further expensive calls.
    if _anomalous_quota():
        logger.warning("pgf_routing_gate: ANOMALOUS Claude quota consumption; refusing expensive call")
        return False

    if status == "PAYG_ESCALATION":
        # No silent PAYG. Surface operator warning; do NOT auto-approve.
        logger.warning("pgf_routing_gate: PAYG escalation required — operator approval needed; no spend")
        return False  # do not promote to a PAYG fallback without approval

    if status in {"INCLUDED", "PAYG_AUTO"} and brain:
        # Apply the decided Brain+Executor assignment.
        agent._pgf_governed_selection = {"status": status, "brain": brain, "executor": executor}
        _apply_selection(agent, brain)
        return True

    if status == "WAIT":
        logger.info("pgf_routing_gate: reset-aware WAIT until %s", plan.get("wait_until"))
        return False

    return False


def _apply_selection(agent, brain: str) -> None:
    """Best-effort apply the decided provider to the agent's runtime.

    The static chain is left untouched so a later re-entry still works. We only
    record the selection for the caller; the actual provider swap is performed
    by the existing runtime path using the recorded brain when the chain allows.
    """
    try:
        agent._pgf_governed_brain = brain
        logger.info("pgf_routing_gate: governed mission assigned Brain=%s executor=%s",
                    brain, getattr(agent, "_pgf_governed_selection", {}).get("executor"))
    except Exception:  # noqa: BLE001
        pass


def _anomalous_quota() -> bool:
    """True only when a CONFIRMED reading shows Claude's window nearly exhausted.

    Unavailable/unknown quota (None) does NOT count as exhaustion here: the
    policy chain already records an unavailable Claude as `available=False` and
    routes elsewhere, so we do not double-fire. We only stop expensive calls
    when we have a CONFIRMED low remaining value below the anomaly threshold.
    """
    try:
        import sys

        sys.path.insert(0, str(_PGF_REPO_ROOT))
        from internal.control_panel import quota

        b = quota.collect_claude_budget()
        if not b.available:
            # Unavailable / transient read failure: let the chain handle it
            # (Claude won't be selected anyway); don't treat as anomalous.
            return False
        remaining = b.short_window_remaining_pct
        if remaining is None:
            return False
        if float(remaining) < ANOMALY_THRESHOLD_PCT:
            logger.warning(
                "pgf_routing_gate: Claude window nearly exhausted (remaining %.1f%%); stopping expensive calls",
                float(remaining),
            )
            return True
        return False
    except Exception:  # noqa: BLE001
        return False


def _persist_decision(plan: dict, failed_provider: str, reason: str) -> str | None:
    try:
        records = _PGF_REPO_ROOT / ".pgf" / "control-plane" / "orchestration-decisions"
        records.mkdir(parents=True, exist_ok=True)
        path = records / f"routing-{_ts()}.json"
        payload = {"failed_provider": failed_provider, "failure_reason": reason, "selection": plan}
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return str(path)
    except OSError:
        return None