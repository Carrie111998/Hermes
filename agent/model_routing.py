"""Centralized task-to-model routing for Hermes turn execution.

This module is the single routing decision point for gateway, CLI, cron, and
delegated subagent tasks. It keeps routing policy deterministic and applies a
strict precedence order:

1) explicit user override
2) workflow-specific route
3) safety/risk escalation
4) automatic task classification
5) default route
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import re
from typing import Any, Dict, Iterable, Optional

from hermes_constants import parse_reasoning_effort


logger = logging.getLogger(__name__)

_REASONING_LEVELS = {
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
}
_MODEL_TIERS = {"luna", "terra", "sol"}
_PROFILE_STRENGTH = {
    "deterministic": 0,
    "fast": 1,
    "fast_plus": 2,
    "balanced": 3,
    "creative": 4,
    "strong": 5,
    "maximum": 6,
    "ultra": 7,
}

_DEFAULT_PROFILES: Dict[str, Dict[str, str]] = {
    "deterministic": {"model": "luna", "reasoning": "none"},
    "fast": {"model": "luna", "reasoning": "low"},
    "fast_plus": {"model": "luna", "reasoning": "medium"},
    "balanced": {"model": "terra", "reasoning": "medium"},
    "creative": {"model": "terra", "reasoning": "high"},
    "strong": {"model": "sol", "reasoning": "high"},
    "maximum": {"model": "sol", "reasoning": "max"},
    "ultra": {"model": "sol", "reasoning": "ultra"},
}

_DEFAULT_WORKFLOW_ROUTES: tuple[tuple[str, str], ...] = (
    ("youtube editor handoff", "fast"),
    ("send editor email", "fast"),
    ("send editor iphone message", "fast"),
    ("create lead magnet task", "fast"),
    ("create youtube description draft", "fast"),
    ("create email draft", "fast"),
    ("update notion", "fast"),
    ("update project status", "fast"),
    ("organize assets", "fast"),
    ("send routine confirmations", "fast"),
)

_APPROVED_EXECUTION_RE = re.compile(
    r"^(?:send it|send|do it|do this|do that|go ahead|approved|"
    r"yes send|yes,? send|ship it|run it|run this|execute|execute it|"
    r"execute this|proceed|make it happen)$",
    re.IGNORECASE,
)
_FACT_PATTERNS = (
    re.compile(
        r"^(?:what(?:'?s| is)|tell me) the (?:current )?"
        r"(?:time|date|day|weather|temperature)"
        r"(?: (?:in|at) [\w\s,.'-]+)?\??$",
        re.IGNORECASE,
    ),
    re.compile(r"^what time is it(?: (?:in|at) [\w\s,.'-]+)?\??$", re.IGNORECASE),
    re.compile(r"^current time(?: (?:in|at) [\w\s,.'-]+)?\??$", re.IGNORECASE),
    re.compile(
        r"^(?:what(?:'?s| is) )?(?:the )?"
        r"(?:tallest|largest|biggest|smallest|highest|longest|shortest|"
        r"deepest|widest|oldest|newest|fastest|richest|heaviest|lightest)"
        r" [\w\s'-]+\??$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:how (?:far|long|tall|big|old|deep|wide|high) "
        r"(?:is|are|to|from) [\w\s,.'-]+\??|distance (?:to|from|between) [\w\s,.'-]+\??)$",
        re.IGNORECASE,
    ),
)
_DETERMINISTIC_HINT_RE = re.compile(
    r"\b(?:validate|validation|dedup|deduplicate|retry|poll(?:ing)?|"
    r"date math|arithmetic|status mapping|payload formatting|"
    r"check whether|verify whether|file exists|deployment succeeded|"
    r"url validation|required-field)\b",
    re.IGNORECASE,
)
_TRIVIAL_UTILITY_RE = re.compile(
    r"\b(?:save this|save that|find (?:the )?(?:email|file)|summarize|"
    r"translate|nearest|closest|store hours|run (?:the )?.*workflow)\b",
    re.IGNORECASE,
)
_AMBIGUOUS_HINT_RE = re.compile(
    r"\b(?:several|multiple|ambiguous|which one|which file|which email|"
    r"could match|might match|more than one)\b",
    re.IGNORECASE,
)
_WORKER_RE = re.compile(
    r"\b(?:build (?:a )?new automation|modify (?:the )?(?:existing )?workflow|"
    r"github|cloudflare|webflow|ordinary implementation|normal coding|"
    r"troubleshoot|draft (?:the )?email sequence (?:from|based on) (?:an )?approved|"
    r"research testimonials?|incorporate testimonials?|synthesi[sz]e information)\b",
    re.IGNORECASE,
)
_CREATIVE_RE = re.compile(
    r"\b(?:youtube (?:video )?packet|notion flashcards?|title generation|"
    r"thumbnail copy|a/?b testing ideas?|brand-specific youtube|youtube outliers?|"
    r"youtube trend analysis|deeper research|polished execution plan|"
    r"complicated implementation planning|multi-repository|higher-risk code changes)\b",
    re.IGNORECASE,
)
_STRATEGY_RE = re.compile(
    r"\b(?:launch strategy|offer construction|pricing strategy|"
    r"product positioning|campaign direction|business strategy|"
    r"architecture|system design|difficult debugging|production incident|"
    r"major migration|high-stakes|final review|major launch|flagship youtube|"
    r"terra failed|tried twice)\b",
    re.IGNORECASE,
)
_EXCEPTIONAL_RE = re.compile(
    r"\b(?:extremely difficult debugging|major architecture migration|"
    r"security-sensitive review|major production failure|comprehensive launch postmortem|"
    r"substantial financial consequences|conflicting research needing deep resolution)\b",
    re.IGNORECASE,
)
_ULTRA_RE = re.compile(
    r"\b(?:parallel subagents?|simultaneously|multi-stream investigation|"
    r"across several services|across multiple repositories|compare several major strategies)\b",
    re.IGNORECASE,
)
_HIGH_RISK_RE = re.compile(
    r"\b(?:purchase|financial commitment|delete important data|publish publicly|"
    r"full email campaign|major production deploy(?:ment)?|"
    r"change permissions|change credentials|irreversible)\b",
    re.IGNORECASE,
)

_INLINE_ROUTE_RE = re.compile(
    r"\[\[?\s*route\s*:\s*(?P<body>[^\]]+?)\s*\]?\]",
    re.IGNORECASE,
)
_GPT56_TIER_RE = re.compile(
    r"^(?P<prefix>.*?gpt-5\.6-)(?P<tier>luna|terra|sol)(?P<suffix>(?:-pro)?)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class InlineRouteOverride:
    """Per-message explicit routing directive parsed from the user prompt."""

    model_tier: Optional[str] = None
    reasoning_tier: Optional[str] = None
    profile: Optional[str] = None
    no_escalation: bool = False
    maximum_quality: bool = False
    raw: str = ""


@dataclass(frozen=True)
class RoutingDecision:
    """Normalized result consumed by each surface-specific route wrapper."""

    model: str
    reasoning_config: Optional[dict]
    profile: str
    category: str
    reason: str
    override_used: bool
    escalation_reason: Optional[str]
    no_escalation: bool
    retry_count: int
    workflow_match: Optional[str]
    clean_message: str

    def as_dict(self) -> dict:
        return {
            "model": self.model,
            "reasoning_config": self.reasoning_config,
            "profile": self.profile,
            "category": self.category,
            "reason": self.reason,
            "override_used": self.override_used,
            "escalation_reason": self.escalation_reason,
            "no_escalation": self.no_escalation,
            "retry_count": self.retry_count,
            "workflow_match": self.workflow_match,
            "clean_message": self.clean_message,
        }


def _message_to_text(message: Any) -> str:
    """Extract a routing-friendly text representation from a user message."""
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        chunks: list[str] = []
        for item in message:
            if isinstance(item, str):
                chunks.append(item)
            elif isinstance(item, dict):
                if item.get("type") == "text":
                    text = item.get("text")
                    if isinstance(text, str):
                        chunks.append(text)
        return " ".join(chunks)
    return str(message or "")


def _normalize_text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _reasoning_label(reasoning_config: Optional[dict]) -> str:
    if not isinstance(reasoning_config, dict):
        return ""
    if reasoning_config.get("enabled") is False:
        return "none"
    effort = str(reasoning_config.get("effort") or "").strip().lower()
    return effort if effort in _REASONING_LEVELS else ""


def _derive_tier_model(base_model: str, tier: str) -> Optional[str]:
    """Derive a Luna/Terra/Sol sibling model from the active base model."""
    if tier not in _MODEL_TIERS:
        return None
    model = str(base_model or "").strip()
    if not model:
        return None
    match = _GPT56_TIER_RE.match(model)
    if match:
        return f"{match.group('prefix')}{tier}{match.group('suffix')}"
    return None


def _parse_inline_override(message_text: str) -> tuple[str, Optional[InlineRouteOverride]]:
    """Parse and strip an inline routing directive.

    Syntax (shared across Telegram/CLI/cron prompts):
        [[route: luna high no-escalation]]
        [route: profile=creative]
        [[route: maximum-quality]]
    """
    match = _INLINE_ROUTE_RE.search(message_text or "")
    if not match:
        return message_text, None
    raw_directive = match.group("body") or ""
    lowered = raw_directive.lower()
    tokens = [tok for tok in re.split(r"[\s,]+", lowered) if tok]

    model_tier: Optional[str] = None
    reasoning_tier: Optional[str] = None
    profile: Optional[str] = None
    no_escalation = False
    maximum_quality = False

    for token in tokens:
        token = token.strip()
        if not token:
            continue
        if token in {"no-escalation", "no_escalation", "noescalation"}:
            no_escalation = True
            continue
        if token in {"maximum-quality", "maximum_quality", "max-quality", "max_quality"}:
            maximum_quality = True
            continue
        if token.startswith("model=") or token.startswith("tier="):
            _, value = token.split("=", 1)
            value = value.strip().lower()
            if value in _MODEL_TIERS:
                model_tier = value
            continue
        if token.startswith("reasoning=") or token.startswith("effort="):
            _, value = token.split("=", 1)
            value = value.strip().lower()
            if value in _REASONING_LEVELS:
                reasoning_tier = value
            continue
        if token.startswith("profile="):
            _, value = token.split("=", 1)
            value = value.strip().lower()
            if value:
                profile = value
            continue
        if token in _MODEL_TIERS:
            model_tier = token
            continue
        if token in _REASONING_LEVELS:
            reasoning_tier = token
            continue
        if token in _PROFILE_STRENGTH:
            profile = token

    if "maximum quality" in lowered or "max quality" in lowered:
        maximum_quality = True

    override = InlineRouteOverride(
        model_tier=model_tier,
        reasoning_tier=reasoning_tier,
        profile=profile,
        no_escalation=no_escalation,
        maximum_quality=maximum_quality,
        raw=raw_directive.strip(),
    )
    clean_message = (message_text[: match.start()] + message_text[match.end() :]).strip()
    return clean_message, override


def _normalize_profiles(
    *,
    policy_profiles: Any,
    legacy_turn_routing: Optional[dict],
) -> Dict[str, Dict[str, str]]:
    apply_legacy_bridge = not isinstance(policy_profiles, dict) or not policy_profiles
    profiles: Dict[str, Dict[str, str]] = {
        name: dict(values) for name, values in _DEFAULT_PROFILES.items()
    }
    if isinstance(policy_profiles, dict):
        for name, raw in policy_profiles.items():
            key = str(name or "").strip().lower()
            if key not in profiles:
                continue
            if isinstance(raw, dict):
                model = str(raw.get("model") or "").strip()
                reasoning = str(raw.get("reasoning") or "").strip().lower()
                if model:
                    profiles[key]["model"] = model
                if reasoning:
                    profiles[key]["reasoning"] = reasoning
            elif isinstance(raw, str):
                profiles[key]["model"] = raw.strip()

    # Backward-compatible bridge from agent.turn_routing to routing profiles.
    if apply_legacy_bridge and isinstance(legacy_turn_routing, dict):
        bridge = {
            "fast": ("trivial_model", "trivial_reasoning"),
            "balanced": ("moderate_model", "moderate_reasoning"),
            "creative": ("high_model", "high_reasoning"),
        }
        for profile_name, (m_key, r_key) in bridge.items():
            model = str(legacy_turn_routing.get(m_key) or "").strip()
            reasoning = str(legacy_turn_routing.get(r_key) or "").strip().lower()
            if model:
                profiles[profile_name]["model"] = model
            if reasoning:
                profiles[profile_name]["reasoning"] = reasoning

    return profiles


def _normalize_workflow_routes(configured_routes: Any) -> list[tuple[str, str]]:
    routes: list[tuple[str, str]] = list(_DEFAULT_WORKFLOW_ROUTES)
    if isinstance(configured_routes, dict):
        for match_text, profile in configured_routes.items():
            m = str(match_text or "").strip().lower()
            p = str(profile or "").strip().lower()
            if m and p in _PROFILE_STRENGTH:
                routes.append((m, p))
    elif isinstance(configured_routes, list):
        for entry in configured_routes:
            if not isinstance(entry, dict):
                continue
            m = str(entry.get("match") or "").strip().lower()
            p = str(entry.get("profile") or "").strip().lower()
            if m and p in _PROFILE_STRENGTH:
                routes.append((m, p))
    # Deduplicate while keeping first-wins ordering.
    seen: set[tuple[str, str]] = set()
    deduped: list[tuple[str, str]] = []
    for route in routes:
        if route in seen:
            continue
        seen.add(route)
        deduped.append(route)
    return deduped


def _profile_model(
    profile: str,
    *,
    profiles: Dict[str, Dict[str, str]],
    base_model: str,
) -> str:
    cfg = profiles.get(profile) or {}
    configured = str(cfg.get("model") or "").strip().lower()
    if configured in _MODEL_TIERS:
        return _derive_tier_model(base_model, configured) or f"gpt-5.6-{configured}"
    if configured:
        return str(cfg.get("model")).strip()
    # Fallback to tier-derived sibling when possible, otherwise keep base model.
    tier = str((_DEFAULT_PROFILES.get(profile) or {}).get("model") or "").strip().lower()
    if tier in _MODEL_TIERS:
        return _derive_tier_model(base_model, tier) or base_model
    return base_model


def _profile_reasoning(
    profile: str,
    *,
    profiles: Dict[str, Dict[str, str]],
    fallback_reasoning: Optional[dict],
) -> Optional[dict]:
    cfg = profiles.get(profile) or {}
    value = str(cfg.get("reasoning") or "").strip().lower()
    if not value:
        return fallback_reasoning
    parsed = parse_reasoning_effort(value)
    return parsed if parsed is not None else fallback_reasoning


def _classify_profile(message_text: str) -> tuple[str, str, str]:
    text = _normalize_text(message_text).lower()
    if not text:
        return "fast", "trivial.empty", "Empty/whitespace-only request"
    if len(text) <= 80 and _APPROVED_EXECUTION_RE.match(text):
        return "fast", "trivial.approved_execution", "Short approved execution confirmation"
    if _DETERMINISTIC_HINT_RE.search(text):
        return "deterministic", "trivial.deterministic", "Deterministic validation/mapping/retry task"
    if len(text) <= 140 and any(p.match(text) for p in _FACT_PATTERNS):
        return "fast", "trivial.fact_lookup", "Quick factual lookup request"
    if _TRIVIAL_UTILITY_RE.search(text):
        if _AMBIGUOUS_HINT_RE.search(text):
            return "fast_plus", "trivial.ambiguous", "Light ambiguity in otherwise fast utility task"
        return "fast", "trivial.utility", "Low-judgment utility/retrieval/workflow request"
    if _ULTRA_RE.search(text):
        return "ultra", "strategy.parallel_ultra", "Parallel multi-stream investigation requested"
    if _EXCEPTIONAL_RE.search(text):
        return "maximum", "strategy.exceptional", "Exceptional high-stakes technical/strategic request"
    if _STRATEGY_RE.search(text):
        return "strong", "strategy.high_stakes", "High-stakes strategy/architecture/debugging request"
    if _CREATIVE_RE.search(text):
        return "creative", "creative.deep_work", "Creative/research-heavy production task"
    if _WORKER_RE.search(text):
        return "balanced", "worker.general", "General implementation/workflow task"
    return "balanced", "worker.default", "Fallback balanced worker route"


def _workflow_profile(message_text: str, workflow_routes: Iterable[tuple[str, str]]) -> tuple[Optional[str], Optional[str]]:
    text = _normalize_text(message_text).lower()
    if not text:
        return None, None
    for phrase, profile in workflow_routes:
        if phrase and phrase in text:
            return profile, phrase
    return None, None


def _risk_escalation_profile(
    *,
    selected_profile: str,
    message_text: str,
    allow_escalation: bool,
    no_escalation: bool,
) -> tuple[str, Optional[str]]:
    if not allow_escalation or no_escalation:
        return selected_profile, None
    text = _normalize_text(message_text)
    if not text or selected_profile not in _PROFILE_STRENGTH:
        return selected_profile, None
    if not _HIGH_RISK_RE.search(text):
        return selected_profile, None
    if _PROFILE_STRENGTH[selected_profile] >= _PROFILE_STRENGTH["strong"]:
        return selected_profile, None
    return "strong", "High-risk action keywords detected"


def classify_turn_complexity(message: Any) -> str:
    """Legacy compatibility shim for callers that still consume 3 buckets."""
    profile, _category, _reason = _classify_profile(_message_to_text(message))
    if profile in {"deterministic", "fast", "fast_plus"}:
        return "trivial"
    if profile == "balanced":
        return "moderate"
    return "high"


def resolve_routing_decision(
    *,
    message: Any,
    base_model: str,
    fallback_reasoning_config: Optional[dict],
    config: Optional[dict],
    surface: str,
    retry_count: int = 0,
    workflow_model_override: Optional[str] = None,
    workflow_reasoning_override: Optional[dict] = None,
) -> RoutingDecision:
    """Return the centralized routing decision for a single turn/task."""
    cfg = config if isinstance(config, dict) else {}
    routing_cfg = cfg.get("routing") if isinstance(cfg.get("routing"), dict) else {}
    legacy_turn_cfg = (
        cfg.get("agent", {}).get("turn_routing")
        if isinstance(cfg.get("agent"), dict)
        and isinstance(cfg.get("agent", {}).get("turn_routing"), dict)
        else {}
    )
    routing_enabled = routing_cfg.get("enabled")
    if routing_enabled is None:
        routing_enabled = bool(legacy_turn_cfg.get("enabled"))
    else:
        routing_enabled = bool(routing_enabled)
    allow_escalation = bool(routing_cfg.get("allow_escalation", True))
    default_profile = str(routing_cfg.get("default_profile") or "balanced").strip().lower()
    if default_profile not in _PROFILE_STRENGTH:
        default_profile = "balanced"

    raw_message = _message_to_text(message)
    clean_message, inline_override = _parse_inline_override(raw_message)
    no_escalation = bool(getattr(inline_override, "no_escalation", False))
    explicit_override_used = False
    workflow_override_applied = False
    escalation_reason: Optional[str] = None
    workflow_match: Optional[str] = None

    profiles = _normalize_profiles(
        policy_profiles=routing_cfg.get("profiles"),
        legacy_turn_routing=legacy_turn_cfg if routing_enabled else None,
    )
    workflow_routes = _normalize_workflow_routes(routing_cfg.get("workflow_routes"))

    selected_profile = default_profile if routing_enabled else "session"
    category = "default"
    reason = "Routing disabled; using session/default route"

    # Priority 1: explicit inline override.
    if routing_enabled and inline_override:
        forced_profile: Optional[str] = None
        if inline_override.maximum_quality:
            forced_profile = "maximum"
        elif inline_override.profile in _PROFILE_STRENGTH:
            forced_profile = inline_override.profile
        elif inline_override.model_tier in _MODEL_TIERS:
            forced_profile = {
                "luna": "fast",
                "terra": "balanced",
                "sol": "strong",
            }[inline_override.model_tier]
        if forced_profile:
            selected_profile = forced_profile
            category = "override.explicit"
            reason = "Explicit inline route directive"
            explicit_override_used = True

    # Priority 2: workflow route (or explicit caller workflow pin).
    if routing_enabled and not explicit_override_used:
        if workflow_model_override:
            selected_profile = default_profile
            category = "workflow.model_pin"
            reason = "Workflow-level model override supplied by caller"
            workflow_override_applied = True
        else:
            matched_profile, matched_phrase = _workflow_profile(clean_message, workflow_routes)
            if matched_profile:
                selected_profile = matched_profile
                category = "workflow.known"
                reason = f"Workflow route matched '{matched_phrase}'"
                workflow_match = matched_phrase

    # Priority 3 and 4: risk escalation and automatic classification.
    if routing_enabled and not explicit_override_used and not workflow_override_applied and category == "default":
        selected_profile, category, reason = _classify_profile(clean_message)
        selected_profile, escalation_reason = _risk_escalation_profile(
            selected_profile=selected_profile,
            message_text=clean_message,
            allow_escalation=allow_escalation,
            no_escalation=no_escalation,
        )
        if escalation_reason:
            category = "escalation.risk"
            reason = escalation_reason

    # Priority 5: fallback/default route is already encoded above.
    model = base_model
    reasoning_config = fallback_reasoning_config

    if routing_enabled:
        model = _profile_model(
            selected_profile,
            profiles=profiles,
            base_model=base_model,
        ) or base_model
        reasoning_config = _profile_reasoning(
            selected_profile,
            profiles=profiles,
            fallback_reasoning=fallback_reasoning_config,
        )

        # Apply explicit override field-level adjustments after profile mapping.
        if inline_override:
            if inline_override.model_tier in _MODEL_TIERS:
                derived = _derive_tier_model(model or base_model, inline_override.model_tier)
                if derived:
                    model = derived
                else:
                    model = f"gpt-5.6-{inline_override.model_tier}"
            if inline_override.reasoning_tier in _REASONING_LEVELS:
                parsed = parse_reasoning_effort(inline_override.reasoning_tier)
                if parsed is not None:
                    reasoning_config = parsed

    if workflow_model_override and not explicit_override_used:
        model = str(workflow_model_override).strip() or model
    if workflow_reasoning_override is not None and not explicit_override_used:
        reasoning_config = workflow_reasoning_override

    if not model:
        model = base_model

    reasoning_label = _reasoning_label(reasoning_config) or "session-default"
    logger.info(
        "Routing decision surface=%s profile=%s model=%s reasoning=%s category=%s "
        "override=%s no_escalation=%s escalation=%s workflow=%s retry_count=%s reason=%s",
        surface,
        selected_profile,
        model,
        reasoning_label,
        category,
        explicit_override_used,
        no_escalation,
        escalation_reason or "",
        workflow_match or "",
        retry_count,
        reason,
    )

    return RoutingDecision(
        model=model,
        reasoning_config=reasoning_config,
        profile=selected_profile,
        category=category,
        reason=reason,
        override_used=explicit_override_used,
        escalation_reason=escalation_reason,
        no_escalation=no_escalation,
        retry_count=max(0, int(retry_count or 0)),
        workflow_match=workflow_match,
        clean_message=clean_message,
    )


__all__ = [
    "InlineRouteOverride",
    "RoutingDecision",
    "classify_turn_complexity",
    "resolve_routing_decision",
]
