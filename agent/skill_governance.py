"""Config-driven provenance-aware skill governance.

This module is intentionally lightweight enough to be imported by skill
selection, gateway, and search paths.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Optional

from hermes_constants import get_config_path, get_hermes_home

logger = logging.getLogger(__name__)


class GovernanceClassification(str, Enum):
    CURRENT = "CURRENT"
    COMPATIBILITY_ONLY = "COMPATIBILITY_ONLY"
    STALE = "STALE"
    CONFLICTING = "CONFLICTING"
    UNKNOWN = "UNKNOWN"


class GovernanceMode(str, Enum):
    AUTO = "auto"
    PRELOAD = "preload"
    EXPLICIT = "explicit"
    RETRIEVAL = "retrieval"


@dataclass(frozen=True)
class GovernanceConfig:
    registry_path: str = ""
    task_class: str = ""
    protected_task_classes: tuple[str, ...] = ()
    retrieval_ranking: bool = True


@dataclass(frozen=True)
class SkillRegistryEntry:
    name: str
    classification: GovernanceClassification
    aliases: tuple[str, ...] = ()
    provenance: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GovernanceContext:
    mode: GovernanceMode
    task_class: str = ""
    historical_intent: bool = False


@dataclass(frozen=True)
class SkillGovernanceDecision:
    requested_name: str
    canonical_name: str
    classification: GovernanceClassification
    allowed: bool
    reason: str
    mode: GovernanceMode
    task_class: str
    protected_task: bool
    historical_intent: bool
    matched_alias: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict)
    registry_path: str = ""


_CONFIG_CACHE: tuple[tuple[str, int], GovernanceConfig] | None = None
_REGISTRY_CACHE: tuple[tuple[str, int], dict[str, SkillRegistryEntry]] | None = None


def _normalize_name(value: str) -> str:
    return str(value or "").strip().lower()


def _safe_mtime(path: Path) -> int:
    try:
        return int(path.stat().st_mtime_ns)
    except OSError:
        return -1


def _load_yaml_file(path: Path) -> dict[str, Any]:
    try:
        from agent.skill_utils import yaml_load

        raw = yaml_load(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        logger.debug("Failed to load YAML file %s", path, exc_info=True)
        return {}


def _load_governance_config() -> GovernanceConfig:
    global _CONFIG_CACHE
    config_path = get_config_path()
    cache_key = (str(config_path), _safe_mtime(config_path))
    if _CONFIG_CACHE and _CONFIG_CACHE[0] == cache_key:
        return _CONFIG_CACHE[1]

    raw = _load_yaml_file(config_path) if config_path.exists() else {}
    skills_cfg = raw.get("skills") if isinstance(raw.get("skills"), dict) else {}
    gov = skills_cfg.get("governance") if isinstance(skills_cfg.get("governance"), dict) else {}
    protected = gov.get("protected_task_classes")
    if isinstance(protected, str):
        protected = [protected]
    protected_norm = tuple(
        cls for cls in (_normalize_name(v) for v in (protected or [])) if cls
    )
    cfg = GovernanceConfig(
        registry_path=str(gov.get("registry_path") or "").strip(),
        task_class=_normalize_name(gov.get("task_class") or ""),
        protected_task_classes=protected_norm,
        retrieval_ranking=bool(gov.get("retrieval_ranking", True)),
    )
    _CONFIG_CACHE = (cache_key, cfg)
    return cfg


def _resolve_registry_path(config: GovernanceConfig) -> Path | None:
    raw = config.registry_path.strip()
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = (get_hermes_home() / candidate).resolve()
    return candidate


def _parse_classification(value: Any) -> GovernanceClassification:
    normalized = str(value or "").strip().upper()
    try:
        return GovernanceClassification(normalized)
    except ValueError:
        return GovernanceClassification.UNKNOWN


def _parse_registry_entry(item: dict[str, Any]) -> SkillRegistryEntry | None:
    name = str(item.get("name") or "").strip()
    if not name:
        return None
    aliases = item.get("aliases") or []
    if isinstance(aliases, str):
        aliases = [aliases]
    parsed_aliases = tuple(
        alias for alias in (str(v).strip() for v in aliases) if alias
    )
    provenance = item.get("provenance")
    if not isinstance(provenance, dict):
        provenance = {}
    return SkillRegistryEntry(
        name=name,
        classification=_parse_classification(item.get("classification")),
        aliases=parsed_aliases,
        provenance=copy.deepcopy(provenance),
    )


def _load_registry_entries() -> tuple[dict[str, SkillRegistryEntry], str]:
    global _REGISTRY_CACHE
    cfg = _load_governance_config()
    path = _resolve_registry_path(cfg)
    if path is None:
        return {}, ""
    cache_key = (str(path), _safe_mtime(path))
    if _REGISTRY_CACHE and _REGISTRY_CACHE[0] == cache_key:
        return _REGISTRY_CACHE[1], str(path)

    raw = _load_yaml_file(path) if path.exists() else {}
    items = raw.get("skills") or raw.get("entries") or []
    if not isinstance(items, list):
        items = []
    entries: dict[str, SkillRegistryEntry] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        parsed = _parse_registry_entry(item)
        if parsed is None:
            continue
        for key in {_normalize_name(parsed.name), *(_normalize_name(a) for a in parsed.aliases)}:
            if key:
                entries[key] = parsed
    _REGISTRY_CACHE = (cache_key, entries)
    return entries, str(path)


def governance_context(
    *,
    mode: GovernanceMode | str,
    task_class: str | None = None,
    historical_intent: bool = False,
) -> GovernanceContext:
    cfg = _load_governance_config()
    resolved_mode = mode if isinstance(mode, GovernanceMode) else GovernanceMode(str(mode))
    resolved_task = _normalize_name(task_class) or cfg.task_class
    return GovernanceContext(
        mode=resolved_mode,
        task_class=resolved_task,
        historical_intent=bool(historical_intent),
    )


def _lookup_entry(skill_name: str) -> tuple[SkillRegistryEntry | None, str | None, str]:
    entries, registry_path = _load_registry_entries()
    lookup = _normalize_name(skill_name)
    entry = entries.get(lookup)
    if entry is None:
        return None, None, registry_path
    matched_alias = None
    if lookup != _normalize_name(entry.name):
        matched_alias = skill_name
    return entry, matched_alias, registry_path


def _decision_reason(
    *,
    classification: GovernanceClassification,
    protected_task: bool,
    historical_intent: bool,
) -> tuple[bool, str]:
    if not protected_task:
        return True, "unprotected task class"
    if classification == GovernanceClassification.CURRENT:
        return True, "current registry entry"
    if classification == GovernanceClassification.COMPATIBILITY_ONLY:
        if historical_intent:
            return True, "compatibility-only entry allowed by historical intent"
        return False, "compatibility-only entry requires explicit historical intent"
    if classification == GovernanceClassification.STALE:
        return False, "stale registry entry blocked for protected task class"
    if classification == GovernanceClassification.CONFLICTING:
        return False, "conflicting registry entry blocked for protected task class"
    return False, "unknown registry entry blocked for protected task class"


def evaluate_skill_selection(
    skill_name: str,
    *,
    context: GovernanceContext,
    emit_log: bool = True,
) -> SkillGovernanceDecision:
    cfg = _load_governance_config()
    entry, matched_alias, registry_path = _lookup_entry(skill_name)
    classification = (
        entry.classification if entry is not None else GovernanceClassification.UNKNOWN
    )
    protected = bool(context.task_class) and (
        _normalize_name(context.task_class) in cfg.protected_task_classes
    )
    allowed, reason = _decision_reason(
        classification=classification,
        protected_task=protected,
        historical_intent=context.historical_intent,
    )
    canonical_name = entry.name if entry is not None else skill_name
    decision = SkillGovernanceDecision(
        requested_name=skill_name,
        canonical_name=canonical_name,
        classification=classification,
        allowed=allowed,
        reason=reason,
        mode=context.mode,
        task_class=context.task_class,
        protected_task=protected,
        historical_intent=context.historical_intent,
        matched_alias=matched_alias,
        provenance=copy.deepcopy(entry.provenance if entry is not None else {}),
        registry_path=registry_path,
    )
    if emit_log:
        log_skill_governance_decision(decision)
    return decision


def evaluate_skill_selections(
    skill_names: Iterable[str],
    *,
    context: GovernanceContext,
) -> list[SkillGovernanceDecision]:
    return [
        evaluate_skill_selection(skill_name, context=context)
        for skill_name in skill_names
        if str(skill_name or "").strip()
    ]


def log_skill_governance_decision(decision: SkillGovernanceDecision) -> None:
    payload = {
        "skill": decision.requested_name,
        "canonical_name": decision.canonical_name,
        "classification": decision.classification.value,
        "allowed": decision.allowed,
        "mode": decision.mode.value,
        "task_class": decision.task_class or "",
        "protected_task": decision.protected_task,
        "historical_intent": decision.historical_intent,
        "registry_path": decision.registry_path,
        "reason": decision.reason,
    }
    if decision.allowed:
        logger.info("skill_governance allow: %s", payload)
    else:
        logger.warning("skill_governance reject: %s", payload)


def filter_allowed_skill_names(
    skill_names: Iterable[str],
    *,
    context: GovernanceContext,
) -> tuple[list[str], list[SkillGovernanceDecision]]:
    decisions = evaluate_skill_selections(skill_names, context=context)
    allowed = [decision.requested_name for decision in decisions if decision.allowed]
    return allowed, decisions


def _classification_rank(
    classification: GovernanceClassification,
    *,
    protected_task: bool,
) -> int:
    if protected_task:
        ranks = {
            GovernanceClassification.CURRENT: 40,
            GovernanceClassification.COMPATIBILITY_ONLY: 25,
            GovernanceClassification.UNKNOWN: 10,
            GovernanceClassification.STALE: -10,
            GovernanceClassification.CONFLICTING: -20,
        }
    else:
        ranks = {
            GovernanceClassification.CURRENT: 20,
            GovernanceClassification.COMPATIBILITY_ONLY: 16,
            GovernanceClassification.UNKNOWN: 10,
            GovernanceClassification.STALE: 6,
            GovernanceClassification.CONFLICTING: 0,
        }
    return ranks.get(classification, 0)


def rank_skill_search_results(
    results: list[Any],
    *,
    context: GovernanceContext,
) -> list[Any]:
    cfg = _load_governance_config()
    if not cfg.retrieval_ranking or not results:
        return results
    protected = bool(context.task_class) and (
        _normalize_name(context.task_class) in cfg.protected_task_classes
    )

    decorated: list[tuple[int, int, Any]] = []
    for idx, result in enumerate(results):
        name = str(getattr(result, "name", "") or "")
        identifier = str(getattr(result, "identifier", "") or "")
        target = name or identifier
        decision = evaluate_skill_selection(
            target,
            context=context,
            emit_log=False,
        )
        extra = getattr(result, "extra", None)
        if isinstance(extra, dict):
            extra.setdefault("governance", {})
            extra["governance"].update(
                {
                    "classification": decision.classification.value,
                    "allowed": decision.allowed,
                    "reason": decision.reason,
                    "task_class": decision.task_class,
                    "protected_task": decision.protected_task,
                }
            )
        decorated.append(
            (
                -_classification_rank(
                    decision.classification,
                    protected_task=protected,
                ),
                idx,
                result,
            )
        )
    decorated.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in decorated]


def governance_sort_tuple(
    result: Any,
    *,
    context: GovernanceContext,
) -> tuple[int, int]:
    name = str(getattr(result, "name", "") or "")
    identifier = str(getattr(result, "identifier", "") or "")
    decision = evaluate_skill_selection(
        name or identifier,
        context=context,
        emit_log=False,
    )
    return (
        -_classification_rank(
            decision.classification,
            protected_task=decision.protected_task,
        ),
        0 if decision.allowed else 1,
    )
