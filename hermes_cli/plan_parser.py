"""Parser for Hermes-v2 Plan File v2 Contract.

The v2 contract is documented in
``skills/software-development/plan/SKILL.md`` (Plan File v2 Contract
section). Plans that follow this contract can be approved via the
``/plan approve`` slash command and round-tripped into a Kanban board
(H-22 in the hermes-v2 plan).

This module is intentionally framework-light: it uses PyYAML (already
a runtime dep) for the frontmatter and a few regexes for the task
block. No filesystem side effects, no logging, no DB calls. H-22 is
the integration point that wires the parsed output into Kanban.

The contract:

1. Plan starts with ``---\\n...\\n---\\n`` YAML frontmatter. Required
   keys: ``slug``, ``title``, ``goal``, ``scope_tiers``,
   ``risks``, ``verification``.
2. Exactly one fenced block ``\\`\\`\\`tasks\\n...\\n\\`\\`\\` ``
   containing machine-readable task lines.
3. Each task line starts with ``- [ ] T<n>: <Title>`` and may add
   ``skill:``, ``verify:``, ``parent:``, ``depends:``, and canonical
   ``paths:`` segments (with legacy ``path:``/``files:`` aliases).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import yaml


# ── Patterns ───────────────────────────────────────────────────────────

_FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\n(?P<fm>.*?)\n---[ \t]*\n",
    re.DOTALL,
)

_TASKS_FENCE_RE = re.compile(
    r"```tasks[ \t]*\n(?P<body>.*?)\n```",
    re.DOTALL,
)

# Catch a task-like bullet that is malformed (e.g. ``- [ ] T99`` with no
# title, or ``- [ ]`` alone). Used by the parser to surface silent drops so
# an operator who fat-fingered a task line still sees the offending row in
# the validation error list — the previous parser just ignored such lines.
_TASK_LIKE_BAD_PREFIX_RE = re.compile(r"^- \[[ xX]\]")

# T1, T1.1, T1.2.3 — stable within-plan task IDs.
_TASK_LINE_RE = re.compile(
    r"^- \[ \] (?P<id>T\d+(?:\.\d+)*):[ \t]+(?P<rest>.+)$",
)

# Optional ``| key: value`` segments on a task line. A line may carry
# multiple ``|`` segments in any order; the title is everything before
# the first ``|``.
_KV_SEGMENT_RE = re.compile(
    r"[ \t]*\|[ \t]*(?P<key>[a-z_]+):[ \t]*(?P<value>.+?)(?=[ \t]*\||[ \t]*$)"
)


_REQUIRED_FRONTMATTER_KEYS = (
    "slug",
    "title",
    "goal",
    "scope_tiers",
    "risks",
    "verification",
)

_SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


# ── Result types ───────────────────────────────────────────────────────


@dataclass
class PlanTask:
    """One ``- [ ]`` line in the ``tasks`` block."""

    raw_id: str  # e.g. "T1.2"
    title: str
    skill: Optional[str] = None
    verify: Optional[str] = None
    parent: Optional[str] = None
    depends: List[str] = field(default_factory=list)
    paths: List[str] = field(default_factory=list)
    line_no: int = 0  # 1-based line within the tasks block body


@dataclass
class MalformedTaskLine:
    """A ``- [ ]`` line inside the ``tasks`` fence that did not match
    the canonical ``T<id>: <title>`` regex.

    ``line_no`` is the 1-based line number within the fence body (the
    same coordinate space as :attr:`PlanTask.line_no`), and ``text``
    is the raw line stripped of trailing whitespace. Surfacing these
    here (rather than letting them silently drop) is what stops an
    editor's typo (``- [ ]`` with no id, ``- [ ] T99`` with no title,
    etc.) from disappearing without trace and breaking the seed.
    """

    line_no: int
    text: str


@dataclass
class ParsedPlan:
    """Successful parse of a v2 plan file."""

    slug: str
    title: str
    goal: str
    scope_tiers: Dict[str, List[str]]
    risks: List[str]
    verification: List[str]
    created_by: Optional[str] = None
    created_at: Optional[str] = None
    model: Optional[str] = None
    provider: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)
    tasks: List[PlanTask] = field(default_factory=list)
    # Lines inside the ``tasks`` fence that look like tasks but fail
    # the canonical regex. The list is empty for clean plans; populated
    # during parse so :func:`validate` can surface the offending rows.
    malformed_task_lines: List[MalformedTaskLine] = field(default_factory=list)


@dataclass
class PlanValidationError:
    """One failed check from :func:`validate`."""

    code: str
    message: str
    line_no: Optional[int] = None


# ── Parser ─────────────────────────────────────────────────────────────


def _parse_frontmatter(text: str) -> Tuple[Dict[str, Any], int]:
    """Return ``(frontmatter_dict, body_start_offset)``.

    Body start is the offset of the first character after the closing
    ``---`` line, so callers can scan the rest of the document for the
    tasks block. Returns ``({}, 0)`` if the frontmatter is absent and raises
    :class:`ValueError` when an envelope contains invalid YAML.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, 0
    try:
        parsed = yaml.safe_load(m.group("fm")) or {}
    except yaml.YAMLError as exc:
        raise ValueError("invalid YAML frontmatter") from exc
    if not isinstance(parsed, dict):
        raise ValueError("frontmatter is not a YAML mapping")
    return parsed, m.end()


def _parse_tasks_block(text: str) -> Tuple[List[PlanTask], List[MalformedTaskLine]]:
    """Extract and parse the ``tasks`` fenced block.

    Returns ``(tasks, malformed_task_lines)``:

    * ``tasks`` — the list of well-formed ``- [ ] T<id>: <title>`` rows.
    * ``malformed_task_lines`` — every ``- [ ]`` line in the fence that
      did not match the canonical regex. The previous parser dropped
      these silently; surfacing them here lets :func:`validate` emit
      structured errors instead of letting an operator's typo
      disappear.

    The contract guarantees:

    * At most ONE ``tasks`` fence is allowed. Callers that need to enforce
      "exactly one" should use :func:`_find_tasks_fences` — :func:`parse_plan`
      raises on > 1 fence.
    """
    m = _TASKS_FENCE_RE.search(text)
    if not m:
        return [], []
    body = m.group("body")
    tasks: List[PlanTask] = []
    malformed: List[MalformedTaskLine] = []
    for offset, raw_line in enumerate(body.split("\n"), start=1):
        line = raw_line.rstrip()
        m2 = _TASK_LINE_RE.match(line)
        if not m2:
            # Capture lines that *look* like task bullets but fail the
            # canonical regex — the operator typed ``- [ ]`` and meant
            # a task. We ignore bare ``-`` bullets (those are prose)
            # and any line that doesn't even start with ``- [``.
            if _TASK_LIKE_BAD_PREFIX_RE.match(line):
                malformed.append(
                    MalformedTaskLine(line_no=offset, text=line)
                )
            continue
        raw_id = m2.group("id")
        rest = m2.group("rest").strip()
        # Find the first ``|`` segment boundary. Title is everything
        # before it; everything after is parsed for ``key: value``
        # pairs (a title may legally contain ``|``).
        kv_start: Optional[int] = None
        for seg in _KV_SEGMENT_RE.finditer(rest):
            # A kv segment must NOT start at column 0 (no leading ``|``).
            if seg.start() == 0:
                continue
            kv_start = seg.start()
            break
        if kv_start is None:
            title = rest
            kv_text = ""
        else:
            title = rest[:kv_start].rstrip()
            kv_text = rest[kv_start:]
        task = PlanTask(raw_id=raw_id, title=title, line_no=offset)
        for seg in _KV_SEGMENT_RE.finditer(kv_text):
            key = seg.group("key")
            value = seg.group("value").strip()
            if key == "skill":
                task.skill = value
            elif key == "verify":
                task.verify = value
            elif key == "parent":
                task.parent = value
            elif key == "depends":
                # Comma-separated list of T-IDs. Accept both ``T1, T2``
                # and the bracket form ``[T1, T2]``.
                cleaned = value.strip()
                if cleaned.startswith("[") and cleaned.endswith("]"):
                    cleaned = cleaned[1:-1]
                task.depends = [t.strip() for t in cleaned.split(",") if t.strip()]
            elif key in {"path", "paths", "files"}:
                # Machine-readable file scope. ``paths`` is canonical; the
                # singular and ``files`` aliases keep hand-written plans terse.
                cleaned = value.strip()
                if cleaned.startswith("[") and cleaned.endswith("]"):
                    cleaned = cleaned[1:-1]
                task.paths = [p.strip() for p in cleaned.split(",") if p.strip()]
        tasks.append(task)
    return tasks, malformed


def _find_tasks_fences(text: str) -> List[re.Match]:
    """Return every ``tasks`` fence match in ``text``.

    Used to enforce the v2 contract's "exactly one tasks fence" rule.
    :func:`parse_plan` calls this to surface a structured
    :class:`PlanValidationError` (``code="multiple_tasks_fences"``) instead
    of silently consuming the first fence — the previous behaviour let an
    editor split the block in half and only seed the top half.
    """
    return list(_TASKS_FENCE_RE.finditer(text))


def parse_plan(text: str) -> ParsedPlan:
    """Parse a v2 plan string into a :class:`ParsedPlan`.

    Raises :class:`ValueError` when frontmatter is absent, malformed, or
    missing any required contract key, OR when more than one ``tasks``
    fence is present (the v2 contract is single-fence — silently taking
    the first would let an editor split a plan in half and only seed the
    top half). Use :func:`is_v2_plan` before parsing when genuine
    free-form plans should follow a fallback path.

    Task-line syntax errors inside the fence (e.g. ``- [ ] T99`` with no
    title, or ``- [ ]`` alone) are NOT raised here — :func:`validate`
    reports them via :class:`PlanValidationError` ``code="malformed_task_line"``.
    """
    fm, body_start = _parse_frontmatter(text)
    if body_start == 0:
        raise ValueError("no YAML frontmatter found — not a v2 plan")
    missing_keys = [key for key in _REQUIRED_FRONTMATTER_KEYS if key not in fm]
    if missing_keys:
        raise ValueError(f"missing required frontmatter key: {missing_keys[0]}")

    # [hermes-v2] H-20: enforce the "exactly one tasks fence" contract
    # at parse time so callers can't silently take the first fence when
    # an editor / generator emitted two. ``find_tasks_fences`` is the
    # cheap list scan; the actual body parse still consumes the first
    # match (consistent with ``_parse_tasks_block``) but the caller
    # sees a clear ``ValueError`` instead of a half-seeded plan.
    fences = _find_tasks_fences(text)
    if len(fences) > 1:
        raise ValueError(
            f"plan contains {len(fences)} ```tasks``` fences; the v2 "
            "contract allows exactly one — split the plan into separate "
            "files or merge the tasks into a single fence."
        )

    slug = str(fm["slug"])
    title = str(fm["title"])
    goal = str(fm["goal"])
    scope_tiers_raw = fm["scope_tiers"] or {}
    if isinstance(scope_tiers_raw, dict):
        scope_tiers = {
            str(k): [str(x) for x in (v or [])]
            for k, v in scope_tiers_raw.items()
        }
    else:
        scope_tiers = {}
    risks_raw = fm["risks"] or []
    risks = [str(x) for x in risks_raw] if isinstance(risks_raw, list) else [str(risks_raw)]
    verification_raw = fm["verification"] or []
    verification = (
        [str(x) for x in verification_raw]
        if isinstance(verification_raw, list)
        else [str(verification_raw)]
    )
    tasks, malformed_lines = _parse_tasks_block(text)
    return ParsedPlan(
        slug=slug,
        title=title,
        goal=goal,
        scope_tiers=scope_tiers,
        risks=risks,
        verification=verification,
        created_by=str(fm.get("created_by")) if fm.get("created_by") else None,
        created_at=str(fm.get("created_at")) if fm.get("created_at") else None,
        model=str(fm.get("model")) if fm.get("model") else None,
        provider=str(fm.get("provider")) if fm.get("provider") else None,
        extra={k: v for k, v in fm.items() if k not in _REQUIRED_FRONTMATTER_KEYS},
        tasks=tasks,
        malformed_task_lines=malformed_lines,
    )


def validate(plan: ParsedPlan) -> List[PlanValidationError]:
    """Check parser guarantees from the v2 contract.

    Returns a (possibly empty) list of :class:`PlanValidationError`. An
    empty result means the plan is approvable.

    Detected errors include:

    * ``slug_invalid`` — slug is not lowercase kebab-case.
    * ``title_empty`` / ``goal_empty`` — required frontmatter fields empty.
    * ``no_tasks`` — plan contains no ``- [ ]`` lines.
    * ``duplicate_id`` — same task id appears twice.
    * ``empty_verify`` — ``verify:`` segment present with no command.
    * ``malformed_task_line`` — ``- [ ]`` line that does not match the
      canonical ``T<id>: <title>`` shape (e.g. ``- [ ] T99`` with no
      title, or ``- [ ]`` alone). Previously these silently dropped out
      of the seeded task list.
    * ``dangling_reference`` — ``parent:`` / ``depends:`` references an
      unknown id.
    * ``dependency_cycle`` — parent/depends graph has a cycle (including
      self-cycles like ``T1 -> T1``). Detected before any board mutation
      so a cyclic plan is rejected with no kanban rows left behind.
    """
    errors: List[PlanValidationError] = []
    if _SLUG_RE.fullmatch(plan.slug) is None:
        errors.append(PlanValidationError(
            "slug_invalid",
            "slug must be lowercase kebab-case (letters, digits, single hyphens)",
        ))
    if not plan.title.strip():
        errors.append(PlanValidationError("title_empty", "title must be non-empty"))
    if not plan.goal.strip():
        errors.append(PlanValidationError("goal_empty", "goal must be non-empty"))
    if not plan.tasks:
        errors.append(PlanValidationError(
            "no_tasks",
            "plan must contain at least one '- [ ]' line in a ```tasks``` block",
        ))
    seen_ids: set = set()
    for t in plan.tasks:
        if t.raw_id in seen_ids:
            errors.append(PlanValidationError(
                "duplicate_id",
                f"task id {t.raw_id!r} appears more than once",
                line_no=t.line_no,
            ))
        seen_ids.add(t.raw_id)
        if t.verify is not None and not t.verify.strip():
            errors.append(PlanValidationError(
                "empty_verify",
                f"task {t.raw_id!r} has verify: but no command",
                line_no=t.line_no,
            ))
        if not t.title.strip():
            # ``- [ ] T1: …`` with no title (or only whitespace) used
            # to silently slip past the regex match. Surface it as a
            # structured error so the operator can fix it instead of
            # seeing a half-empty task seeded onto the board.
            errors.append(PlanValidationError(
                "malformed_task_line",
                f"task {t.raw_id!r} has no title (expected '- [ ] {t.raw_id}: <title>')",
                line_no=t.line_no,
            ))
    # [hermes-v2] H-22: surface task-like rows that did not match the
    # canonical regex (``- [ ]`` alone, ``- [ ] T99`` with no title,
    # etc.). The parser records them in ``plan.malformed_task_lines``
    # during ``_parse_tasks_block`` — flipping them into
    # ``PlanValidationError`` here lets ``/plan approve`` abort before
    # any kanban row is committed, with the offending line numbers
    # intact for the operator.
    for bad in plan.malformed_task_lines:
        errors.append(PlanValidationError(
            "malformed_task_line",
            "task-like line did not match '- [ ] T<id>: <title>': "
            f"{bad.text!r}",
            line_no=bad.line_no,
        ))
    for t in plan.tasks:
        for ref in ([t.parent] if t.parent else []) + list(t.depends):
            if ref and ref not in seen_ids and ref != "root":
                errors.append(PlanValidationError(
                    "dangling_reference",
                    f"task {t.raw_id!r} references unknown id {ref!r}",
                    line_no=t.line_no,
                ))
        # [hermes-v2] H-22: self-cycle in the parent's parent: /
        # depends: segment is also flagged. Without this a ``T1:
        # parent: T1`` plan would silently grow a parent link to
        # itself, and ``link_tasks`` raises ValueError only AFTER the
        # root task is already committed — leaving a half-seeded tree
        # in the DB. We pre-validate here so the cycle is reported
        # before any kanban row is touched.
        if t.parent and t.parent == t.raw_id:
            errors.append(PlanValidationError(
                "dependency_cycle",
                f"task {t.raw_id!r} depends on itself (parent: {t.parent!r})",
                line_no=t.line_no,
            ))
        for dep in t.depends:
            if dep == t.raw_id:
                errors.append(PlanValidationError(
                    "dependency_cycle",
                    f"task {t.raw_id!r} depends on itself (depends: {dep!r})",
                    line_no=t.line_no,
                ))

    # [hermes-v2] H-22: detect multi-node parent/depends cycles
    # (``T1 -> T2 -> T3 -> T1`` etc.) before any board mutation. The
    # previous parser+seeder pair only flagged self-cycles and let
    # longer cycles propagate to ``link_tasks`` → ``ValueError``,
    # which the seeder did not catch — partial-insertion board
    # mutations were the visible failure mode. The check runs on
    # the resolved id set (duplicate_id errors are not silently
    # ignored: a cycle that includes a duplicate id is still
    # detected here and surfaces alongside duplicate_id).
    if seen_ids and not any(e.code == "duplicate_id" for e in errors):
        cycle_errors = _detect_dependency_cycles(plan.tasks, seen_ids)
        errors.extend(cycle_errors)

    return errors


def _detect_dependency_cycles(
    tasks: List[PlanTask],
    valid_ids: set,
) -> List[PlanValidationError]:
    """Return one ``dependency_cycle`` error per cycle found in the
    parent/depends graph, or an empty list when the graph is a DAG.

    A *cycle* here is any directed cycle in the union of parent edges
    (parent: T) and depends edges (depends: [T, …]). Self-edges are
    flagged by the per-task loop in :func:`validate` so they always
    appear even when the cycle detector would not emit them.

    Detection strategy: Tarjan-style DFS from every node, marking the
    current recursion stack; a back-edge to a node already on the stack
    closes a cycle. We surface each distinct cycle once via its
    lexicographically smallest member — keeps the user-facing error
    list small and deterministic.
    """
    # Build adjacency list: for each task id, list of task ids it points
    # to. ``parent: root`` is filtered out — root is a syntactic marker
    # the seeder resolves to the root task id, not a graph edge.
    adjacency: Dict[str, List[str]] = {tid: [] for tid in valid_ids}
    for t in tasks:
        if t.raw_id not in adjacency:
            continue
        if t.parent and t.parent in valid_ids and t.parent != t.raw_id:
            adjacency[t.raw_id].append(t.parent)
        for dep in t.depends:
            if dep in valid_ids and dep != t.raw_id:
                adjacency[t.raw_id].append(dep)

    errors: List[PlanValidationError] = []
    seen_cycle_keys: set = set()

    WHITE, GRAY, BLACK = 0, 1, 2
    color: Dict[str, int] = {tid: WHITE for tid in adjacency}
    stack: List[str] = []

    def _visit(node: str) -> None:
        if color.get(node, BLACK) == BLACK:
            return
        if color[node] == GRAY:
            # Back-edge — extract the cycle from the recursion stack.
            try:
                idx = stack.index(node)
            except ValueError:  # pragma: no cover - defensive
                return
            cycle = stack[idx:] + [node]
            key = "|".join(sorted(set(cycle)))
            if key in seen_cycle_keys:
                return
            seen_cycle_keys.add(key)
            # Surface the smallest cycle element as the offender;
            # the full path lets the operator trace the loop.
            errors.append(PlanValidationError(
                "dependency_cycle",
                "dependency cycle detected: " + " -> ".join(cycle),
            ))
            return
        color[node] = GRAY
        stack.append(node)
        for nxt in adjacency.get(node, ()):
            _visit(nxt)
        stack.pop()
        color[node] = BLACK

    for tid in sorted(adjacency):
        if color[tid] == WHITE:
            _visit(tid)

    return errors


def is_v2_plan(text: str) -> bool:
    """Return ``True`` when text starts with a frontmatter envelope.

    Content validity belongs to :func:`parse_plan`; keeping envelope detection
    separate ensures malformed structured plans surface validation errors while
    documents without frontmatter retain the free-form fallback.
    """
    return _FRONTMATTER_RE.match(text) is not None
