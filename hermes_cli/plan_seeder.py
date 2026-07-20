"""Seed an approved plan into a Kanban board.

Used by the kimi-mode plugin's ``/plan approve`` slash command (H-22 in
the hermes-v2 plan). Given a plan file path, this module:

1. Reads the plan body.
2. If it follows the v2 contract (defined in
   :mod:`hermes_cli.plan_parser`), creates a structured tree: one root
   task (with the plan file attached) plus one child task per
   ``- [ ] T<n>`` line in the ``tasks`` block, with parent/depends
   links reconstructed.
3. If it is NOT a v2 plan (free-form prose only), creates a single
   triage task whose body is the plan text, leaving promotion /
   decomposition to the operator.

All tasks are created **without an assignee** and in ``initial_status``
``todo`` — the dispatcher guard at ``kanban_db.py:8188`` refuses to
spawn workers for unassigned tasks, so the seeded board is inert
until Basti (or a supervised session) explicitly assigns a worker to
the next task to work on. This is the H-00-incident guard.

Idempotency: the slug + task-id (``H-XX`` or ``T<n>``) form the
``idempotency_key`` for each created task. Re-approving the same plan
is a no-op.

Refs: H-20 (parser contract), H-22 (this integration), H-00 (the
incident that motivated the unassigned guard).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_cli.plan_parser import (
    ParsedPlan,
    PlanTask,
    PlanValidationError,
    is_v2_plan,
    parse_plan,
    validate,
)


logger = logging.getLogger(__name__)


# ── Result types ───────────────────────────────────────────────────────


@dataclass
class SeedResult:
    """Summary of a successful seed operation."""

    root_task_id: str
    child_task_ids: List[str] = field(default_factory=list)
    fallback: bool = False  # True iff free-form fallback was used
    idempotent_replay: bool = False  # True iff re-approval skipped creation


class PlanSeedError(ValueError):
    """Raised when a v2 plan fails validation. The exception carries
    the list of :class:`PlanValidationError` so callers can render a
    human-readable summary."""

    def __init__(self, errors: List[PlanValidationError]) -> None:
        self.errors = errors
        super().__init__(
            "plan has validation errors: "
            + "; ".join(f"{e.code}: {e.message}" for e in errors)
        )


# ── Seeding primitives ────────────────────────────────────────────────


def _priority_for_tier(tier: str) -> int:
    """Map scope-tier letter to Kanban priority (higher = sooner).

    A=10, B=5, C=1, anything else=0. Returned as a positive int that
    fits in Kanban's priority column (defaults to 0).
    """
    return {"A": 10, "B": 5, "C": 1}.get(tier.upper(), 0)


def _build_root_body(plan: ParsedPlan, plan_path: str) -> str:
    """Build the body text for the root task.

    The body carries the plan's goal, scope tiers, risks, and
    verification list so a worker who picks up the root has the
    context they need without re-reading the attached file. The plan
    file itself is attached separately via ``add_attachment``.
    """
    parts: List[str] = []
    parts.append(f"# {plan.title}\n")
    parts.append(f"**Goal:** {plan.goal}\n")
    if plan.scope_tiers:
        parts.append("\n**Scope tiers:**")
        for tier, items in sorted(plan.scope_tiers.items()):
            parts.append(f"- **{tier}**: {', '.join(items)}")
    if plan.risks:
        parts.append("\n**Risks:**")
        for r in plan.risks:
            parts.append(f"- {r}")
    if plan.verification:
        parts.append("\n**Verification:**")
        for v in plan.verification:
            parts.append(f"- `{v}`")
    parts.append(f"\n**Source plan:** `{plan_path}`")
    parts.append(
        f"\n**Slug:** `{plan.slug}` · "
        f"**Created by:** {plan.created_by or 'unknown'} · "
        f"**Created at:** {plan.created_at or 'unknown'}"
    )
    return "\n".join(parts)


def _build_child_body(plan: ParsedPlan, task: PlanTask) -> str:
    """Build the body text for a child task."""
    parts: List[str] = []
    parts.append(f"**Plan task:** `{task.raw_id}` — {task.title}")
    if task.skill:
        parts.append(f"\n**Skill(s):** `{task.skill}`")
    if task.verify:
        parts.append(f"\n**Verify command:**")
        parts.append(f"\n```bash\n{task.verify}\n```")
    if task.parent:
        parts.append(f"\n**Parent:** `{task.parent}`")
    if task.depends:
        parts.append(f"\n**Depends on:** {', '.join(task.depends)}")
    return "\n".join(parts)


# ── Public entry points ───────────────────────────────────────────────


def seed_plan_to_kanban(
    plan_path: str | os.PathLike[str],
    board: Optional[str] = None,
) -> SeedResult:
    """Read a plan file and seed it into the named Kanban board.

    Strategy:

    - **v2 plan + validates** → create root task + per-line child
      tasks with parent/depends links; attach the plan file to the
      root.
    - **v2 plan + validation fails** → raise :class:`PlanSeedError`
      carrying the list of issues. Callers should surface them to
      the user.
    - **Free-form plan** → create a single triage task whose body is
      the plan text; flag ``fallback=True`` in the result.

    All tasks are created **unassigned**, ``initial_status="todo"``.
    The dispatcher will not pick them up until someone assigns a
    worker — see the module docstring for the rationale.

    Args:
        plan_path: Filesystem path to the plan markdown file.
        board: Optional Kanban board slug. Defaults to whatever the
            operator's CLI context resolves to (typically the
            current board). Pass explicitly when called from a
            non-interactive context (cron, plugin, automation).

    Returns:
        A :class:`SeedResult` describing what was created. Callers
        should report the ``root_task_id`` (and, for v2 plans, the
        child count) to the user.
    """
    path = Path(plan_path)
    if not path.exists():
        raise FileNotFoundError(f"plan file not found: {plan_path}")

    body = path.read_text(encoding="utf-8")

    # Lazy import: kanban_db pulls in sqlite3 + the full kanban
    # module graph, which we don't want at import time for callers
    # that only need the parser (e.g. webui). H-22's plugin is the
    # primary caller, so this is fine in practice.
    from hermes_cli import kanban_db as kb

    if not is_v2_plan(body):
        return _seed_freeform_fallback(kb, body, path, board)

    plan = parse_plan(body)
    errors = validate(plan)
    if errors:
        raise PlanSeedError(errors)

    return _seed_v2_plan(kb, plan, path, board)


def _seed_v2_plan(
    kb: Any,
    plan: ParsedPlan,
    path: Path,
    board: Optional[str],
) -> SeedResult:
    """Internal: seed a validated v2 plan into a Kanban board.

    Status choices:

    - **Root task**: ``initial_status="blocked"``. The kanban
      ``create_task`` API only accepts ``{"blocked", "running"}`` for
      initial status; ``todo`` is not directly creatable. Children of
      ``root`` auto-promote to ``todo`` when the parent is in
      ``blocked`` (see the parent-status logic in ``kanban_db.py``),
      so the whole board stays dispatcher-inert until the operator
      promotes the root.
    - **Child tasks** with parents: default initial status. Parent
      not-yet-done → child lands in ``todo``. This matches the
      plan-level intent ("Status todo, ohne Assignee").
    - **Unassigned across the board**: dispatcher guard at
      ``kanban_db.py:8188`` blocks spawning even if the root is
      manually promoted.
    """
    root_idem = f"{plan.slug}:root"
    child_idem_prefix = f"{plan.slug}:task"

    with kb.connect_closing() as conn:
        root_body = _build_root_body(plan, str(path))
        # Scope-tier letter drives priority: A > B > C.
        priority = max(
            (_priority_for_tier(tier) for tier in plan.scope_tiers.keys()),
            default=0,
        )
        root_task_id = kb.create_task(
            conn,
            title=plan.title,
            body=root_body,
            assignee=None,
            created_by=plan.created_by,
            idempotency_key=root_idem,
            priority=priority,
            initial_status="blocked",
            skills=None,
            board=board,
        )
        # Attach the plan file to the root task. The attachment API
        # expects the blob to already be at ``stored_path``; copy the
        # file into the board's attachments dir.
        if root_task_id:
            attachments_dir = kb.task_attachments_dir(root_task_id, board=board)
            attachments_dir.mkdir(parents=True, exist_ok=True)
            stored = attachments_dir / path.name
            stored.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            kb.add_attachment(
                conn,
                root_task_id,
                filename=path.name,
                stored_path=str(stored),
                uploaded_by=plan.created_by,
            )

        # Second pass: create all child tasks with parent/depends
        # resolved. Tasks referencing ``root`` are linked under the
        # root task; tasks referencing another ``T<n>`` get a
        # depends-link.
        task_id_map: Dict[str, str] = {}  # raw_id -> kanban task id
        child_task_ids: List[str] = []

        for plan_task in plan.tasks:
            child_idem = f"{child_idem_prefix}:{plan_task.raw_id}"
            child_body = _build_child_body(plan, plan_task)
            # Children inherit the parent's priority (one tier per
            # child isn't tracked — the plan level already encodes
            # tier membership in the frontmatter).
            parents: List[str] = []
            if plan_task.parent == "root":
                parents.append(root_task_id)
            elif plan_task.parent:
                # Parent T<n> — defer to a second link_tasks pass.
                pass
            skills = (
                [s.strip() for s in plan_task.skill.split(",") if s.strip()]
                if plan_task.skill
                else None
            )
            child_task_id = kb.create_task(
                conn,
                title=f"{plan_task.raw_id}: {plan_task.title}",
                body=child_body,
                assignee=None,
                created_by=plan.created_by,
                parents=tuple(parents),
                idempotency_key=child_idem,
                priority=priority,
                # Omit initial_status — default "running" falls through
                # to the parent-aware branch, which sets the task to
                # ``todo`` because its parent isn't done yet.
                skills=skills,
                board=board,
            )
            task_id_map[plan_task.raw_id] = child_task_id
            child_task_ids.append(child_task_id)

        # Third pass: link non-root parents and depends.
        for plan_task in plan.tasks:
            child_task_id = task_id_map[plan_task.raw_id]
            if plan_task.parent and plan_task.parent != "root":
                parent_id = task_id_map.get(plan_task.parent)
                if parent_id:
                    kb.link_tasks(conn, parent_id, child_task_id)
            for dep_raw_id in plan_task.depends:
                dep_id = task_id_map.get(dep_raw_id)
                if dep_id:
                    kb.link_tasks(conn, dep_id, child_task_id)

    return SeedResult(
        root_task_id=root_task_id,
        child_task_ids=child_task_ids,
        fallback=False,
    )


def _seed_freeform_fallback(
    kb: Any,
    body: str,
    path: Path,
    board: Optional[str],
) -> SeedResult:
    """Free-form plan: seed a single triage task with the full text."""
    title = path.stem.replace("-", " ").replace("_", " ").strip() or "Free-form plan"
    idem = f"freeform:{path.stem}"
    with kb.connect_closing() as conn:
        root_task_id = kb.create_task(
            conn,
            title=f"Decompose: {title}",
            body=body,
            assignee=None,
            created_by="kimi-mode",
            idempotency_key=idem,
            priority=0,
            # No parent → falls into ready branch. Assignee is None so
            # the dispatcher guard at ``kanban_db.py:8188`` blocks
            # spawning until someone explicitly assigns it.
            board=board,
        )
        if root_task_id:
            attachments_dir = kb.task_attachments_dir(root_task_id, board=board)
            attachments_dir.mkdir(parents=True, exist_ok=True)
            stored = attachments_dir / path.name
            stored.write_text(body, encoding="utf-8")
            kb.add_attachment(
                conn,
                root_task_id,
                filename=path.name,
                stored_path=str(stored),
                uploaded_by="kimi-mode",
            )
    return SeedResult(
        root_task_id=root_task_id,
        child_task_ids=[],
        fallback=True,
    )
