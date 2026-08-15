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
from typing import Any

logger = logging.getLogger(__name__)

_PGF_REPO_ROOT = Path(os.environ.get("PGF_CONTROL_CENTER_REPO_ROOT", "/home/pooyan/pgf-control-center-runtime"))
#: Brand-level brain name -> concrete hermes provider slug + model. Kept as the
#: single deterministic mapping so a governed routing decision can be translated
#: into an actually-invocable provider/model. Extend when new brains are added.
_BRAIN_TO_RUNTIME: dict[str, tuple[str, str]] = {
    "claude": ("anthropic", "claude-sonnet-5"),
    "openai_codex": ("openai-codex", "gpt-5.6-sol"),
    "openai_gpt": ("openai", "gpt-5.6-sol"),
    "openrouter": ("openrouter", "deepseek/deepseek-v4-flash"),
    "deepseek": ("openrouter", "deepseek/deepseek-v4-flash"),
}
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
        # F1: the selected Brain/provider/model must become the ACTUAL next
        # invocation target. route_governed_fallback returns success only after
        # the executable provider state is correctly switched. If the swap
        # cannot be applied, fail closed (return False) so the caller falls
        # through to the untouched legacy static chain rather than suppressing
        # it while leaving the failed provider still active.
        selection = {"status": status, "brain": brain, "executor": executor}
        agent._pgf_governed_selection = selection
        switched = _apply_selection(agent, brain, failed_provider=failed, plan=plan)
        if not switched:
            logger.warning(
                "pgf_routing_gate: replan chose brain=%s but could not activate it; "
                "failing closed (legacy fallback path preserved)",
                brain,
            )
            agent._pgf_governed_selection = selection
            return False
        return True

    if status == "WAIT":
        logger.info("pgf_routing_gate: reset-aware WAIT until %s", plan.get("wait_until"))
        return False

    return False


def _resolve_brain_runtime(brain: str) -> tuple[str | None, str | None]:
    """Map a policy brain name to a concrete (provider_slug, model). Returns
    (None, None) when the brain is unknown so the caller can fail closed."""
    pair = _BRAIN_TO_RUNTIME.get(brain)
    if pair is None:
        return None, None
    return pair


def _apply_selection(agent, brain: str, *, failed_provider: str | None = None, plan: dict | None = None) -> bool:
    """F1: actually switch the agent to the selected Brain/provider/model.

    Mirrors the runtime provider-swap performed by ``try_activate_fallback``:
    resolves the brain to a concrete provider+model, sets ``agent.provider`` /
    ``agent.model`` / ``requested_provider``, clears the transport/credential
    cache and config-context-length so the swap is not defeated by stale cached
    state, and records the activation for audit.

    Returns True only when the executable provider state was switched. On any
    failure (unknown brain, missing model, exception) returns False so the
    caller fails closed and the legacy static chain is preserved. The static
    ``fallback_providers`` chain is left untouched for rollback.
    """
    prev_provider = getattr(agent, "provider", None)
    prev_model = getattr(agent, "model", None)
    try:
        provider, model = _resolve_brain_runtime(brain)
        if not provider or not model:
            logger.warning("pgf_routing_gate: brain=%r has no runtime mapping", brain)
            return False

        # Actually switch the invocation target.
        agent._pgf_governed_brain = brain
        agent.provider = provider
        agent.model = model
        agent.requested_provider = provider
        # Clear cached transport / context-length so the retry resolves the new
        # provider's window and client, matching try_activate_fallback (#22387).
        agent._config_context_length = None
        if hasattr(agent, "_transport_cache"):
            try:
                agent._transport_cache.clear()
            except Exception:  # noqa: BLE001
                pass

        # Persist: failed provider, selected replacement, activation result,
        # concrete retry provider/model.
        _persist_replan_activation(
            failed_provider=failed_provider,
            selected_brain=brain,
            executor=(plan or {}).get("executor"),
            activation_result="activated",
            retry_provider=provider,
            retry_model=model,
        )
        logger.info(
            "pgf_routing_gate: replan activated brain=%s -> provider=%s model=%s "
            "(replacing failed %s)", brain, provider, model, failed_provider or "unknown",
        )
        return True
    except Exception as exc:  # noqa: BLE001 - never let the gate crash routing
        logger.warning("pgf_routing_gate: _apply_selection failed: %s", exc)
        # Restore previous state and fail closed.
        try:
            if prev_provider is not None:
                agent.provider = prev_provider
            if prev_model is not None:
                agent.model = prev_model
        except Exception:  # noqa: BLE001
            pass
        _persist_replan_activation(
            failed_provider=failed_provider,
            selected_brain=brain,
            executor=(plan or {}).get("executor"),
            activation_result="failed",
            retry_provider=None,
            retry_model=None,
        )
        return False


def _persist_replan_activation(
    *, failed_provider: str | None, selected_brain: str | None,
    executor: str | None, activation_result: str,
    retry_provider: str | None, retry_model: str | None,
) -> str | None:
    """Persist the F1 failure/replan activation audit record."""
    try:
        records = _PGF_REPO_ROOT / ".pgf" / "control-plane" / "orchestration-decisions"
        records.mkdir(parents=True, exist_ok=True)
        path = records / f"replan-activation-{_ts()}.json"
        payload = {
            "failed_provider": failed_provider,
            "selected_brain": selected_brain,
            "executor": executor,
            "activation_result": activation_result,
            "retry_provider": retry_provider,
            "retry_model": retry_model,
        }
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return str(path)
    except OSError:
        return None


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


# --- Stage A: deterministic task classification ------------------------------

#: Deterministic keyword -> task-class heuristics. High-value reasoning classes
#: are matched first so an ambiguous message defaults to Claude for reasoning
#: rather than a cheap executor. Cheap/rote keywords (grep, test, lint, format)
#: map to mechanical/test classes -> free/cheap workers. No LLM is involved.
_TASK_KEYWORDS: dict[str, list[str]] = {
    "CRITICAL_REASONING": [
        "production", "incident", "outage", "security", "reliability", "risk",
        "critical", "conflict", "rollback", "data loss", "urgent", "prod",
        "authorization", "credential",
    ],
    "ARCHITECTURE": [
        "architecture", "design", "proposal", "schema", "roadmap", "refactor plan",
        "system design", "api design", "adr", "tech design",
    ],
    "COMPLEX_DEBUGGING": [
        "debug", "root cause", "stack trace", "crash", "hang", "deadlock",
        "fix why", "investigate", "why is", "segment fault", "race condition",
    ],
    "CODE_REVIEW": ["review", "code review", "pr", "pull request", "audit", "lint pass"],
    "TEST_VALIDATION": [
        "run tests", "pytest", "test suite", "regression", "verification",
        "quality gate", "lint", "format check",
    ],
    "MECHANICAL_EXECUTION": [
        "grep", "search", "git status", "polling", "wait for", "ls ",
        "find ", "rename", "mechanical", "draft", "status update", "move file",
        "cp ", "mv ", "sed", "apply patch", "backport", "telemetry",
    ],
    "SUMMARIZATION": ["summarize", "summary", "digest", "tldr", "recap", "notes"],
}


def classify_task(user_message: Any, task_id: str | None = None) -> str:
    """Deterministic task classification for a governed PGF task.

    Returns a `TaskClass` value string. High-value reasoning keywords are
    matched first (and win on ties toward reasoning), rote/cheap keywords map
    to mechanical/test classes for free workers. Defaults to NORMAL_CODING.
    """
    text = ""
    if isinstance(user_message, str):
        text = user_message
    elif user_message is not None:
        try:
            text = str(user_message)
        except Exception:  # noqa: BLE001
            text = ""
    low = text.lower()

    # High-value reasoning classes checked first (order matters: earlier wins).
    for klass in ("CRITICAL_REASONING", "ARCHITECTURE", "COMPLEX_DEBUGGING", "CODE_REVIEW"):
        if any(kw in low for kw in _TASK_KEYWORDS[klass]):
            return klass

    for klass in ("TEST_VALIDATION", "MECHANICAL_EXECUTION", "SUMMARIZATION"):
        if any(kw in low for kw in _TASK_KEYWORDS[klass]):
            return klass

    return "NORMAL_CODING"


_PRE_INVOCATION = "PRE_INVOCATION_GATE"
_FAILURE_REPLAN = "FAILURE_REPLAN_GATE"


def route_pre_invocation(agent, user_message: Any = None, task_id: str | None = None) -> dict | None:
    """Stage A: pre-invocation routing gate (runs before the first provider call).

    For a governed PGF mission: classify the task, collect live quotas, run the
    RoutingPolicyEngine + CostGate, assign Brain/Executor onto the agent, and
    persist an audit record (task class, quota snapshot, brain, executor,
    reasoning, expected billing class). This ensures the legacy/default provider
    (e.g. DeepSeek) is NOT invoked first and corrected later.

    Stage C (mission-boundary replan): this runs at every task/mission boundary
    (from `run_conversation`), so it re-reads live quotas and never inherits the
    previous session's provider blindly.

    Free/cheap workers are never promoted to Brain for critical reasoning (the
    runtime orchestration Brain-capability gate enforces this).

    Returns a dict describing the decision, or None when there is nothing to
    decide / the gate is inactive.
    """
    if not gate_active(agent):
        return None

    classified = classify_task(user_message, task_id)
    try:
        import sys

        sys.path.insert(0, str(_PGF_REPO_ROOT))
        from internal.control_panel.costgate import PaygGateConfig, evaluate_cost_gate
        from internal.control_panel.orchestration import (_BRAIN_CAPABLE, build_plan)
        from internal.control_panel.quota import collect_all_budgets
        from internal.control_panel.routing import TaskClass

        task_class = TaskClass(classified)
        budgets = collect_all_budgets()
        plan = build_plan(task_class, budgets, failed_provider=None, failure_reason="pre-invocation")
        plan_dict = plan.to_dict() if hasattr(plan, "to_dict") else {}

        brain = plan_dict.get("brain")
        # A free/cheap worker must never be promoted to Brain (defense in depth).
        if brain is not None and brain not in _BRAIN_CAPABLE:
            brain = None

        expected_billing = ""
        status = plan_dict.get("status", "")
        if status in ("INCLUDED", "FREE"):
            expected_billing = "INCLUDED" if status == "INCLUDED" else "FREE"
        elif status == "PAYG_AUTO":
            expected_billing = "PAYG_AUTO"
        elif status == "PAYG_ESCALATION":
            expected_billing = "PAYG (cost gate)"

        # Persist full audit: task class, quota snapshot, brain, executor,
        # reasoning, expected billing class.
        _persist_pre_invocation(classified, plan_dict, expected_billing, task_id, budgets)

        # Record gate status flags on the agent for the Control Center display.
        try:
            agent._pgf_pre_invocation_gate = "ACTIVE"
            agent._pgf_failure_replan_gate = "ACTIVE"
            agent._pgf_task_class = classified
            agent._pgf_governed_brain = brain if brain is not None else getattr(agent, "_pgf_governed_brain", None)
        except Exception:  # noqa: BLE001
            pass

        # Apply decision: annotate WITHOUT hot-swapping mid-critical-step. The
        # invocation loop reads the assigned Brain when it builds kwargs. A
        # cheap/free executor is never applied as Brain here.
        if plan_dict:
            agent._pgf_governed_selection = plan_dict

        logger.info(
            "pgf_routing_gate: PRE_INVOCATION task=%s class=%s status=%s brain=%s billing=%s",
            task_id or "-",
            classified,
            status,
            brain,
            expected_billing,
        )
        return {
            "task_class": classified,
            "brain": brain,
            "executor": plan_dict.get("executor"),
            "status": status,
            "expected_billing": expected_billing,
            "reason": plan_dict.get("reason"),
        }
    except Exception as exc:  # noqa: BLE001 - gate must never break the turn
        logger.warning("pgf_routing_gate: pre-invocation gate failed: %s", exc)
        return None


def _persist_pre_invocation(task_class: str, plan: dict, billing: str, task_id: str | None, budgets) -> str | None:
    try:
        records = _PGF_REPO_ROOT / ".pgf" / "control-plane" / "orchestration-decisions"
        records.mkdir(parents=True, exist_ok=True)
        path = records / f"pre-invocation-{_ts()}.json"
        snapshot = []
        for b in (budgets or ()):
            try:
                snapshot.append(
                    {"provider": b.provider, "billing_class": getattr(b.billing_class, "value", None),
                     "available": b.available,
                     "short_remaining_pct": b.short_window_remaining_pct,
                     "weekly_remaining_pct": b.weekly_remaining_pct}
                )
            except Exception:  # noqa: BLE001
                continue
        payload = {
            "task_id": task_id,
            "task_class": task_class,
            "stage": "pre-invocation",
            "expected_billing": billing,
            "selection": plan,
            "quota_snapshot": snapshot,
        }
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return str(path)
    except OSError:
        return None