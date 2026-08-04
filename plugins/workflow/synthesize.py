"""Template synthesis — convert a completed dynamic workflow's DAG into a
reusable static pipeline YAML.

The payoff of the dynamic loop: a dynamic run that discovered a working
shape (objective → nodes → patterns → success) gets captured as a
machine-parseable static pipeline. Future runs use the fast, bounded,
kanban-backed static path instead of re-discovering the shape.

Mapping (dynamic → static):
  - DynamicNode.goal        → node.task (generalized)
  - DynamicNode.depends_on  → node.depends_on (unchanged)
  - workflow.objective      → {context.objective} placeholder
  - pattern=review-loop     → producer node gets reviews: [reviewer];
                              reviewer node declared but NOT depended on
                              by anything (static engine dispatches it via
                              the reviews pipeline)
  - max_review_retries      → workflow.max_retries (static engine default
                              for the review loop)
  - roles                   → generated role placeholders ({worker-1}, ...)
                              with a comment telling the operator to map
                              them to real fleet agents

Output location: ``$HERMES_HOME/workflows/<name>.yaml`` — the directory
both the static engine (HERMES_WORKFLOW_FILES / HERMES_HOME fallback) and
the registry (user-saved dynamic templates) scan, so a synthesized
template is immediately startable via workflow_start.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_ID_SAFE = re.compile(r"[^a-z0-9_.\-]+")
_RECORD_STATUSES = {"completed", "failed", "cancelled"}

_REVIEW_PATTERN = "review-loop"


def _slugify(text: str, fallback: str = "node") -> str:
    """Lowercase-hyphenated id from arbitrary text."""
    slug = _ID_SAFE.sub("-", text.lower()).strip("-")
    return slug[:80] or fallback


def _sanitize_task(goal: str, objective: str) -> str:
    """Generalize a node goal into a reusable task description.

    Replaces exact objective text with {context.objective} so the
    synthesized template works for new inputs.
    """
    task = (goal or "").strip()
    if not task:
        return "Complete the assigned work. Include workflow_id and node_id in your summary."
    if objective and objective.strip():
        task = task.replace(objective.strip(), "{context.objective}")
    return task


def _generalize_feedback_goal(goal: str) -> str:
    """Strip per-run review feedback from a reworked producer's goal.

    During rework the engine enriches the goal with
    '[Review feedback — rework round N]: ...'. Synthesis drops that
    trailing feedback block so the template starts clean.
    """
    if not goal:
        return goal
    marker = "\n\n[Review feedback"
    idx = goal.find(marker)
    if idx != -1:
        return goal[:idx].strip()
    return goal.strip()


def build_static_yaml(
    workflow_id: str,
    objective: str,
    nodes: list[dict],
    *,
    name: str = "",
    max_review_retries: Optional[int] = None,
) -> str:
    """Build a static pipeline YAML from a dynamic workflow's public view.

    Args:
        workflow_id: Source dynamic workflow id (metadata only).
        objective: The dynamic workflow's objective.
        nodes: Node public views in node_order (dicts with node_id,
            goal, depends_on, pattern, review_target, max_review_retries).
        name: Template name (slugified; defaults from workflow_id).
        max_review_retries: Workflow-level review rework budget.

    Returns:
        The YAML text (not yet written to disk).
    """
    template_name = _slugify(name or workflow_id, fallback="synthesized-workflow")

    # ── Pass 1: classify nodes ──
    work_nodes: list[dict] = []        # real agent work
    reviewers: list[dict] = []         # pattern=review-loop nodes
    for n in nodes:
        if n.get("pattern") == _REVIEW_PATTERN:
            reviewers.append(n)
        else:
            work_nodes.append(n)

    # Map reviewer → producer (review_target, else first dep)
    review_map: dict[str, list[str]] = {}  # producer_id -> [reviewer_ids]
    for r in reviewers:
        producer = r.get("review_target") or (
            r.get("depends_on") or [""]
        )[0]
        if producer and any(
            w.get("node_id") == producer for w in work_nodes
        ):
            review_map.setdefault(producer, []).append(r["node_id"])

    # ── Pass 2: role assignment ──
    # Distinct roles per work node; reviewers share the operator's choice
    # via comment. Deterministic: role name = worker-N in node_order.
    role_of: dict[str, str] = {}
    role_names: list[str] = []
    for idx, n in enumerate(work_nodes, start=1):
        role = f"{{worker-{idx}}}"
        role_of[n["node_id"]] = role
        role_names.append(role)
    if reviewers:
        role_names.append("{reviewer-1}")

    lines: list[str] = []
    lines.append("# =============================================================================")
    lines.append("# SYNTHESIZED PIPELINE — from dynamic workflow run")
    lines.append(f"# Source dynamic workflow: {workflow_id}")
    lines.append("#")
    lines.append("# Generalization notes:")
    lines.append("#   - The original objective is referenced as {context.objective} —")
    lines.append("#     pass a new objective via workflow_start(context={'objective': ...}).")
    lines.append("#   - Role placeholders below MUST be mapped to real fleet agents")
    lines.append("#     before starting (e.g. worker-1: newton).")
    if review_map:
        lines.append("#   - Review-loop shape preserved: producers declare `reviews:`;")
        lines.append("#     the static engine runs the bounded rework loop automatically.")
    lines.append("# =============================================================================")
    lines.append("")
    lines.append(f"name: {template_name}")
    lines.append("description: >-")
    lines.append(f"  Synthesized from dynamic workflow {workflow_id}. "
                 f"Runs the discovered shape as a static pipeline.")
    lines.append("")
    lines.append("roles:")
    for role in role_names:
        lines.append(f"  {role.strip('{}')}: <agent-name>  # TODO: map to a real profile")
    if max_review_retries is not None:
        lines.append("")
        lines.append(f"max_retries: {max_review_retries}  # review rework budget from the source run")
    lines.append("")
    lines.append("nodes:")
    lines.append("")

    # ── Pass 3: emit work nodes ──
    for n in work_nodes:
        nid = n["node_id"]
        task = _generalize_feedback_goal(n.get("goal") or "")
        task = _sanitize_task(task, objective)
        lines.append(f"  {nid}:")
        lines.append(f"    agent: \"{role_of[nid]}\"")
        # Indent multi-line task
        task_lines = task.splitlines() or [""]
        lines.append("    task: >")
        for tl in task_lines:
            lines.append(f"      {tl}" if tl else "      ")
        if n.get("depends_on"):
            deps = n["depends_on"]
            # Only depend on work nodes — reviewers are handled via reviews:
            deps = [d for d in deps if d in role_of]
            if deps:
                lines.append(f"    depends_on: [{', '.join(deps)}]")
        if nid in review_map:
            lines.append(f"    reviews: [{', '.join(review_map[nid])}]")
        lines.append("")

    # ── Pass 4: emit reviewer nodes (declared, dispatched via reviews) ──
    for r in reviewers:
        nid = r["node_id"]
        task = _generalize_feedback_goal(r.get("goal") or "")
        task = _sanitize_task(task, objective)
        lines.append(f"  {nid}:")
        lines.append("    agent: \"{reviewer-1}\"")
        lines.append("    task: >")
        task_lines = task.splitlines() or [""]
        for tl in task_lines:
            lines.append(f"      {tl}" if tl else "      ")
        lines.append("    # Review node: dispatched by the engine via the")
        lines.append("    # producer's reviews: pipeline — do not add depends_on.")
        lines.append("")

    return "\n".join(lines)


def template_path(name: str) -> Path:
    """Resolve the output path for a synthesized template."""
    hermes_home = Path(
        os.environ.get("HERMES_HOME", "") or str(Path.home() / ".hermes")
    ).expanduser()
    d = hermes_home / "workflows"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{_slugify(name, fallback='synthesized-workflow')}.yaml"


def synthesize_template(
    workflow_id: str,
    *,
    name: str = "",
    role_map: Optional[Dict[str, str]] = None,
    overwrite: bool = False,
) -> Dict[str, Any]:
    """Synthesize a static pipeline from a completed dynamic workflow.

    Args:
        workflow_id: The dynamic workflow to capture (searched across all
            scopes, active + completed).
        name: Template name (defaults from workflow_id).
        role_map: Optional {role: agent} mapping applied after
            generation, e.g. {"worker-1": "newton"}. Roles left unmapped
            keep their TODO placeholder.
        overwrite: Allow replacing an existing template file.

    Returns:
        {ok, path, name, node_count, reviewer_count, note} or
        {ok: False, error: ...}.
    """
    from plugins.workflow.dynamic import find_workflow_by_id

    wf = find_workflow_by_id(workflow_id)
    if wf is None:
        return {
            "ok": False,
            "error": f"unknown workflow_id: {workflow_id!r} (not found in active or completed workflows)",
        }

    nodes = [wf.nodes[nid].public_view() for nid in wf.node_order if nid in wf.nodes]
    if not nodes:
        return {"ok": False, "error": "workflow has no nodes to synthesize"}

    template_name = _slugify(name or workflow_id, fallback="synthesized-workflow")
    out_path = template_path(template_name)
    if out_path.exists() and not overwrite:
        return {
            "ok": False,
            "error": f"template already exists: {out_path} (pass overwrite=true to replace)",
        }

    yaml_text = build_static_yaml(
        workflow_id=wf.workflow_id,
        objective=wf.objective,
        nodes=nodes,
        name=template_name,
        max_review_retries=wf.max_review_retries,
    )

    # Apply role map if provided (simple token replacement on the YAML).
    if role_map:
        for role, agent in role_map.items():
            yaml_text = yaml_text.replace(
                f"{role}: <agent-name>  # TODO: map to a real profile",
                f"{role}: {agent}",
            )

    out_path.write_text(yaml_text, encoding="utf-8")

    reviewer_count = sum(
        1 for n in nodes if n.get("pattern") == _REVIEW_PATTERN
    )
    return {
        "ok": True,
        "path": str(out_path),
        "name": template_name,
        "node_count": len(nodes),
        "reviewer_count": reviewer_count,
        "note": "Map role placeholders to real agents in the YAML, then run workflow_start(workflow=...).",
    }
