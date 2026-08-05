"""Kanban decomposer — fan a triage task out into a graph of child tasks.

Invoked by ``hermes kanban decompose [task_id | --all]`` and the
auto-decompose path in the gateway dispatcher loop. Reads the user's
profile roster (with descriptions) and asks the auxiliary LLM to
return a task graph in JSON. Then atomically creates the children,
links them under the root, and flips the root ``triage -> todo``.

The root task stays alive and becomes the parent of every leaf child,
so when the whole graph completes the root wakes back up — its
assignee (the orchestrator profile) gets a chance to judge completion
and add more tasks if the work isn't done yet.

Design notes
------------

* Mirrors the shape of ``hermes_cli/kanban_specify.py``: lazy aux
  client import inside the function, lenient response parse, never
  raises on expected failure modes.

* The system prompt sees the *configured* profile roster — names plus
  descriptions plus the default fallback. Profiles without a
  description are still listed (with a note) so the decomposer can
  match on name as a fallback, but the user has an obvious incentive
  to describe them.

* ``fanout=false`` collapses to the same effect as ``kanban specify``:
  we tighten the body and flip ``triage -> todo`` as a single task,
  no children created. This makes ``decompose`` a strict superset of
  ``specify`` from the user's perspective.

* If the LLM picks an assignee that doesn't exist as a profile, we
  rewrite it to the configured ``default_assignee`` (or the default
  profile if unset). A child task NEVER ends up with ``assignee=None``.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

from hermes_cli import kanban_db as kb
from hermes_cli import profiles as profiles_mod

logger = logging.getLogger(__name__)


# The native decomposition call is intentionally a fresh envelope at every
# level. A recursive child must not inherit the remaining excerpt budget of its
# parent plan; the whole child body is the next call's input, capped once at the
# same external 4k boundary.
NATIVE_DECOMPOSER_ENVELOPE_CHARS = 4000
_RECURSION_POLICY_RE = re.compile(r"(?m)^\s*R=(0|1);T=(\d+);")
_CHILD_MARKER_RE = re.compile(r"(?m)^LINGUAL_ADMITTED_CHILD_V1 (\{[^\n]+\})$")
_RECURSIVE_BODY_MARKER = "Recursive decomposition metadata:"


_SYSTEM_PROMPT = """You are the Kanban decomposer for the Hermes Agent board.

A user dropped a rough idea into the Triage column. Your job is to break it
into a small graph of concrete child tasks and route each one to the best-
matching profile from the available roster.

You will be given:
  - The original task title and body
  - The list of available profiles (each with name + description)
  - The fallback "default_assignee" used when no profile fits

Output a single JSON object with this exact shape:

  {
    "fanout": true,
    "rationale": "<one sentence on why this decomposition>",
    "tasks": [
      {
        "title": "<concrete task title, imperative voice, <= 80 chars>",
        "body":  "<detailed spec for the worker on this child task>",
        "assignee": "<profile name from the roster, or null for default>",
        "parents": [<int>, ...]
      },
      ...
    ]
  }

Rules:
  - "parents" is a list of INDICES (0-based) into this same "tasks" list,
    expressing actual data dependencies. Tasks with no parents run in
    PARALLEL. Tasks with parents wait until every parent completes.
  - Prefer parallelism. If two tasks can be done independently, give
    them no parents so the dispatcher fans them out at once.
  - Use 2-6 tasks for normal work. Don't create 20 tiny tasks. Don't
    cram everything into 1 task.
  - Pick assignees from the roster by matching the task to the profile's
    DESCRIPTION (not just the name). When nothing matches well, use null
    and the system will route to the default_assignee.
  - Each child task body is what a fresh worker will read with no other
    context — be specific about goal, approach, and acceptance criteria.

When the task is genuinely a single unit of work (no useful decomposition),
return:

  {
    "fanout": false,
    "rationale": "<one sentence>",
    "title": "<tightened title>",
    "body":  "<concrete spec for a single worker>",
    "assignee": "<profile name from the roster, or null for default>"
  }

In that case the task stays as one work item, just with a tightened spec and
a concrete assignee. If no profile fits, use null and the system will route to
the default_assignee.

No preamble, no closing remarks, no code fences. Output only the JSON object.
"""


_USER_TEMPLATE = """Task id: {task_id}
Title: {title}
Body:
{body}

Available profiles (assignees you may pick from):
{roster}

Default assignee (used when no profile fits a task): {default_assignee}
"""


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


@dataclass
class DecomposeOutcome:
    """Result of decomposing a single triage task."""

    task_id: str
    ok: bool
    reason: str = ""
    fanout: bool = False
    child_ids: list[str] | None = None
    new_title: Optional[str] = None


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _extract_json_blob(raw: str) -> Optional[dict]:
    if not raw:
        return None
    stripped = _FENCE_RE.sub("", raw.strip())
    first = stripped.find("{")
    last = stripped.rfind("}")
    if first == -1 or last == -1 or last <= first:
        return None
    candidate = stripped[first : last + 1]
    try:
        val = json.loads(candidate)
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(val, dict):
        return None
    return val


def _profile_author() -> str:
    """Mirror of ``hermes_cli.kanban._profile_author``."""
    return (
        os.environ.get("HERMES_PROFILE")
        or os.environ.get("USER")
        or "decomposer"
    )


def _load_config() -> dict:
    try:
        from hermes_cli.config import load_config
        return load_config() or {}
    except Exception:
        return {}


def _resolve_orchestrator_profile(cfg: dict) -> str:
    """Resolve which profile owns the root/orchestration task after fan-out.

    Falls back to the active default profile when ``kanban.orchestrator_profile``
    is unset, so a task is never stranded for lack of an orchestrator.
    """
    kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    explicit = (kanban_cfg.get("orchestrator_profile") or "").strip()
    if explicit:
        try:
            if profiles_mod.profile_exists(explicit):
                return explicit
        except Exception:
            pass
    # Fall back to the active default profile.
    try:
        return profiles_mod.get_active_profile_name() or "default"
    except Exception:
        return "default"


def _resolve_default_assignee(cfg: dict) -> str:
    """Resolve which profile catches child tasks the orchestrator can't route."""
    kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    explicit = (kanban_cfg.get("default_assignee") or "").strip()
    if explicit:
        try:
            if profiles_mod.profile_exists(explicit):
                return explicit
        except Exception:
            pass
    try:
        return profiles_mod.get_active_profile_name() or "default"
    except Exception:
        return "default"


def _build_roster() -> tuple[list[dict], set[str]]:
    """Return (roster_for_prompt, valid_assignee_names).

    Each roster entry is ``{name, description, has_description}``. The
    valid-set is used after the LLM responds to rewrite invalid
    assignees to the default fallback.
    """
    roster: list[dict] = []
    valid: set[str] = set()
    try:
        all_profiles = profiles_mod.list_profiles()
    except Exception as exc:
        logger.warning("decompose: failed to list profiles: %s", exc)
        return roster, valid
    for p in all_profiles:
        desc = (p.description or "").strip()
        roster.append({
            "name": p.name,
            "description": desc or f"(no description; profile named {p.name!r})",
            "has_description": bool(desc),
        })
        valid.add(p.name)
    return roster, valid


def _format_roster(roster: list[dict]) -> str:
    if not roster:
        return "  (no profiles installed — decomposer cannot route work)"
    lines = []
    for entry in roster:
        tag = "" if entry["has_description"] else " ⚠ undescribed"
        lines.append(f"  - {entry['name']}{tag}: {entry['description']}")
    return "\n".join(lines)


def _normalize_assignee_choice(
    assignee: object,
    *,
    default_assignee: str,
    valid_names: set[str],
) -> str:
    """Return a valid assignee, falling back to ``default_assignee``.

    Fan-out children and the single-task fallback should share the same
    routing guarantee: promoted work must not be left unassigned.
    """
    if not isinstance(assignee, str) or not assignee.strip():
        return default_assignee
    chosen = assignee.strip()
    if chosen not in valid_names:
        return default_assignee
    return chosen


def _frozen_plan_digest(body: Optional[str]) -> Optional[str]:
    """Extract the single admitted frozen-plan binding from a task body."""
    if not isinstance(body, str):
        return None
    matches = kb.FROZEN_PLAN_DIGEST_RE.findall(body)
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError("frozen-plan digest mismatch: body has multiple bindings")
    return matches[0]


def _recursive_policy(task: kb.Task) -> tuple[bool, int]:
    """Read the persisted recursion decision, with a legacy body fallback."""
    if task.recursion_enabled is not None:
        enabled = bool(task.recursion_enabled)
        threshold = task.recursion_trigger_chars
        return enabled, int(threshold) if threshold is not None else 400
    match = _RECURSION_POLICY_RE.search(task.body or "")
    if match is None:
        return False, 400
    return match.group(1) == "1", int(match.group(2))


def _marker_metadata(body: str) -> dict:
    """Read optional recursive fields from the admitted-child body marker."""
    matches = list(_CHILD_MARKER_RE.finditer(body))
    if not matches:
        return {}
    if len(matches) != 1:
        raise ValueError("recursive child body has multiple admitted-child markers")
    try:
        marker = json.loads(matches[0].group(1))
    except json.JSONDecodeError as exc:
        raise ValueError("recursive child body marker is malformed") from exc
    if not isinstance(marker, dict):
        raise ValueError("recursive child body marker is malformed")
    return marker


def _thread_recursive_body(
    body: str,
    *,
    root_digest: str,
    depth: int,
    plan_item_index: Optional[int],
    recursion_trigger_chars: int,
) -> str:
    """Carry the root binding and per-level metadata into a child body."""
    existing_digest = _frozen_plan_digest(body)
    if existing_digest is not None and existing_digest != root_digest:
        raise ValueError("frozen-plan digest mismatch")
    if existing_digest is None:
        body = (
            f"{body.rstrip()}\nFrozen artifact: {root_digest}"
            if body.strip()
            else f"Frozen artifact: {root_digest}"
        )
    marker_matches = list(_CHILD_MARKER_RE.finditer(body))
    if marker_matches:
        marker = _marker_metadata(body)
        marker.setdefault("depth", depth)
        if plan_item_index is not None:
            marker.setdefault("plan_item_index", plan_item_index)
        replacement = (
            "LINGUAL_ADMITTED_CHILD_V1 "
            + json.dumps(marker, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        )
        match = marker_matches[0]
        body = body[: match.start()] + replacement + body[match.end() :]
    metadata_line = (
        f"{_RECURSIVE_BODY_MARKER} depth={depth}; "
        f"plan_item_index={plan_item_index if plan_item_index is not None else 'none'}; "
        f"root_frozen_plan_digest={root_digest}; "
        f"envelope_chars={NATIVE_DECOMPOSER_ENVELOPE_CHARS}; "
        f"trigger_chars={recursion_trigger_chars}"
    )
    if _RECURSIVE_BODY_MARKER not in body:
        body = f"{body.rstrip()}\n{metadata_line}"
    return body


def _recursive_child_metadata(
    task: kb.Task,
    entry: dict,
    body: str,
    *,
    root_digest: str,
    recursion_trigger_chars: int,
) -> tuple[str, int, Optional[int]]:
    """Bind recursive child metadata to its parent and frozen root.

    The bridge may carry metadata both in the JSON envelope and in the
    ``LINGUAL_ADMITTED_CHILD_V1`` body marker. Treat disagreement as a hard
    failure: otherwise the DB columns and the receipt body describe different
    trees.
    """
    marker = _marker_metadata(body)
    marker_digest = marker.get("root_frozen_plan_digest")
    if marker_digest is not None and marker_digest != root_digest:
        raise ValueError("frozen-plan digest mismatch")
    declared_digest = entry.get("root_frozen_plan_digest")
    if declared_digest is not None and declared_digest != root_digest:
        raise ValueError("frozen-plan digest mismatch")

    def metadata_int(name: str, *, non_negative: bool = False) -> Optional[int]:
        value = entry.get(name)
        marker_value = marker.get(name)
        if value is None:
            value = marker_value
        elif marker_value is not None and marker_value != value:
            raise ValueError(f"recursive child {name} mismatch")
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"recursive child {name} must be an integer")
        if (non_negative and value < 0) or (not non_negative and value < 1):
            qualifier = "non-negative" if non_negative else "positive"
            raise ValueError(f"recursive child {name} must be {qualifier}")
        return value

    depth = metadata_int("depth")
    is_recursive_call = task.plan_item_index is not None
    if depth is None:
        depth = task.depth + 1 if is_recursive_call else 1
    expected_depth = task.depth + 1 if is_recursive_call else None
    if expected_depth is not None and depth != expected_depth:
        raise ValueError(
            f"recursive child depth mismatch: expected {expected_depth}, got {depth}"
        )
    if depth > 2:
        raise ValueError("recursive child depth exceeds 2")

    plan_item_index = metadata_int("plan_item_index", non_negative=True)
    if is_recursive_call:
        if plan_item_index is None:
            plan_item_index = task.plan_item_index
        if plan_item_index != task.plan_item_index:
            raise ValueError("recursive child plan_item_index mismatch")

    if plan_item_index is None:
        raise ValueError("recursive child plan_item_index is required")
    body = _thread_recursive_body(
        body,
        root_digest=root_digest,
        depth=depth,
        plan_item_index=plan_item_index,
        recursion_trigger_chars=recursion_trigger_chars,
    )
    return body, depth, plan_item_index


def decompose_task(
    task_id: str,
    *,
    author: Optional[str] = None,
    timeout: Optional[int] = None,
) -> DecomposeOutcome:
    """Decompose a triage task into a graph of child tasks.

    Returns an outcome describing what happened. Never raises for
    expected failure modes (task not in triage, no aux client
    configured, API error, malformed response, decomposer returned
    fanout=true with empty task list) — those surface via ``ok=False``.
    """
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
    if task is None:
        return DecomposeOutcome(task_id, False, "unknown task id")
    if task.status != "triage":
        return DecomposeOutcome(
            task_id, False, f"task is not in triage (status={task.status!r})"
        )

    cfg = _load_config()
    orchestrator = _resolve_orchestrator_profile(cfg)
    default_assignee = _resolve_default_assignee(cfg)
    kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    auto_promote = bool(kanban_cfg.get("auto_promote_children", True))
    recursive_enabled, recursion_trigger_chars = _recursive_policy(task)
    root_digest: Optional[str] = None
    if recursive_enabled:
        try:
            root_digest = _frozen_plan_digest(task.body)
        except ValueError as exc:
            return DecomposeOutcome(task_id, False, str(exc))
        if root_digest is None:
            return DecomposeOutcome(
                task_id,
                False,
                "recursive decomposition requires a root frozen-plan digest",
            )
    roster, valid_names = _build_roster()

    try:
        from agent.auxiliary_client import call_llm  # type: ignore
    except Exception as exc:
        logger.debug("decompose: auxiliary client import failed: %s", exc)
        return DecomposeOutcome(task_id, False, "auxiliary client unavailable")

    user_msg = _USER_TEMPLATE.format(
        task_id=task.id,
        title=_truncate(task.title or "", 400),
        body=_truncate(
            task.body or "(no body)",
            NATIVE_DECOMPOSER_ENVELOPE_CHARS,
        ),
        roster=_format_roster(roster),
        default_assignee=default_assignee,
    )

    try:
        # Route through call_llm so auxiliary.kanban_decomposer.* config
        # (provider/model/base_url, extra_body, reasoning_effort, retries)
        # all apply — the previous direct client.chat.completions.create()
        # path dropped auxiliary.<task>.extra_body entirely (#35566).
        system_prompt = _SYSTEM_PROMPT
        if recursive_enabled:
            system_prompt += (
                "\nRecursive mode is enabled for this admitted root. Every child "
                "must carry integer `depth` and `plan_item_index` fields. A "
                "depth-1 partition owns its plan item; recursive descendants "
                "inherit that same plan_item_index. Copy exactly one `Frozen "
                "artifact: <root digest>` line into every child body and never "
                "change the root digest. The root receives a fresh 4,000-character "
                "decomposition envelope at every recursive call; do not split "
                "this child body using the parent's remaining allocation.\n"
            )
        resp = call_llm(
            task="kanban_decomposer",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.3,
            max_tokens=NATIVE_DECOMPOSER_ENVELOPE_CHARS,
            timeout=timeout or 180,
        )
    except Exception as exc:
        logger.info(
            "decompose: API call failed for %s (%s)", task_id, exc,
        )
        return DecomposeOutcome(task_id, False, f"LLM error: {type(exc).__name__}")

    try:
        raw = resp.choices[0].message.content or ""
    except Exception:
        raw = ""

    parsed = _extract_json_blob(raw)
    if parsed is None:
        return DecomposeOutcome(task_id, False, "LLM returned malformed JSON")

    fanout = bool(parsed.get("fanout"))
    audit_author = author or _profile_author()

    if not fanout:
        # Fall back to single-task spec promotion (same effect as specify).
        new_title = parsed.get("title")
        new_body = parsed.get("body")
        title_val = new_title.strip() if isinstance(new_title, str) and new_title.strip() else None
        body_val = new_body if isinstance(new_body, str) and new_body.strip() else None
        if recursive_enabled and body_val is not None:
            try:
                body_val = _thread_recursive_body(
                    body_val,
                    root_digest=root_digest or "",
                    depth=task.depth,
                    plan_item_index=task.plan_item_index,
                    recursion_trigger_chars=recursion_trigger_chars,
                )
            except ValueError as exc:
                return DecomposeOutcome(task_id, False, str(exc))
        assignee_val = None
        if not task.assignee:
            assignee_val = _normalize_assignee_choice(
                parsed.get("assignee"),
                default_assignee=default_assignee,
                valid_names=valid_names,
            )
        if title_val is None and body_val is None:
            return DecomposeOutcome(
                task_id, False, "decomposer returned fanout=false with no title/body",
            )
        with kb.connect_closing() as conn:
            ok = kb.specify_triage_task(
                conn,
                task_id,
                title=title_val,
                body=body_val,
                assignee=assignee_val,
                author=audit_author,
            )
        if not ok:
            return DecomposeOutcome(
                task_id, False, "task moved out of triage before promotion",
            )
        return DecomposeOutcome(
            task_id, True, "single task (no fanout)",
            fanout=False, new_title=title_val,
        )

    raw_tasks = parsed.get("tasks") or []
    if not isinstance(raw_tasks, list) or not raw_tasks:
        return DecomposeOutcome(
            task_id, False, "decomposer returned fanout=true with empty tasks list",
        )

    # Rewrite invalid assignees to the default fallback. Never leave a
    # task with assignee=None — the user explicitly does not want that.
    children: list[dict] = []
    for idx, entry in enumerate(raw_tasks):
        if not isinstance(entry, dict):
            return DecomposeOutcome(
                task_id, False, f"tasks[{idx}] is not an object",
            )
        title = entry.get("title")
        if not isinstance(title, str) or not title.strip():
            return DecomposeOutcome(
                task_id, False, f"tasks[{idx}].title is missing or empty",
            )
        body = entry.get("body")
        if not isinstance(body, str):
            body = ""
        depth: Optional[int] = None
        plan_item_index: Optional[int] = None
        if recursive_enabled:
            try:
                body, depth, plan_item_index = _recursive_child_metadata(
                    task,
                    entry,
                    body,
                    root_digest=root_digest or "",
                    recursion_trigger_chars=recursion_trigger_chars,
                )
            except ValueError as exc:
                return DecomposeOutcome(task_id, False, str(exc))
        assignee = entry.get("assignee")
        chosen = _normalize_assignee_choice(
            assignee,
            default_assignee=default_assignee,
            valid_names=valid_names,
        )
        if (
            isinstance(assignee, str)
            and assignee.strip()
            and assignee.strip() not in valid_names
        ):
            logger.info(
                "decompose: task %s child %d picked unknown assignee %r — "
                "routing to default_assignee %r",
                task_id, idx, assignee, default_assignee,
            )
        parents = entry.get("parents") or []
        if not isinstance(parents, list):
            parents = []
        # Clean parent indices: drop non-int and out-of-range.
        clean_parents = [p for p in parents if isinstance(p, int) and 0 <= p < len(raw_tasks) and p != idx]
        children.append({
            "title": title.strip()[:200],
            "body": body.strip(),
            "assignee": chosen,
            "parents": clean_parents,
            **(
                {
                    "depth": depth,
                    "plan_item_index": plan_item_index,
                    "recursion_enabled": recursive_enabled,
                    "recursion_trigger_chars": recursion_trigger_chars,
                }
                if recursive_enabled
                else {}
            ),
        })

    try:
        with kb.connect_closing() as conn:
            child_ids = kb.decompose_triage_task(
                conn,
                task_id,
                root_assignee=orchestrator,
                children=children,
                author=audit_author,
                auto_promote=auto_promote,
                root_frozen_plan_digest=root_digest if recursive_enabled else None,
            )
    except ValueError as exc:
        return DecomposeOutcome(task_id, False, f"DB rejected graph: {exc}")
    except Exception as exc:
        logger.exception("decompose: DB error on task %s", task_id)
        return DecomposeOutcome(task_id, False, f"DB error: {type(exc).__name__}")

    if child_ids is None:
        return DecomposeOutcome(
            task_id, False, "task moved out of triage before decomposition",
        )

    return DecomposeOutcome(
        task_id, True, f"decomposed into {len(child_ids)} children",
        fanout=True, child_ids=child_ids,
    )


def list_triage_ids(*, tenant: Optional[str] = None) -> list[str]:
    """Return task ids currently in the triage column."""
    with kb.connect_closing() as conn:
        rows = kb.list_tasks(
            conn,
            status="triage",
            tenant=tenant,
            limit=1000,
        )
    return [row.id for row in rows]
