"""
Graph Task — Cron & Non-Interactive Bridge (v2 — fixed token monitoring)

Makes graph_task work without a parent agent context by spawning a
lightweight standalone AIAgent, hooking it into delegate_task, and
running the full graph pipeline (budget hints + JSONL tracing + post-hoc
token enforcement).

Key fix from v1: BudgetMonitor on the PARENT agent always saw 0 tokens
because the parent only dispatches.  Now we use the parent only for a
TIME watchdog, then enforce the TOKEN budget post-hoc from the actual
child token counts extracted from the delegate_task result.

Also provides ``graph_task_standalone()`` for direct use in cron jobs,
scripts, and --query mode.
"""

import json
import logging
import os
import time
import threading
import uuid
from typing import Any, Dict, List, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)


# ── Time-only watchdog (replaces BudgetMonitor for parent) ──────────


class _TimeWatchdog:
    """Minimal timer: interrupts the parent if elapsed > max_seconds.

    Unlike BudgetMonitor, this does NOT track tokens — it only enforces
    the wall-clock cap.  Token enforcement happens post-hoc from child
    result extraction.
    """

    def __init__(self, parent: Any, max_seconds: int, node_id: str = ""):
        self._parent = parent
        self._max_seconds = max_seconds
        self._node_id = node_id
        self._started_at: float = 0.0
        self._timer: Optional[threading.Timer] = None
        self._fired = False

    def start(self) -> "_TimeWatchdog":
        self._started_at = time.monotonic()
        self._timer = threading.Timer(
            self._max_seconds, self._on_timeout
        )
        self._timer.daemon = True
        self._timer.start()
        return self

    def stop(self) -> Dict[str, Any]:
        if self._timer is not None:
            self._timer.cancel()
        elapsed = round(time.monotonic() - self._started_at, 2)
        return {
            "elapsed_seconds": elapsed,
            "timeout_fired": self._fired,
            "max_seconds": self._max_seconds,
        }

    def _on_timeout(self) -> None:
        self._fired = True
        try:
            if hasattr(self._parent, "interrupt"):
                self._parent.interrupt(
                    f"TimeWatchdog: {self._max_seconds}s cap exceeded "
                    f"(node={self._node_id})"
                )
        except Exception:
            pass


# ── Standalone graph_task ──────────────────────────────────────────


def graph_task_standalone(
    goal: Optional[str] = None,
    context: Optional[str] = None,
    tasks: Optional[List[Dict[str, Any]]] = None,
    role: Optional[str] = None,
    max_tokens: Optional[int] = None,
    max_seconds: Optional[int] = None,
    max_tokens_per_node: Optional[int] = None,
    max_seconds_per_node: Optional[int] = None,
    verify_schema: Optional[str] = None,
    trace_id: Optional[str] = None,
    model_config: Optional[Dict[str, str]] = None,
    toolsets: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Standalone graph_task with post-hoc token enforcement.

    toolsets: explicit toolset list for the spawned child agent (e.g.
    ["x_search", "terminal", "file", "skills"]). When None, the child
    inherits the parent's derived toolsets (default: terminal/file/web —
    which EXCLUDES x_search). Pass this explicitly so the child actually
    gets the search tool the task needs.
    """
    from tools.graph_task import (
        _resolve_graph_budget,
        _estimate_tokens,
        _append_trace,
        _trace_file_path,
    )

    budget = _resolve_graph_budget(
        max_tokens=max_tokens,
        max_seconds=max_seconds,
        max_tokens_per_node=max_tokens_per_node,
        max_seconds_per_node=max_seconds_per_node,
    )
    run_id = trace_id or f"graph-standalone-{uuid.uuid4().hex[:8]}"
    started_at = time.time()

    # Build task list
    task_list = tasks if isinstance(tasks, list) else (
        [{"goal": goal, "context": context or "", "role": role or "leaf"}]
        if goal else []
    )
    if not task_list:
        return {"error": "Provide 'goal' or 'tasks'.", "run_id": run_id}

    # Inject budget block
    budget_block = (
        f"\n\nBUDGET: Max {budget['max_tokens_per_node']:,} tokens, "
        f"{budget['max_seconds_per_node']}s per node. "
        f"Run cap: {budget['max_tokens_per_run']:,} tokens, "
        f"{budget['max_seconds_per_run']}s total. "
        f"STOP immediately if you exceed {budget['max_tokens_per_node']:,} "
        f"tokens or {budget['max_seconds_per_node']}s per node."
    )
    for t in task_list:
        ctx = t.get("context") or ""
        t["context"] = (ctx + budget_block) if ctx.strip() else budget_block.strip()

    n_nodes = len(task_list)

    # Pre-flight: better estimate — floor at 5000 for subagent overhead
    goal_tokens = _estimate_tokens(str([t.get("goal", "") for t in task_list]))
    estimated_tokens = n_nodes * max(goal_tokens, 5000)

    preflight = {
        "n_nodes": n_nodes,
        "estimated_tokens": estimated_tokens,
        "budget_tokens": budget["max_tokens_per_run"],
        "budget_seconds": budget["max_seconds_per_run"],
        "within_budget": estimated_tokens <= budget["max_tokens_per_run"] * 0.8,
    }

    # Trace spawn
    _append_trace({
        "event": "graph_task_spawn",
        "run_id": run_id,
        "timestamp": time.time(),
        "mode": "standalone",
        "n_nodes": n_nodes,
        "budget": budget,
        "goals": [t["goal"][:80] for t in task_list],
    })

    # Build parent agent. enabled_toolsets=None means "all tools enabled"
    # (the default) — only pass it when the caller requested an explicit
    # toolset list so the child inherits exactly those tools.
    try:
        from run_agent import AIAgent
        mc = model_config or {}
        parent_kwargs: Dict[str, Any] = dict(
            model=mc.get("model") or os.environ.get("HERMES_MODEL", "r-reason"),
            provider=mc.get("provider") or os.environ.get("HERMES_PROVIDER", "custom"),
            api_key=mc.get("api_key") or "",
            base_url=mc.get("base_url") or "",
            max_iterations=1,
            quiet_mode=True,
            skip_memory=True,
            platform="cron",
        )
        if toolsets is not None:
            parent_kwargs["enabled_toolsets"] = toolsets
        parent = AIAgent(**parent_kwargs)
    except Exception as exc:
        return {"error": f"Failed to build parent agent: {exc}", "run_id": run_id}

    # Time watchdog (replaces BudgetMonitor — token enforcement is post-hoc)
    watchdog = _TimeWatchdog(parent, budget["max_seconds_per_run"], node_id=run_id)

    # Dispatch via delegate_task
    import tools.delegate_tool as dt

    result_str: str = ""
    try:
        watchdog.start()

        if len(task_list) == 1:
            t = task_list[0]
            result_str = dt.delegate_task(
                goal=t["goal"],
                context=t.get("context"),
                role=t.get("role", "leaf"),
                background=False,
                parent_agent=parent,
            )
        else:
            result_str = dt.delegate_task(
                tasks=task_list,
                role=role or "leaf",
                background=False,
                parent_agent=parent,
            )

        wd_result = watchdog.stop()

    except Exception as exc:
        watchdog.stop()
        return {"error": f"delegate_task failed: {exc}", "run_id": run_id}

    elapsed = round(time.time() - started_at, 2)

    # Parse results
    try:
        results = json.loads(result_str) if isinstance(result_str, str) else result_str
    except json.JSONDecodeError:
        results = {"raw": str(result_str)[:500]}

    # Flatten: delegate_task returns either:
    #   dict: {"results": [child_results], ...}   (single task dispatch)
    #   list: [dict_with_results, ...]             (batch dispatch)
    flat_results: list = []
    if isinstance(results, dict) and "results" in results:
        # Single task: results["results"] is the list of child results
        inner = results.get("results")
        if isinstance(inner, list):
            flat_results.extend(inner)
        else:
            flat_results.append(results)
    elif isinstance(results, list):
        for item in results:
            if isinstance(item, dict) and "results" in item:
                inner = item.get("results")
                if isinstance(inner, list):
                    flat_results.extend(inner)
                else:
                    flat_results.append(item)
            else:
                flat_results.append(item)
    else:
        flat_results = [results]

    # ═══ POST-HOC TOKEN ENFORCEMENT (the fix) ═════════════════════
    # Extract REAL token usage from child results — this is the
    # authoritative number, not the parent's session_total_tokens (0).
    child_tokens = 0
    for r in flat_results:
        if isinstance(r, dict):
            tok = r.get("tokens", {})
            if isinstance(tok, dict):
                child_tokens += int(tok.get("input", 0) or 0)
                child_tokens += int(tok.get("output", 0) or 0)

    tokens_used = child_tokens

    # NOW check budget against REAL token counts
    token_budget_exceeded = (
        max_tokens is not None
        and tokens_used > 0
        and tokens_used > max_tokens
    )
    time_budget_exceeded = wd_result["timeout_fired"]
    budget_exceeded = token_budget_exceeded or time_budget_exceeded

    if token_budget_exceeded:
        exceeded_reason = (
            f"Token budget exceeded: {tokens_used} > {max_tokens} "
            f"(child used {tokens_used} tokens across all API calls)"
        )
    elif time_budget_exceeded:
        exceeded_reason = (
            f"Time budget exceeded: {elapsed}s > {budget['max_seconds_per_run']}s"
        )
    else:
        exceeded_reason = ""

    # Post-hoc enforcement. A hard run-cap breach (tokens > max_tokens)
    # ALWAYS blocks — never accept the partial output — regardless of
    # policy.allow_partial. This stops runaway children from passing
    # 5x-budget results through as "partial" (incident 2026-08-01).
    from tools.graph_enforcer import enforce_budget, resolve_policy
    if token_budget_exceeded:
        enforcement = [
            {
                "action": "block",
                "reason": (
                    f"{exceeded_reason} — hard cap breached; "
                    f"output rejected (not accepted as partial)."
                ),
            }
        ]
    else:
        policy = resolve_policy("research" if verify_schema else "cron")
        # Align the policy's per-node token threshold with the ACTUAL
        # per-node cap passed by the caller (default policy says 25K,
        # which would warn on every healthy run that uses 30-100K).
        if policy.max_tokens is not None and max_tokens_per_node:
            from dataclasses import replace
            policy = replace(policy, max_tokens=max_tokens_per_node)
        enforcement = [
            {"action": e.action, "reason": e.reason}
            for e in enforce_budget(
                policy, run_id, tokens_used, elapsed, result=flat_results,
            )
        ]

    # ═══ Trace complete — with REAL token numbers ═════════════════
    _append_trace({
        "event": "graph_task_complete",
        "run_id": run_id,
        "timestamp": time.time(),
        "mode": "standalone",
        "elapsed_seconds": elapsed,
        "estimated_tokens_used": tokens_used,
        "within_budget": not budget_exceeded,
        "n_results": len(flat_results),
        "error_count": sum(
            1 for r in flat_results
            if isinstance(r, dict) and "error" in r
        ),
    })

    # Assemble output
    return {
        "run_id": run_id,
        "status": "completed" if not budget_exceeded else "budget_exceeded",
        "preflight": preflight,
        "budget": budget,
        "results": flat_results,
        "enforcement": enforcement,
        "_budget": {
            "tokens_used": tokens_used,
            "seconds_elapsed": elapsed,
            "budget_exceeded": budget_exceeded,
            "exceeded_reason": exceeded_reason,
            "token_budget": max_tokens,
            "time_budget": budget["max_seconds_per_run"],
        },
        "trace_file": str(_trace_file_path()),
    }
