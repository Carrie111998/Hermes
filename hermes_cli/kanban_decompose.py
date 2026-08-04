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

* Capability routing (fleet/agentcard-routing, routing-rules.md): when
  the AgentCard registry is available (``kanban.cards_dir``, default
  ``<hermes-root>/workspace/fleet/cards``), the LLM no longer picks
  assignees — it classifies each task as ``primary_domain`` +
  ``requires_capabilities`` from the catalog, and
  ``hermes_cli.agentcard_router`` deterministically routes by matching
  capabilities against the profile cards (domain guard first, then
  scoring, then tie-breaks). Every machine-routed child produces one
  JSON audit line in ``fleet/routing-audit.log`` and a comment on the
  root task. A missing/unusable registry falls back to the legacy
  description-based routing below (log tag ``AGENTCARD_REGISTRY_EMPTY``).

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
from pathlib import Path
from typing import Optional

from hermes_cli import agentcard_router
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
        "body":  "<detailed spec for the worker on this child task>",
        "primary_domain": "<domain id from the domain catalog, or null>",
        "requires_capabilities": ["<capability ids from the capability catalog>"],
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
  - CLASSIFY, do not assign: for each task emit "primary_domain" (one
    domain id from the domain catalog, or null when nothing fits) and
    "requires_capabilities" (an array of capability ids from the
    capability catalog; empty when nothing applies). The system routes
    deterministically by matching these against the profiles' AgentCards.
    NEVER invent ids — emit only ids present in the catalogs.
  - "assignee" is a legacy override only: emit a profile name from the
    roster ONLY when the task must be pinned to a specific profile. When
    omitted, the machine routes by capability.
  - Each child task body is what a fresh worker will read with no other
    context — be specific about goal, approach, and acceptance criteria.

When the task is genuinely a single unit of work (no useful decomposition),
return:

  {
    "fanout": false,
    "rationale": "<one sentence>",
    "title": "<tightened title>",
    "body":  "<concrete spec for a single worker>",
    "primary_domain": "<domain id from the domain catalog, or null>",
    "requires_capabilities": ["<capability ids from the capability catalog>"]
  }

In that case the task stays as one work item, just with a tightened spec.
The machine routes it by capability; "assignee" is a legacy override
(profile name from the roster) used only when the task must be pinned.

No preamble, no closing remarks, no code fences. Output only the JSON object.
"""


_USER_TEMPLATE = """Task id: {task_id}
Title: {title}
Body:
{body}

Available profiles (roster — only used for legacy "assignee" overrides):
{roster}

Capability catalog (emit ONLY these ids in "requires_capabilities"):
{catalog}

Default assignee (used when routing finds no match): {default_assignee}
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


def _resolve_cards_dir(cfg: dict) -> Optional[Path]:
    """Resolve the AgentCard registry directory (``kanban.cards_dir``).

    Falls back to ``<hermes-root>/workspace/fleet/cards`` when unset. A
    missing/unusable directory is handled at load time with the
    ``AGENTCARD_REGISTRY_EMPTY`` log tag, so this never raises.
    """
    kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    explicit = (kanban_cfg.get("cards_dir") or "").strip()
    if explicit:
        try:
            return Path(explicit).expanduser()
        except Exception:
            return None
    try:
        return agentcard_router.default_cards_dir()
    except Exception:
        return None


def _load_agentcard_registry(
    cards_dir: Optional[Path],
    valid_names: set[str],
) -> agentcard_router.RegistryResult:
    """Stage 0 for the decomposer: load + validate the card registry.

    Cards whose profile is not an installed Hermes profile are excluded by
    the router (a routed winner must be spawnable); warnings are logged with
    their normative log tag (``CARD_INVALID`` / ``AGENTCARD_REGISTRY_EMPTY``).
    Never raises — an unusable registry just means description-based fallback.
    """
    if cards_dir is None:
        return agentcard_router.RegistryResult()
    try:
        result = agentcard_router.load_registry(
            cards_dir,
            known_profiles=valid_names or None,
        )
    except Exception as exc:
        logger.warning("decompose: agentcard registry load failed: %s", exc)
        return agentcard_router.RegistryResult()
    for warning in result.warnings:
        logger.warning("decompose: %s", warning)
    return result


def _format_catalog(cards: dict) -> str:
    """Render the capability/domain catalog the LLM classifies against."""
    if not cards:
        return "  (no agent cards loaded — description-based fallback routing)"
    domains = sorted(
        {
            dom
            for card in cards.values()
            for dom in (card.get("domain_boundaries") or {}).get("owns", [])
            if isinstance(dom, str)
        }
    )
    caps = [
        (cap.get("id"), cap.get("domain"), profile)
        for profile in sorted(cards)
        for cap in (cards[profile].get("capabilities") or [])
        if isinstance(cap, dict) and isinstance(cap.get("id"), str)
    ]
    lines = [f"  Domains: {', '.join(domains)}"]
    lines.append("  Capability ids (id — domain — owning profile):")
    for cid, dom, profile in sorted(caps):
        lines.append(f"    - {cid} ({dom}, {profile})")
    return "\n".join(lines)


def _llm_tie_breaker(
    winners: list[str],
    cards: dict,
    task: dict,
) -> Optional[str]:
    """routing-rules.md stage 3.3 — one bounded LLM call to break a tie.

    Passes each tied candidate's description + capabilities as context and
    asks for exactly one profile name from the tie set. Returns ``None``
    (→ deterministic tie-break) when the auxiliary LLM is unavailable or
    answers with something outside the tie set.
    """
    try:
        from agent.auxiliary_client import call_llm  # type: ignore
    except Exception:
        return None
    roster = "\n".join(
        f"- {p}: {str(cards[p].get('description') or '')[:200]}"
        for p in winners
    )
    prompt = (
        "A routing tie must be broken between these profiles:\n"
        f"{roster}\n\nTask: {str(task.get('title') or '')[:300]}\n\n"
        'Answer with exactly one profile name from the list, as JSON: '
        '{"profile": "<name>"}.'
    )
    try:
        resp = call_llm(
            task="kanban_decomposer",
            messages=[
                {
                    "role": "system",
                    "content": "You break routing ties. Answer with JSON only.",
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
            max_tokens=60,
            timeout=30,
        )
        raw = resp.choices[0].message.content or ""
    except Exception:
        return None
    name: Optional[str] = None
    parsed = _extract_json_blob(raw)
    if isinstance(parsed, dict):
        candidate = parsed.get("profile")
        if isinstance(candidate, str):
            name = candidate.strip()
    if name is None:
        name = raw.strip().strip('"').strip()
    return name if name in winners else None


def _choose_child_assignee(
    entry: dict,
    *,
    task_id: str,
    idx: int,
    default_assignee: str,
    valid_names: set[str],
    cards: dict,
    audits: list[dict],
) -> str:
    """Pick a child's assignee: legacy override, capability routing, or default.

    * Explicit ``assignee`` from the LLM (a valid profile name) is honored as
      a legacy override — backward compatible with pre-AgentCard responses.
    * Otherwise, when the registry is loaded and the LLM classified the task
      (``primary_domain`` / ``requires_capabilities``), the machine routes
      deterministically; the decision is appended to ``audits``.
    * Otherwise the configured ``default_assignee`` catches the child.
    """
    assignee = entry.get("assignee")
    if isinstance(assignee, str) and assignee.strip():
        return _normalize_assignee_choice(
            assignee,
            default_assignee=default_assignee,
            valid_names=valid_names,
        )

    primary_domain = entry.get("primary_domain")
    capabilities = entry.get("requires_capabilities") or []
    if not isinstance(capabilities, list):
        capabilities = []
    capabilities = [c for c in capabilities if isinstance(c, str) and c.strip()]

    if cards and (primary_domain or capabilities):
        routed = False
        try:
            result = agentcard_router.route_task(
                {
                    "task_id": f"{task_id}#{idx}",
                    "title": str(entry.get("title") or ""),
                    "body": str(entry.get("body") or ""),
                    "primary_domain": primary_domain,
                    "requires_capabilities": capabilities,
                },
                cards,
                default_assignee=default_assignee,
                tie_breaker=_llm_tie_breaker,
            )
        except Exception as exc:
            logger.warning(
                "decompose: capability routing failed for child %d: %s", idx, exc,
            )
        else:
            routed = True
            audits.append(result.audit)
            return _normalize_assignee_choice(
                result.winner,
                default_assignee=default_assignee,
                valid_names=valid_names,
            )
        if not routed:
            logger.info(
                "decompose: child %d classified but routing failed — "
                "routing to default_assignee %r",
                idx, default_assignee,
            )
    return default_assignee


def _write_routing_audit(audits: list[dict], *, task_id: str, author: str) -> None:
    """Section 6: append audit lines to ``fleet/routing-audit.log`` and comment.

    Both writes are best-effort — a broken audit trail must not fail the
    decomposition itself. The board comment carries the one-line decision
    (winner, matched_capability_ids, primary_domain) per routed child.
    """
    if not audits:
        return
    audit_log = None
    try:
        cfg = _load_config()
        cards_dir = _resolve_cards_dir(cfg)
        if cards_dir is not None:
            audit_log = agentcard_router.audit_log_path(cards_dir)
    except Exception:
        audit_log = None
    if audit_log is not None:
        try:
            for audit in audits:
                agentcard_router.append_audit_line(audit, audit_log)
        except Exception as exc:
            logger.warning("decompose: routing audit log write failed: %s", exc)
    try:
        lines = [
            "capability routing (winner, primary_domain, "
            "matched_capability_ids, fallback):"
        ]
        for audit in audits:
            matched = ",".join(audit["matched_capability_ids"]) or "-"
            fallback = audit.get("fallback_used") or "-"
            lines.append(
                f"- {audit['title'][:60] or '(untitled)'} -> {audit['winner']} "
                f"(domain={audit['primary_domain']}, matched={matched}, "
                f"fallback={fallback})"
            )
        with kb.connect_closing() as conn:
            kb.add_comment(conn, task_id, author, "\n".join(lines))
    except Exception as exc:
        logger.warning("decompose: routing audit comment write failed: %s", exc)


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
    roster, valid_names = _build_roster()

    # AgentCard capability routing (T5): load + validate the registry once per
    # dispatch. An unusable registry (missing dir, invalid cards, no schema)
    # falls back to description-based routing with AGENTCARD_REGISTRY_EMPTY.
    cards_dir = _resolve_cards_dir(cfg)
    registry = _load_agentcard_registry(cards_dir, valid_names)
    cards = registry.cards
    catalog = _format_catalog(cards)

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
        catalog=catalog,
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
        single_audits: list[dict] = []
        if not task.assignee:
            assignee_val = _choose_child_assignee(
                parsed,
                task_id=task_id,
                idx=0,
                default_assignee=default_assignee,
                valid_names=valid_names,
                cards=cards,
                audits=single_audits,
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
        _write_routing_audit(single_audits, task_id=task_id, author=audit_author)
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
    audits: list[dict] = []
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
        assignee = entry.get("assignee")
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
        chosen = _choose_child_assignee(
            entry,
            task_id=task_id,
            idx=idx,
            default_assignee=default_assignee,
            valid_names=valid_names,
            cards=cards,
            audits=audits,
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

    try:
        with kb.connect_closing() as conn:
            child_ids = kb.decompose_triage_task(
                conn,
                task_id,
                root_assignee=orchestrator,
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

    # Section 6 audit trail: one JSON line per routed child + board comment.
    _write_routing_audit(audits, task_id=task_id, author=audit_author)

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
