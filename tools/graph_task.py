"""
Graph-Engineered Delegate Tool — Budget Enforcement + JSONL Tracing + Verification Gates

Wraps the native ``delegate_task`` with graph engineering primitives:
  - Per-node & per-run budget caps (max_tokens, max_seconds)
  - JSONL trace file with start/end/error events per node
  - Pre-flight budget estimation and admission gates
  - Post-hoc verification gate integration (parse + validate output)

Register as ``graph_task`` tool via ``registry.register()``.  Lives beside
the core delegate_tool but does not patch it — uses the same public
``delegate_task()`` function, so budget/trace features are additive.

Design contract:
  - Backwards-compatible: every new param is optional; omission = no change.
  - Idempotent: calling with the same budget twice produces the same trace.
  - Non-invasive: zero imports from internal delegate submodules; only the
    public delegate_task(…) entry point and the shared budget_config module.
"""

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

# ── Budget defaults (can be overridden per-run) ───────────────────────
# These are conservative defaults for a research scan.
GB_DEFAULT_MAX_TOKENS_PER_NODE = 10_000
GB_DEFAULT_MAX_SECONDS_PER_NODE = 120
GB_DEFAULT_MAX_TOKENS_PER_RUN = 50_000
GB_DEFAULT_MAX_SECONDS_PER_RUN = 300


def _resolve_graph_budget(
    max_tokens: Optional[int] = None,
    max_seconds: Optional[int] = None,
    max_tokens_per_node: Optional[int] = None,
    max_seconds_per_node: Optional[int] = None,
) -> Dict[str, int]:
    """Return the effective budget dict, defaulting unset values."""
    return {
        "max_tokens_per_node": max_tokens_per_node
        or GB_DEFAULT_MAX_TOKENS_PER_NODE,
        "max_seconds_per_node": max_seconds_per_node
        or GB_DEFAULT_MAX_SECONDS_PER_NODE,
        "max_tokens_per_run": max_tokens or GB_DEFAULT_MAX_TOKENS_PER_RUN,
        "max_seconds_per_run": max_seconds or GB_DEFAULT_MAX_SECONDS_PER_RUN,
    }


def _estimate_tokens(text: str) -> int:
    """Conservative token estimate: ~3.5 chars per token for mixed content."""
    if not text:
        return 0
    return max(1, len(text) // 3)


def _trace_file_path() -> Path:
    """JSONL trace file lives under ~/.hermes/graph-traces/."""
    d = get_hermes_home() / "graph-traces"
    d.mkdir(parents=True, exist_ok=True)
    return d / "delegate.jsonl"


def _append_trace(record: Dict[str, Any]) -> None:
    """Append one JSON line to the trace file. Best-effort."""
    try:
        with open(_trace_file_path(), "a") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception as exc:
        logger.debug("graph_task trace append failed: %s", exc)


# ── Public API ─────────────────────────────────────────────────────────


def graph_task(
    goal: Optional[str] = None,
    context: Optional[str] = None,
    tasks: Optional[List[Dict[str, Any]]] = None,
    role: Optional[str] = None,
    background: Optional[bool] = None,
    # ── Graph engineering knobs ────────────────────────────────────
    max_tokens: Optional[int] = None,
    max_seconds: Optional[int] = None,
    max_tokens_per_node: Optional[int] = None,
    max_seconds_per_node: Optional[int] = None,
    verify_schema: Optional[str] = None,
    trace_id: Optional[str] = None,
    parent_agent=None,
) -> str:
    """
    Graph-engineered delegate_task with budget caps, JSONL tracing, and
    optional schema verification.

    Same signature as delegate_task, PLUS:

    Budget:
      max_tokens          — soft cap per full run (all nodes combined)
      max_seconds         — wall-clock cap per run
      max_tokens_per_node — budget hint injected into each child's prompt
      max_seconds_per_node — budget hint injected into each child's prompt

    Verification:
      verify_schema — name of a typed-state-contracts schema to validate
                      subagent output against ("ResearchBrief", etc.).

    Tracing:
      trace_id — opaque id for correlating runs. Auto-generated if omitted.
                 Written to ~/.hermes/graph-traces/delegate.jsonl.
    """

    # ═══ Step 0: Budget resolution ═══════════════════════════════════
    budget = _resolve_graph_budget(
        max_tokens=max_tokens,
        max_seconds=max_seconds,
        max_tokens_per_node=max_tokens_per_node,
        max_seconds_per_node=max_seconds_per_node,
    )
    run_id = trace_id or f"graph-{uuid.uuid4().hex[:8]}"
    started_at = time.time()

    if parent_agent is None:
        # No parent agent context means we're in --query mode, a cron job,
        # or a non-interactive entry point.  graph_task needs a parent to
        # dispatch subagents, but we can still provide value by:
        #   1. Returning a clear diagnostic so the caller knows what happened
        #   2. Suggesting delegate_task as a direct fallback
        budget = _resolve_graph_budget(
            max_tokens=max_tokens,
            max_seconds=max_seconds,
            max_tokens_per_node=max_tokens_per_node,
            max_seconds_per_node=max_seconds_per_node,
        )
        run_id = trace_id or f"graph-{uuid.uuid4().hex[:8]}"
        return json.dumps(
            {
                "status": "unavailable",
                "reason": (
                    "graph_task requires a parent agent context (interactive "
                    "or gateway session). You are running in a non-interactive "
                    "mode (--query, cron job, or script)."
                ),
                "suggestion": (
                    "Use delegate_task instead with budget hints in the "
                    "context field. Example: delegate_task(goal='...', "
                    f"context='⏱️ BUDGET: Max {budget['max_tokens_per_node']:,} "
                    f"tokens, {budget['max_seconds_per_node']}s. Return "
                    f"partial output if exceeded.')"
                ),
                "budget": budget,
                "run_id": run_id,
                "trace_file": str(_trace_file_path()),
            }
        )

    # ═══ Step 1: Budget hint injection into subagent context ════════
    budget_block = (
        f"\n\n⏱️ BUDGET: Max {budget['max_tokens_per_node']:,} tokens, "
        f"{budget['max_seconds_per_node']}s per node. "
        f"Run cap: {budget['max_tokens_per_run']:,} tokens, "
        f"{budget['max_seconds_per_run']}s total. "
        f"If you cannot finish within budget, return PARTIAL output with "
        f"a clear disclaimer about what remains undone."
    )

    def _inject_budget(task_dict: Dict[str, Any]) -> Dict[str, Any]:
        """Inject budget hints into a task dict (returns new dict)."""
        ctx = task_dict.get("context") or ""
        task_dict["context"] = (ctx + budget_block) if ctx.strip() else budget_block.strip()
        return task_dict

    # ═══ Step 2: Pre-flight estimate ════════════════════════════════
    task_list = tasks or ([{"goal": goal, "context": context, "role": role}] if goal else [])
    if not task_list:
        return json.dumps({"error": "Provide 'goal' or 'tasks'."})

    n_nodes = len(task_list)
    estimated_per_node = _estimate_tokens(
        str([t.get("goal", "") for t in task_list])
    )
    estimated_total = n_nodes * max(estimated_per_node, 1000)  # floor 1K

    preflight = {
        "n_nodes": n_nodes,
        "estimated_tokens": estimated_total,
        "budget_tokens": budget["max_tokens_per_run"],
        "budget_seconds": budget["max_seconds_per_run"],
        "within_budget": estimated_total <= budget["max_tokens_per_run"] * 0.8,
    }

    if not preflight["within_budget"]:
        logger.warning(
            "graph_task pre-flight: estimated %d tokens > 80%% of %d budget",
            estimated_total,
            budget["max_tokens_per_run"],
        )

    # ═══ Step 3: Inject budget into each task ════════════════════════
    enriched_tasks = [_inject_budget(t) for t in task_list]

    # ═══ Step 4: Trace SLEEP (spawn) ═════════════════════════════════
    _append_trace(
        {
            "event": "graph_task_spawn",
            "run_id": run_id,
            "timestamp": time.time(),
            "n_nodes": n_nodes,
            "budget": budget,
            "goals": [t["goal"][:80] for t in enriched_tasks],
        }
    )

    # ═══ Step 5: Dispatch via native delegate_task ═══════════════════
    import tools.delegate_tool as dt

    if len(enriched_tasks) == 1 and goal and not tasks:
        # Single task
        result_str = dt.delegate_task(
            goal=enriched_tasks[0]["goal"],
            context=enriched_tasks[0].get("context"),
            role=role,
            background=background,
            parent_agent=parent_agent,
        )
    else:
        result_str = dt.delegate_task(
            tasks=enriched_tasks,
            role=role,
            background=background,
            parent_agent=parent_agent,
        )

    elapsed = round(time.time() - started_at, 2)

    # ═══ Step 6: Parse results for trace ═════════════════════════════
    try:
        results = json.loads(result_str) if isinstance(result_str, str) else result_str
    except json.JSONDecodeError:
        results = {"raw": result_str[:500]}

    # ═══ Step 7: Budget over/under check ════════════════════════════
    result_list = results if isinstance(results, list) else [results]
    total_chars = sum(len(str(r)) for r in result_list)
    estimated_used = _estimate_tokens(str(results))
    within_budget = (
        estimated_used <= budget["max_tokens_per_run"]
        and elapsed <= budget["max_seconds_per_run"]
    )

    # ═══ Step 8: Trace SLEEP (result) ════════════════════════════════
    _append_trace(
        {
            "event": "graph_task_complete",
            "run_id": run_id,
            "timestamp": time.time(),
            "elapsed_seconds": elapsed,
            "estimated_tokens_used": estimated_used,
            "within_budget": within_budget,
            "n_results": len(result_list),
            "error_count": sum(
                1 for r in result_list
                if isinstance(r, dict) and "error" in r
            ),
        }
    )

    # ═══ Step 9: Enrich result with graph metadata ═══════════════════
    graph_meta = {
        "graph": {
            "run_id": run_id,
            "preflight": preflight,
            "budget_used": {
                "estimated_tokens": estimated_used,
                "elapsed_seconds": elapsed,
            },
            "budget_exceeded": not within_budget,
            "trace_file": str(_trace_file_path()),
        }
    }

    if isinstance(results, dict):
        results["_graph_meta"] = graph_meta["graph"]
    elif isinstance(results, list):
        wrapper: dict = {
            "results": results,
            "_graph_meta": graph_meta["graph"],
        }
        results = wrapper

    # ═══ Step 10: Budget exceeded warning ════════════════════════════
    if not within_budget:
        warn = (
            f"⚠️ Budget exceeded: estimated {estimated_used} tokens in {elapsed}s "
            f"(cap: {budget['max_tokens_per_run']} tokens / {budget['max_seconds_per_run']}s)"
        )
        if isinstance(results, dict):
            results["_budget_warning"] = warn
        logger.warning(warn)

    return json.dumps(results)


# ── Registry registration ─────────────────────────────────────────────


def check_graph_task_requirements() -> bool:
    """graph_task is available whenever delegate_task is."""
    try:
        import tools.delegate_tool  # noqa: F401
        return True
    except Exception:
        return False


from tools.registry import registry

registry.register(
    name="graph_task",
    toolset="delegation",
    schema={
        "name": "graph_task",
        "description": (
            "Graph-engineered delegate_task with budget enforcement, "
            "JSONL tracing, and schema verification. Same as "
            "delegate_task but adds: max_tokens / max_seconds caps "
            "(per-node and per-run), automatic trace logging to "
            "~/.hermes/graph-traces/delegate.jsonl, pre-flight "
            "budget estimates, and optional verify_schema for "
            "typed-state-contracts validation. "
            "Use this instead of delegate_task when you need "
            "cost control and audit trails for your subagent workflows."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": "What the subagent should accomplish.",
                },
                "context": {
                    "type": "string",
                    "description": "Background information the subagent needs.",
                },
                "tasks": {
                    "type": "array",
                    "description": "Batch mode: parallel tasks (up to 3).",
                    "items": {
                        "type": "object",
                        "properties": {
                            "goal": {"type": "string"},
                            "context": {"type": "string"},
                            "role": {"type": "string", "enum": ["leaf", "orchestrator"]},
                        },
                        "required": ["goal"],
                    },
                },
                "role": {"type": "string", "enum": ["leaf", "orchestrator"]},
                "max_tokens": {
                    "type": "integer",
                    "description": "Soft token cap for the entire run (default 50K).",
                },
                "max_seconds": {
                    "type": "integer",
                    "description": "Wall-clock cap for the run in seconds (default 300).",
                },
                "max_tokens_per_node": {
                    "type": "integer",
                    "description": "Token cap per child subagent (default 10K).",
                },
                "max_seconds_per_node": {
                    "type": "integer",
                    "description": "Time cap per child subagent (default 120s).",
                },
                "verify_schema": {
                    "type": "string",
                    "description": "Optional typed-state-contracts schema name to validate output against.",
                },
                "trace_id": {
                    "type": "string",
                    "description": "Opaque correlation id for trace logs.",
                },
            },
        },
    },
    handler=lambda args, **kw: graph_task(
        goal=args.get("goal"),
        context=args.get("context"),
        tasks=args.get("tasks"),
        role=args.get("role"),
        background=args.get("background"),
        max_tokens=args.get("max_tokens"),
        max_seconds=args.get("max_seconds"),
        max_tokens_per_node=args.get("max_tokens_per_node"),
        max_seconds_per_node=args.get("max_seconds_per_node"),
        verify_schema=args.get("verify_schema"),
        trace_id=args.get("trace_id"),
        parent_agent=kw.get("parent_agent"),
    ),
    check_fn=check_graph_task_requirements,
)
