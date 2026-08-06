"""Seed an approved plan into a Kanban board.

Used by the kimi-mode plugin's ``/plan approve`` slash command (H-22 in
the hermes-v2 plan). Given a plan file path, this module:

1. Reads the plan body.
2. If it follows the v2 contract (defined in
   :mod:`hermes_cli.plan_parser`), creates a structured tree: one blocked
   root task (with the plan file attached) plus linked todo child tasks.
3. If it has no frontmatter, creates one unassigned triage task and leaves
   decomposition to the operator unless explicitly requested by the caller.

Structured tasks remain unassigned so the dispatcher cannot start them before
an operator routes the work. This is the H-00 incident guard.

The slug plus task ID forms each structured task's idempotency key. Replays
reuse tasks and attachment rows, refresh the attachment blob and size metadata,
and avoid duplicate links or events.

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
    idempotent_replay: bool = False  # True iff the root already existed
    decomposition_attempted: bool = False
    decomposition_ok: bool = False
    decomposition_message: Optional[str] = None


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
    if task.paths:
        parts.append(f"\n**Paths:** {', '.join(f'`{path}`' for path in task.paths)}")
    return "\n".join(parts)


def _existing_task_id(conn: Any, idempotency_key: str) -> Optional[str]:
    row = conn.execute(
        "SELECT id FROM tasks WHERE idempotency_key = ? "
        "AND status != 'archived' ORDER BY created_at DESC LIMIT 1",
        (idempotency_key,),
    ).fetchone()
    return str(row["id"]) if row else None


def _sync_attachment(
    kb: Any,
    conn: Any,
    task_id: str,
    *,
    path: Path,
    body: str,
    board: Optional[str],
    uploaded_by: Optional[str],
) -> None:
    """Keep one plan attachment blob and its size metadata in sync."""
    existing = next(
        (item for item in kb.list_attachments(conn, task_id) if item.filename == path.name),
        None,
    )
    if existing is not None:
        stored = Path(existing.stored_path)
    else:
        attachments_dir = kb.task_attachments_dir(task_id, board=board)
        attachments_dir.mkdir(parents=True, exist_ok=True)
        stored = attachments_dir / path.name
    payload = body.encode("utf-8")
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_bytes(payload)
    if existing is None:
        kb.add_attachment(
            conn,
            task_id,
            filename=path.name,
            stored_path=str(stored),
            size=len(payload),
            uploaded_by=uploaded_by,
        )
    elif existing.size != len(payload):
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_attachments SET size = ? WHERE id = ?",
                (len(payload), existing.id),
            )


def _ensure_link(kb: Any, conn: Any, parent_id: str, child_id: str) -> None:
    """Create one dependency edge and event only when it is absent."""
    if parent_id not in kb.parent_ids(conn, child_id):
        kb.link_tasks(conn, parent_id, child_id)


# ── Public entry points ───────────────────────────────────────────────


def seed_plan_to_kanban(
    plan_path: str | os.PathLike[str],
    board: Optional[str] = None,
    *,
    decompose_freeform: bool = False,
) -> SeedResult:
    """Read a plan file and seed it into the named Kanban board.

    Structured plans create one blocked root and todo children. Free-form plans
    create one unassigned triage task; callers may request immediate auxiliary
    decomposition with ``decompose_freeform=True``. Replays refresh the stored
    plan blob but do not duplicate tasks, links, attachment rows, or events.
    """
    path = Path(plan_path)
    if not path.exists():
        raise FileNotFoundError(f"plan file not found: {plan_path}")

    body = path.read_text(encoding="utf-8")

    # Lazy import: kanban_db pulls in sqlite3 + the full kanban module graph.
    from hermes_cli import kanban_db as kb

    if not is_v2_plan(body):
        return _seed_freeform_fallback(
            kb,
            body,
            path,
            board,
            decompose_freeform=decompose_freeform,
        )

    try:
        plan = parse_plan(body)
    except ValueError as exc:
        raise PlanSeedError([
            PlanValidationError("frontmatter_invalid", str(exc)),
        ]) from exc
    # [hermes-v2] H-22: validate BEFORE any board mutation. The
    # ``dependency_cycle`` detector lives in :func:`validate` so a
    # cyclic plan raises ``PlanSeedError`` before ``kb.connect_closing``
    # even opens a transaction — the previous code path created the
    # root task, then crashed inside ``link_tasks`` when the cycle was
    # only detected at link-insert time, leaving a half-seeded board
    # (root committed, edges absent, seeder raises ``ValueError`` not
    # ``PlanSeedError`` so the CLI cannot cleanly recover).
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
    """Seed a validated plan as one blocked root and linked todo tasks."""
    root_idem = f"{plan.slug}:root"
    child_idem_prefix = f"{plan.slug}:task"

    with kb.connect_closing(board=board) as conn:
        idempotent_replay = _existing_task_id(conn, root_idem) is not None
        root_body = _build_root_body(plan, str(path))
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
        _sync_attachment(
            kb,
            conn,
            root_task_id,
            path=path,
            body=path.read_text(encoding="utf-8"),
            board=board,
            uploaded_by=plan.created_by,
        )

        task_id_map: Dict[str, str] = {}
        child_task_ids: List[str] = []

        for plan_task in plan.tasks:
            child_idem = f"{child_idem_prefix}:{plan_task.raw_id}"
            child_body = _build_child_body(plan, plan_task)
            # Every top-level task belongs to the plan root even when the
            # optional ``parent: root`` segment was omitted.
            parents: List[str] = []
            if plan_task.parent in (None, "root"):
                parents.append(root_task_id)
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
                skills=skills,
                board=board,
            )
            task_id_map[plan_task.raw_id] = child_task_id
            child_task_ids.append(child_task_id)

        for plan_task in plan.tasks:
            child_task_id = task_id_map[plan_task.raw_id]
            if plan_task.parent and plan_task.parent != "root":
                parent_id = task_id_map.get(plan_task.parent)
                if parent_id:
                    _ensure_link(kb, conn, parent_id, child_task_id)
            for dep_raw_id in plan_task.depends:
                dep_id = task_id_map.get(dep_raw_id)
                if dep_id:
                    _ensure_link(kb, conn, dep_id, child_task_id)

    return SeedResult(
        root_task_id=root_task_id,
        child_task_ids=child_task_ids,
        fallback=False,
        idempotent_replay=idempotent_replay,
    )


def _seed_freeform_fallback(
    kb: Any,
    body: str,
    path: Path,
    board: Optional[str],
    *,
    decompose_freeform: bool,
) -> SeedResult:
    """Seed free-form prose as triage and optionally run the decomposer."""
    title = path.stem.replace("-", " ").replace("_", " ").strip() or "Free-form plan"
    idem = f"freeform:{path.stem}"
    with kb.connect_closing(board=board) as conn:
        idempotent_replay = _existing_task_id(conn, idem) is not None
        root_task_id = kb.create_task(
            conn,
            title=f"Decompose: {title}",
            body=body,
            assignee=None,
            created_by="kimi-mode",
            idempotency_key=idem,
            priority=0,
            triage=True,
            board=board,
        )
        _sync_attachment(
            kb,
            conn,
            root_task_id,
            path=path,
            body=body,
            board=board,
            uploaded_by="kimi-mode",
        )

    child_task_ids: List[str] = []
    decomposition_attempted = False
    decomposition_ok = False
    decomposition_message: Optional[str] = None
    with kb.connect_closing(board=board) as conn:
        child_rows = conn.execute(
            "SELECT child_id FROM task_links WHERE parent_id = ? ORDER BY rowid",
            (root_task_id,),
        ).fetchall()
    child_task_ids = [str(row["child_id"]) for row in child_rows]

    if child_task_ids:
        # A successful prior decomposition is durable in the child links. Do
        # not call the decomposer again: the root is now todo/non-triage and
        # that retry would only report a misleading status failure.
        decomposition_ok = True
        decomposition_message = (
            "already decomposed; reused "
            f"{len(child_task_ids)} existing child task(s)"
        )
    elif decompose_freeform:
        from hermes_cli.kanban_decompose import decompose_task

        decomposition_attempted = True
        outcome = decompose_task(root_task_id, author="kimi-mode", board=board)
        decomposition_ok = outcome.ok
        decomposition_message = outcome.reason
        child_task_ids = list(outcome.child_ids or [])

    return SeedResult(
        root_task_id=root_task_id,
        child_task_ids=child_task_ids,
        fallback=True,
        idempotent_replay=idempotent_replay,
        decomposition_attempted=decomposition_attempted,
        decomposition_ok=decomposition_ok,
        decomposition_message=decomposition_message,
    )
