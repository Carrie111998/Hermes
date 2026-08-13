#!/usr/bin/env python3
"""
Workflow orchestration layer for delegate_task.

The parent describes a batch workflow as an ordered list of steps. Each
step defines EXACTLY ONE of:

  - {"parallel": [task, ...]}  -- independent items run concurrently under a
                                  semaphore bounded by max_concurrent. A
                                  failing item becomes a structured error
                                  entry; the rest of the batch continues.
  - {"pipeline": [task, ...]}  -- strictly sequential stages; the output of
                                  stage N is appended to the context of the
                                  prompts of stage N+1 (a failed stage feeds
                                  its error text forward so the next stage
                                  can decide to recover or proceed).

Caps (see workflow_max_concurrent / WORKFLOW_MAX_ITEMS):
  - max_concurrent = min(8, delegation.max_concurrent_children)
  - max_items      = 32 total items across ALL steps

Failure semantics: item failure NEVER kills the workflow. Only a
validation failure (malformed schema, missing goal, over-cap) aborts the
whole call before any child is spawned. The result is a flat per-item
array with status in {completed, error}; pipeline/parallel order is
preserved via task_index.

Pattern reference: deepseek-harness packages/workflow/workflow-worker-thread
(runtime.ts agent()/parallel()/pipeline(); FIFO semaphore maxConcurrentAgents,
maxTotalAgents backstop, fatal-vs-item error semantics). Ported as a pattern,
not code.

Design decision (see PR): (a) extend the existing delegate_task batch with a
workflow mode, instead of a new core tool -- the batch already accepts
tasks=[] with thread-pool parallelism, so a workflow is a superset that
reuses the exact child build/run/finalize machinery. v1 runs synchronously:
pipeline stages depend on each other's output, so the whole workflow joins
on itself and returns ONE consolidated result (same contract as a sync
batch).
"""

import json
import logging
import threading
import time
from concurrent.futures import as_completed
from typing import Any, Dict, List, Optional, Tuple

# Reference the delegate machinery through the module (NOT via
# `from tools.delegate_tool import X`): the existing delegation tests patch
# `tools.delegate_tool._run_single_child` / `_build_child_preserving_parent_tools`
# on the module attribute, so call-time lookup keeps those patches effective.
from tools import delegate_tool as _delegate_tool

logger = logging.getLogger(__name__)

# Total item cap across all steps of one workflow call (max_items).
WORKFLOW_MAX_ITEMS = 32
# Hard ceiling on the workflow semaphore width, independent of the user's
# delegation.max_concurrent_children knob. Mirrors the reference harness's
# "bounded fan-out" posture: max_concurrent = min(8, configured cap).
WORKFLOW_MAX_CONCURRENT_CAP = 8

_STEP_KIND_PARALLEL = "parallel"
_STEP_KIND_PIPELINE = "pipeline"
_STEP_KINDS = (_STEP_KIND_PARALLEL, _STEP_KIND_PIPELINE)


def workflow_max_concurrent() -> int:
    """Semaphore width for workflow runs: min(8, delegation.max_concurrent_children).

    Always >= 1. Reads the same config knob as the flat batch path so the
    user's delegation.max_concurrent_children governs workflow fan-out too,
    but never lets a single workflow exceed 8 concurrent children (the
    reference harness caps at min(16, cores-2); 8 is the Hermes-side bound
    on top of the user's explicit knob).
    """
    from tools.delegate_tool import _get_max_concurrent_children

    try:
        configured = _get_max_concurrent_children()
    except Exception:
        configured = 3
    return max(1, min(WORKFLOW_MAX_CONCURRENT_CAP, configured))


def _validate_workflow(
    workflow: Any,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Validate + normalize a workflow description.

    Returns (normalized, error). normalized is
    {"steps": [{"kind": "parallel"|"pipeline", "items": [task, ...]}, ...]}
    with every item guaranteed to have a non-empty 'goal' and the total
    item count under WORKFLOW_MAX_ITEMS. On any structural problem the
    error string is actionable and the call is aborted BEFORE any child is
    spawned (fatal-vs-item: schema errors are fatal, item failures are not).
    """
    if not isinstance(workflow, dict):
        return None, (
            "workflow must be an object with a 'steps' array "
            f"(got {type(workflow).__name__})."
        )
    steps = workflow.get("steps")
    if not isinstance(steps, list) or not steps:
        return None, "workflow.steps must be a non-empty array of step objects."

    normalized_steps: List[Dict[str, Any]] = []
    total_items = 0
    for si, step in enumerate(steps):
        if not isinstance(step, dict):
            return None, f"workflow.steps[{si}] must be an object."

        kind_keys = [k for k in _STEP_KINDS if step.get(k) is not None]
        if len(kind_keys) != 1:
            return None, (
                f"workflow.steps[{si}] must define exactly one of "
                f"'parallel' or 'pipeline' (got {sorted(kind_keys) or 'none'})."
            )
        kind = kind_keys[0]
        items = step[kind]
        if not isinstance(items, list) or not items:
            return None, (
                f"workflow.steps[{si}].{kind} must be a non-empty array of "
                "task objects."
            )

        normalized_items: List[Dict[str, Any]] = []
        for ii, item in enumerate(items):
            if not isinstance(item, dict):
                return None, (
                    f"workflow.steps[{si}].{kind}[{ii}] must be an object "
                    f"(got {type(item).__name__})."
                )
            goal = str(item.get("goal") or "").strip()
            if not goal:
                return None, (
                    f"workflow.steps[{si}].{kind}[{ii}] is missing a 'goal'."
                )
            # Same placeholder/short-goal quality gate as the flat batch
            # (batch-only rules that the single-goal form is exempt from).
            normalized = " ".join(goal.lower().split())
            if _delegate_tool._PLACEHOLDER_GOAL_RE.match(normalized):
                return None, (
                    f"workflow.steps[{si}].{kind}[{ii}] has a placeholder "
                    f"goal ({goal!r}). Replace it with a specific, "
                    "self-contained description."
                )
            marker = _delegate_tool._TEMPLATE_MARKER_RE.search(goal)
            if marker:
                return None, (
                    f"workflow.steps[{si}].{kind}[{ii}] goal contains an "
                    f"unexpanded template marker ({marker.group(0)!r}). "
                    "Substitute the real value before calling delegate_task."
                )
            if len(goal) < _delegate_tool._MIN_BATCH_GOAL_LEN:
                return None, (
                    f"workflow.steps[{si}].{kind}[{ii}] goal is too short "
                    f"({goal!r}). Write a specific, self-contained goal of "
                    f"at least {_delegate_tool._MIN_BATCH_GOAL_LEN} characters."
                )
            normalized_items.append(item)

        total_items += len(normalized_items)
        if total_items > WORKFLOW_MAX_ITEMS:
            return None, (
                f"workflow exceeds the {WORKFLOW_MAX_ITEMS}-item cap: "
                f"{total_items} items across {len(steps)} step(s). Split "
                "into multiple delegate_task calls."
            )
        normalized_steps.append({"kind": kind, "items": normalized_items})

    return {"steps": normalized_steps}, None


def _workflow_item_error_entry(
    task_index: int,
    item: Dict[str, Any],
    error: str,
    top_role: str,
    *,
    step_index: int,
    step_kind: str,
) -> Dict[str, Any]:
    """Structured error entry for a failed workflow item (batch continues)."""
    return {
        "task_index": task_index,
        "step_index": step_index,
        "step_kind": step_kind,
        "status": "error",
        "summary": None,
        "error": error,
        "api_calls": 0,
        "duration_seconds": 0.0,
        "_child_role": _delegate_tool._normalize_role(item.get("role") or top_role),
    }


def _pipeline_context(item: Dict[str, Any], prev_entry: Optional[Dict[str, Any]]) -> str:
    """Context for a pipeline stage, threading the previous stage's output.

    v1 contract: the output (summary) of stage N is included in the context
    of the prompts of stage N+1. When stage N failed there is no output —
    its structured error text is passed forward instead, labeled, so the
    next stage can decide whether to recover or proceed. The workflow
    itself is never aborted by a failed stage.
    """
    ctx = item.get("context") or ""
    if prev_entry is None:
        return ctx
    if prev_entry.get("status") == "completed":
        header = (
            "OUTPUT FROM THE PREVIOUS PIPELINE STAGE — build on it "
            "to continue the work:"
        )
        body = prev_entry.get("summary") or ""
    else:
        header = (
            "THE PREVIOUS PIPELINE STAGE FAILED — its error is below. "
            "Recover from it or proceed with what you have:"
        )
        body = prev_entry.get("error") or ""
    sep = "\n\n" if ctx else ""
    return f"{ctx}{sep}{header}\n{body}"


def run_workflow(
    workflow: Dict[str, Any],
    *,
    parent_agent,
    creds: Dict[str, Any],
    effective_max_iter: int,
    top_role: str,
    origin_ui_session_id: Optional[str] = None,
    origin_owner_transport: Any = None,
    origin_owner_session_record: Any = None,
) -> Dict[str, Any]:
    """Execute a workflow description and return the consolidated result.

    Returns either {"error": str} (fatal, pre-spawn validation failure —
    the caller turns that into a tool_error) or the combined result dict:

        {
          "mode": "workflow",
          "results": [per-item entries, ordered by task_index],
          "max_items": 32,
          "max_concurrent": N,
          "total_duration_seconds": X,
        }

    Each per-item entry: {task_index, step_index, step_kind, status,
    summary, error?, api_calls, duration_seconds, ...} with status in
    {completed, error}. Internal fields (_child_role, _child_cost_usd) are
    stripped by _finalize_child_results, same as the flat batch path.
    """
    validated, err = _validate_workflow(workflow)
    if err:
        return {"error": err}
    steps = validated["steps"]

    max_concurrent = workflow_max_concurrent()
    sem = threading.BoundedSemaphore(max_concurrent)

    total_items = sum(len(step["items"]) for step in steps)
    results: List[Dict[str, Any]] = []
    # (global_index, item, child) + flat item list feed the shared
    # _finalize_child_results (summary budget, memory manager, hooks, cost
    # rollup) exactly like the flat batch path.
    all_children: List[Tuple[int, Dict[str, Any], Any]] = []
    flat_items: List[Dict[str, Any]] = []

    overall_start = time.monotonic()

    def _build_child(task_index: int, item: Dict[str, Any], context: str):
        effective_role = _delegate_tool._normalize_role(item.get("role") or top_role)
        return _delegate_tool._build_child_preserving_parent_tools(
            task_index=task_index,
            goal=item["goal"],
            context=context,
            toolsets=None,  # children always inherit the parent's toolsets
            model=creds.get("model"),
            max_iterations=effective_max_iter,
            task_count=total_items,
            parent_agent=parent_agent,
            override_provider=creds.get("provider"),
            override_base_url=creds.get("base_url"),
            override_api_key=creds.get("api_key"),
            override_api_mode=creds.get("api_mode"),
            override_request_overrides=creds.get("request_overrides"),
            override_max_tokens=creds.get("max_output_tokens"),
            override_acp_command=creds.get("command"),
            override_acp_args=creds.get("args"),
            role=effective_role,
        )

    def _run_item(
        task_index: int,
        item: Dict[str, Any],
        child,
        step_index: int,
        step_kind: str,
    ) -> Dict[str, Any]:
        """Run one item under the semaphore; item errors never propagate."""
        with sem:
            try:
                entry = _delegate_tool._run_single_child(
                    task_index,
                    item["goal"],
                    child,
                    parent_agent,
                    owner_session_id=origin_ui_session_id,
                    owner_transport=origin_owner_transport,
                    owner_session_record=origin_owner_session_record,
                )
            except Exception as exc:  # noqa: BLE001 — item isolation contract
                logger.debug(
                    "workflow item %d (%s step %d) raised: %s",
                    task_index, step_kind, step_index, exc,
                )
                entry = _workflow_item_error_entry(
                    task_index, item, str(exc), top_role,
                    step_index=step_index, step_kind=step_kind,
                )
        entry = dict(entry)
        entry["task_index"] = task_index
        entry["step_index"] = step_index
        entry["step_kind"] = step_kind
        # Workflow contract: status ∈ {completed, error}. The child runner
        # may report failed/interrupted — normalize those to error while
        # keeping the exit reason visible.
        if entry.get("status") != "completed":
            entry["status"] = "error"
            entry.setdefault(
                "error",
                entry.get("exit_reason")
                or entry.get("summary")
                or "child agent failed",
            )
        return entry

    global_index = 0
    for step_index, step in enumerate(steps):
        kind = step["kind"]
        items = step["items"]

        if kind == _STEP_KIND_PARALLEL:
            # Build every child on the calling thread (thread-safe
            # construction, same as the flat batch), then fan out. The pool
            # width AND the semaphore both cap at max_concurrent; the
            # semaphore is the authoritative cap that also covers pipeline
            # stages and any future interleaved execution.
            built: List[Tuple[int, Dict[str, Any], Any]] = []
            for item in items:
                child = _build_child(global_index, item, item.get("context") or "")
                built.append((global_index, item, child))
                all_children.append((global_index, item, child))
                flat_items.append(item)
                global_index += 1

            from tools.daemon_pool import DaemonThreadPoolExecutor

            with DaemonThreadPoolExecutor(max_workers=max_concurrent) as pool:
                futures = {
                    pool.submit(_run_item, gi, item, child, step_index, kind): gi
                    for gi, item, child in built
                }
                for future in as_completed(futures):
                    results.append(future.result())

        else:  # pipeline: strictly sequential stages, context threading
            prev_entry: Optional[Dict[str, Any]] = None
            for item in items:
                context = _pipeline_context(item, prev_entry)
                child = _build_child(global_index, item, context)
                all_children.append((global_index, item, child))
                flat_items.append(item)
                prev_entry = _run_item(global_index, item, child, step_index, kind)
                results.append(prev_entry)
                global_index += 1

    # Match input order (task_index is the global item index).
    results.sort(key=lambda r: r.get("task_index", -1))

    # Shared finalization: summary budget against parent context headroom,
    # memory manager on_delegation, subagent_stop hooks, cost rollup.
    _delegate_tool._finalize_child_results(results, flat_items, all_children, parent_agent)

    return {
        "mode": "workflow",
        "results": results,
        "max_items": WORKFLOW_MAX_ITEMS,
        "max_concurrent": max_concurrent,
        "total_duration_seconds": round(time.monotonic() - overall_start, 2),
    }


def _strip_workflow_model_hidden_fields(workflow: Any) -> Any:
    """Strip model-hidden task fields (acp_command/acp_args) from workflow items.

    Mirrors delegate_tool._strip_model_hidden_task_fields for the nested
    step/item shape. Returns the input unchanged when nothing was stripped.
    """
    if not isinstance(workflow, dict):
        return workflow
    steps = workflow.get("steps")
    if not isinstance(steps, list):
        return workflow


    changed = False
    new_steps = []
    for step in steps:
        if not isinstance(step, dict):
            new_steps.append(step)
            continue
        new_step = dict(step)
        for key in _STEP_KINDS:
            items = step.get(key)
            if isinstance(items, list):
                stripped = _delegate_tool._strip_model_hidden_task_fields(items)
                if stripped is not items:
                    new_step[key] = stripped
                    changed = True
        new_steps.append(new_step)
    if not changed:
        return workflow
    new_workflow = dict(workflow)
    new_workflow["steps"] = new_steps
    return new_workflow
