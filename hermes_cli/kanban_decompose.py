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

* Lane pinning: If the root task has a concrete PRODUCT-lane assignee
  (ocr/genealogy/qms/graves/brandysamd/reptile) OR its title/body
  starts with ``# <lane>:``, every child is assigned that lane and the
  LLM per-child routing is skipped. This ensures lane-scoped seeds
  keep their children in the same lane.

* Intake exclusion: ``intake`` is NEVER a valid child assignee or
  default fallback. If resolution would land on intake, it is rewritten
  to the parent lane (if pinned) or to a non-intake default.
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
        "body":  "<GOAL / REFS / PROCEDURE / DONE-CONDITION / FAIL headings required; otherwise the child is not minted>",
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
    context. It MUST include the five headings GOAL, REFS, PROCEDURE,
    DONE-CONDITION, FAIL. Without them the child is not minted.
  - Do NOT assign ocr/genealogy/qms/reptile work to brandysamd unless the
    parent task is already on the brandysamd lane. Brandys is not the
    default compute for other lanes.

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
_LANE_MARKER_RE = re.compile(r"^#\s+(\w+):\s*", re.MULTILINE)

# PRODUCT lanes that children may inherit from parent.
PRODUCT_LANES = {"ocr", "genealogy", "qms", "graves", "brandysamd", "reptile"}

# Keywords that indicate a task needs model training/fine-tuning/inference.
# Such tasks should route to brandysamd (compute-capable profile) regardless of parent lane.
# Includes: HTR training, census/OCR runs, embeddings, benchmarks, and AVX-512 work.
MODEL_TRAINING_KEYWORDS = {
    # Original keywords
    "train", "fine-tune", "fine-tuning", "finetune", "finetuning",
    "learned-projection", "learned projection",
    "run-model", "run model", "inference",
    "post-train", "post-training", "post_train", "post_training",
    "model", "gguf", "quantiz", "embedding",
    # NEW (2026-08-25): Census/HTR/compute-heavy keywords
    "census", "loghi", "laypa",
    "avx-512", "avx512",
    "htr train", "train htr",  # HTR training variants
    "bench", "benchmark",
    "extract embedding", "extract embeddings", "extract-embedding", "extract-embeddings",
}
MODEL_TRAINING_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(kw) for kw in MODEL_TRAINING_KEYWORDS) + r")\b",
    re.IGNORECASE
)


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


def _extract_parent_lane(task) -> Optional[str]:
    """Extract the parent lane from the root task assignee or body marker.

    Returns a PRODUCT_LANE name if found, None otherwise.
    Rules:
    - If assignee is a PRODUCT_LANE, return it.
    - If body starts with "# <lane>:", extract and return the lane.
    - Otherwise None (generic triage, no lane pinning).
    """
    # Check assignee is a product lane.
    if task.assignee and task.assignee in PRODUCT_LANES:
        return task.assignee

    # Check body for "# <lane>:" marker.
    if task.body:
        match = _LANE_MARKER_RE.search(task.body)
        if match:
            candidate = match.group(1).lower()
            if candidate in PRODUCT_LANES:
                return candidate

    return None


def _needs_model_training(task_title: str, task_body: str) -> bool:
    """Legacy keyword detector. NOT used for assignee routing.

    Kept so older tests that import the name still collect. Routing ocr/reptile
    work to brandysamd because the title contains 'embedding' was an invented
    default and is disabled.
    """
    combined = f"{task_title or ''}\n{task_body or ''}"
    return bool(MODEL_TRAINING_PATTERN.search(combined))


def _pe_gate_body(body: str) -> tuple[bool, str]:
    """Fail-closed: a child without the 5-field contract is not minted."""
    try:
        import sys
        from pathlib import Path

        scripts = str(Path.home() / ".hermes" / "scripts")
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        from pe_gate import gate_card

        return gate_card(body or "", verify_paths=False)
    except Exception as exc:
        return False, f"pe_gate unavailable: {exc}"


def _resolve_default_assignee_safe(cfg: dict, parent_lane: Optional[str]) -> str:
    """Resolve a non-intake default assignee.

    If the normal resolution lands on 'intake', rewrite to the parent_lane
    (if pinned) or to a safe fallback profile (never 'intake').
    """
    kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    explicit = (kanban_cfg.get("default_assignee") or "").strip()

    # Try explicit config first.
    if explicit and explicit != "intake":
        try:
            if profiles_mod.profile_exists(explicit):
                return explicit
        except Exception:
            pass

    # Try active profile; if it's intake, use parent_lane or default.
    try:
        active = profiles_mod.get_active_profile_name() or "default"
        if active != "intake":
            return active
        # Active is intake; use parent_lane if available.
        if parent_lane:
            return parent_lane
        # Fallback to 'default' (not intake).
        return "default"
    except Exception:
        # Fallback to 'default' (not intake).
        return "default"


def _build_roster() -> tuple[list[dict], set[str]]:
    """Return (roster_for_prompt, valid_assignee_names).

    Each roster entry is ``{name, description, has_description}``. The
    valid-set is used after the LLM responds to rewrite invalid
    assignees to the default fallback.

    Note: 'intake' is excluded from valid assignees as it is never
    a child task assignee (only a seed-stage profile).
    """
    roster: list[dict] = []
    valid: set[str] = set()
    try:
        all_profiles = profiles_mod.list_profiles()
    except Exception as exc:
        logger.warning("decompose: failed to list profiles: %s", exc)
        return roster, valid
    for p in all_profiles:
        # Exclude 'intake' from valid child assignees.
        if p.name == "intake":
            continue
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
    parent_lane: Optional[str] = None,
) -> str:
    """Return a valid assignee, falling back to ``default_assignee``.

    Fan-out children and the single-task fallback should share the same
    routing guarantee: promoted work must not be left unassigned.

    If assignee is 'intake' or invalid, return default_assignee.
    If default_assignee is 'intake', return parent_lane (if set) or 'default'.
    """
    if not isinstance(assignee, str) or not assignee.strip():
        return default_assignee

    chosen = assignee.strip()

    # Reject 'intake' explicitly.
    if chosen == "intake":
        return default_assignee

    # Reject unknown names.
    if chosen not in valid_names:
        return default_assignee

    return chosen


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
    
    The board is determined from HERMES_KANBAN_BOARD env var (set by the
    dispatcher) or resolved via kb.get_current_board(). All child tasks are
    created on the same board as the parent.
    """
    # Determine which board the task is on. The dispatcher sets HERMES_KANBAN_BOARD,
    # but if not available, get_current_board() checks the environment and defaults.
    board = kb.get_current_board()
    
    with kb.connect_closing(board=board) as conn:
        task = kb.get_task(conn, task_id)
    if task is None:
        return DecomposeOutcome(task_id, False, "unknown task id")
    if task.status != "triage":
        return DecomposeOutcome(
            task_id, False, f"task is not in triage (status={task.status!r})"
        )

    cfg = _load_config()
    orchestrator = _resolve_orchestrator_profile(cfg)
    parent_lane = _extract_parent_lane(task)
    default_assignee = _resolve_default_assignee_safe(cfg, parent_lane)
    kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    auto_promote = bool(kanban_cfg.get("auto_promote_children", True))
    roster, valid_names = _build_roster()

    try:
        from agent.auxiliary_client import call_llm  # type: ignore
    except Exception as exc:
        logger.debug("decompose: auxiliary client import failed: %s", exc)
        return DecomposeOutcome(task_id, False, "auxiliary client unavailable")

    user_msg = _USER_TEMPLATE.format(
        task_id=task.id,
        title=_truncate(task.title or "", 400),
        body=_truncate(task.body or "(no body)", 4000),
        roster=_format_roster(roster),
        default_assignee=default_assignee,
    )

    try:
        # Route through call_llm so auxiliary.kanban_decomposer.* config
        # (provider/model/base_url, extra_body, reasoning_effort, retries)
        # all apply — the previous direct client.chat.completions.create()
        # path dropped auxiliary.<task>.extra_body entirely (#35566).
        resp = call_llm(
            task="kanban_decomposer",
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.3,
            max_tokens=4000,
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
        assignee_val = None
        if not task.assignee:
            assignee_val = _normalize_assignee_choice(
                parsed.get("assignee"),
                default_assignee=default_assignee,
                valid_names=valid_names,
                parent_lane=parent_lane,
            )
        if title_val is None and body_val is None:
            return DecomposeOutcome(
                task_id, False, "decomposer returned fanout=false with no title/body",
            )
        with kb.connect_closing(board=board) as conn:
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
    # If parent_lane is pinned, all children inherit it.
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

        # LANE PINNING: If parent_lane is set, ALL children inherit it.
        # Do not hijack ocr/reptile/genealogy children onto brandysamd because
        # the title contains "embedding" or "bench". That invented default was
        # corrected in lane AGENTS.md 2026-08-26 and kept minting wrong cards.
        if parent_lane:
            chosen = parent_lane
        else:
            assignee = entry.get("assignee")
            chosen = _normalize_assignee_choice(
                assignee,
                default_assignee=default_assignee,
                valid_names=valid_names,
                parent_lane=parent_lane,
            )
            if (
                isinstance(assignee, str)
                and assignee.strip()
                and assignee.strip() not in valid_names
                and assignee.strip() != "intake"
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
        })

    pe_fails: list[str] = []
    for idx, ch in enumerate(children):
        ok, fb = _pe_gate_body(ch["body"])
        if not ok:
            pe_fails.append(f"tasks[{idx}] {ch['title']!r}: {fb}")
    if pe_fails:
        return DecomposeOutcome(
            task_id,
            False,
            "PE contract gate rejected gateless children; no cards minted:\n"
            + "\n".join(pe_fails),
        )

    # ROOT-PROMOTION FIX: Ensure the root never lands on intake.
    # If parent_lane is pinned, use it. Otherwise use orchestrator UNLESS
    # it is intake, in which case fall back to default_assignee.
    root_assignee = parent_lane or (orchestrator if orchestrator != "intake" else default_assignee)

    try:
        with kb.connect_closing(board=board) as conn:
            child_ids = kb.decompose_triage_task(
                conn,
                task_id,
                root_assignee=root_assignee,
                children=children,
                author=audit_author,
                auto_promote=auto_promote,
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
    """Return task ids currently in the triage column on the current board."""
    board = kb.get_current_board()
    with kb.connect_closing(board=board) as conn:
        rows = kb.list_tasks(
            conn,
            status="triage",
            tenant=tenant,
            limit=1000,
        )
    return [row.id for row in rows]
