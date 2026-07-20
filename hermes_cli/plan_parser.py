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
3. Each task line: ``- [ ] T<n>: <Title> | skill: <s> | verify: <cmd>``
   where ``<n>`` is a stable integer, and ``skill:`` + ``verify:`` are
   optional but recommended.
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
    line_no: int = 0  # 1-based line within the tasks block body


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
    tasks block. Returns ``({}, 0)`` if the frontmatter is absent.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, 0
    parsed = yaml.safe_load(m.group("fm")) or {}
    if not isinstance(parsed, dict):
        # Frontmatter wasn't a mapping — treat as malformed.
        raise ValueError("frontmatter is not a YAML mapping")
    return parsed, m.end()


def _parse_tasks_block(text: str) -> List[PlanTask]:
    """Extract and parse the ``tasks`` fenced block."""
    m = _TASKS_FENCE_RE.search(text)
    if not m:
        return []
    body = m.group("body")
    tasks: List[PlanTask] = []
    for offset, raw_line in enumerate(body.split("\n"), start=1):
        line = raw_line.rstrip()
        m2 = _TASK_LINE_RE.match(line)
        if not m2:
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
                # and the bracket form ``[T1, T2]`` — the latter is what
                # the SKILL.md examples use.
                cleaned = value.strip()
                if cleaned.startswith("[") and cleaned.endswith("]"):
                    cleaned = cleaned[1:-1]
                task.depends = [t.strip() for t in cleaned.split(",") if t.strip()]
        tasks.append(task)
    return tasks


def parse_plan(text: str) -> ParsedPlan:
    """Parse a v2 plan string into a :class:`ParsedPlan`.

    Raises :class:`ValueError` if the frontmatter is absent (callers
    should treat that as "not a v2 plan" and fall back to free-form
    handling — see :func:`is_v2_plan`). Missing *required* keys raise
    too — see :func:`validate` for soft checks that surface them as
    :class:`PlanValidationError` instead.
    """
    fm, _ = _parse_frontmatter(text)
    if not fm:
        raise ValueError("no YAML frontmatter found — not a v2 plan")
    try:
        slug = str(fm["slug"])
        title = str(fm["title"])
        goal = str(fm["goal"])
    except KeyError as e:
        raise ValueError(f"missing required frontmatter key: {e.args[0]}") from None
    scope_tiers_raw = fm.get("scope_tiers") or {}
    if isinstance(scope_tiers_raw, dict):
        scope_tiers = {
            str(k): [str(x) for x in (v or [])]
            for k, v in scope_tiers_raw.items()
        }
    else:
        scope_tiers = {}
    risks_raw = fm.get("risks") or []
    risks = [str(x) for x in risks_raw] if isinstance(risks_raw, list) else [str(risks_raw)]
    verification_raw = fm.get("verification") or []
    verification = (
        [str(x) for x in verification_raw]
        if isinstance(verification_raw, list)
        else [str(verification_raw)]
    )
    tasks = _parse_tasks_block(text)
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
    )


def validate(plan: ParsedPlan) -> List[PlanValidationError]:
    """Check parser guarantees from the v2 contract.

    Returns a (possibly empty) list of :class:`PlanValidationError`. An
    empty result means the plan is approvable.
    """
    errors: List[PlanValidationError] = []
    if not plan.slug or not all(c.isalnum() or c in "-_" for c in plan.slug):
        errors.append(PlanValidationError(
            "slug_invalid",
            "slug must be URL-safe (lowercase, digits, hyphens, underscores)",
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
    for t in plan.tasks:
        for ref in ([t.parent] if t.parent else []) + list(t.depends):
            if ref and ref not in seen_ids and ref != "root":
                errors.append(PlanValidationError(
                    "dangling_reference",
                    f"task {t.raw_id!r} references unknown id {ref!r}",
                    line_no=t.line_no,
                ))
    return errors


def is_v2_plan(text: str) -> bool:
    """Cheap check: ``True`` iff the text starts with valid YAML frontmatter.

    Useful for H-22's free-form fallback: if ``is_v2_plan(body)`` is
    False, route to ``decompose_task`` instead of the structured parser.

    Strict version — only returns True when the frontmatter actually
    parses as a YAML mapping. ``---\\nfoo\\n---\\n`` (where ``foo``
    isn't a mapping) is treated as NOT a v2 plan.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return False
    try:
        parsed = yaml.safe_load(m.group("fm"))
    except yaml.YAMLError:
        return False
    return isinstance(parsed, dict) and bool(parsed)
