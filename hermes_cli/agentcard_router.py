"""AgentCard capability router — the machine half of "the LLM classifies, the machine routes".

Implements ``fleet/routing-rules.md`` (design task t_03690cc0) stages 0-3 for the
single-dispatcher orchestrator:

* **Stage 0** — load ``cards/*.json`` from the fleet workspace at dispatch time and
  validate each card against ``agentcard.schema.json``. Invalid cards are excluded
  (tag ``CARD_INVALID``); a registry with no valid card falls back to
  description-based routing (tag ``AGENTCARD_REGISTRY_EMPTY``).
* **Stage 1** — domain guard. The task's primary domain is resolved from its
  declared fields, and the guard table (routing-rules.md section 4.1) adds the
  domain owner with a +inf bonus no score can beat; cards whose
  ``domain_boundaries.forbidden`` contains the primary domain are removed before
  scoring; a guarded domain whose owner card is missing/invalid still routes to
  the owner's *name* from the table (tag ``GUARD_FALLBACK_BY_NAME``) — the table is
  the Bolsotron of routing, domain boundaries are not sacrificed for registry
  convenience.
* **Stage 2** — capability scoring: +3 per exact ``requires_capabilities`` match,
  +1 per same-domain capability, +1 per keyword hit (max +3 per card). Declared
  capability ids unknown to the registry route to ``default_assignee``
  (tag ``CAPABILITY_UNKNOWN``).
* **Stage 3** — selection with tie-breaks: domain owner first, then an optional
  LLM disambiguation (tag ``TIE_LLM``), then ``authority`` and lexicographic
  profile name (tag ``TIE_DETERMINISTIC``).
* **Stage 4** — no candidate scores > 0 and no guard bonus → ``default_assignee``
  (tag ``NO_CANDIDATE``).

Every decision is returned as a section-6 audit dict and can be appended as one
JSON line to ``fleet/routing-audit.log`` via :func:`append_audit_line` (append
only, never truncate).

The module is deliberately pure (no kanban DB, no profile lookup, no network):
the orchestrator decides *when* to route and *what* to do with the winner; this
module decides *who wins* and why. Card validation uses ``jsonschema``
(draft 2020-12); the schema file ships in the fleet workspace next to the cards
directory (``<cards_dir>/../agentcard.schema.json`` by default).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# routing-rules.md section 4.1 — normative guard table. This is the mirror of
# the cards' domain_boundaries.owns and the last line of defense: when an owner
# card is missing or invalid, the table still routes the domain to its profile.
GUARD_TABLE: dict[str, str] = {
    "design": "arquiteto",
    "implementation": "coder",
    "qa": "qa-browser",
    "security": "security",
    "research": "researcher",
    "ops": "caretaker",
    "triage": "gestor",
    "invention": "inventor",
}

# Log tags (routing-rules.md section 11 — the normative failure vocabulary).
TAG_REGISTRY_EMPTY = "AGENTCARD_REGISTRY_EMPTY"
TAG_CARD_INVALID = "CARD_INVALID"
TAG_GUARD_FALLBACK = "GUARD_FALLBACK_BY_NAME"
TAG_CAPABILITY_UNKNOWN = "CAPABILITY_UNKNOWN"
TAG_NO_CANDIDATE = "NO_CANDIDATE"
TAG_TIE_LLM = "TIE_LLM"
TAG_TIE_DETERMINISTIC = "TIE_DETERMINISTIC"
TAG_OVERRIDE = "OVERRIDE_BY_ASSIGNEE"

# Example cards are templates, not fleet members (validate_cards.py agrees).
_SKIP_CARD_FILES = {"example.agentcard.json"}

TieBreaker = Callable[[list[str], dict, dict], Optional[str]]
"""``(tied_profiles, cards, task) -> profile name from the tie set, or None``."""


@dataclass
class RegistryResult:
    """Outcome of loading + validating a card registry (never raises)."""

    cards: dict[str, dict] = field(default_factory=dict)
    """Valid cards keyed by profile name. An invalid card is simply absent."""

    warnings: list[str] = field(default_factory=list)
    """Human-readable warnings; every one is prefixed with its log tag."""

    @property
    def empty(self) -> bool:
        return not self.cards


@dataclass
class RouteResult:
    """A routing decision: winner profile plus the section-6 audit dict."""

    winner: str
    audit: dict


def default_cards_dir() -> Path:
    """``<hermes-root>/workspace/fleet/cards`` — the canonical fleet layout."""
    try:
        from hermes_constants import get_default_hermes_root
        return Path(get_default_hermes_root()) / "workspace" / "fleet" / "cards"
    except Exception:  # pragma: no cover - defensive; root resolution never fails
        return Path.home() / ".hermes" / "workspace" / "fleet" / "cards"


def audit_log_path(cards_dir: Path) -> Path:
    """``fleet/routing-audit.log`` — one JSON line per decision (section 6)."""
    return Path(cards_dir).parent / "routing-audit.log"


def _schema_path_for(cards_dir: Path) -> Path:
    return Path(cards_dir).parent / "agentcard.schema.json"


def load_registry(
    cards_dir: Path,
    schema_path: Optional[Path] = None,
    *,
    known_profiles: Optional[set[str]] = None,
) -> RegistryResult:
    """Stage 0 — load and validate every ``cards/*.json`` in ``cards_dir``.

    * Every card is validated against the schema (draft 2020-12). Invalid cards
      are excluded with a ``CARD_INVALID`` warning — a broken card must not
      brick dispatch.
    * A missing schema file means the registry cannot be trusted: every card is
      excluded and ``AGENTCARD_REGISTRY_EMPTY`` is reported.
    * Registry invariants from routing-rules.md section 5.0.3: a capability id
      declared by more than one card excludes all declaring cards; a domain
      owned by more than one card keeps only the highest-``authority`` owner.
    * When ``known_profiles`` is given (the orchestrator's installed-profile
      set), cards for unknown profiles are excluded — a routed winner must be a
      spawnable Hermes profile.

    Never raises: every failure mode is folded into ``RegistryResult.warnings``.
    """
    result = RegistryResult()
    cards_dir = Path(cards_dir)
    if not cards_dir.is_dir():
        result.warnings.append(
            f"{TAG_REGISTRY_EMPTY}: cards dir {cards_dir} does not exist — "
            "falling back to description-based routing"
        )
        return result

    schema = _load_schema(schema_path or _schema_path_for(cards_dir))
    if schema is None:
        result.warnings.append(
            f"{TAG_REGISTRY_EMPTY}: schema file {_schema_path_for(cards_dir)} "
            "missing or unparseable — cards cannot be validated, falling back "
            "to description-based routing"
        )
        return result

    validator = None
    try:
        from jsonschema import Draft202012Validator
        validator = Draft202012Validator(schema)
    except Exception as exc:
        logger.debug("agentcard: jsonschema unavailable (%s)", exc)
        validator = None

    raw_cards: dict[str, tuple[Path, dict]] = {}
    for path in sorted(cards_dir.glob("*.json")):
        if path.name in _SKIP_CARD_FILES:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            result.warnings.append(
                f"{TAG_CARD_INVALID}: {path.name} unreadable/not JSON ({exc}) — excluded"
            )
            continue
        if not isinstance(data, dict):
            result.warnings.append(
                f"{TAG_CARD_INVALID}: {path.name} is not a JSON object — excluded"
            )
            continue
        profile = data.get("profile")
        if not isinstance(profile, str) or not profile.strip():
            result.warnings.append(
                f"{TAG_CARD_INVALID}: {path.name} has no 'profile' string — excluded"
            )
            continue
        if known_profiles is not None and profile not in known_profiles:
            result.warnings.append(
                f"{TAG_CARD_INVALID}: {path.name} declares profile {profile!r} "
                "which is not installed — excluded"
            )
            continue
        if validator is not None:
            errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
            if errors:
                first = errors[0]
                where = "/".join(str(p) for p in first.path) or "$"
                result.warnings.append(
                    f"{TAG_CARD_INVALID}: {path.name} fails schema at {where}: "
                    f"{first.message[:120]} — excluded"
                )
                continue
        raw_cards[profile] = (path, data)

    # Registry invariant 1: a capability id declared by >1 card excludes all
    # declaring cards.
    by_cap: dict[str, list[str]] = {}
    for profile, (_, card) in raw_cards.items():
        for cap in card.get("capabilities", []) or []:
            cid = cap.get("id") if isinstance(cap, dict) else None
            if isinstance(cid, str):
                by_cap.setdefault(cid, []).append(profile)
    for cid, holders in by_cap.items():
        if len(holders) > 1:
            for profile in holders:
                raw_cards.pop(profile, None)
            result.warnings.append(
                f"{TAG_CARD_INVALID}: capability {cid!r} declared by "
                f"{', '.join(sorted(holders))} — all excluded (duplicate id)"
            )

    # Registry invariant 2: a domain owned by >1 card keeps only the
    # highest-authority owner; the losers are excluded for that domain.
    by_domain: dict[str, list[str]] = {}
    for profile, (_, card) in raw_cards.items():
        for dom in (card.get("domain_boundaries") or {}).get("owns", []) or []:
            if isinstance(dom, str):
                by_domain.setdefault(dom, []).append(profile)
    for dom, holders in by_domain.items():
        if len(holders) > 1:
            loser = min(
                holders,
                key=lambda p: (
                    -int(raw_cards[p][1].get("authority", 0) or 0),
                    p,
                ),
            )
            raw_cards.pop(loser, None)
            result.warnings.append(
                f"{TAG_CARD_INVALID}: domain {dom!r} owned by "
                f"{', '.join(sorted(holders))} — {loser} excluded (loses "
                "authority tie for duplicate ownership)"
            )

    result.cards = {p: card for p, (_, card) in raw_cards.items()}
    if not result.cards:
        result.warnings.append(
            f"{TAG_REGISTRY_EMPTY}: no valid cards in {cards_dir} — falling "
            "back to description-based routing"
        )
    return result


def _load_schema(path: Path) -> Optional[dict]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def resolve_primary_domain(task: dict, cards: dict) -> Optional[str]:
    """routing-rules.md section 5 stage 1.1 — the task's primary domain.

    Declared ``primary_domain`` wins; otherwise the domain of the first
    declared capability found in the registry; otherwise ``inferred_domain``
    (the LLM's classification); otherwise ``None`` (open domain / no guard).
    """
    declared_domain = task.get("primary_domain")
    if isinstance(declared_domain, str) and declared_domain.strip():
        return declared_domain.strip()
    for cid in task.get("requires_capabilities", []) or []:
        if not isinstance(cid, str):
            continue
        for card in cards.values():
            for cap in card.get("capabilities", []) or []:
                if isinstance(cap, dict) and cap.get("id") == cid:
                    dom = cap.get("domain")
                    if isinstance(dom, str) and dom:
                        return dom
    inferred = task.get("inferred_domain")
    if isinstance(inferred, str) and inferred.strip():
        return inferred.strip()
    return None


def _capability_index(cards: dict) -> dict[str, str]:
    """Capability id -> owning profile (registry invariant: unique)."""
    index: dict[str, str] = {}
    for profile, card in cards.items():
        for cap in card.get("capabilities", []) or []:
            if isinstance(cap, dict) and isinstance(cap.get("id"), str):
                index[cap["id"]] = profile
    return index


def _score_candidate(profile: str, card: dict, task: dict, domain: Optional[str]) -> tuple[int, list[str]]:
    """Stage 2 scoring for one card. Returns (score, matched_capability_ids)."""
    declared = [c for c in (task.get("requires_capabilities") or []) if isinstance(c, str)]
    text = " ".join(
        str(task.get("title", "")) + " " + str(task.get("body", ""))
    ).lower()
    score = 0
    matched: list[str] = []
    keyword_points = 0
    for cap in card.get("capabilities", []) or []:
        if not isinstance(cap, dict):
            continue
        cid = cap.get("id")
        if isinstance(cid, str) and cid in declared:
            score += 3  # rule 2.1: exact match
            matched.append(cid)
        if domain and cap.get("domain") == domain:
            score += 1  # rule 2.2: same-domain
        for kw in cap.get("keywords", []) or []:
            if isinstance(kw, str) and kw.lower() in text:
                keyword_points += 1
                break  # one keyword hit per capability
    score += min(keyword_points, 3)  # rule 2.3: keyword, max +3 per card
    return score, matched


def route_task(
    task: dict,
    cards: dict,
    *,
    default_assignee: str,
    tie_breaker: Optional[TieBreaker] = None,
) -> RouteResult:
    """Run stages 1-4 of routing-rules.md and return (winner, audit).

    ``task`` carries ``task_id``, ``title``, ``body`` and the optional
    classified facts ``primary_domain`` / ``requires_capabilities`` /
    ``inferred_domain``. ``cards`` is a validated registry from
    :func:`load_registry`. ``default_assignee`` is the config fallback
    (``kanban.default_assignee``); fallback outcomes return that name as the
    winner, so the caller never needs to interpret a sentinel.

    Deterministic unless a tie reaches ``tie_breaker`` (the only non-pure step,
    and optional). The audit dict matches routing-rules.md section 6.
    """
    task_id = str(task.get("task_id") or "")
    title = str(task.get("title") or "")
    declared = [c for c in (task.get("requires_capabilities") or []) if isinstance(c, str)]
    domain = resolve_primary_domain(task, cards)

    audit: dict = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "task_id": task_id,
        "title": title[:200],
        "primary_domain": domain,
        "requires_capabilities_declared": declared,
        "requires_capabilities_inferred": [],
        "candidates": [],
        "winner": default_assignee,
        "matched_capability_ids": [],
        "fallback_used": None,
        "stage": "3.1",
    }

    # Stage 2 early-out: declared capability ids the registry has never heard
    # of are the fleet's signal that a specialist is missing.
    index = _capability_index(cards)
    unknown = [cid for cid in declared if cid not in index]
    if unknown:
        audit.update(
            {
                "winner": default_assignee,
                "fallback_used": TAG_CAPABILITY_UNKNOWN,
                "stage": "2",
                "unknown_ids": unknown,
            }
        )
        return RouteResult(default_assignee, audit)

    # Stage 1.3: forbidden removal — score never overrides boundaries.
    candidates = {
        profile: card
        for profile, card in cards.items()
        if not (
            domain
            and domain in ((card.get("domain_boundaries") or {}).get("forbidden") or [])
        )
    }

    # Stage 1.2/1.4: domain guard.
    owner = GUARD_TABLE.get(domain) if domain else None
    guarded: list[str] = []
    guard_fallback: Optional[str] = None
    if owner:
        if owner in candidates:
            guarded.append(owner)
        else:
            # Owner card missing/invalid — the table still holds (Bolsotron).
            guard_fallback = owner

    # Stage 2: scoring (all valid cards minus forbidden ones).
    scored: dict[str, tuple[int, list[str]]] = {}
    for profile, card in candidates.items():
        s, matched = _score_candidate(profile, card, task, domain)
        scored[profile] = (s, matched)
    if guard_fallback:
        # Stage 1.4: route by guard-table name, no scoring contest.
        audit.update(
            {
                "winner": guard_fallback,
                "fallback_used": TAG_GUARD_FALLBACK,
                "stage": "1.4",
                "candidates": [
                    {
                        "profile": p,
                        "score": s,
                        "guard": False,
                        "matched_ids": m,
                    }
                    for p, (s, m) in sorted(
                        scored.items(), key=lambda kv: -kv[1][0]
                    )
                ],
            }
        )
        return RouteResult(guard_fallback, audit)

    # A card is a candidate only if it scores > 0 or holds the guard bonus.
    eligible = {
        p: (s, m)
        for p, (s, m) in scored.items()
        if s > 0 or p in guarded
    }
    if not eligible:
        audit.update(
            {
                "winner": default_assignee,
                "fallback_used": TAG_NO_CANDIDATE,
                "stage": "4",
                "candidates": [
                    {
                        "profile": p,
                        "score": s,
                        "guard": p in guarded,
                        "matched_ids": m,
                    }
                    for p, (s, m) in sorted(
                        scored.items(), key=lambda kv: -kv[1][0]
                    )
                ],
            }
        )
        return RouteResult(default_assignee, audit)

    # Stage 3: selection. +inf guard bonus dominates any finite score.
    def effective(profile: str) -> float:
        return float("inf") if profile in guarded else float(scored[profile][0])

    best = max(effective(p) for p in eligible)
    winners = [p for p in eligible if effective(p) == best]

    matched_ids = scored[winners[0]][1]
    if len(winners) > 1:
        # Tie-break 3.2: the card owning the primary domain (at most one).
        owns = [
            w
            for w in winners
            if domain
            and domain in ((cards[w].get("domain_boundaries") or {}).get("owns") or [])
        ]
        if len(owns) == 1:
            winners = owns
        else:
            # Tie-break 3.3/3.4: LLM disambiguation, else authority/lexicographic.
            chosen = None
            if tie_breaker is not None:
                try:
                    chosen = tie_breaker(list(winners), cards, task)
                except Exception as exc:
                    logger.debug("agentcard: tie-breaker failed: %s", exc)
                    chosen = None
            if chosen in winners:
                audit["stage"] = "3.3"
                audit["fallback_used"] = TAG_TIE_LLM
                winners = [chosen]
            else:
                audit["stage"] = "3.4"
                audit["fallback_used"] = TAG_TIE_DETERMINISTIC
                winners = [
                    max(
                        winners,
                        key=lambda w: (
                            int(cards[w].get("authority", 0) or 0),
                            w,
                        ),
                    )
                ]

    winner = winners[0]
    matched_ids = scored[winner][1]
    audit.update(
        {
            "winner": winner,
            "matched_capability_ids": matched_ids,
            "candidates": [
                {
                    "profile": p,
                    "score": "inf" if p in guarded else s,
                    "guard": p in guarded,
                    "matched_ids": m,
                }
                for p, (s, m) in sorted(
                    scored.items(),
                    key=lambda kv: (
                        -float("inf") if kv[0] in guarded else -kv[1][0],
                        kv[0],
                    ),
                )
            ],
        }
    )
    return RouteResult(winner, audit)


def append_audit_line(audit: dict, log_path: Path) -> Path:
    """Append one JSON line for a decision (section 6: append only, never truncate)."""
    log_path = Path(log_path)
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    line = json.dumps(audit, ensure_ascii=False, sort_keys=True)
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return log_path
